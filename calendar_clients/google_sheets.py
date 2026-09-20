from __future__ import annotations

from pathlib import Path

from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials


class SheetsClient:
    """Wraps the Google Sheets API behind a small, mockable interface --
    the thin layer `utilities/calendar_metadata_sheet.py` and
    `utilities/event_label_sheet.py` build their higher-level logic on
    top of, the same way `CalendarClient` is a thin layer under
    `utilities/reallocating_calendar.py`. Knows nothing about event
    labels, time notes, or any other meaning attached to a tab; just
    spreadsheets, tabs, rows, and columns.

    This only talks to the Sheets API, never the Drive API directly --
    there's no need for a spreadsheet's id to be looked up by searching
    Drive; `utilities/calendar_metadata_sheet.py` gets it from
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
        new spreadsheet's id. Its one default tab is left exactly as
        the API creates it (sheetId 0, titled "Sheet1") -- callers that
        want it renamed/tagged do so afterwards, e.g. via
        `update_sheet_properties`/`create_sheet_metadata`."""
        spreadsheet = (
            self._sheets_service.spreadsheets()
            .create(body={"properties": {"title": title}}, fields="spreadsheetId")
            .execute()
        )
        return spreadsheet["spreadsheetId"]

    def rename_spreadsheet(self, spreadsheet_id: str, title: str) -> None:
        """Rename `spreadsheet_id` itself (its document title, e.g. what
        shows up in Drive and the browser tab) -- not any one tab within
        it; see `update_sheet_properties` for that."""
        self._sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSpreadsheetProperties": {
                            "properties": {"title": title},
                            "fields": "title",
                        }
                    }
                ]
            },
        ).execute()

    def add_sheet(
        self, spreadsheet_id: str, title: str, *, tab_color: dict[str, float] | None = None
    ) -> int:
        """Add a new tab titled `title` to `spreadsheet_id`, optionally
        with a `tab_color` (an RGB dict, e.g. {"red": 0.26, "green":
        0.52, "blue": 0.96}) -- purely a visible cue for a human looking
        at the spreadsheet, see `utilities/calendar_metadata_sheet.py`.
        Returns the new tab's sheetId."""
        properties: dict = {"title": title}
        if tab_color is not None:
            properties["tabColor"] = tab_color
        response = (
            self._sheets_service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": properties}}]},
            )
            .execute()
        )
        return response["replies"][0]["addSheet"]["properties"]["sheetId"]

    def update_sheet_properties(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        *,
        title: str | None = None,
        tab_color: dict[str, float] | None = None,
    ) -> None:
        """Update one or more of an existing tab's display properties
        (only `title`/`tab_color` are supported -- the only ones any
        caller has needed so far) in a single call. Passing neither is a
        no-op -- no request is sent."""
        properties: dict = {"sheetId": sheet_id}
        fields = []
        if title is not None:
            properties["title"] = title
            fields.append("title")
        if tab_color is not None:
            properties["tabColor"] = tab_color
            fields.append("tabColor")
        if not fields:
            return
        self._sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": properties,
                            "fields": ",".join(fields),
                        }
                    }
                ]
            },
        ).execute()

    def get_sheet_title(self, spreadsheet_id: str, sheet_id: int) -> str:
        """The current display title of the tab identified by `sheet_id`
        within `spreadsheet_id` -- looked up fresh every time (never
        cached) so a rename since the tab was created/tagged is picked
        up immediately. Raises `ValueError` if no tab with that id
        exists (e.g. it was deleted from under this app)."""
        response = (
            self._sheets_service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
            .execute()
        )
        for sheet in response.get("sheets", []):
            properties = sheet["properties"]
            if properties["sheetId"] == sheet_id:
                return properties["title"]
        raise ValueError(f"No sheet with sheetId {sheet_id} in spreadsheet {spreadsheet_id}")

    def create_sheet_metadata(self, spreadsheet_id: str, sheet_id: int, key: str, value: str) -> None:
        """Tag the tab identified by `sheet_id` with developer metadata
        `key`/`value`, PROJECT-scoped (queryable only by this app's own
        OAuth client -- never shown in the Sheets UI, and invisible to
        any other app). See `find_sheet_id` for the matching lookup, and
        `utilities/calendar_metadata_sheet.py` for why tabs are located
        this way instead of by title or position."""
        self._sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "createDeveloperMetadata": {
                            "developerMetadata": {
                                "metadataKey": key,
                                "metadataValue": value,
                                "location": {"sheetId": sheet_id},
                                "visibility": "PROJECT",
                            }
                        }
                    }
                ]
            },
        ).execute()

    def find_sheet_id(self, spreadsheet_id: str, key: str, value: str) -> int | None:
        """The sheetId of `spreadsheet_id`'s tab tagged `key`/`value` via
        `create_sheet_metadata`, or `None` if no tab carries that tag."""
        response = (
            self._sheets_service.spreadsheets()
            .developerMetadata()
            .search(
                spreadsheetId=spreadsheet_id,
                body={
                    "dataFilters": [
                        {"developerMetadataLookup": {"metadataKey": key, "metadataValue": value}}
                    ]
                },
            )
            .execute()
        )
        matches = response.get("matchedDeveloperMetadata", [])
        if not matches:
            return None
        return matches[0]["developerMetadata"]["location"]["sheetId"]

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

    def clear_rows(self, spreadsheet_id: str, sheet_range: str) -> None:
        """Clear every cell in `sheet_range`, regardless of how many rows
        it previously held -- unlike `write_rows`, which only overwrites
        however many rows it's given and leaves any leftover rows beyond
        that untouched, so it can't shrink a previously-longer range on
        its own."""
        self._sheets_service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=sheet_range, body={}
        ).execute()

    def clear_rows_in_sheet(self, spreadsheet_id: str, sheet_id: int, range_within_sheet: str) -> None:
        """`clear_rows`'s counterpart to `read_rows_in_sheet`/
        `write_rows_in_sheet` -- see `read_rows_in_sheet` for why
        `range_within_sheet` is qualified by `sheet_id`'s current title
        instead of being sent as-is."""
        self.clear_rows(spreadsheet_id, self._qualify(spreadsheet_id, sheet_id, range_within_sheet))

    def read_rows_in_sheet(
        self, spreadsheet_id: str, sheet_id: int, range_within_sheet: str
    ) -> list[list[str]]:
        """Like `read_rows`, but `range_within_sheet` (e.g. "A2:D",
        without a sheet name) is qualified with `sheet_id`'s *current*
        title -- looked up fresh via `get_sheet_title` -- instead of a
        caller assuming an unqualified range means "the first tab"
        (true, but only as long as a spreadsheet has exactly one tab,
        and fragile against reordering once it has more) or remembering
        a title that may since have been renamed."""
        return self.read_rows(spreadsheet_id, self._qualify(spreadsheet_id, sheet_id, range_within_sheet))

    def write_rows_in_sheet(
        self, spreadsheet_id: str, sheet_id: int, range_within_sheet: str, rows: list[list[str]]
    ) -> None:
        """`write_rows`'s counterpart to `read_rows_in_sheet` -- see
        there for why `range_within_sheet` is qualified by `sheet_id`'s
        current title instead of being sent as-is."""
        self.write_rows(spreadsheet_id, self._qualify(spreadsheet_id, sheet_id, range_within_sheet), rows)

    def _qualify(self, spreadsheet_id: str, sheet_id: int, range_within_sheet: str) -> str:
        title = self.get_sheet_title(spreadsheet_id, sheet_id)
        escaped_title = title.replace("'", "''")
        return f"'{escaped_title}'!{range_within_sheet}"
