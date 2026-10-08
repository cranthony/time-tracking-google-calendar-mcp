import pytest

from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.compaction_schedule import CompactionSchedule, ScheduleHint, normalized_time


def _schedule(sheets=None) -> tuple[CompactionSchedule, FakeSheets]:
    sheets = sheets or FakeSheets()
    return CompactionSchedule.ensure(sheets, "spreadsheet"), sheets


def test_starts_with_none_in_a_tab_of_its_own():
    schedule, sheets = _schedule()

    assert schedule.all() == []
    assert sheets.tags[("sheet-role", calendar_metadata_sheet.COMPACTION_SCHEDULE_SHEET_ROLE)] is not None


def test_sets_them_whole_earliest_first_each_with_an_id():
    schedule, sheets = _schedule()

    saved = schedule.set_hints(
        [
            ScheduleHint(time="18:30", label="Evening compaction"),
            ScheduleHint(time="7:05", label="  Morning compaction "),
            ScheduleHint(time="12:00", label="   "),
        ]
    )

    assert [(h.time, h.label) for h in saved] == [
        ("07:05", "Morning compaction"),
        ("12:00", None),
        ("18:30", "Evening compaction"),
    ]
    assert all(h.id for h in saved)
    assert len({h.id for h in saved}) == 3
    # Read back as saved, by anyone.
    assert CompactionSchedule.ensure(sheets, "spreadsheet").all() == saved


def test_replacing_them_keeps_the_ids_given_and_drops_the_rest():
    schedule, _ = _schedule()
    morning, evening = schedule.set_hints([ScheduleHint(time="07:00"), ScheduleHint(time="19:00")])

    saved = schedule.set_hints([ScheduleHint(id=morning.id, time="07:30", label="Morning")])

    assert saved == [ScheduleHint(id=morning.id, time="07:30", label="Morning")]
    assert schedule.all() == saved
    assert evening.id not in {h.id for h in schedule.all()}


def test_an_empty_list_clears_them():
    schedule, _ = _schedule()
    schedule.set_hints([ScheduleHint(time="07:00")])

    assert schedule.set_hints([]) == []
    assert schedule.all() == []


@pytest.mark.parametrize(
    "hints, message",
    [
        ([ScheduleHint(time="7")], r"'7' isn't a time of day"),
        ([ScheduleHint(time="24:00")], r"'24:00' isn't a time of day"),
        ([ScheduleHint(time="07:60")], r"isn't a time of day"),
        ([ScheduleHint()], r"None isn't a time of day"),
        ([ScheduleHint(time="07:00"), ScheduleHint(time="7:00")], r"07:00 is given twice"),
        ([ScheduleHint(time="07:00", label="x" * 101)], r"longer than 100 characters"),
    ],
)
def test_refuses_what_isnt_a_schedule_writing_nothing(hints, message):
    schedule, _ = _schedule()
    schedule.set_hints([ScheduleHint(time="06:00")])

    with pytest.raises(ValueError, match=message):
        schedule.set_hints(hints)
    assert [h.time for h in schedule.all()] == ["06:00"]


@pytest.mark.parametrize(
    "text, time",
    [("7:30", "07:30"), (" 07:30 ", "07:30"), ("00:00", "00:00"), ("23:59", "23:59"), ("7.30", None), ("", None)],
)
def test_reads_times_of_day(text, time):
    assert normalized_time(text) == time
