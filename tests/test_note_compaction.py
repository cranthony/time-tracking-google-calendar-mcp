from datetime import timedelta

import pytest

from tests.event_time_helpers import event_at, time_at
from utilities.note_compaction import (
    CompactionError,
    EventState,
    NeedsClarification,
    NoteDisposition,
    NoteEffect,
    PlanNote,
    plan_compaction,
)


def _day():
    return [
        event_at("09:00-10:00", id="e1", summary="Email", priority=2),
        event_at("10:00-11:00", id="e2", summary="Report", priority=2),
        event_at("12:00-13:00", id="e3", summary="Lunch", priority=1),
        event_at("14:00-15:00", id="e4", summary="Review", priority=3),
        event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
    ]


def _note(number: int, at: str, description: str | None = None) -> PlanNote:
    return PlanNote(id=f"n{number}", timestamp=time_at(at), description=description)


def _effect(kind: str, **fields) -> NoteEffect:
    return NoteEffect(kind=kind, **fields)


def _disposition(number: int, *effects: NoteEffect) -> NoteDisposition:
    return NoteDisposition(note_id=f"n{number}", effects=list(effects))


def _by_event(plan):
    return {c.event_id: c for c in plan.changes if c.event_id}


def _span(state: EventState) -> tuple:
    return state.start, state.end


class TestBasicPlanning:
    def test_a_start_note_runs_until_the_next_boundary_and_the_last_runs_to_now(self):
        plan = plan_compaction(
            [_note(2, "09:05"), _note(3, "10:20")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(3, _effect("ends", event_id="e1"), _effect("starts", event_id="e2")),
            ],
            _day(),
            time_at("11:30"),
        )

        changes = _by_event(plan)
        assert _span(changes["e1"].after) == (time_at("09:05"), time_at("10:20"))
        # Still in progress: runs to now, since that's later than its planned end.
        assert _span(changes["e2"].after) == (time_at("10:20"), time_at("11:30"))
        assert changes["e1"].after.is_fixed_time is True
        assert changes["e1"].after.min_duration_minutes == 75
        # The future is untouched.
        assert "e3" not in changes and "e4" not in changes

    def test_the_in_progress_activity_keeps_its_planned_end_when_that_is_later(self):
        plan = plan_compaction(
            [_note(2, "09:05")],
            [_disposition(2, _effect("starts", event_id="e1"))],
            _day(),
            time_at("09:30"),
        )

        assert _span(_by_event(plan)["e1"].after) == (time_at("09:05"), time_at("10:00"))

    def test_an_in_progress_activity_is_capped_at_bedtime_with_a_warning(self):
        plan = plan_compaction(
            [_note(2, "10:20")],
            [_disposition(2, _effect("starts", event_id="e2"))],
            _day(),
            time_at("12:00+1"),  # a past day being compacted the next afternoon
        )

        assert _span(_by_event(plan)["e2"].after) == (time_at("10:20"), time_at("20:00"))
        assert any("runs until bedtime" in w for w in plan.warnings)

    def test_an_in_progress_activity_earlier_than_bedtime_has_no_warning(self):
        plan = plan_compaction(
            [_note(2, "10:20")],
            [_disposition(2, _effect("starts", event_id="e2"))],
            _day(),
            time_at("11:30"),
        )

        assert not any("bedtime" in w for w in plan.warnings)

    def test_an_explicit_end_leaves_the_following_gap_free(self):
        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "09:30"), _note(4, "10:15")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(3, _effect("ends", event_id="e1")),
                _disposition(4, _effect("starts", event_id="e2")),
            ],
            _day(),
            time_at("10:30"),
        )

        changes = _by_event(plan)
        assert _span(changes["e1"].after) == (time_at("09:00"), time_at("09:30"))
        assert _span(changes["e2"].after) == (time_at("10:15"), time_at("11:00"))

    def test_an_end_only_note_pulls_its_start_back_to_the_previous_boundary(self):
        # e2 has no start note: its start extends back to the previous
        # boundary (the note that ended e1) so no gap is left before it.
        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "09:30"), _note(4, "10:15")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(3, _effect("ends", event_id="e1")),
                _disposition(4, _effect("ends", event_id="e2")),
            ],
            _day(),
            time_at("10:30"),
        )

        changes = _by_event(plan)
        assert _span(changes["e1"].after) == (time_at("09:00"), time_at("09:30"))
        assert _span(changes["e2"].after) == (time_at("09:30"), time_at("10:15"))

    def test_an_end_only_note_with_no_earlier_note_keeps_the_planned_start(self):
        plan = plan_compaction(
            [_note(2, "09:40")],
            [_disposition(2, _effect("ends", event_id="e1"))],
            _day(),
            time_at("10:00"),
        )

        assert _span(_by_event(plan)["e1"].after) == (time_at("09:00"), time_at("09:40"))
        assert any("keeps its planned start" in w for w in plan.warnings)

    def test_an_unplanned_activity_becomes_a_new_event_and_pushes_the_rest_later(self):
        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "09:50"), _note(4, "10:30")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(
                    3,
                    _effect("ends", event_id="e1"),
                    _effect("starts_unplanned", summary="Coffee chat", event_label_id="lab"),
                ),
                _disposition(4, _effect("ends", started_by_note="n3")),
            ],
            _day(),
            time_at("10:45"),
        )

        created = [c for c in plan.changes if c.action == "create"]
        assert len(created) == 1
        assert created[0].after.summary == "Coffee chat"
        assert _span(created[0].after) == (time_at("09:50"), time_at("10:30"))
        assert created[0].after.event_label_id == "lab"
        assert created[0].event_id is None
        # The report was planned for 10:00 but the coffee chat runs to
        # 10:30, so it's pushed later.
        assert _by_event(plan)["e2"].after.start == time_at("10:30")

    def test_unplanned_summaries_and_events_are_not_mutated(self):
        day = _day()
        before = [(e.start, e.end, e.summary, e.status) for e in day]

        plan_compaction(
            [_note(2, "09:05")],
            [_disposition(2, _effect("starts", event_id="e1"))],
            day,
            time_at("09:30"),
        )

        assert [(e.start, e.end, e.summary, e.status) for e in day] == before


class TestMerging:
    def test_an_end_note_colliding_with_the_previous_start_merges_into_one_event(self):
        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "09:30")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(3, _effect("ends", event_id="e2")),
            ],
            _day(),
            time_at("09:45"),
        )

        changes = _by_event(plan)
        assert changes["e1"].after.summary == "Email and Report"
        assert _span(changes["e1"].after) == (time_at("09:00"), time_at("09:30"))
        assert changes["e2"].action == "cancel"
        assert "merged" in changes["e2"].reason
        assert any("merged into one event" in w for w in plan.warnings)

    def test_an_unplanned_activity_merged_with_a_planned_one_keeps_the_planned_id(self):
        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "09:30")],
            [
                _disposition(2, _effect("starts_unplanned", summary="Support call")),
                _disposition(3, _effect("ends", event_id="e1")),
            ],
            _day(),
            time_at("09:45"),
        )

        changes = _by_event(plan)
        assert changes["e1"].after.summary == "Email and Support call"
        assert not [c for c in plan.changes if c.action == "create"]


class TestMarkers:
    def test_marker_text_is_appended_to_the_event_it_falls_inside(self):
        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "09:20", "tried the new build, flaky"), _note(4, "09:50")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(3, _effect("marker")),
                _disposition(4, _effect("ends", event_id="e1")),
            ],
            _day(),
            time_at("10:00"),
        )

        assert _by_event(plan)["e1"].after.description == "Notes:\n- 09:20 tried the new build, flaky"

    def test_marker_text_is_added_below_an_existing_description(self):
        day = _day()
        day[0].description = "Weekly inbox zero"

        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "09:20", "note one"), _note(4, "09:25", "note two")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(3, _effect("marker")),
                _disposition(4, _effect("marker")),
            ],
            day,
            time_at("10:00"),
        )

        assert _by_event(plan)["e1"].after.description == (
            "Weekly inbox zero\n\nNotes:\n- 09:20 note one\n- 09:25 note two"
        )

    def test_a_marker_before_every_activity_is_reported_not_lost_silently(self):
        plan = plan_compaction(
            [_note(2, "08:00", "woke up"), _note(3, "09:00")],
            [_disposition(2, _effect("marker")), _disposition(3, _effect("starts", event_id="e1"))],
            _day(),
            time_at("09:30"),
        )

        assert any("woke up" in w for w in plan.warnings)


class TestUnmappedEvents:
    def test_a_planned_event_inside_the_noted_span_that_no_note_accounts_for_is_cancelled(self):
        day = _day() + [event_at("10:30-11:00", id="e5", summary="Standup", priority=2)]
        day.sort(key=lambda e: e.start)
        # Make room in the input: e2 (10:00-11:00) overlaps e5, so shrink it.
        day[[e.id for e in day].index("e2")].end = time_at("10:30")

        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "10:00"), _note(4, "12:00")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(3, _effect("starts", event_id="e2")),
                _disposition(4, _effect("ends", event_id="e2"), _effect("starts", event_id="e3")),
            ],
            day,
            time_at("12:30"),
        )

        assert _by_event(plan)["e5"].action == "cancel"
        assert "no note accounts" in _by_event(plan)["e5"].reason

    def test_a_past_event_outside_the_noted_span_is_left_alone_with_a_warning(self):
        plan = plan_compaction(
            [_note(2, "10:00")],
            [_disposition(2, _effect("starts", event_id="e2"))],
            _day(),
            time_at("10:30"),
        )

        assert "e1" not in _by_event(plan)
        assert any("left alone" in w and "Email" in w for w in plan.warnings)

    def test_the_end_of_day_sleep_event_is_never_cancelled_for_lack_of_a_note(self):
        day = _day()
        day[-1].start = time_at("11:30")  # a sleep block inside the noted span, in the past
        day[-1].end = time_at("11:45")

        plan = plan_compaction(
            [_note(2, "09:00"), _note(3, "12:00")],
            [
                _disposition(2, _effect("starts", event_id="e1")),
                _disposition(3, _effect("ends", event_id="e1"), _effect("starts", event_id="e3")),
            ],
            day,
            time_at("12:30"),
        )

        sleep = _by_event(plan).get("s1")
        assert sleep is None or "no note accounts" not in sleep.reason


class TestValidation:
    def _plan(self, notes, dispositions, now="11:00"):
        return plan_compaction(notes, dispositions, _day(), time_at(now))

    def test_every_note_needs_a_disposition(self):
        with pytest.raises(CompactionError, match="no disposition for note.*n3"):
            self._plan(
                [_note(2, "09:00"), _note(3, "09:30")],
                [_disposition(2, _effect("starts", event_id="e1"))],
            )

    def test_an_unknown_note_id_is_rejected_with_the_valid_ones(self):
        with pytest.raises(CompactionError, match="unknown note 'n9'.*n2"):
            self._plan(
                [_note(2, "09:00")],
                [_disposition(2, _effect("ignore")), _disposition(9, _effect("ignore"))],
            )

    def test_an_unknown_event_id_is_rejected_with_the_valid_ones(self):
        with pytest.raises(CompactionError, match=r"'nope'.*valid event ids: e1, e2, e3, e4, s1"):
            self._plan([_note(2, "09:00")], [_disposition(2, _effect("starts", event_id="nope"))])

    def test_a_note_cannot_have_a_marker_and_another_effect(self):
        with pytest.raises(CompactionError, match="can't be combined"):
            self._plan(
                [_note(2, "09:00")],
                [_disposition(2, _effect("marker"), _effect("starts", event_id="e1"))],
            )

    def test_an_event_cannot_be_started_twice(self):
        with pytest.raises(CompactionError, match="already started by note n2"):
            self._plan(
                [_note(2, "09:00"), _note(3, "09:30")],
                [
                    _disposition(2, _effect("starts", event_id="e1")),
                    _disposition(3, _effect("starts", event_id="e1")),
                ],
            )

    def test_ends_needs_exactly_one_target(self):
        with pytest.raises(CompactionError, match="exactly one of event_id"):
            self._plan([_note(2, "09:00")], [_disposition(2, _effect("ends"))])

    def test_ends_of_an_unplanned_activity_must_reference_its_starting_note(self):
        with pytest.raises(CompactionError, match="started_by_note 'n7'"):
            self._plan(
                [_note(2, "09:00")], [_disposition(2, _effect("ends", started_by_note="n7"))]
            )

    def test_starts_unplanned_needs_a_summary(self):
        with pytest.raises(CompactionError, match="needs a summary"):
            self._plan([_note(2, "09:00")], [_disposition(2, _effect("starts_unplanned"))])

    def test_notes_after_now_are_rejected(self):
        with pytest.raises(CompactionError, match="after now"):
            self._plan(
                [_note(2, "12:00")], [_disposition(2, _effect("starts", event_id="e3"))], now="11:00"
            )

    def test_two_boundaries_at_the_same_time_are_rejected_not_given_zero_length(self):
        with pytest.raises(CompactionError, match="isn't a positive length"):
            self._plan(
                [_note(2, "09:00"), _note(3, "09:00")],
                [
                    _disposition(2, _effect("starts", event_id="e1")),
                    _disposition(3, _effect("starts", event_id="e2")),
                ],
            )

    def test_every_problem_is_reported_at_once(self):
        with pytest.raises(CompactionError) as excinfo:
            self._plan(
                [_note(2, "09:00"), _note(3, "09:30")],
                [_disposition(2, _effect("starts", event_id="nope"))],
            )

        assert "nope" in str(excinfo.value)
        assert "no disposition for note" in str(excinfo.value)

    def test_ambiguous_notes_come_back_as_questions(self):
        with pytest.raises(NeedsClarification) as excinfo:
            self._plan(
                [_note(2, "09:00", "lunch??")],
                [_disposition(2, _effect("ambiguous", question="Did lunch start or end here?"))],
            )

        assert len(excinfo.value.questions) == 1
        assert "n2" in excinfo.value.questions[0]
        assert "Did lunch start or end here?" in excinfo.value.questions[0]

    def test_a_note_that_ends_something_with_no_start_evidence_is_rejected(self):
        day = _day()
        day[0].start = time_at("10:00")  # planned start is after the note
        day[0].end = time_at("10:30")
        day[1].start = time_at("10:30")

        with pytest.raises(CompactionError, match="nothing says when it began"):
            plan_compaction(
                [_note(2, "09:30")],
                [_disposition(2, _effect("ends", event_id="e1"))],
                day,
                time_at("11:00"),
            )

    def test_no_boundary_notes_means_nothing_to_do(self):
        plan = self._plan([_note(2, "09:00", "hmm")], [_disposition(2, _effect("marker"))])

        assert plan.changes == []


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
