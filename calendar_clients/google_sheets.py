from __future__ import annotations

from pathlib import Path

from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials


class SheetsClient:
    """Wraps the Google Sheets API behind a small, mockable interface --
    the thin layer `utilities/event_label_sheet.py` builds its
    event-label-sheet logic on top of, the same way `CalendarClient` is a
    thin layer under `utilities/reallocating_calendar.py`. Knows nothing
    about event labels; just spreadsheets, rows, and columns.

    This only talks to the Sheets API, never the Drive API directly --
    there's no need for a spreadsheet's id to be looked up by searching
    Drive; `utilities/event_label_sheet.py` gets it from
    `CalendarClient.get_calendar_metadata`/`set_calendar_metadata`
    instead. `drive.file` is still the OAuth scope this relies on,
    though (see `calendar_clients/google_auth.py`): the Sheets API's own
    `spreadsheets.create` requires it (or a broader Drive/Sheets scope)
    even when the Drive API surface itself is never called.
    """

    def __init__(self, sheets_service):
        self._sheets_service = sheets_service

    @classmethod
    def from_credentials(cls, token_path: Path, credentials_path: Path) -> "SheetsClient":
        """See `calendar_clients.google_auth.load_credentials` for
        `token_path`/`credentials_path` -- this shares the same OAuth
        token (and its combined `SCOPES`) as `CalendarClient`, so both
        must be loaded from the same underlying credentials to avoid
        refreshing/writing `token_path` twice; prefer
        `config.build_event_label_sheet` over calling this directly when
        both clients are needed together."""
        creds = load_credentials(token_path, credentials_path)
        return cls(build("sheets", "v4", credentials=creds))

    def create_spreadsheet(self, title: str) -> str:
        """Create a new, empty spreadsheet titled `title`. Returns the
        new spreadsheet's id."""
        spreadsheet = (
            self._sheets_service.spreadsheets()
            .create(body={"properties": {"title": title}}, fields="spreadsheetId")
            .execute()
        )
        return spreadsheet["spreadsheetId"]

    def set_column_width(
        self, spreadsheet_id: str, *, sheet_id: int, column_index: int, pixel_width: int
    ) -> None:
        """Narrow (or widen) a single column, by its 0-based index, on the
        sheet identified by `sheet_id` (0 for the first/default sheet of a
        newly-created spreadsheet)."""
        self._sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": column_index,
                                "endIndex": column_index + 1,
                            },
                            "properties": {"pixelSize": pixel_width},
                            "fields": "pixelSize",
                        }
                    }
                ]
            },
        ).execute()

    def read_rows(self, spreadsheet_id: str, sheet_range: str) -> list[list[str]]:
        """The cell values in `sheet_range` (e.g. "Sheet1!A2:D"), one list
        per row -- a row with trailing blank cells may come back shorter
        than the range's column count (the API omits them), and there are
        no rows at all (`[]`) if the range is entirely empty."""
        response = (
            self._sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_range)
            .execute()
        )
        return response.get("values", [])

    def write_rows(self, spreadsheet_id: str, sheet_range: str, rows: list[list[str]]) -> None:
        """Overwrite the cells starting at `sheet_range`'s top-left corner
        with `rows`. Only writes exactly `len(rows)` rows -- any existing
        rows beyond that within `sheet_range` are left untouched, so a
        caller replacing a previously-longer set of rows must clear the
        old range first (not needed by `utilities/event_label_sheet.py`,
        which always writes back exactly as many rows as it read)."""
        self._sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=sheet_range,
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()
