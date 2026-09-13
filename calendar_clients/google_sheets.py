from __future__ import annotations

from pathlib import Path

from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials


def _escape_query_value(value: str) -> str:
    """Escape a string for use inside a Drive API `q` search expression's
    single-quoted literal (backslash, then the quote itself) -- see
    https://developers.google.com/workspace/drive/api/guides/search-files."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class SheetsClient:
    """Wraps the Google Sheets and Drive APIs behind a small, mockable
    interface -- the thin layer `utilities/event_label_sheet.py` builds its
    event-label-sheet logic on top of, the same way `CalendarClient` is a
    thin layer under `utilities/reallocating_calendar.py`. Knows nothing
    about event labels; just spreadsheets, rows, and Drive's tagging
    (`appProperties`) mechanism for finding a spreadsheet again later.
    """

    def __init__(self, sheets_service, drive_service):
        self._sheets_service = sheets_service
        self._drive_service = drive_service

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
        return cls(build("sheets", "v4", credentials=creds), build("drive", "v3", credentials=creds))

    def create_spreadsheet(self, title: str, properties: dict[str, str]) -> str:
        """Create a new, empty spreadsheet titled `title`, tagged with
        `properties` as Drive `appProperties` (so `find_spreadsheet` can
        find it again later by those same key/value pairs). Returns the
        new spreadsheet's id."""
        spreadsheet = (
            self._sheets_service.spreadsheets()
            .create(body={"properties": {"title": title}}, fields="spreadsheetId")
            .execute()
        )
        spreadsheet_id = spreadsheet["spreadsheetId"]
        self._drive_service.files().update(
            fileId=spreadsheet_id, body={"appProperties": properties}
        ).execute()
        return spreadsheet_id

    def find_spreadsheet(self, properties: dict[str, str]) -> str | None:
        """The id of the most-recently-modified spreadsheet tagged with
        every key/value pair in `properties` (Drive `appProperties`), or
        `None` if none match. `appProperties` (as opposed to `properties`)
        are private to this app -- another app with access to the same
        file can't see or search them -- matching the same
        can't-touch-what-it-didn't-tag spirit as the `drive.file` scope
        itself."""
        query_parts = [
            "mimeType='application/vnd.google-apps.spreadsheet'",
            "trashed=false",
        ]
        for key, value in properties.items():
            query_parts.append(
                f"appProperties has {{key='{_escape_query_value(key)}' "
                f"and value='{_escape_query_value(value)}'}}"
            )
        response = (
            self._drive_service.files()
            .list(
                q=" and ".join(query_parts),
                orderBy="modifiedTime desc",
                pageSize=1,
                fields="files(id)",
            )
            .execute()
        )
        files = response.get("files", [])
        return files[0]["id"] if files else None

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
