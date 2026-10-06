from datetime import date, datetime, timedelta, timezone

import pytest

from calendar_clients.google_calendar import Event
from tests.fake_labels import FakeLabelCalendar
from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet, trait_rollup
from utilities.actions import Actions
from utilities.cancellations import Cancellations
from utilities.facts import Facts
from utilities.people import People, Person
from utilities.trait_rollup import TraitRollup
from utilities.traits import Trait, Traits

UTC = timezone.utc


def _today() -> date:
    """Today in the calendar's time zone (UTC, here)."""
    return datetime.now(UTC).date()


class _Calendar:
    """The listing (cancelled events included) and time zone the rollup reads."""

    def __init__(self, events):
        self.events = events
        self.listed = []

    def get_time_zone(self):
        return UTC

    def list_events(self, time_min, time_max, *, show_deleted=False):
        self.listed.append((time_min, time_max, show_deleted))
        return [
            e for e in self.events
            if e.end > time_min and e.start < time_max and (show_deleted or e.status != "cancelled")
        ]


def _event(event_id, at, with_ids=(), status=None):
    return Event(
        id=event_id, summary="x", start=at, end=at + timedelta(hours=1), status=status,
        facts=Facts(with_ids=list(with_ids)) if with_ids else None,
    )


def _rollup(events):
    sheets = FakeSheets()
    actions = Actions.ensure(FakeLabelCalendar(), sheets, "s")
    people = People.ensure(sheets, "s")
    people._write_people([Person(id="sam", name="Sam", status="active"), Person(id="old", name="Old", status="archived")],
                         [])
    traits = Traits.ensure(sheets, "s")
    traits._write([Trait(id="reliable", name="Reliable", status="active", parts=[{"kind": "continuity"}, {"kind": "follow_through"}])])
    calendar = _Calendar(events)
    cancellations = Cancellations.ensure(sheets, "s", people, traits, actions)
    rollup = TraitRollup.ensure(calendar, actions, people, traits, sheets, "s", cancellations)
    rollup.cancellations = cancellations  # For the tests to record with.
    return rollup, calendar, sheets


_D1, _D2 = date(2026, 9, 1), date(2026, 9, 2)


def _at(day, hour):
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def test_rolls_up_each_active_person_for_each_finished_day():
    events = [
        _event("e1", _at(_D1, 10), with_ids=["sam"]),
        _event("c1", _at(_D2, 10), with_ids=["sam"], status="cancelled"),
        _event("e2", _at(_D2, 12)),
    ]
    rollup, calendar, sheets = _rollup(events)
    rollup.cancellations.record(events[1], "compaction c")

    rows = rollup.roll_up([_D2, _D1, _today()])  # today isn't over: left out

    assert [(r.day, r.person_id) for r in rows] == [
        ("2026-09-01", "self"), ("2026-09-01", "sam"), ("2026-09-02", "self"), ("2026-09-02", "sam"),
    ]
    by_id = {r.id: r for r in rows}
    # Sam: the 1st has a last event but none next (50); the 2nd also lost
    # a cancelled plan (follow-through 75).
    assert by_id["2026-09-01/sam"].scores == {"reliable": 75}
    assert by_id["2026-09-02/sam"].scores == {"reliable": 62}
    assert by_id["2026-09-02/sam"].parts["reliable"]["follow_through"]["said"].startswith("1 cancelled")
    assert calendar.listed[0][2] is False  # cancellations come from their own tab
    assert sheets.tags[("sheet-role", calendar_metadata_sheet.TRAIT_SCORES_SHEET_ROLE)] is not None
    assert rollup.get("sam") == [by_id["2026-09-01/sam"], by_id["2026-09-02/sam"]]
    assert rollup.get(start=_D2, end=_D2) == [by_id["2026-09-02/sam"], by_id["2026-09-02/self"]]


def test_rolling_a_day_up_again_replaces_its_rows():
    rollup, calendar, _ = _rollup([_event("e1", _at(_D1, 10), with_ids=["sam"])])
    rollup.roll_up([_D1, _D2])
    calendar.events = []

    rollup.roll_up([_D1])

    assert len(rollup.get()) == 4
    assert {r.day: r.scores for r in rollup.get("sam")} == {"2026-09-01": {"reliable": 50}, "2026-09-02": {"reliable": 75}}


def test_drops_rows_older_than_it_keeps(monkeypatch):
    rollup, _, _ = _rollup([])
    rollup.roll_up([_D1])
    monkeypatch.setattr(trait_rollup, "KEEP_DAYS", 1)

    rollup.roll_up([_today() - timedelta(days=1)])

    assert {r.day for r in rollup.get()} == {(_today() - timedelta(days=1)).isoformat()}


@pytest.mark.parametrize(
    "start, end, days",
    [
        (_at(_D1, 9), _at(_D2, 9), [_D1]),
        (_at(_D1, 9), datetime(2026, 9, 3, 0, tzinfo=UTC), [_D1, _D2]),
        (_at(_D1, 9), _at(_D1, 23), []),
    ],
)
def test_the_days_a_span_finished(start, end, days):
    rollup, _, _ = _rollup([])

    assert rollup.days_between(start, end) == days


def test_a_cancelled_event_nobody_recorded_doesnt_count_against_follow_through():
    # A plan changed by hand, or a deleted series: cancelled on the
    # calendar, but not a cancellation the user made.
    rollup, _, _ = _rollup([_event("c1", _at(_D2, 10), with_ids=["sam"], status="cancelled")])

    (sam,) = [r for r in rollup.roll_up([_D2]) if r.person_id == "sam"]

    assert sam.parts["reliable"]["follow_through"]["said"].startswith("Nothing cancelled or kept")
