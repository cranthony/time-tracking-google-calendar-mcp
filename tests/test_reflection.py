from datetime import date, datetime, timedelta

import pytest

from tests.fake_sheets import FakeSheets
from tests.test_goal_health import NOW, TODAY, TZ, YESTERDAY, FakeCalendar, _event
from utilities.goal_health import Assessment, GoalHealth
from utilities.goal_sheet import Goal
from utilities.goals import Goals
from utilities.noted_time_sheet import NotedTime
from utilities.reflection import Reflections, reflection_event_id

# TODAY is Friday 2026-10-02; YESTERDAY, Oct 1, is the last day to end.


class FakeNotes:
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
    # Created long enough ago to have days to reflect on.
    tree = store.tree()
    for goal in tree.goals:
        goal.created = date(2026, 9, 1)
    store._sheet.write(tree.goals)
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
_WAKE = Goal(name="Wake", measure={"kind": "wake_time", "target": "07:00"})


class TestChoices:
    def test_with_no_day_named_it_offers_completed_days_not_fully_reflected_on(self):
        reflections, _, _, _, goals = _setup([_WAKE])
        unreflected = {"09-21", "09-24", "09-25", "09-28"}
        for day in range(1, 31):
            if f"09-{day:02d}" not in unreflected:
                _reflect(reflections, goals, date(2026, 9, day))
        _reflect(reflections, goals, YESTERDAY)
        reflections.record(date(2026, 9, 28), [], journal="started, nothing rated", dry_run=False)

        context = reflections.prepare()

        # It's 21:00 on Oct 2, which isn't over: the newest three completed
        # days without a full reflection, each ending at 7am.
        assert context.day is None
        assert not context.due
        assert [(c.day, c.ends) for c in context.choices] == [
            (date(2026, 9, 28), datetime(2026, 9, 29, 7, tzinfo=TZ)),
            (date(2026, 9, 25), datetime(2026, 9, 26, 7, tzinfo=TZ)),
            (date(2026, 9, 24), datetime(2026, 9, 25, 7, tzinfo=TZ)),
        ]
        assert context.older_unreflected == 1
        assert "Ask which day" in context.instructions

    def test_a_day_rated_only_partway_is_offered_as_started(self):
        reflections, health, _, _, goals = _setup([Goal(name="Home"), _feel("Cook", parent="Home")])
        for day in range(1, 31):
            reflections.record(date(2026, 9, day), [], dry_run=False)
        reflections.record(YESTERDAY, [_rating(goals["Cook"], YESTERDAY)], dry_run=False)  # Home's still to go

        (choice,) = reflections.prepare().choices[:1]

        assert (choice.day, choice.started, choice.already_reflected) == (YESTERDAY, True, False)

    def test_when_every_recent_day_is_reflected_it_offers_the_last_one_again(self):
        reflections, _, _, _, goals = _setup([_WAKE])
        for back in range(1, 13):
            _reflect(reflections, goals, TODAY - timedelta(days=back))

        context = reflections.prepare()

        assert [(c.day, c.already_reflected) for c in context.choices] == [(YESTERDAY, True)]
        assert context.older_unreflected == 0


def _reflect(reflections, goals, day):
    reflections.record(day, [_rating(goals["Wake"], day, "skip", method="metric")], dry_run=False)


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

    def test_rates_every_active_goal_proposing_measured_or_recorded_ratings(self):
        reflections, health, store, calendar, goals = _setup([_COOKING, _feel("Feel"), _WAKE, Goal(name="Folder")])
        cooking, feel = goals["Cooking"], goals["Feel"]
        store.create_goal(Goal(name="Paused", status="inactive", measure={"kind": "subjective", "prompt": "?"}))
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T19:00", [cooking.id])]
        health.confirm_assessments([_rating(feel, YESTERDAY - timedelta(days=1), 60)])
        health.record_assessments([_rating(feel, YESTERDAY, 75, rationale="said in passing")])

        context = reflections.prepare(YESTERDAY)

        due = {d.path: d for d in context.due}
        assert set(due) == {"Cooking", "Feel", "Wake"}  # not the paused one, nor one with nothing to rate it by
        assert due["Cooking"].proposed.explanation == "1h of 2h in the day → 50"
        assert due["Wake"].proposed.rating == 100
        assert due["Feel"].proposed.rating == 75 and due["Feel"].proposed.status == "proposed"
        assert due["Feel"].ask is None
        assert [(r.day, r.rating) for r in due["Feel"].recent] == [(date(2026, 9, 30), 60)]
        assert context.unmeasured == ["Folder"]
        assert context.waiting == []

    def test_a_subjective_goal_due_to_be_asked_has_its_prompt(self):
        reflections, *_ = _setup([_feel("Feel")])

        (due,) = reflections.prepare(YESTERDAY).due

        assert (due.ask, due.proposed) == ("How was feel?", None)

    def test_goes_up_the_tree_a_level_at_a_time(self):
        reflections, health, store, calendar, goals = _setup(
            [
                Goal(name="Neighbor"),
                Goal(name="Parents", parent_id="Neighbor", measure={
                    "kind": "count", "target": 1, "interval_days": 60, "zero_at_days": 120,
                }),
                _feel("Cousins", parent="Neighbor"),
            ]
        )
        parents, cousins = goals["Parents"], goals["Cousins"]
        calendar.events = [_event("2026-08-15T10:00", "2026-08-15T16:00", [parents.id])]

        first = reflections.prepare(YESTERDAY)

        assert [d.path for d in first.due] == ["Neighbor › Parents", "Neighbor › Cousins"]
        assert first.waiting == ["Neighbor"]
        assert first.due[0].proposed.rating == 100
        reflections.record(
            YESTERDAY, [first.due[0].proposed, _rating(cousins, YESTERDAY, 50)], dry_run=False
        )

        second = reflections.prepare(YESTERDAY)

        (neighbor,) = second.due
        assert neighbor.path == "Neighbor"
        assert [(s.path, s.rating) for s in neighbor.sub_goals] == [("Neighbor › Parents", 100), ("Neighbor › Cousins", 50)]
        assert neighbor.proposed.explanation == "Mean of 2 sub-goals (100, 50) → 75"
        assert neighbor.measure == {"kind": "rollup", "agg": "mean"}
        assert {a.goal_id for a in second.rated} == {parents.id, cousins.id}
        assert second.waiting == []
        assert not second.already_reflected

    def test_an_llm_goal_sees_its_sub_goals_ratings(self):
        reflections, health, _, _, goals = _setup(
            [Goal(name="Life", measure={"kind": "llm", "rubric": "Balance of the parts"}), _feel("Rest", parent="Life")]
        )
        health.confirm_assessments([_rating(goals["Rest"], YESTERDAY, 30)])

        (life,) = reflections.prepare(YESTERDAY).due

        assert life.proposed is None and life.ask is None
        assert [(s.path, s.rating) for s in life.sub_goals] == [("Life › Rest", 30)]

    def test_counts_time_per_goal_including_sub_goals(self):
        reflections, _, store, calendar, goals = _setup([_COOKING, Goal(name="Tofu", parent_id="Cooking")])
        cooking, tofu = goals["Cooking"], goals["Tofu"]
        calendar.events = [
            _event("2026-10-01T18:00", "2026-10-01T19:00", [tofu.id]),
            _event("2026-10-01T12:00", "2026-10-01T12:30", [cooking.id]),
        ]

        context = reflections.prepare(YESTERDAY)

        assert [(t.path, t.minutes) for t in context.goal_time] == [("Cooking", 90), ("Cooking › Tofu", 60)]

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

    def test_brings_back_the_previous_days_intentions(self):
        reflections, *_ = _setup([_feel("Feel")])
        reflections.record(YESTERDAY - timedelta(days=1), [], intentions=["cook twice", " "], dry_run=False)

        context = reflections.prepare(YESTERDAY)

        assert context.previous_intentions == ["cook twice"]
        assert "record_reflection" in context.instructions

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

        (due,) = reflections.prepare(date(2026, 9, 30)).due

        assert due.ask is None
        assert (due.proposed.rating, due.proposed.method) == (65, "subjective")
        assert due.proposed.metrics == {"carried_from": "2026-09-29", "asked": "2026-09-27"}
        assert due.proposed.explanation == "Carried over from 2026-09-29 (last asked 2026-09-27; asked again 2026-10-04)"

    def test_its_asked_again_once_its_interval_has_passed(self):
        reflections, health, _, _, goals = _setup([_feel("Mood", interval_days=3)])
        mood = goals["Mood"]
        health.confirm_assessments([_rating(mood, date(2026, 9, 28), 60)])

        assert reflections.prepare(date(2026, 9, 30)).due[0].ask is None
        assert reflections.prepare(YESTERDAY).due[0].ask == "How was mood?"

    def test_a_rating_given_in_passing_restarts_the_interval(self):
        reflections, health, _, _, goals = _setup([_feel("Mood", interval_days=3)])
        mood = goals["Mood"]
        health.confirm_assessments([_rating(mood, date(2026, 9, 20), 60)])
        health.record_assessments([_rating(mood, date(2026, 9, 29), 90)])  # proposed, in passing

        (due,) = reflections.prepare(date(2026, 9, 30)).due

        assert due.ask is None
        assert due.proposed.rating == 90
        assert due.proposed.metrics["asked"] == "2026-09-29"

    def test_a_carried_rating_doesnt_restart_the_interval(self):
        reflections, health, _, _, goals = _setup([_feel("Mood", interval_days=2)])
        mood = goals["Mood"]
        health.confirm_assessments(
            [_rating(mood, date(2026, 9, 28), 60), _carried(mood, date(2026, 9, 29), 60, date(2026, 9, 28))]
        )

        assert reflections.prepare(date(2026, 9, 30)).due[0].ask == "How was mood?"

    def test_asked_every_day_by_default(self):
        reflections, health, _, _, goals = _setup([_feel("Mood")])
        health.confirm_assessments([_rating(goals["Mood"], date(2026, 9, 30), 60)])

        assert reflections.prepare(YESTERDAY).due[0].ask == "How was mood?"


def _carried(goal, day, rating, asked):
    return _rating(goal, day, rating, metrics={"carried_from": (day - timedelta(days=1)).isoformat(), "asked": asked.isoformat()})


class TestRecord:
    def test_a_dry_run_previews_without_writing(self):
        reflections, _, _, calendar, goals = _setup([_COOKING, _feel("Feel")])
        cooking = goals["Cooking"]
        rating = _rating(cooking, YESTERDAY, 50, method="metric", explanation="1h of 2h in the day → 50")

        result = reflections.record(YESTERDAY, [rating], journal="busy day", intentions=["rest"])

        assert result.status == "preview"
        assert result.preview.splitlines() == [
            "🟡 Cooking: 50 — 1h of 2h in the day → 50",
            "· Feel: not rated yet",
            "Journal: busy day",
            "Intention: rest",
        ]
        assert result.not_rated == ["Feel"]
        assert not result.complete
        assert calendar.health_events == {}

    def test_committing_confirms_the_ratings_and_records_the_reflection(self):
        reflections, _, store, calendar, goals = _setup([_COOKING, _feel("Feel")])
        cooking, feel = goals["Cooking"], goals["Feel"]

        result = reflections.record(
            YESTERDAY,
            [_rating(cooking, YESTERDAY, 50, method="metric"), _rating(feel, YESTERDAY, "skip")],
            journal="busy day",
            intentions=["rest"],
            dry_run=False,
        )

        assert result.status == "recorded"
        assert result.complete
        assert "every goal is rated" in result.message
        assert {a.status for a in result.assessments} == {"confirmed"}
        reflection = calendar.health_events[reflection_event_id(YESTERDAY)]
        assert reflection["description"] == "busy day"
        assert reflection["summary"] == "📝 Reflection · 2026-10-01"
        assert reflection["extendedProperties"]["private"]["cascading-time-tracker-complete"] == "true"
        listed = {g.name: g for g in store.get_goals().goals}
        assert listed["Cooking"].health == 50  # the cache follows confirmed ratings

    def test_a_level_at_a_time_keeping_the_journal_until_a_new_one_is_given(self):
        reflections, _, _, calendar, goals = _setup([Goal(name="Home"), _feel("Cook", parent="Home")])
        home, cook = goals["Home"], goals["Cook"]

        first = reflections.record(YESTERDAY, [_rating(cook, YESTERDAY, 60)], journal="tired", dry_run=False)

        assert not first.complete
        assert "Ready to rate now: Home" in first.message
        event = calendar.health_events[reflection_event_id(YESTERDAY)]
        assert event["summary"].endswith("(in progress)")

        second = reflections.record(YESTERDAY, [_rating(home, YESTERDAY, 60, method="rollup")], dry_run=False)

        assert second.complete
        assert calendar.health_events[reflection_event_id(YESTERDAY)]["description"] == "tired"
        assert reflections.prepare(YESTERDAY).already_reflected

    def test_refuses_a_goal_before_its_sub_goals(self):
        reflections, _, _, calendar, goals = _setup([Goal(name="Home"), _feel("Cook", parent="Home")])

        with pytest.raises(ValueError, match="Home can't be rated until its sub-goals are \\(Home › Cook\\)"):
            reflections.record(
                YESTERDAY,
                [_rating(goals["Cook"], YESTERDAY, 60), _rating(goals["Home"], YESTERDAY, 60, method="rollup")],
                dry_run=False,
            )

        assert calendar.health_events == {}

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"assessments": "wrong day"}, "rates 2026-10-01, not 2026-09-30"),
            ({"assessments": "twice"}, "rated more than once"),
            ({"intentions": ["a", "b", "c", "d"]}, "at most 3 intentions"),
            ({"journal": "x" * 8001}, "journal is longer than 8000 bytes"),
        ],
    )
    def test_refuses_what_it_cant_record_and_writes_nothing(self, kwargs, message):
        reflections, _, _, calendar, goals = _setup([_COOKING])
        cooking = goals["Cooking"]
        assessments = {
            "wrong day": [_rating(cooking, date(2026, 9, 30))],
            "twice": [_rating(cooking, YESTERDAY), _rating(cooking, YESTERDAY)],
        }.get(kwargs.pop("assessments", None), [])

        with pytest.raises(ValueError, match=message):
            reflections.record(YESTERDAY, assessments, dry_run=False, **kwargs)

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
