from datetime import date, datetime, timedelta

import pytest

from calendar_clients.google_sheets import TabRange
from tests.fake_sheets import FakeSheets
from tests.test_goal_health import NOW, TODAY, TZ, YESTERDAY, FakeCalendar, _event
from utilities.goal_health import Assessment, GoalHealth
from utilities.goal_sheet import Goal
from utilities.goals import OVERALL_ID, Goals
from utilities.noted_time_sheet import NotedTime
from utilities.health_days import day_event_id
from utilities.reflection import Reflections

# TODAY is Friday 2026-10-02; YESTERDAY, Oct 1, is the last day to end.


class FakeNotes:
    whole_tab = TabRange("spreadsheet-1", 99, "A1:C")
    """Prefetched along with the goals; FakeSheets has no cache to fill."""

    def __init__(self, notes=()):
        self.notes = list(notes)

    def read(self, *, include_compacted=False):
        return [n for n in self.notes if include_compacted or n.compaction_id is None]


def _note(at: str, description: str, compaction_id=None) -> NotedTime:
    return NotedTime(
        timestamp=datetime.fromisoformat(at).replace(tzinfo=TZ), description=description, compaction_id=compaction_id
    )


def _setup(goals=(), events=(), notes=()):
    calendar = FakeCalendar(events)
    store = Goals(calendar, FakeSheets(), today=lambda: TODAY)
    for goal in goals:
        parent = goal.parent_id
        if parent is not None and parent not in {g.id for g in store.tree().goals}:
            goal.parent_id = next(g.id for g in store.tree().goals if g.name == parent)
        store.create_goal(goal)
    health = GoalHealth(calendar, store, now=lambda: NOW)
    by_name = {g.name: g for g in store.tree().goals}
    return Reflections(health, store, FakeNotes(notes)), health, store, calendar, by_name


def _rating(goal: Goal, day: date, rating=80, method="subjective", **fields) -> Assessment:
    return Assessment(goal_id=goal.id, day=day, rating=rating, method=method, **fields)


def _feel(name, interval_days=None, parent=None):
    measure = {"kind": "subjective", "prompt": f"How was {name.lower()}?"}
    if interval_days:
        measure["interval_days"] = interval_days
    return Goal(name=name, parent_id=parent, measure=measure)


_COOKING = Goal(name="Cooking", measure={"kind": "duration", "target_min": 120})
_WAKE = Goal(name="Wake", measure={"kind": "time_constraint", "edge": "start", "target": "07:00"})


class TestChoices:
    def test_with_no_day_named_it_offers_completed_days_not_fully_reflected_on(self):
        reflections, _, _, _, goals = _setup([_WAKE, _feel("Feel")])
        unreflected = {"09-21", "09-24", "09-25", "09-28"}
        for day in range(1, 31):
            if f"09-{day:02d}" not in unreflected:
                _reflect(reflections, goals, date(2026, 9, day))
        _reflect(reflections, goals, YESTERDAY)
        reflections.record(date(2026, 9, 28), [], dry_run=False)  # started: Feel's still to answer

        context = reflections.prepare()

        # It's 21:00 on Oct 2, which isn't over: the newest three completed
        # days without a full reflection, each ending at 7am.
        assert context.day is None
        assert not context.questions
        assert [(c.day, c.ends, c.started) for c in context.choices] == [
            (date(2026, 9, 28), datetime(2026, 9, 29, 7, tzinfo=TZ), True),
            (date(2026, 9, 25), datetime(2026, 9, 26, 7, tzinfo=TZ), False),
            (date(2026, 9, 24), datetime(2026, 9, 25, 7, tzinfo=TZ), False),
        ]
        assert context.older_unreflected == 1
        assert "Ask which day" in context.instructions

    def test_when_every_recent_day_is_reflected_it_offers_the_last_one_again(self):
        reflections, _, _, _, goals = _setup([_WAKE])
        for back in range(1, 13):
            reflections.record(TODAY - timedelta(days=back), [], dry_run=False)

        context = reflections.prepare()

        assert [(c.day, c.already_reflected) for c in context.choices] == [(YESTERDAY, True)]
        assert context.older_unreflected == 0


def _reflect(reflections, goals, day):
    """Rate every goal for `day`: Feel is the only question."""
    reflections.record(day, [_rating(goals["Feel"], day, 70)], dry_run=False)


def _rated(result) -> dict:
    return {a.goal_id: a for a in result.rated}


class TestPrepare:
    def test_a_days_events_run_from_waking_to_waking(self):
        sleep_into_1st = _event("2026-09-30T23:30", "2026-10-01T07:15", is_end_of_day_sleep=True)
        sleep_into_2nd = _event("2026-10-02T00:45", "2026-10-02T08:00", is_end_of_day_sleep=True)
        late = _event("2026-10-02T00:10", "2026-10-02T00:40")  # After midnight, before sleep.
        early = _event("2026-10-01T06:00", "2026-10-01T07:00")  # Before waking: the day before.
        reflections, *_ = _setup([_WAKE], events=[sleep_into_1st, sleep_into_2nd, late, early])

        context = reflections.prepare(YESTERDAY)

        assert (context.starts, context.ends) == (sleep_into_1st.end, sleep_into_2nd.end)
        assert "00:10-00:40" in context.events_digest  # The night's late event...
        assert "00:45-08:00" in context.events_digest  # ...and its sleep.
        assert "06:00-07:00" not in context.events_digest

    def test_asks_only_what_needs_judgement(self):
        reflections, health, store, calendar, goals = _setup(
            [_COOKING, _feel("Feel"), _WAKE, Goal(name="Folder"), Goal(name="Life", measure={"kind": "llm", "rubric": "Balance"})]
        )
        feel = goals["Feel"]
        store.create_goal(Goal(name="Paused", status="inactive", measure={"kind": "subjective", "prompt": "?"}))
        health.confirm_assessments([_rating(feel, YESTERDAY - timedelta(days=1), 60)])

        context = reflections.prepare(YESTERDAY)

        assert [(q.path, q.kind, q.prompt, q.rubric) for q in context.questions] == [
            ("Feel", "subjective", "How was feel?", None),
            ("Life", "llm", None, "Balance"),
        ]
        assert [(r.day, r.rating) for r in context.questions[0].recent] == [(date(2026, 9, 30), 60)]
        assert context.unmeasured == ["Folder"]
        assert not context.already_reflected

    def test_an_llm_goal_sees_its_sub_goals_ratings_provisional_or_not(self):
        reflections, _, _, calendar, goals = _setup(
            [
                Goal(name="Life", measure={"kind": "llm", "rubric": "Balance of the parts"}),
                Goal(name="Cooking", parent_id="Life", measure={"kind": "duration", "target_min": 120}),
                _feel("Rest", parent="Life"),
            ]
        )
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T19:00", [goals["Cooking"].id])]

        life = next(q for q in reflections.prepare(YESTERDAY).questions if q.kind == "llm")

        assert [(s.path, s.rating, s.provisional) for s in life.sub_goals] == [
            ("Life › Cooking", 50, False),
            ("Life › Rest", None, False),
        ]

    def test_a_goal_answered_already_isnt_asked_again(self):
        reflections, *_, goals = _setup([_feel("Feel")])
        reflections.record(YESTERDAY, [_rating(goals["Feel"], YESTERDAY, 70)], dry_run=False)

        context = reflections.prepare(YESTERDAY)

        assert context.questions == []
        assert context.already_reflected

    def test_counts_time_per_goal_including_sub_goals(self):
        reflections, _, store, calendar, goals = _setup([_COOKING, Goal(name="Tofu", parent_id="Cooking")])
        cooking, tofu = goals["Cooking"], goals["Tofu"]
        calendar.events = [
            _event("2026-10-01T18:00", "2026-10-01T19:00", [tofu.id]),
            _event("2026-10-01T12:00", "2026-10-01T12:30", [cooking.id]),
        ]

        context = reflections.prepare(YESTERDAY)

        assert [(t.path, t.minutes) for t in context.goal_time] == [
            ("Cooking", 90),
            ("Overall", 90),  # Time toward any goal, each event once.
            ("Cooking › Tofu", 60),
        ]

    def test_digests_the_days_events_and_notes(self):
        reflections, _, _, calendar, goals = _setup(
            [_COOKING],
            notes=[
                _note("2026-10-01T18:15", "started the curry", compaction_id="c1"),
                _note("2026-10-02T06:30", "not compacted yet"),  # Oct 1's day: before waking
                _note("2026-10-02T09:00", "today"),
            ],
        )
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T20:30", [goals["Cooking"].id])]
        calendar.events[0].summary = "Dinner"

        context = reflections.prepare(YESTERDAY)

        assert "Thu 18:00-20:30 Dinner [Cooking]" in context.events_digest
        assert context.notes_digest == "Thu 18:15 started the curry\nFri 06:30 not compacted yet"
        assert context.uncompacted_notes == 1
        assert "record_reflection" in context.instructions

    def test_lists_temporary_weights_whose_date_has_come(self):
        reflections, _, store, _, goals = _setup([_feel("Feel"), _feel("Rest")])
        feel, rest = goals["Feel"], goals["Rest"]
        weights = {
            feel.id: 1,
            rest.id: {"weight": 0, "until": YESTERDAY.isoformat(), "then": 1},
        }
        store.update_goal(Goal(id=OVERALL_ID, measure={"kind": "rollup", "agg": "weighted", "weights": weights}))

        (expired,) = reflections.prepare(YESTERDAY).expired_weights
        weights[rest.id]["until"] = TODAY.isoformat()
        store.update_goal(Goal(id=OVERALL_ID, measure={"kind": "rollup", "agg": "weighted", "weights": weights}))

        assert (expired.goal_path, expired.sub_goal_path, expired.weight, expired.then) == ("Overall", "Rest", 0, 1)
        assert reflections.prepare(YESTERDAY).expired_weights == []

    def test_refuses_a_day_that_hasnt_started(self):
        reflections, *_ = _setup([_feel("Feel")])

        with pytest.raises(ValueError, match="hasn't started yet"):
            reflections.prepare(TODAY + timedelta(days=1))


class TestSubjectiveIntervals:
    def test_within_its_interval_the_previous_days_rating_carries_over(self):
        reflections, health, _, _, goals = _setup([_feel("Mood", interval_days=7)])
        mood = goals["Mood"]
        health.confirm_assessments(
            [
                _rating(mood, date(2026, 9, 27), 60),  # asked
                _carried(mood, date(2026, 9, 28), 60, date(2026, 9, 27)),
                _carried(mood, date(2026, 9, 29), 65, date(2026, 9, 27)),  # changed on confirming
            ]
        )

        result = reflections.record(date(2026, 9, 30), [])

        assert result.questions == []
        carried = _rated(result)[mood.id]
        assert (carried.rating, carried.method) == (65, "subjective")
        assert carried.metrics == {"carried_from": "2026-09-29", "asked": "2026-09-27"}
        assert carried.explanation == "Carried over from 2026-09-29 (last asked 2026-09-27; asked again 2026-10-04)"

    def test_its_asked_again_once_its_interval_has_passed(self):
        reflections, health, _, _, goals = _setup([_feel("Mood", interval_days=3)])
        health.confirm_assessments([_rating(goals["Mood"], date(2026, 9, 28), 60)])

        assert reflections.prepare(date(2026, 9, 30)).questions == []
        assert [q.prompt for q in reflections.prepare(YESTERDAY).questions] == ["How was mood?"]

    def test_a_rating_given_in_passing_restarts_the_interval(self):
        reflections, health, _, _, goals = _setup([_feel("Mood", interval_days=3)])
        mood = goals["Mood"]
        health.confirm_assessments([_rating(mood, date(2026, 9, 20), 60)])
        health.record_assessments([_rating(mood, date(2026, 9, 29), 90)])  # proposed, in passing

        result = reflections.record(date(2026, 9, 30), [])

        assert result.questions == []
        assert _rated(result)[mood.id].rating == 90
        assert _rated(result)[mood.id].metrics["asked"] == "2026-09-29"

    def test_a_carried_rating_doesnt_restart_the_interval(self):
        reflections, health, _, _, goals = _setup([_feel("Mood", interval_days=2)])
        mood = goals["Mood"]
        health.confirm_assessments(
            [_rating(mood, date(2026, 9, 28), 60), _carried(mood, date(2026, 9, 29), 60, date(2026, 9, 28))]
        )

        assert len(reflections.prepare(date(2026, 9, 30)).questions) == 1

    def test_asked_every_day_by_default(self):
        reflections, health, _, _, goals = _setup([_feel("Mood")])
        health.confirm_assessments([_rating(goals["Mood"], date(2026, 9, 30), 60)])

        assert len(reflections.prepare(YESTERDAY).questions) == 1


class TestOnlyIf:
    def _setup(self, interval_days=None, kind="subjective", **only_if):
        measure = (
            {"kind": "subjective", "prompt": "How did practice go?"} if kind == "subjective"
            else {"kind": "llm", "rubric": "Was the practice focused?"}
        )
        if interval_days:
            measure["interval_days"] = interval_days
        reflections, health, store, calendar, goals = _setup(
            [Goal(name="Piano"), Goal(name="Practice", parent_id="Piano", measure=measure)]
        )
        only_if = {"events_of": goals["Piano"].id, **only_if}
        store.update_goal(Goal(id=goals["Practice"].id, measure={**measure, "only_if": only_if}))
        return reflections, health, calendar, goals["Piano"], goals["Practice"]

    @pytest.mark.parametrize("kind", ["subjective", "llm"])
    def test_a_day_without_its_goals_events_is_skipped_without_asking(self, kind):
        reflections, _, _, piano, practice = self._setup(kind=kind)

        result = reflections.record(YESTERDAY, [])

        assert result.questions == []
        skipped = _rated(result)[practice.id]
        assert (skipped.rating, skipped.method) == ("skip", kind)
        assert skipped.explanation == "No events of Piano that day → skip"
        assert skipped.unmet

    def test_a_day_with_them_is_asked(self):
        reflections, _, calendar, piano, _ = self._setup()
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T18:30", [piano.id])]

        assert [q.prompt for q in reflections.prepare(YESTERDAY).questions] == ["How did practice go?"]

    @pytest.mark.parametrize("include_sub_goals, asked", [(True, True), (False, False)])
    def test_events_of_its_goals_sub_goals_count_unless_left_out(self, include_sub_goals, asked):
        reflections, _, calendar, piano, _ = self._setup(include_sub_goals=include_sub_goals)
        store = reflections._goals
        store.create_goal(Goal(name="Scales", parent_id=piano.id))
        scales = next(g for g in store.tree().goals if g.name == "Scales")
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T18:30", [scales.id])]

        assert bool(reflections.prepare(YESTERDAY).questions) == asked

    def test_its_interval_passes_over_skipped_days_carrying_the_last_answer(self):
        reflections, health, calendar, piano, practice = self._setup(interval_days=7)
        health.confirm_assessments(
            [
                _rating(practice, date(2026, 9, 27), 60),  # asked
                _unmet(practice, piano, date(2026, 9, 28)),
                _unmet(practice, piano, date(2026, 9, 29)),
            ]
        )
        calendar.events = [_event("2026-09-30T18:00", "2026-09-30T18:30", [piano.id])]

        result = reflections.record(date(2026, 9, 30), [])

        assert result.questions == []
        carried = _rated(result)[practice.id]
        assert (carried.rating, carried.metrics["carried_from"]) == (60, "2026-09-27")

    def test_a_skipped_day_isnt_an_answer(self):
        reflections, health, calendar, piano, practice = self._setup(interval_days=2)
        health.confirm_assessments(
            [_rating(practice, date(2026, 9, 27), 60), _unmet(practice, piano, date(2026, 9, 28))]
        )
        calendar.events = [_event("2026-09-29T18:00", "2026-09-29T18:30", [piano.id])]

        assert len(reflections.prepare(date(2026, 9, 29)).questions) == 1


def _unmet(goal, source, day):
    return _rating(goal, day, "skip", explanation="No events of Piano that day → skip", metrics={"only_if": source.id})


def _carried(goal, day, rating, asked):
    return _rating(goal, day, rating, metrics={"carried_from": (day - timedelta(days=1)).isoformat(), "asked": asked.isoformat()})


def _neighbor():
    """Neighbor (priority 2, top-level) over Parents (measured: visited 6
    weeks ago, so 100) and Cousins (asked); Cooking (priority 0) measured
    at 50; Tidy (no priority) at 0."""
    reflections, health, store, calendar, goals = _setup(
        [
            Goal(name="Neighbor", priority=2),
            Goal(name="Parents", parent_id="Neighbor", measure={"kind": "count", "target": 1, "interval_days": 60}),
            _feel("Cousins", parent="Neighbor"),
            Goal(name="Cooking", priority=0, measure={"kind": "duration", "target_min": 120}),
            Goal(name="Tidy", measure={"kind": "count", "target": 1}),
        ]
    )
    calendar.events = [
        _event("2026-08-15T10:00", "2026-08-15T16:00", [goals["Parents"].id]),
        _event("2026-10-01T18:00", "2026-10-01T19:00", [goals["Cooking"].id]),
    ]
    return reflections, health, calendar, goals


class TestRecord:
    def test_a_dry_run_fills_in_what_it_can_and_rolls_up_provisionally(self):
        reflections, _, calendar, goals = _neighbor()

        result = reflections.record(YESTERDAY, [])

        assert result.status == "preview"
        assert result.summary.splitlines() == [
            "**Thu Oct 1 · Overall ~50 🟡** (1 answer to go)",
            "",
            "**Priority 0**",
            "🟡 **Cooking 50**: 1h of 2h in the day",
            "",
            "**Priority 2**",
            "🟢 **Neighbor ~100**: Cousins ? · Parents 100",
            "🔴 **Tidy 0**: 0 of 1 events in the day",
        ]
        assert [(q.path, q.prompt) for q in result.questions] == [("Neighbor › Cousins", "How was cousins?")]
        assert (result.overall, result.overall_provisional) == (50, True)
        assert not result.complete
        assert calendar.health_events == {}

    def test_answers_make_it_final_and_confirm_every_rating_in_one_write(self):
        reflections, _, calendar, goals = _neighbor()

        result = reflections.record(YESTERDAY, [_rating(goals["Cousins"], YESTERDAY, 40)], dry_run=False)

        assert result.status == "recorded"
        assert result.complete
        assert "every goal is rated" in result.message
        assert result.summary.splitlines()[0] == "**Thu Oct 1 · Overall 40 🟡**"
        assert "🟢 **Neighbor 70**: Cousins 40 · Parents 100" in result.summary
        assert {a.goal_id for a in result.recorded} == {
            goals[name].id for name in ("Neighbor", "Parents", "Cousins", "Cooking", "Tidy", "Overall")
        }
        assert {a.status for a in result.recorded} == {"confirmed"}
        (event,) = calendar.health_events.values()
        assert event["id"] == day_event_id(YESTERDAY, 1)
        assert event["summary"] == "📝 Reflection · 2026-10-01 · 🟡 40"
        assert event["extendedProperties"]["private"]["cascading-time-tracker-complete"] == "true"
        assert reflections.prepare(YESTERDAY).already_reflected

    def test_a_proposed_llm_rating_is_provisional_until_recorded(self):
        reflections, _, _, _, goals = _setup([Goal(name="Life", measure={"kind": "llm", "rubric": "Balance"})])
        life = _rating(goals["Life"], YESTERDAY, 60, method="llm", rationale="Busy but fine")

        preview = reflections.record(YESTERDAY, [life], [goals["Life"].id])
        recorded = reflections.record(YESTERDAY, [life], dry_run=False)

        assert "🟡 **Life ~60**: Busy but fine" in preview.summary
        assert preview.summary.splitlines()[0].endswith("(1 answer to go)")
        assert preview.overall_provisional
        assert "🟡 **Life 60**: Busy but fine" in recorded.summary
        assert recorded.complete

    def test_unanswered_questions_leave_the_rest_recorded_and_the_day_in_progress(self):
        reflections, _, calendar, goals = _neighbor()

        result = reflections.record(YESTERDAY, [], dry_run=False)

        assert not result.complete
        assert "1 question(s) still to answer" in result.message
        # Provisional rollups aren't written.
        assert {a.goal_id for a in result.recorded} == {goals[name].id for name in ("Parents", "Cooking", "Tidy")}
        assert calendar.health_events[day_event_id(YESTERDAY, 1)]["summary"].endswith("(in progress)")
        assert [q.path for q in reflections.prepare(YESTERDAY).questions] == ["Neighbor › Cousins"]

    def test_a_changed_rating_is_kept_and_rolls_up_again(self):
        reflections, _, _, goals = _neighbor()
        cousins = _rating(goals["Cousins"], YESTERDAY, 40)
        reflections.record(YESTERDAY, [cousins], dry_run=False)
        tidy = _rating(
            goals["Tidy"], YESTERDAY, 80, method="metric", explanation="0 of 1 events in the day → 0", rationale="Did tidy"
        )

        reflections.record(YESTERDAY, [tidy], dry_run=False)
        again = reflections.record(YESTERDAY, [], dry_run=False)

        assert _rated(again)[goals["Tidy"].id].rating == 80  # kept, though measured at 0
        assert _rated(again)[goals["Cousins"].id].rating == 40  # answered: not asked again
        assert _rated(again)[OVERALL_ID].explanation == "Mean of 3 sub-goals (70, 50, 80) → 67"
        assert again.recorded == []  # nothing changed

    def test_lines_are_by_priority_then_best_first_with_unmeasured_ones_last(self):
        reflections, health, store, calendar, goals = _setup(
            [
                Goal(name="Home", priority=1),
                Goal(name="Sweep", parent_id="Home", measure={"kind": "count", "target": 1}),
                Goal(name="Dust", parent_id="Home", priority=1, measure={"kind": "count", "target": 1}),
                Goal(name="Promise", priority=1),
                Goal(name="Read", priority=1, measure={"kind": "duration", "target_min": 60}),
            ]
        )
        calendar.events = [_event("2026-10-01T09:00", "2026-10-01T09:30", [goals["Dust"].id])]
        health.confirm_assessments([_rating(goals["Read"], YESTERDAY - timedelta(days=1), 80, method="metric")])

        result = reflections.record(YESTERDAY, [])

        assert [(line.name, line.rating, line.state, line.change) for line in result.lines] == [
            ("Dust", 100, "final", None),
            ("Home", 50, "final", None),
            ("Read", 0, "final", -80),
            ("Promise", None, "unmeasured", None),
        ]
        assert "🔴 **Read 0** ↓80: 0m of 1h in the day\n" in result.summary + "\n"
        assert "🟡 **Home 50**: Sweep 0" in result.summary  # Dust has a line of its own
        assert "⚪ **Promise**: not measured" in result.summary

    def test_a_name_shared_with_another_goal_comes_after_its_parents(self):
        reflections, _, _, _, goals = _setup(
            [
                Goal(name="Family", priority=1),
                Goal(name="Parents", parent_id="Family"),
                Goal(name="Visit", parent_id="Parents", measure={"kind": "count", "target": 1}),
                Goal(name="Cousins", parent_id="Family"),
                Goal(name="Visit", parent_id="Cousins", measure={"kind": "count", "target": 1}),
            ]
        )

        result = reflections.record(YESTERDAY, [])

        assert "🔴 **Family 0**: Parents › Visit 0 · Cousins › Visit 0" in result.summary

    @pytest.mark.parametrize(
        "assessments, proposed, message",
        [
            ("wrong day", None, "rates 2026-10-01, not 2026-09-30"),
            ("twice", None, "rated more than once"),
            ("none", ["xxxxxx"], "proposed names goals not rated in this call: xxxxxx"),
        ],
    )
    def test_refuses_what_it_cant_record_and_writes_nothing(self, assessments, proposed, message):
        reflections, _, _, calendar, goals = _setup([_COOKING])
        cooking = goals["Cooking"]
        assessments = {
            "wrong day": [_rating(cooking, date(2026, 9, 30))],
            "twice": [_rating(cooking, YESTERDAY), _rating(cooking, YESTERDAY)],
            "none": [],
        }[assessments]

        with pytest.raises(ValueError, match=message):
            reflections.record(YESTERDAY, assessments, proposed, dry_run=False)

        assert calendar.health_events == {}


class TestSleepBoundaries:
    def test_a_day_whose_sleep_isnt_logged_is_refused_and_flagged(self):
        reflections, _, _, calendar, _ = _setup([_WAKE])
        calendar.nightly_sleep = False
        calendar.events = [
            _event("2026-09-29T23:00", "2026-09-30T07:00", is_end_of_day_sleep=True),
            _event("2026-09-30T23:00", "2026-10-01T07:00", is_end_of_day_sleep=True),
        ]

        with pytest.raises(ValueError, match="No end-of-day sleep ends on 2026-09-29"):
            reflections.prepare(date(2026, 9, 29))
        # Waking on the 2nd isn't logged, so the 1st isn't over.
        with pytest.raises(ValueError, match="2026-10-01 isn't over yet: it ends when you wake on 2026-10-02"):
            reflections.prepare(YESTERDAY)
        choices = {c.day: c for c in reflections.prepare().choices}

        assert choices[date(2026, 9, 30)].missing_sleep == []
        assert choices[date(2026, 9, 30)].starts == datetime(2026, 9, 30, 7, tzinfo=TZ)
        assert choices[date(2026, 9, 29)].missing_sleep == [date(2026, 9, 29)]
        assert choices[date(2026, 9, 29)].starts is None

    def test_a_day_still_going_on_cant_be_prepared_or_recorded(self):
        reflections, *_ = _setup([_WAKE])

        with pytest.raises(ValueError, match="isn't over yet"):
            reflections.prepare(TODAY)
        with pytest.raises(ValueError, match="isn't over yet"):
            reflections.record(TODAY, [], dry_run=False)
