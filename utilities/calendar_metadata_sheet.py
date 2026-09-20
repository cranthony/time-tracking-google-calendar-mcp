"""The shared Google Sheet -- one per calendar -- that this app's
calendar-scoped data outside of Calendar's own API lives in: today, a
calendar's event labels (`utilities/event_label_sheet.py`'s
`EventLabelSheet`) and its uncompacted time notes, with room for more
kinds of data later. One spreadsheet, one tab per kind of data.

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

from calendar_clients.google_calendar import CalendarClient
from calendar_clients.google_sheets import SheetsClient

SPREADSHEET_TITLE = "Calendar Metadata"

_SPREADSHEET_ID_METADATA_KEY = "calendar-metadata-spreadsheet-id"

_LEGACY_SPREADSHEET_ID_METADATA_KEY = "event-label-sheet-id"
"""Pre-dates this module: originally recorded a dedicated, single-tab
"Event Labels" spreadsheet, back when that was the only kind of data
this app kept in a Sheet. Still checked as a fallback so an existing
calendar's spreadsheet is adopted in place -- renamed, its one tab
tagged -- into the new multi-tab scheme, instead of creating a second,
redundant spreadsheet. See `ensure_spreadsheet`."""

_SHEET_ROLE_METADATA_KEY = "sheet-role"

_TAB_COLOR = {"red": 0.26, "green": 0.52, "blue": 0.96}
"""A consistent, recognizable color for every tab this app manages --
see the module docstring."""


def ensure_spreadsheet(calendar_client: CalendarClient, sheets_client: SheetsClient) -> tuple[str, bool]:
    """This calendar's metadata spreadsheet id, and whether it was just
    created (as opposed to already existing -- whether already under
    the current key, or adopted from the legacy one). Callers
    provisioning a tab that needs initial data (e.g.
    `EventLabelSheet.ensure`) need to know which: an *adopted* legacy
    spreadsheet's event-labels tab already has real data that must not
    be overwritten."""
    spreadsheet_id = calendar_client.get_calendar_metadata(_SPREADSHEET_ID_METADATA_KEY)
    if spreadsheet_id is not None:
        return spreadsheet_id, False

    legacy_spreadsheet_id = calendar_client.get_calendar_metadata(_LEGACY_SPREADSHEET_ID_METADATA_KEY)
    if legacy_spreadsheet_id is not None:
        sheets_client.rename_spreadsheet(legacy_spreadsheet_id, SPREADSHEET_TITLE)
        calendar_client.set_calendar_metadata(_SPREADSHEET_ID_METADATA_KEY, legacy_spreadsheet_id)
        calendar_client.set_calendar_metadata(_LEGACY_SPREADSHEET_ID_METADATA_KEY, None)
        return legacy_spreadsheet_id, False

    spreadsheet_id = sheets_client.create_spreadsheet(SPREADSHEET_TITLE)
    calendar_client.set_calendar_metadata(_SPREADSHEET_ID_METADATA_KEY, spreadsheet_id)
    return spreadsheet_id, True


def ensure_tab(
    sheets_client: SheetsClient,
    spreadsheet_id: str,
    *,
    role: str,
    title: str,
    reuse_sheet_id: int | None = None,
) -> tuple[int, bool]:
    """The sheetId of `spreadsheet_id`'s tab tagged `role`, and whether
    it was just created/adopted (as opposed to already tagged from a
    previous call).

    If no tab is tagged `role` yet: adopts `reuse_sheet_id` (an
    already-existing tab -- renamed, colored, and tagged in place) when
    given, or else adds and tags a brand-new tab titled `title`.
    `reuse_sheet_id` exists for `EventLabelSheet.ensure` to adopt a
    fresh spreadsheet's default first tab (sheetId 0, titled "Sheet1")
    in place, rather than leaving it as an unused, untagged extra tab
    alongside a second, newly-added one -- and, via `ensure_spreadsheet`'s
    legacy fallback above, that same sheetId 0 is exactly the tab an
    adopted pre-existing spreadsheet's real data already lives on.
    """
    sheet_id = sheets_client.find_sheet_id(spreadsheet_id, _SHEET_ROLE_METADATA_KEY, role)
    if sheet_id is not None:
        return sheet_id, False

    if reuse_sheet_id is not None:
        sheet_id = reuse_sheet_id
        sheets_client.update_sheet_properties(
            spreadsheet_id, sheet_id, title=title, tab_color=_TAB_COLOR
        )
    else:
        sheet_id = sheets_client.add_sheet(spreadsheet_id, title, tab_color=_TAB_COLOR)
    sheets_client.create_sheet_metadata(spreadsheet_id, sheet_id, _SHEET_ROLE_METADATA_KEY, role)
    return sheet_id, True


TIME_NOTES_SHEET_ROLE = "uncompacted-time-notes"
TIME_NOTES_SHEET_TITLE = "Uncompacted Time Notes"
"""No MCP tool or CLI command reads/writes this tab yet -- schema is a
separate, future step (see `config.ensure_time_notes_sheet`). This just
provisions its container, the same one-time bootstrap reasoning as
`EventLabelSheet.ensure` -- see `create_calendar.py`."""
