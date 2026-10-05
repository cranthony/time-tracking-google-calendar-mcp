"""The shared Google Sheet -- one per calendar -- that this app's
calendar-scoped data outside of Calendar's own API lives in: today, a
calendar's goals, its time notes and its compaction journal, with room
for more kinds of data later. One spreadsheet, one tab per kind of data.

Each tab is located by developer metadata (`calendar_clients/
google_sheets.py`'s `SheetsClient.create_sheet_metadata`/`find_sheet_id`)
rather than its title or position, so a user renaming a tab -- or
reordering tabs -- never breaks the app's ability to find the right one
again. The spreadsheet itself is found the same way anything else this
app stores on the calendar is: a key in the calendar's own description,
via `CalendarClient.get_calendar_metadata`/`set_calendar_metadata`.

Every tab this app manages also gets a consistent tab color (see
`_TAB_COLOR`) and a human-readable title, set once at creation -- purely
so a user looking at the spreadsheet can tell at a glance which tabs the
app is using. Neither is what the app itself relies on to find a tab
again (that's the metadata tag); a user is free to rename a tab (or the
spreadsheet) afterwards without breaking anything.
"""

from __future__ import annotations

from collections.abc import Callable

from calendar_clients.google_calendar import CalendarClient
from calendar_clients.google_sheets import SheetsClient

SPREADSHEET_TITLE = "Calendar Metadata"

_SPREADSHEET_ID_METADATA_KEY = "calendar-metadata-spreadsheet-id"

_SHEET_ROLE_METADATA_KEY = "sheet-role"

_TAB_COLOR = {"red": 0.26, "green": 0.52, "blue": 0.96}
"""A consistent, recognizable color for every tab this app manages --
see the module docstring."""


def ensure_spreadsheet(calendar_client: CalendarClient, sheets_client: SheetsClient) -> tuple[str, bool]:
    """This calendar's metadata spreadsheet id, and whether it was just
    created (as opposed to already existing). A caller creating
    a tab with initial data (the goals tab, see `utilities/goals.py`)
    can then put it in a new spreadsheet's default first tab instead of
    leaving that one unused (see `create_tab`)."""
    spreadsheet_id = calendar_client.get_calendar_metadata(_SPREADSHEET_ID_METADATA_KEY)
    if spreadsheet_id is not None:
        return spreadsheet_id, False

    spreadsheet_id = sheets_client.create_spreadsheet(SPREADSHEET_TITLE)
    calendar_client.set_calendar_metadata(_SPREADSHEET_ID_METADATA_KEY, spreadsheet_id)
    return spreadsheet_id, True


def ensure_tab(
    sheets_client: SheetsClient,
    spreadsheet_id: str,
    *,
    role: str,
    title: str,
) -> tuple[int, bool]:
    """The sheetId of `spreadsheet_id`'s tab tagged `role`, and whether
    it was just created (as opposed to already tagged from a previous
    call). If no tab is tagged `role` yet, adds and tags a brand-new tab
    titled `title`."""
    sheet_id = sheets_client.find_sheet_id(spreadsheet_id, _SHEET_ROLE_METADATA_KEY, role)
    if sheet_id is not None:
        return sheet_id, False

    sheet_id = sheets_client.add_sheet(spreadsheet_id, title, tab_color=_TAB_COLOR)
    sheets_client.create_sheet_metadata(spreadsheet_id, sheet_id, _SHEET_ROLE_METADATA_KEY, role)
    return sheet_id, True


def find_tab(sheets_client: SheetsClient, spreadsheet_id: str, role: str) -> int | None:
    """The sheetId of `spreadsheet_id`'s tab tagged `role`, or `None` if
    there isn't one."""
    return sheets_client.find_sheet_id(spreadsheet_id, _SHEET_ROLE_METADATA_KEY, role)


def create_tab(
    sheets_client: SheetsClient,
    spreadsheet_id: str,
    *,
    role: str,
    title: str,
    populate: Callable[[int], None],
    reuse_sheet_id: int | None = None,
) -> int:
    """Create (or adopt `reuse_sheet_id`, an already-existing tab such as
    a new spreadsheet's default first one, renamed and colored in place)
    a tab for `role`, call `populate(sheet_id)` to write its contents, and only then
    tag it -- so a failure partway leaves no tagged-but-empty tab for the
    next `find_tab` to mistake for a finished one. For a tab whose first
    contents matter, e.g. the goals tab migrated from event labels (see
    `utilities/goals.py`); `ensure_tab` tags first, which is fine for a
    tab that starts empty."""
    if reuse_sheet_id is not None:
        sheet_id = reuse_sheet_id
        sheets_client.update_sheet_properties(spreadsheet_id, sheet_id, title=title, tab_color=_TAB_COLOR)
    else:
        sheet_id = sheets_client.add_sheet(spreadsheet_id, title, tab_color=_TAB_COLOR)
    populate(sheet_id)
    sheets_client.create_sheet_metadata(spreadsheet_id, sheet_id, _SHEET_ROLE_METADATA_KEY, role)
    return sheet_id


MIN_TAB_ROWS = 1000
"""The fewest rows (data and blank, header included) a tab is left with
after garbage collection deletes some -- writing past the end of a tab's
grid fails, and 1000 is what Sheets gives a new tab."""

TIME_NOTES_SHEET_ROLE = "uncompacted-time-notes"
TIME_NOTES_SHEET_TITLE = "Noted Times"
"""See `utilities/noted_time_sheet.py`'s `NotedTimeSheet` for the tab
this identifies -- a calendar's uncompacted time notes."""

GOALS_SHEET_ROLE = "goals"
GOALS_SHEET_TITLE = "Goals"
"""See `utilities/goal_sheet.py`'s `GoalSheet` for the tab this
identifies -- a calendar's goals."""

COMPACTIONS_SHEET_ROLE = "compactions"
COMPACTIONS_SHEET_TITLE = "Compactions"
"""See `utilities/compaction_journal.py`'s `CompactionJournal` for the tab
this identifies -- the write-ahead journal of note compactions."""

TRAITS_SHEET_ROLE = "traits"
TRAITS_SHEET_TITLE = "Traits"
"""See `utilities/traits.py`'s `Traits` for the tab this identifies -- the
traits goals can be rated by."""

GOAL_DETAILS_SHEET_ROLE = "goal-details"
GOAL_DETAILS_SHEET_TITLE = "Goal Details"
"""See `utilities/goal_details.py`'s `GoalDetails` for the tab this
identifies -- goals' descriptions."""
