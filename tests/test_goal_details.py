from datetime import date

import pytest

from tests.fake_sheets import FakeSheets
from utilities import goal_details
from utilities.goal_details import GoalDetails, add_to_section, section


def _details() -> tuple[GoalDetails, FakeSheets]:
    sheets = FakeSheets()
    return GoalDetails.ensure(sheets, "s"), sheets


class TestGoalDetails:
    def test_a_goal_without_a_description_has_none(self):
        details, _ = _details()

        assert details.get("g1") is None

    def test_sets_replaces_and_removes_a_description(self):
        details, _ = _details()
        details.set("g1", "First")
        details.set("g2", "Second")

        details.set("g1", "Changed")
        assert (details.get("g1"), details.get("g2")) == ("Changed", "Second")

        details.set("g1", "")
        assert details.get("g1") is None
        details.set("g3", "Third")  # Into the row g1 left blank.
        assert details.get("g3") == "Third"
        assert details.get("g2") == "Second"

    def test_a_long_description_continues_across_cells(self, monkeypatch):
        monkeypatch.setattr(goal_details, "MAX_CELL_CHARS", 4)
        details, sheets = _details()

        details.set("g1", "abcdefghij")
        assert details.get("g1") == "abcdefghij"
        assert sheets.cell(1, "D2") == "ij"

        details.set("g1", "short")
        assert details.get("g1") == "short"

    def test_refuses_a_description_too_long_to_keep(self, monkeypatch):
        monkeypatch.setattr(goal_details, "MAX_DESCRIPTION_CHARS", 5)
        details, _ = _details()

        with pytest.raises(ValueError, match="at most 5 characters"):
            details.set("g1", "toolong")


_DESCRIPTION = """Old friend.

## What matters to them

- 2026-09-01: loves jazz

## Other

More."""


class TestSections:
    def test_reads_a_section_up_to_the_next_heading(self):
        assert section(_DESCRIPTION) == "- 2026-09-01: loves jazz"
        assert section("Nothing here") is None
        assert section(None) is None

    def test_adds_dated_bullets_to_the_end_of_the_section(self):
        updated = add_to_section(_DESCRIPTION, ["new job in November", "loves jazz"], date(2026, 10, 5))

        assert updated == """Old friend.

## What matters to them

- 2026-09-01: loves jazz
- 2026-10-05: new job in November

## Other

More.
"""

    def test_adds_the_section_if_theres_none(self):
        assert add_to_section("Old friend.", ["likes tea"], date(2026, 10, 5)) == (
            "Old friend.\n\n## What matters to them\n\n- 2026-10-05: likes tea\n"
        )
        assert add_to_section(None, ["likes tea"], date(2026, 10, 5)) == (
            "## What matters to them\n\n- 2026-10-05: likes tea\n"
        )

    def test_adding_whats_there_already_changes_nothing(self):
        assert add_to_section(_DESCRIPTION, ["loves jazz"], date(2026, 10, 5)) == _DESCRIPTION
