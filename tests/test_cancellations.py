from datetime import datetime, timedelta, timezone

from calendar_clients.google_calendar import Event
from tests.fake_labels import FakeLabelCalendar
from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.action_groups import ActionGroup
from utilities.actions import Action, Actions
from utilities.cancellations import KEEP_DAYS, Cancellations
from utilities.facts import Facts
from utilities.habits import Habit, Habits
from utilities.people import People, Person
from utilities.traits import Trait, Traits

UTC = timezone.utc
_AT = datetime(2026, 9, 2, 10, tzinfo=UTC)


def _store(parts, people=()):
    sheets = FakeSheets()
    actions = Actions.ensure(FakeLabelCalendar(), sheets, "s")
    actions._sheet.write([
        Action(id="gym", name="Work out", status="active", label_id="label-gym"),
        Action(id="eat", name="Eat", status="active", label_id="label-eat"),
    ])
    store = People.ensure(sheets, "s")
    store._write_people(list(people), [])
    traits = Traits.ensure(sheets, "s")
    traits._write([Trait(id="reliable", name="Reliable", status="active", parts=parts)])
    return Cancellations.ensure(sheets, "s", store, traits, actions), sheets


def _event(event_id="c1", action_ids=("gym",), with_ids=(), for_ids=()):
    return Event(
        id=event_id, summary="Gym", start=_AT, end=_AT + timedelta(hours=1), action_ids=list(action_ids),
        facts=Facts(with_ids=list(with_ids) or None, for_ids=list(for_ids) or None),
    )


_SAM = Person(id="sam", name="Sam", status="active")


def test_a_with_part_counts_the_user_and_everyone_it_was_planned_with():
    store, sheets = _store([{"kind": "follow_through"}], [_SAM])

    matches = store.matches(_event(with_ids=["sam"]))

    assert [(m.person_id, m.engagement, m.parts, m.trait_names) for m in matches] == [
        ("self", "with", ["reliable/follow_through"], ["Reliable"]),
        ("sam", "with", ["reliable/follow_through"], ["Reliable"]),
    ]
    assert sheets.tags[("sheet-role", calendar_metadata_sheet.CANCELLATIONS_SHEET_ROLE)] is not None


def test_a_for_part_counts_only_who_it_was_for():
    store, _ = _store([{"kind": "follow_through", "engagement_type": "for"}], [_SAM])

    assert [m.person_id for m in store.matches(_event(for_ids=["sam"]))] == ["sam"]
    assert store.matches(_event(with_ids=["sam"])) == []


def test_a_part_with_an_action_counts_only_events_of_it():
    store, _ = _store([{"kind": "follow_through", "action": "eat"}])

    assert store.matches(_event(action_ids=["gym"])) == []
    assert [m.person_id for m in store.matches(_event(action_ids=["eat"]))] == ["self"]


def test_someone_no_longer_active_isnt_counted():
    store, _ = _store([{"kind": "follow_through"}], [Person(id="old", name="Old", status="archived")])

    assert [m.person_id for m in store.matches(_event(with_ids=["old"]))] == ["self"]


def test_only_a_matching_cancellation_is_recorded():
    store, _ = _store([{"kind": "continuity"}])

    assert store.record(_event(), "delete_event", at=_AT) == []
    assert store.all() == []


def test_recording_writes_a_row_per_person_and_again_harmlessly():
    store, _ = _store([{"kind": "follow_through"}], [_SAM])
    event = _event(with_ids=["sam"])

    store.record(event, "compaction abc", at=_AT)
    store.record(event, "compaction abc", at=_AT)

    rows = store.all()
    assert [(r.id, r.source, r.parts) for r in rows] == [
        ("c1/sam/with", "compaction abc", ["reliable/follow_through"]),
        ("c1/self/with", "compaction abc", ["reliable/follow_through"]),
    ]
    sam = next(r for r in rows if r.person_id == "sam").to_event()
    assert (sam.start, sam.end, sam.action_ids, sam.facts.with_ids) == (_AT, _AT + timedelta(hours=1), ["gym"], ["sam"])


def test_rows_older_than_it_keeps_are_dropped_as_new_ones_are_written():
    store, _ = _store([{"kind": "follow_through"}])
    old = _event("old")
    old.start -= timedelta(days=KEEP_DAYS + 1)
    old.end -= timedelta(days=KEEP_DAYS + 1)
    store.record(old, "delete_event", at=_AT - timedelta(days=1))

    store.record(_event("new"), "delete_event", at=_AT)

    assert [r.event_id for r in store.all()] == ["new"]


def test_by_person_gives_each_persons_cancellations_newest_first():
    store, _ = _store([{"kind": "follow_through"}], [_SAM])
    earlier = _event("early", with_ids=["sam"])
    earlier.start -= timedelta(days=1)
    earlier.end -= timedelta(days=1)
    store.record(earlier, "delete_event", at=_AT)
    store.record(_event("late", with_ids=["sam"]), "compaction abc", at=_AT)

    by_person = store.by_person()

    assert [c.event_id for c in by_person["sam"]] == ["late", "early"]
    late = by_person["sam"][0]
    assert (late.start, late.engagement, late.source, late.cancelled_at) == (_AT, "with", "compaction abc", _AT)
    assert [c.event_id for c in by_person["self"]] == ["late", "early"]


def _with_habits(parts, habits, people=()):
    """`_store`, with `habits` in a Habits tab, and the actions in groups."""
    store, sheets = _store(parts, people)
    actions = store._actions
    actions._group_sheet.write([ActionGroup(id="body", name="Body")])
    actions._sheet.write([
        Action(id="gym", name="Work out", status="active", label_id="label-gym", group_id="body"),
        Action(id="eat", name="Eat", status="active", label_id="label-eat"),
    ])
    habit_store = Habits.ensure(sheets, "s")
    habit_store._sheet.write(list(habits))
    store._habits = habit_store
    return store, sheets


def test_a_habit_counts_a_cancel_in_its_scope_under_its_id():
    store, _ = _with_habits(
        [{"kind": "follow_through"}],
        [
            Habit(id="fit", name="Fitness", action_id="body", status="active"),
            Habit(id="food", name="Eating well", action_id="eat", status="active"),
            Habit(id="old", name="Old", action_id="gym", status="archived"),
        ],
    )

    matches = store.matches(_event(action_ids=["gym"]))

    assert [(m.person_id, m.person_name, m.engagement, m.parts) for m in matches] == [
        ("self", "Me", "with", ["reliable/follow_through"]),
        ("habit:fit", "the Fitness habit", "with", ["reliable/follow_through"]),
    ]


def test_a_habits_for_parts_and_parts_of_other_actions_dont_count():
    store, _ = _with_habits(
        [{"kind": "follow_through", "engagement_type": "for"}, {"kind": "follow_through", "action": "eat"}],
        [Habit(id="fit", name="Fitness", action_id="body", status="active")],
    )

    assert [m.person_id for m in store.matches(_event(action_ids=["gym"]))] == []


def test_a_habits_cancellations_are_recorded_and_listed_under_its_id():
    store, _ = _with_habits(
        [{"kind": "follow_through"}], [Habit(id="fit", name="Fitness", action_id="body", status="active")]
    )

    store.record(_event(), "delete_event", at=_AT)

    (row,) = store.by_person()["habit:fit"]
    assert (row.event_id, row.engagement, row.parts) == ("c1", "with", ["reliable/follow_through"])
    habit_row = next(r for r in store.all() if r.person_id == "habit:fit")
    # As follow-through reads it: the user's, with no one in its facts.
    assert habit_row.to_event().facts == Facts()
