from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import EventLabel as RawEventLabel
from utilities.event_labels import EventLabel, EventLabels

_HEADER_ROW = ["id", "name", "background_color", "priority"]
_SHEET_ID_KEY = "event-label-sheet-id"


def _sheets_client(rows: list[list[str]]) -> MagicMock:
    """A SheetsClient mock backed by an in-memory header/data range, so
    reads reflect whatever the code under test last wrote."""
    state = {"A1:D1": [_HEADER_ROW], "A2:D": rows}
    sheets_client = MagicMock()
    sheets_client.read_rows.side_effect = lambda _spreadsheet_id, rng: state[rng]

    def write_rows(_spreadsheet_id, rng, new_rows):
        state[rng] = new_rows

    sheets_client.write_rows.side_effect = write_rows
    return sheets_client


def _tracked_calendar_client(raw_labels=(), etag='"etag-1"', sheet_id="sheet-1") -> MagicMock:
    calendar_client = MagicMock()
    calendar_client.get_calendar_metadata.return_value = sheet_id
    calendar_client.list_event_labels.return_value = (list(raw_labels), etag)
    return calendar_client


class TestInit:
    def test_reuses_an_already_tracked_sheet(self):
        calendar_client = _tracked_calendar_client(sheet_id="sheet-1")
        sheets_client = MagicMock()

        event_labels = EventLabels(calendar_client, sheets_client)

        assert event_labels.sheet_id == "sheet-1"
        sheets_client.create_spreadsheet.assert_not_called()
        calendar_client.set_calendar_metadata.assert_not_called()

    def test_creates_and_tracks_a_sheet_when_none_exists(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        calendar_client.list_event_labels.return_value = (
            [RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work")],
            '"etag-1"',
        )
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "new-sheet"

        event_labels = EventLabels(calendar_client, sheets_client)

        assert event_labels.sheet_id == "new-sheet"
        calendar_client.set_calendar_metadata.assert_called_once_with(_SHEET_ID_KEY, "new-sheet")
        # Pre-populated with the calendar's current labels.
        assert sheets_client.write_rows.call_args_list[-1].args == (
            "new-sheet",
            "A2:D",
            [["l1", "Design Work", "#8e24aa", ""]],
        )


class TestListLabels:
    def test_returns_calendar_labels_with_priority_from_the_sheet(self):
        calendar_client = _tracked_calendar_client(
            raw_labels=[RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work")],
        )
        sheets_client = _sheets_client([["l1", "Design Work", "#8e24aa", "1"]])
        event_labels = EventLabels(calendar_client, sheets_client)

        result = event_labels.list_labels()

        assert result == [
            EventLabel(id="l1", name="Design Work", background_color="#8e24aa", priority=1)
        ]

    def test_leaves_the_calendar_untouched_when_already_in_sync(self):
        calendar_client = _tracked_calendar_client(
            raw_labels=[RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work")],
        )
        sheets_client = _sheets_client([["l1", "Design Work", "#8e24aa", "1"]])
        event_labels = EventLabels(calendar_client, sheets_client)

        event_labels.list_labels()

        calendar_client.replace_event_labels.assert_not_called()


class TestSyncLabels:
    def test_creates_labels_for_blank_id_rows_and_writes_back_new_ids(self):
        calendar_client = _tracked_calendar_client(raw_labels=[])
        calendar_client.replace_event_labels.return_value = [
            RawEventLabel(id="new-id", background_color="#8e24aa", name="Design Work"),
        ]
        sheets_client = _sheets_client([["", "Design Work", "#8e24aa", "2"]])
        event_labels = EventLabels(calendar_client, sheets_client)

        result = event_labels.sync_labels()

        sent_labels = calendar_client.replace_event_labels.call_args.args[0]
        assert sent_labels == [RawEventLabel(id=None, background_color="#8e24aa", name="Design Work")]
        assert result == [
            EventLabel(id="new-id", name="Design Work", background_color="#8e24aa", priority=2)
        ]
        assert sheets_client.read_rows(None, "A2:D") == [["new-id", "Design Work", "#8e24aa", "2"]]

    def test_passes_the_etag_from_list_event_labels_through(self):
        calendar_client = _tracked_calendar_client(raw_labels=[], etag='"etag-xyz"')
        calendar_client.replace_event_labels.return_value = [
            RawEventLabel(id="new-id", background_color="#8e24aa", name="Design Work"),
        ]
        sheets_client = _sheets_client([["", "Design Work", "#8e24aa", ""]])
        event_labels = EventLabels(calendar_client, sheets_client)

        event_labels.sync_labels()

        sent_labels = calendar_client.replace_event_labels.call_args.args[0]
        assert calendar_client.replace_event_labels.call_args.args[1] == '"etag-xyz"'
        assert sent_labels == [RawEventLabel(id=None, background_color="#8e24aa", name="Design Work")]

    def test_deletes_labels_missing_from_the_sheet(self):
        # A label not present in any sheet row simply isn't included in
        # what's sent to replace_event_labels -- CalendarClient.
        # replace_event_labels is what actually removes it.
        calendar_client = _tracked_calendar_client(
            raw_labels=[RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work")],
        )
        calendar_client.replace_event_labels.return_value = []
        sheets_client = _sheets_client([])
        event_labels = EventLabels(calendar_client, sheets_client)

        result = event_labels.sync_labels()

        calendar_client.replace_event_labels.assert_called_once()
        assert calendar_client.replace_event_labels.call_args.args[0] == []
        assert result == []

    def test_does_not_write_back_when_no_ids_were_assigned(self):
        calendar_client = _tracked_calendar_client(
            raw_labels=[RawEventLabel(id="l1", background_color="#000000", name="Design Work")],
        )
        calendar_client.replace_event_labels.return_value = [
            RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work"),
        ]
        sheets_client = _sheets_client([["l1", "Design Work", "#8e24aa", ""]])
        event_labels = EventLabels(calendar_client, sheets_client)

        event_labels.sync_labels()

        # Only the constructor's own read/writes happened -- sync_labels
        # itself didn't need to write back, since every row already had
        # an id.
        sheets_client.write_rows.assert_not_called()


class TestCreateLabel:
    def test_appends_to_the_sheet_and_syncs(self):
        calendar_client = _tracked_calendar_client(raw_labels=[])
        calendar_client.replace_event_labels.return_value = [
            RawEventLabel(id="new-id", background_color="#8e24aa", name="Design Work"),
        ]
        sheets_client = _sheets_client([])
        event_labels = EventLabels(calendar_client, sheets_client)

        result = event_labels.create_label(
            EventLabel(name="Design Work", background_color="#8e24aa")
        )

        assert result == [
            EventLabel(id="new-id", name="Design Work", background_color="#8e24aa", priority=None)
        ]


class TestUpdateLabel:
    def test_requires_an_id(self):
        calendar_client = _tracked_calendar_client(raw_labels=[])
        sheets_client = _sheets_client([])
        event_labels = EventLabels(calendar_client, sheets_client)

        with pytest.raises(ValueError):
            event_labels.update_label(EventLabel(name="No id"))

    def test_raises_when_id_is_unknown(self):
        calendar_client = _tracked_calendar_client(raw_labels=[])
        sheets_client = _sheets_client([["l1", "Design Work", "#8e24aa", ""]])
        event_labels = EventLabels(calendar_client, sheets_client)

        with pytest.raises(ValueError):
            event_labels.update_label(EventLabel(id="missing", name="New Name"))

    def test_merges_given_fields_and_keeps_the_rest(self):
        calendar_client = _tracked_calendar_client(
            raw_labels=[RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work")],
        )
        calendar_client.replace_event_labels.return_value = [
            RawEventLabel(id="l1", background_color="#8e24aa", name="New Name"),
        ]
        sheets_client = _sheets_client([["l1", "Design Work", "#8e24aa", ""]])
        event_labels = EventLabels(calendar_client, sheets_client)

        event_labels.update_label(EventLabel(id="l1", name="New Name"))

        sent_labels = calendar_client.replace_event_labels.call_args.args[0]
        assert sent_labels == [
            RawEventLabel(id="l1", background_color="#8e24aa", name="New Name")
        ]
