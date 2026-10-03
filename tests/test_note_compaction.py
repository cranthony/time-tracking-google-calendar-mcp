from datetime import timedelta

import pytest

from calendar_clients.google_calendar import MAX_DESCRIPTION_BYTES, Event
from tests.event_time_helpers import event_at, time_at
from utilities.note_compaction import (
    CompactionError,
    EventDecision,
    EventState,
    PlanNote,
    plan_compaction,
)

_NEXT_DAY = timedelta(days=1)


def _day():
    return [
        event_at("09:00-10:00", id="e1", summary="Email", priority=2),
        event_at("10:00-11:00", id="e2", summary="Report", priority=2),
        event_at("12:00-13:00", id="e3", summary="Lunch", priority=1),
        event_at("14:00-15:00", id="e4", summary="Review", priority=3),
        event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
    ]


def _evening():
    """The salsa evening: notes only mark leaving early and finishing dinner."""
    return [
        event_at("17:00-18:15", id="w", summary="Work", priority=2),
        event_at("18:30-19:30", id="salsa", summary="Google Salsa class", priority=2),
        event_at("19:30-20:00", id="dinner", summary="Dinner", priority=2),
        event_at("20:00-21:00", id="read", summary="Reading", priority=3),
        event_at("22:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
    ]


def _overnight():
    """Last night's sleep, then the next day (the day after `DAY`)."""
    return [
        event_at("20:00-07:00+1", id="s0", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        event_at("07:00+1-08:00+1", id="gr", summary="Getting Ready", priority=2),
        Event(
            id="s1",
            summary="Sleep",
            start=time_at("22:00+1"),
            end=time_at("07:00+1") + _NEXT_DAY,
            priority=0,
            is_end_of_day_sleep=True,
        ),
    ]


def _note(number: int, at: str, description: str | None = None) -> PlanNote:
    return PlanNote(id=f"n{number}", timestamp=time_at(at), description=description)


def _keep(event_id: str, **fields) -> EventDecision:
    return EventDecision(action="keep", event_id=event_id, **fields)


def _by_event(plan):
    return {c.event_id: c for c in plan.changes if c.event_id}


def _span(state: EventState) -> tuple:
    return state.start, state.end


def _plan(notes, decisions, day=None, now="11:30", **kwargs):
    return plan_compaction(notes, decisions, day if day is not None else _day(), time_at(now), **kwargs)


class TestSilenceMeansOnSchedule:
    def test_past_events_nobody_mentions_are_pinned_where_they_were_planned(self):
        plan = _plan([], [])

        changes = _by_event(plan)
        assert set(changes) == {"e1", "e2"}
        assert _span(changes["e1"].after) == (time_at("09:00"), time_at("10:00"))
        assert changes["e1"].after.is_fixed_time is True
        assert changes["e1"].after.min_duration_minutes == 60
        assert "as planned" in changes["e1"].reason

    def test_an_already_pinned_past_event_needs_no_change(self):
        day = _day()
        day[0].is_fixed_time = True
        day[0].min_duration = timedelta(hours=1)

        assert "e1" not in _by_event(_plan([], [], day))

    def test_an_event_still_in_progress_is_left_alone(self):
        assert "e2" not in _by_event(_plan([], [], now="10:30"))

    def test_an_end_note_for_one_event_does_not_merge_it_with_the_one_before(self):
        # Only dinner's end was noted: the class and dinner's start still
        # happened as planned, and nothing is merged or cancelled.
        plan = _plan(
            [_note(1, "18:15", "Leaving for salsa early to prep"), _note(2, "20:10", "Done with dinner")],
            [
                EventDecision(action="create", summary="Salsa prep", start_note="n1", end=time_at("18:30")),
                _keep("dinner", end_note="n2"),
                _keep("read", start_note="n2"),
            ],
            _evening(),
            now="22:30",
        )

        changes = _by_event(plan)
        assert _span(changes["salsa"].after) == (time_at("18:30"), time_at("19:30"))
        assert changes["salsa"].after.summary == "Google Salsa class"
        assert _span(changes["dinner"].after) == (time_at("19:30"), time_at("20:10"))
        assert _span(changes["read"].after) == (time_at("20:10"), time_at("21:00"))
        assert not any(c.action == "cancel" for c in plan.changes)
        [created] = [c for c in plan.changes if c.action == "create"]
        assert _span(created.after) == (time_at("18:15"), time_at("18:30"))

    def test_the_end_of_day_sleep_event_is_never_pinned(self):
        plan = _plan([], [], now="07:00+1")

        assert "s1" not in _by_event(plan)


class TestKeep:
    def test_notes_set_the_edges_they_mark_and_the_result_is_pinned(self):
        plan = _plan(
            [_note(1, "09:05"), _note(2, "10:20")],
            [_keep("e1", start_note="n1", end_note="n2"), _keep("e2", start_note="n2")],
        )

        changes = _by_event(plan)
        assert _span(changes["e1"].after) == (time_at("09:05"), time_at("10:20"))
        assert changes["e1"].after.is_fixed_time is True
        assert changes["e1"].after.min_duration_minutes == 75
        assert "realigned" in changes["e1"].reason
        # An edge left out stays as planned.
        assert _span(changes["e2"].after) == (time_at("10:20"), time_at("11:00"))
        # The future is untouched.
        assert "e3" not in changes and "e4" not in changes

    def test_an_explicit_time_wins_over_its_note(self):
        # "Leaving 15 minutes early" -- the note's own time isn't the edge.
        plan = _plan(
            [_note(1, "08:30", "leaving in 15")],
            [_keep("e1", start=time_at("08:45"), start_note="n1")],
        )

        assert _span(_by_event(plan)["e1"].after) == (time_at("08:45"), time_at("10:00"))
        email = next(e for e in plan.timeline.events if e.event_id == "e1")
        assert email.start_note == "n1"

    def test_renames_and_annotates(self):
        plan = _plan([], [_keep("e1", summary="Deep work", annotate="phone rang")])

        change = _by_event(plan)["e1"]
        assert change.after.summary == "Deep work"
        assert change.after.description == "Notes:\n- phone rang"

    def test_renaming_a_future_event_does_not_pin_it(self):
        change = _by_event(_plan([], [_keep("e3", summary="Team lunch")]))["e3"]

        assert change.after.summary == "Team lunch"
        assert change.after.is_fixed_time is None
        assert _span(change.after) == (time_at("12:00"), time_at("13:00"))

    def test_moving_a_future_event_reschedules_it_and_reflows_the_rest(self):
        plan = _plan([], [_keep("e3", start=time_at("13:30"), end=time_at("14:30"))])

        changes = _by_event(plan)
        assert _span(changes["e3"].after) == (time_at("13:30"), time_at("14:30"))
        assert changes["e3"].after.is_fixed_time is True
        assert "moved as requested" in changes["e3"].reason
        assert changes["e4"].after.start == time_at("14:30")

    def test_reflows_a_contiguous_block_and_keeps_a_later_fixed_time_event_pinned(self):
        day = [
            event_at("11:30-12:00", id="lunch", summary="Lunch", priority=2),
            event_at("12:00-14:00", id="work", summary="Work", priority=2),
            event_at(
                "14:00-15:00",
                id="commute",
                summary="Commute",
                priority=2,
                is_fixed_time=True,
                min_duration=timedelta(hours=1),
            ),
            event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        ]

        plan = _plan([], [_keep("lunch", start=time_at("12:15"), end=time_at("12:45"))], day, now="10:00")

        changes = _by_event(plan)
        assert _span(changes["lunch"].after) == (time_at("12:15"), time_at("12:45"))
        assert _span(changes["work"].after) == (time_at("12:00"), time_at("12:15"))
        assert "commute" not in changes
        [created] = [c for c in plan.changes if c.action == "create"]
        assert created.after.summary == "Work"
        assert _span(created.after) == (time_at("12:45"), time_at("14:00"))

    def test_does_not_mutate_its_inputs(self):
        day = _day()
        decisions = [_keep("e1", summary="Deep work", end=time_at("09:30"))]

        _plan([], decisions, day)

        assert (day[0].summary, day[0].end) == ("Email", time_at("10:00"))
        assert decisions[0].summary == "Deep work"


class TestOverlaps:
    def test_an_overrun_into_a_past_event_is_rejected_naming_both(self):
        with pytest.raises(CompactionError) as excinfo:
            _plan([_note(1, "10:20")], [_keep("e1", end_note="n1")])

        message = str(excinfo.value)
        assert "'Email' (e1, as decided" in message
        assert "overlaps 'Report' (e2, on schedule" in message
        assert "Decide which gives way" in message

    def test_it_passes_once_the_decisions_say_which_gives_way(self):
        plan = _plan([_note(1, "10:20")], [_keep("e1", end_note="n1"), _keep("e2", start_note="n1")])

        assert _span(_by_event(plan)["e2"].after) == (time_at("10:20"), time_at("11:00"))

    def test_an_overrun_into_an_event_still_in_progress_reflows_it(self):
        plan = _plan([_note(1, "10:20")], [_keep("e1", end_note="n1")], now="10:30")

        assert _by_event(plan)["e2"].after.start == time_at("10:20")

    def test_a_new_event_over_a_past_one_is_rejected(self):
        with pytest.raises(CompactionError, match="'Coffee'.*overlaps 'Email'|'Email'.*overlaps 'Coffee'"):
            _plan([], [EventDecision(action="create", summary="Coffee", start=time_at("09:30"), end=time_at("09:45"))])

    def test_a_note_cannot_silently_eat_into_last_nights_sleep(self):
        day = _overnight()

        with pytest.raises(CompactionError, match="'Sleep' \\(s0, on schedule"):
            _plan([_note(1, "06:40+1")], [_keep("gr", start_note="n1")], day, now="09:00+1", day_start=time_at("06:40+1"))

    def test_waking_early_shortens_last_nights_sleep_when_asked(self):
        day = _overnight()

        plan = _plan(
            [_note(1, "06:40+1")],
            [_keep("s0", end_note="n1"), _keep("gr", start_note="n1")],
            day,
            now="09:00+1",
            day_start=time_at("06:40+1"),
        )

        changes = _by_event(plan)
        assert changes["s0"].after.end == time_at("06:40+1")
        assert "s1" not in changes
        assert not plan.warnings


class TestCancelCreateAndMerge:
    def test_cancel(self):
        change = _by_event(_plan([], [EventDecision(action="cancel", event_id="e2")]))["e2"]

        assert change.action == "cancel"
        assert "didn't happen" in change.reason

    def test_create_makes_a_pinned_new_event(self):
        plan = _plan(
            [_note(1, "11:05", "coffee"), _note(2, "11:20")],
            [EventDecision(action="create", summary="Coffee", start_note="n1", end_note="n2", goal_ids=["g1"])],
        )

        [created] = [c for c in plan.changes if c.action == "create"]
        assert _span(created.after) == (time_at("11:05"), time_at("11:20"))
        assert created.after.is_fixed_time is True
        assert created.after.goal_ids == ["g1"]
        # Its anchoring note's text isn't added to it -- it set its edge.
        assert created.after.description is None

    def test_keep_can_set_or_clear_an_events_goals_alone(self):
        day = _day()
        day[1].goal_ids = ["old"]

        plan = _plan([], [_keep("e1", goal_ids=["g1", "g2"]), _keep("e2", goal_ids=[]), _keep("e3")], day)

        changes = _by_event(plan)
        assert changes["e1"].after.goal_ids == ["g1", "g2"]
        assert changes["e2"].after.goal_ids == []
        assert "e3" not in changes or changes["e3"].after.goal_ids == changes["e3"].before.goal_ids

    def test_a_future_events_goals_change_without_pinning_it(self):
        plan = _plan([], [_keep("e4", goal_ids=["g1"])], now="11:30")

        change = _by_event(plan)["e4"]
        assert change.reason == "set the goals it serves"
        assert change.after.goal_ids == ["g1"]
        assert change.after.is_fixed_time is None

    def test_the_timeline_marks_goals_and_totals_their_time(self):
        day = _day()
        day[0].goal_ids = ["work"]
        day[1].goal_ids = ["work"]
        plan = _plan(
            [],
            [_keep("e2", goal_ids=["work", "writing"])],
            day,
            goal_names={"work": "Time Tracker", "writing": "Writing"},
        )

        lines = plan.timeline.text.splitlines()
        assert any("Email · on schedule  ◆ Time Tracker" in line for line in lines)
        assert any("Report · on schedule  ◆ Time Tracker ◇ Writing" in line for line in lines)
        assert "Goal time: Time Tracker 2h00m · Writing 1h00m" in lines

    def test_create_needs_both_edges(self):
        with pytest.raises(CompactionError, match="needs both a start and an end"):
            _plan([_note(1, "11:05")], [EventDecision(action="create", summary="Coffee", start_note="n1")])

    def test_create_needs_a_summary(self):
        with pytest.raises(CompactionError, match="needs a summary"):
            _plan([], [EventDecision(action="create", start=time_at("11:05"), end=time_at("11:20"))])

    def test_merge_folds_one_event_into_another(self):
        plan = _plan([], [EventDecision(action="merge", event_id="e2", into="e1")])

        changes = _by_event(plan)
        assert changes["e2"].action == "cancel"
        assert changes["e2"].reason == "merged into 'Email'"
        assert _span(changes["e1"].after) == (time_at("09:00"), time_at("11:00"))
        assert changes["e1"].after.summary == "Email and Report"
        report = next(e for e in plan.timeline.events if e.event_id == "e2")
        assert (report.status, report.merged_into) == ("merged", "Email and Report")

    def test_a_rename_on_the_target_overrides_the_joined_title(self):
        plan = _plan(
            [], [EventDecision(action="merge", event_id="e2", into="e1"), _keep("e1", summary="Morning work")]
        )

        assert _by_event(plan)["e1"].after.summary == "Morning work"

    def test_cannot_merge_into_a_cancelled_event(self):
        with pytest.raises(CompactionError, match="itself being cancelled"):
            _plan(
                [],
                [
                    EventDecision(action="merge", event_id="e2", into="e1"),
                    EventDecision(action="cancel", event_id="e1"),
                ],
            )

    def test_the_end_of_day_sleep_event_cannot_be_merged(self):
        with pytest.raises(CompactionError, match="can't be merged"):
            _plan([], [EventDecision(action="merge", event_id="e4", into="s1")], now="16:00")


class TestNotesAddedToEvents:
    def test_a_note_that_sets_no_edge_is_added_to_the_event_it_falls_within(self):
        plan = _plan([_note(1, "09:20", "phone rang"), _note(2, "09:40", "back to it")], [])

        assert _by_event(plan)["e1"].after.description == "Notes:\n- 09:20 phone rang\n- 09:40 back to it"

    def test_notes_and_annotate_text_go_below_an_existing_description(self):
        day = _day()
        day[0].description = "Inbox zero"

        plan = _plan([_note(1, "09:20", "phone rang")], [_keep("e1", annotate="mostly replies")], day)

        assert _by_event(plan)["e1"].after.description == (
            "Inbox zero\n\nNotes:\n- 09:20 phone rang\n- mostly replies"
        )

    def test_notes_that_would_overflow_a_description_are_refused_naming_them(self):
        # Calendar silently cuts a description past MAX_DESCRIPTION_BYTES.
        day = _day()
        day[0].description = "x" * (MAX_DESCRIPTION_BYTES - 20)

        with pytest.raises(CompactionError, match=r"'Email'.*ignore_notes \(n1, n2\)"):
            _plan([_note(1, "09:20", "phone rang"), _note(2, "09:40", "back to it")], [], day)

    def test_an_overflowing_annotate_is_refused(self):
        with pytest.raises(CompactionError, match=r"'Email'.*shorten its `annotate`"):
            _plan([], [_keep("e1", annotate="x" * MAX_DESCRIPTION_BYTES)])

    def test_the_limit_counts_utf8_bytes(self):
        # Under the limit in characters, over it in bytes.
        with pytest.raises(CompactionError, match="bytes"):
            _plan([], [_keep("e1", annotate="é" * (MAX_DESCRIPTION_BYTES // 2))])

    def test_a_description_that_just_fits_is_kept(self):
        annotate = "x" * (MAX_DESCRIPTION_BYTES - len("Notes:\n- "))

        plan = _plan([], [_keep("e1", annotate=annotate)])

        assert len(_by_event(plan)["e1"].after.description) == MAX_DESCRIPTION_BYTES

    def test_a_note_added_to_a_future_event_still_in_progress_changes_only_its_description(self):
        change = _by_event(_plan([_note(1, "10:15", "started outline")], [], now="10:30"))["e2"]

        assert change.reason == "added the notes that fall during it"
        assert change.after.is_fixed_time is None

    def test_ignored_notes_are_not_added(self):
        plan = _plan([_note(1, "09:20", "phone rang")], [], ignore_notes=["n1"])

        assert _by_event(plan)["e1"].after.description is None
        assert plan.timeline.notes[0].ignored is True

    def test_a_note_outside_every_event_is_reported(self):
        plan = _plan([_note(1, "11:10", "wandered")], [])

        assert any("doesn't fall within any event" in w for w in plan.warnings)


class TestValidation:
    def test_an_unknown_event_id_is_rejected_with_the_valid_ones(self):
        with pytest.raises(CompactionError, match="valid event ids: e1, e2, e3, e4, s1"):
            _plan([], [_keep("nope")])

    def test_an_unknown_note_id_is_rejected_with_the_valid_ones(self):
        with pytest.raises(CompactionError, match="'n9' isn't one of this round's notes; valid note ids: n1"):
            _plan([_note(1, "09:05")], [_keep("e1", start_note="n9")])

    def test_an_unknown_ignored_note_is_rejected(self):
        with pytest.raises(CompactionError, match="ignore_notes: 'n9'"):
            _plan([], [], ignore_notes=["n9"])

    def test_an_event_cannot_have_two_decisions(self):
        with pytest.raises(CompactionError, match="more than one decision"):
            _plan([], [_keep("e1"), EventDecision(action="cancel", event_id="e1")])

    def test_times_only_go_with_keep_and_create(self):
        with pytest.raises(CompactionError, match="only go with 'keep' or 'create'"):
            _plan([], [EventDecision(action="cancel", event_id="e1", end=time_at("09:30"))])

    def test_an_unknown_action_is_rejected(self):
        with pytest.raises(CompactionError, match="unknown action 'skip'"):
            _plan([], [EventDecision(action="skip", event_id="e1")])

    def test_a_non_positive_length_is_rejected(self):
        with pytest.raises(CompactionError, match="isn't a positive length"):
            _plan([], [_keep("e1", end=time_at("09:00"))])

    def test_notes_after_now_are_rejected(self):
        with pytest.raises(CompactionError, match="timestamped after now"):
            _plan([_note(1, "12:00")], [])

    def test_every_problem_is_reported_at_once(self):
        with pytest.raises(CompactionError) as excinfo:
            _plan([], [_keep("nope"), _keep("e1", start_note="n9")])

        assert "'nope'" in str(excinfo.value) and "'n9'" in str(excinfo.value)

    def test_with_nothing_to_change_it_says_so(self):
        day = [e for e in _day() if e.id != "e1" and e.id != "e2"]

        plan = _plan([], [], day)

        assert plan.changes == []
        assert plan.warnings == ["nothing on the calendar needs to change"]


class TestMovingBedtime:
    """Moving the day's own end-of-day sleep event moves where the day
    ends, rather than placing it like any other fact -- see `_end_day_at`."""

    @staticmethod
    def _evening(*extra):
        return [
            event_at("17:00-18:00", id="e1", summary="Dinner", priority=1),
            *extra,
            event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        ]

    def test_a_later_bedtime_just_moves_it(self):
        plan = _plan([], [_keep("s1", start=time_at("22:30"))], now="08:00")

        changes = _by_event(plan)
        assert set(changes) == {"s1"}
        assert _span(changes["s1"].after) == (time_at("22:30"), time_at("07:00") + _NEXT_DAY)
        assert changes["s1"].after.is_fixed_time is True
        assert plan.warnings == []

    def test_an_earlier_bedtime_shortens_what_runs_past_it(self):
        reading = event_at("18:00-20:00", id="e5", summary="Reading", priority=3)

        plan = _plan([], [_keep("s1", start=time_at("19:00"))], self._evening(reading), now="08:00")

        changes = _by_event(plan)
        assert _span(changes["e5"].after) == (time_at("18:00"), time_at("19:00"))
        assert changes["e5"].reason.startswith("shortened to end at the new bedtime")
        # Nothing of Reading is carried over past the sleep, into the next day.
        assert [c for c in plan.changes if c.action == "create"] == []

    def test_an_earlier_bedtime_cancels_what_starts_after_it(self):
        journal = event_at("19:30-20:00", id="e5", summary="Journal", priority=1)

        plan = _plan([], [_keep("s1", start=time_at("19:00"))], self._evening(journal), now="08:00")

        change = _by_event(plan)["e5"]
        assert change.action == "cancel"
        assert change.reason.startswith("doesn't fit before the new bedtime")

    def test_an_earlier_bedtime_over_a_fixed_time_event_is_an_error(self):
        call = event_at(
            "18:30-19:30", id="e5", summary="Call", is_fixed_time=True, min_duration=timedelta(hours=1)
        )

        with pytest.raises(CompactionError, match="doesn't fit starting at .*fixed-time 'Call'"):
            _plan([], [_keep("s1", start=time_at("19:00"))], self._evening(call), now="08:00")

    def test_moving_its_end_warns_that_the_next_day_is_not_adjusted(self):
        plan = _plan([], [_keep("s1", end=time_at("08:00") + _NEXT_DAY)], now="08:00")

        assert set(_by_event(plan)) == {"s1"}
        assert len(plan.warnings) == 1
        assert "doesn't adjust the next day" in plan.warnings[0]

    def test_facts_before_it_still_reflow_into_the_day(self):
        reading = event_at("17:00-19:00", id="e5", summary="Reading", priority=3)

        plan = _plan(
            [_note(1, "17:30"), _note(2, "19:30")],
            [_keep("e5", start_note="n1", end_note="n2"), _keep("s1", start=time_at("22:00"))],
            [event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True), reading],
            now="19:30",
        )

        changes = _by_event(plan)
        assert _span(changes["e5"].after) == (time_at("17:30"), time_at("19:30"))
        assert _span(changes["s1"].after) == (time_at("22:00"), time_at("07:00") + _NEXT_DAY)

    def test_with_a_day_start_last_nights_sleep_is_not_the_days_end(self):
        day = _overnight()

        # Sleeping in pushes into the morning -- an ordinary fact, not a
        # move of the day's end (which would warn about the next day).
        plan = _plan(
            [_note(1, "07:20+1", "up")],
            [_keep("s0", end_note="n1"), _keep("gr", start_note="n1")],
            day,
            now="09:00+1",
            day_start=time_at("07:00+1"),
        )

        assert _by_event(plan)["s0"].after.end == time_at("07:20+1")
        assert plan.warnings == []


class TestNotedSleep:
    """Going to bed later than planned: the day's end is settled before
    anything else is placed, so an activity noted just before the new
    bedtime reflows against it, not against the sleep's old start."""

    def _day(self):
        return [
            event_at("21:00-23:30", id="e1", summary="Reading", priority=3),
            event_at(
                "00:00+1-07:00+1",
                id="s1",
                summary="Sleep",
                priority=0,
                is_end_of_day_sleep=True,
                is_fixed_time=True,
                min_duration=timedelta(hours=7),
            ),
        ]

    def _notes(self):
        return [_note(1, "23:47", "Starting to get ready for bed"), _note(2, "00:01+1", "Finished")]

    def _get_ready(self):
        return EventDecision(action="create", summary="Get ready for bed", start_note="n1", end_note="n2")

    def test_a_later_bedtime_makes_room_for_an_activity_before_it(self):
        plan = _plan(
            self._notes(), [self._get_ready(), _keep("s1", start_note="n2")], self._day(), now="07:00+1"
        )

        changes = _by_event(plan)
        assert _span(changes["s1"].after) == (time_at("00:01+1"), time_at("07:00+1"))
        created = [c for c in plan.changes if c.action == "create"]
        assert [(c.after.summary, _span(c.after)) for c in created] == [
            ("Get ready for bed", (time_at("23:47"), time_at("00:01+1")))
        ]
        assert not any("doesn't adjust the next day" in w for w in plan.warnings)

    def test_an_earlier_bedtime_needs_what_ran_past_it_ended_too(self):
        with pytest.raises(CompactionError, match="'Reading' \\(e1, on schedule"):
            _plan([_note(1, "23:00")], [_keep("s1", start_note="n1")], self._day(), now="07:00+1")

        plan = _plan(
            [_note(1, "23:00")],
            [_keep("s1", start_note="n1"), _keep("e1", end_note="n1")],
            self._day(),
            now="07:00+1",
        )

        changes = _by_event(plan)
        assert _span(changes["s1"].after) == (time_at("23:00"), time_at("07:00+1"))
        assert _span(changes["e1"].after) == (time_at("21:00"), time_at("23:00"))

    def test_running_into_sleep_without_moving_it_is_rejected(self):
        with pytest.raises(CompactionError, match="overlaps 'Sleep' \\(s1, on schedule"):
            _plan(self._notes(), [self._get_ready()], self._day(), now="07:00+1")


class TestFixedTimeEvents:
    def test_moving_an_event_later_reflows_around_a_fixed_time_event_further_on(self):
        day = [
            event_at("09:35-12:30", id="w", summary="Work", priority=3),
            event_at("12:30-13:30", id="l", summary="Lunch", priority=1),
            event_at("13:30-17:00", id="a", summary="Afternoon", priority=3),
            event_at(
                "17:00-18:00",
                id="c",
                summary="Commute",
                priority=2,
                is_fixed_time=True,
                min_duration=timedelta(hours=1),
            ),
            event_at("22:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        ]

        plan = _plan(
            [_note(1, "10:00"), _note(2, "10:41")],
            [
                EventDecision(action="create", summary="Laying in bed", start_note="n1", end_note="n2"),
                _keep("w", start=time_at("11:30"), end=time_at("14:25")),
            ],
            day,
            now="10:45",
        )

        changes = _by_event(plan)
        assert _span(changes["w"].after) == (time_at("11:30"), time_at("14:25"))
        assert _span(changes["a"].after) == (time_at("15:25"), time_at("17:00"))
        assert "c" not in changes
        created = sorted((c.after.summary, _span(c.after)) for c in plan.changes if c.action == "create")
        assert created == [
            ("Afternoon", (time_at("18:00"), time_at("18:55"))),
            ("Laying in bed", (time_at("10:00"), time_at("10:41"))),
        ]

    def test_overlapping_a_fixed_time_event_nobody_moved_says_so(self):
        day = [
            event_at(
                "12:00-13:00", id="c", summary="Call", is_fixed_time=True, min_duration=timedelta(hours=1)
            ),
            event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        ]

        with pytest.raises(CompactionError, match="doesn't fit: it overlaps fixed-time 'Call'"):
            _plan([], [EventDecision(action="create", summary="Gym", start=time_at("11:30"), end=time_at("12:30"))], day)

    def test_an_event_fixed_time_only_through_its_goal_is_fixed_too(self):
        day = [
            event_at("12:00-13:00", id="c", summary="Call", goal_is_fixed_time=True),
            event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        ]

        with pytest.raises(CompactionError, match="doesn't fit: it overlaps fixed-time 'Call'"):
            _plan([], [EventDecision(action="create", summary="Gym", start=time_at("11:30"), end=time_at("12:30"))], day)


class TestTimeline:
    def _salsa(self):
        return _plan(
            [
                _note(1, "18:15", "Leaving for salsa early to prep"),
                _note(2, "19:00", "Learned the cross-body lead"),
                _note(3, "20:10", "Done with dinner"),
            ],
            [
                EventDecision(action="create", summary="Salsa prep", start_note="n1", end=time_at("18:30")),
                _keep("dinner", end_note="n3"),
                _keep("read", start_note="n3"),
            ],
            _evening(),
            now="20:30",
        )

    def test_reports_what_happened_to_each_event(self):
        statuses = {e.summary: e.status for e in self._salsa().timeline.events}

        assert statuses == {
            "Work": "on_schedule",
            "Salsa prep": "new",
            "Google Salsa class": "on_schedule",
            "Dinner": "adjusted",
            "Reading": "adjusted",
        }  # Sleep is ahead and untouched, so not shown

    def test_shows_a_future_event_only_once_the_plan_moves_it(self):
        plan = _plan([], [_keep("dinner", end=time_at("20:30"))], _evening(), now="19:55")

        statuses = {e.event_id: e.status for e in plan.timeline.events}
        assert statuses["read"] == "reflowed"  # pushed later by dinner running long
        assert "s1" not in statuses  # still ahead, and untouched
        assert "s1" not in _by_event(plan)

    def test_reports_what_each_note_did(self):
        notes = {n.id: n for n in self._salsa().timeline.notes}

        assert notes["n1"].anchors == ["start of Salsa prep"]
        assert notes["n2"].annotates == "Google Salsa class"
        assert notes["n3"].anchors == ["end of Dinner", "start of Reading"]

    def test_renders_the_two_lanes_side_by_side(self):
        lines = self._salsa().timeline.text.splitlines()

        def row(prefix):
            return next(line for line in lines if line.startswith(prefix))

        assert "Leaving for salsa early to prep" in row("18:15") and "─ ├ Salsa prep · new" in row("18:15")
        assert row("18:30").endswith("├ Google Salsa class · on schedule")
        assert "● Learned the cross-body lead" in row("19:00")
        assert row("19:00").endswith("│   ↳ added to Google Salsa class")
        assert "─ └ Dinner ends · 10m late (planned 20:00)" in row("20:10")
        assert any("├ Reading · starts 10m late (planned 20:00)" in line for line in lines)
        assert "┄┄ now" in row("20:30")

    def test_marks_the_last_compaction_before_what_came_at_the_same_moment(self):
        plan = _plan([_note(1, "09:10", "email")], [], last_compaction=time_at("09:00"))
        lines = plan.timeline.text.splitlines()

        marker = next(i for i, line in enumerate(lines) if "┄┄ last compaction" in line)
        assert lines[marker].startswith("09:00")
        assert "┌ Email" in lines[marker + 1]

    def test_dates_a_last_compaction_on_an_earlier_day(self):
        plan = _plan(
            [_note(1, "09:10+1", "email")],
            [],
            [event_at("09:00+1-10:00+1", id="e1", summary="Email", priority=2)],
            now="11:30+1",
            last_compaction=time_at("21:00"),
        )

        assert "┄┄ last compaction (Thu 01 Jan)" in plan.timeline.text

    def test_shows_cancelled_and_moved_events(self):
        plan = _plan(
            [],
            [EventDecision(action="cancel", event_id="e2"), _keep("e3", start=time_at("12:30"), end=time_at("13:30"))],
        )

        text = plan.timeline.text
        assert "✕ Report · cancelled (was 10:00–11:00)" in text
        assert "┌ Lunch · moved 30m later (planned 12:00–13:00)" in text

    def test_marks_notes_that_were_not_added_anywhere(self):
        plan = _plan([_note(1, "11:10", "wandered"), _note(2, "09:20", "skip me")], [], ignore_notes=["n2"])

        assert "○ wandered" in plan.timeline.text
        assert "○ skip me" in plan.timeline.text


class TestEventDecision:
    def test_round_trips_through_json(self):
        decision = EventDecision(
            action="keep", event_id="e1", start=time_at("09:05"), end_note="n2", annotate="x"
        )

        assert EventDecision.from_json_dict(decision.to_json_dict()) == decision

    def test_json_omits_unset_fields(self):
        assert EventDecision(action="cancel", event_id="e1").to_json_dict() == {
            "action": "cancel",
            "event_id": "e1",
        }


class TestEventState:
    def test_round_trips_through_json(self):
        state = EventState.from_event(
            event_at(
                "09:00-10:00",
                summary="Email",
                description="d",
                min_duration=timedelta(minutes=30),
                is_fixed_time=True,
                priority=2,
            )
        )

        assert EventState.from_json_dict(state.to_json_dict()) == state

    def test_json_omits_unset_fields(self):
        state = EventState.from_event(event_at("09:00-10:00"))

        assert "description" not in state.to_json_dict()

    def test_to_event_builds_an_event_with_the_given_id(self):
        event = EventState.from_event(event_at("09:00-10:00", min_duration=timedelta(minutes=30))).to_event("x1")

        assert event.id == "x1"
        assert event.min_duration == timedelta(minutes=30)
