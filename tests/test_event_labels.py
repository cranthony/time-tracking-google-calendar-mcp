from unittest.mock import MagicMock

from calendar_clients.google_calendar import EventLabel as RawEventLabel
from utilities.event_labels import EventLabel, EventLabels


def make_event_labels(calendar_client=None, event_label_sheet=None) -> EventLabels:
    return EventLabels(calendar_client or MagicMock(), event_label_sheet or MagicMock())


class TestListLabels:
    def test_returns_plain_labels_when_no_sheet_found(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = [
            RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work")
        ]
        event_label_sheet = MagicMock()
        event_label_sheet.find_sheet.return_value = None
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        result = event_labels.list_labels()

        assert result == [
            EventLabel(id="l1", background_color="#8e24aa", name="Design Work", priority=None)
        ]
        event_label_sheet.read_rows.assert_not_called()

    def test_fills_in_priority_from_matching_sheet_row(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = [
            RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work"),
            RawEventLabel(id="l2", background_color="#d50000", name="Untracked"),
        ]
        event_label_sheet = MagicMock()
        event_label_sheet.find_sheet.return_value = "sheet-1"
        event_label_sheet.read_rows.return_value = [["l1", "Design Work", "#8e24aa", "1"]]
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        result = event_labels.list_labels()

        assert result[0].priority == 1
        assert result[1].priority is None
        event_label_sheet.read_rows.assert_called_once_with("sheet-1")


class TestCreateLabel:
    def test_creates_with_explicit_background_color(self):
        calendar_client = MagicMock()
        calendar_client.create_event_label.return_value = RawEventLabel(
            id="l1", background_color="#8e24aa", name="Design Work"
        )
        event_labels = make_event_labels(calendar_client)

        result = event_labels.create_label("#8e24aa", "Design Work")

        calendar_client.create_event_label.assert_called_once_with("#8e24aa", "Design Work")
        assert result == EventLabel(
            id="l1", background_color="#8e24aa", name="Design Work", priority=None
        )

    def test_derives_background_color_from_priority(self):
        calendar_client = MagicMock()
        calendar_client.create_event_label.return_value = RawEventLabel(
            id="l1", background_color="#fbd75b", name="Design Work"
        )
        event_labels = make_event_labels(calendar_client)

        result = event_labels.create_label(name="Design Work", priority=1)

        calendar_client.create_event_label.assert_called_once_with("#fbd75b", "Design Work")
        assert result.priority == 1

    def test_defaults_background_color_when_neither_given(self):
        calendar_client = MagicMock()
        calendar_client.create_event_label.return_value = RawEventLabel(
            id="l1", background_color="#a4bdfc"
        )
        event_labels = make_event_labels(calendar_client)

        event_labels.create_label(name="No color, no priority")

        calendar_client.create_event_label.assert_called_once_with("#a4bdfc", "No color, no priority")


class TestUpdateLabel:
    def test_updates_background_color_only(self):
        calendar_client = MagicMock()
        calendar_client.update_event_label.return_value = RawEventLabel(
            id="l1", background_color="#8e24aa", name="Old"
        )
        event_labels = make_event_labels(calendar_client)

        result = event_labels.update_label("l1", background_color="#8e24aa")

        calendar_client.update_event_label.assert_called_once_with(
            "l1", background_color="#8e24aa", name=None
        )
        assert result.priority is None

    def test_updating_priority_alone_recolors_using_the_derived_color(self):
        calendar_client = MagicMock()
        calendar_client.update_event_label.return_value = RawEventLabel(
            id="l1", background_color="#7ae7bf", name="Design Work"
        )
        event_labels = make_event_labels(calendar_client)

        result = event_labels.update_label("l1", priority=3)

        calendar_client.update_event_label.assert_called_once_with(
            "l1", background_color="#7ae7bf", name=None
        )
        assert result.priority == 3

    def test_explicit_background_color_wins_over_priority(self):
        calendar_client = MagicMock()
        calendar_client.update_event_label.return_value = RawEventLabel(
            id="l1", background_color="#123456"
        )
        event_labels = make_event_labels(calendar_client)

        event_labels.update_label("l1", background_color="#123456", priority=3)

        calendar_client.update_event_label.assert_called_once_with(
            "l1", background_color="#123456", name=None
        )

    def test_neither_background_color_nor_priority_keeps_current_color(self):
        calendar_client = MagicMock()
        calendar_client.update_event_label.return_value = RawEventLabel(
            id="l1", background_color="#d50000", name="New"
        )
        event_labels = make_event_labels(calendar_client)

        event_labels.update_label("l1", name="New")

        calendar_client.update_event_label.assert_called_once_with(
            "l1", background_color=None, name="New"
        )


class TestDeleteLabel:
    def test_delegates_to_calendar_client(self):
        calendar_client = MagicMock()
        calendar_client.delete_event_label.return_value = RawEventLabel(
            id="l1", background_color="#8e24aa", name="Design Work"
        )
        event_labels = make_event_labels(calendar_client)

        result = event_labels.delete_label("l1")

        calendar_client.delete_event_label.assert_called_once_with("l1")
        assert result == EventLabel(
            id="l1", background_color="#8e24aa", name="Design Work", priority=None
        )


class TestCreateSheet:
    def test_prepopulates_current_labels(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = [
            RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work"),
            RawEventLabel(id="l2", background_color="#d50000"),
        ]
        event_label_sheet = MagicMock()
        event_label_sheet.create_sheet.return_value = "sheet-1"
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        result = event_labels.create_sheet("My Labels")

        assert result == "sheet-1"
        event_label_sheet.create_sheet.assert_called_once_with(
            "My Labels",
            [["l1", "Design Work", "#8e24aa", ""], ["l2", "", "#d50000", ""]],
        )

    def test_passes_none_when_no_labels_exist(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = []
        event_label_sheet = MagicMock()
        event_label_sheet.create_sheet.return_value = "sheet-1"
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        event_labels.create_sheet()

        event_label_sheet.create_sheet.assert_called_once_with("Event Labels", None)

    def test_propagates_already_tracked_error(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = []
        event_label_sheet = MagicMock()
        event_label_sheet.create_sheet.side_effect = ValueError("already tracked")
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        try:
            event_labels.create_sheet()
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestSyncFromSheet:
    def test_raises_when_no_sheet_is_tracked(self):
        event_label_sheet = MagicMock()
        event_label_sheet.resolve_sheet_id.side_effect = ValueError("not tracked")
        event_labels = make_event_labels(event_label_sheet=event_label_sheet)

        try:
            event_labels.sync_from_sheet()
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_creates_labels_for_blank_id_rows_and_writes_back_new_ids(self):
        calendar_client = MagicMock()
        calendar_client.replace_event_labels.return_value = [
            RawEventLabel(id="new-id", background_color="#8e24aa", name="Design Work"),
        ]
        event_label_sheet = MagicMock()
        event_label_sheet.resolve_sheet_id.return_value = "sheet-1"
        event_label_sheet.read_rows.return_value = [["", "Design Work", "#8e24aa", "2"]]
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        result = event_labels.sync_from_sheet()

        sent_labels = calendar_client.replace_event_labels.call_args.args[0]
        assert sent_labels == [RawEventLabel(id=None, background_color="#8e24aa", name="Design Work")]
        assert result == [
            EventLabel(id="new-id", background_color="#8e24aa", name="Design Work", priority=2)
        ]
        event_label_sheet.write_rows.assert_called_once_with(
            "sheet-1", [["new-id", "Design Work", "#8e24aa", "2"]]
        )

    def test_derives_background_color_from_priority_for_blank_color_rows(self):
        calendar_client = MagicMock()
        calendar_client.replace_event_labels.return_value = [
            RawEventLabel(id="new-id", background_color="#fbd75b", name="Design Work"),
        ]
        event_label_sheet = MagicMock()
        event_label_sheet.resolve_sheet_id.return_value = "sheet-1"
        event_label_sheet.read_rows.return_value = [["", "Design Work", "", "1"]]
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        event_labels.sync_from_sheet()

        sent_labels = calendar_client.replace_event_labels.call_args.args[0]
        assert sent_labels == [RawEventLabel(id=None, background_color="#fbd75b", name="Design Work")]

    def test_overwrites_existing_label_matched_by_id(self):
        calendar_client = MagicMock()
        calendar_client.replace_event_labels.return_value = [
            RawEventLabel(id="l1", background_color="#000000", name="Renamed"),
        ]
        event_label_sheet = MagicMock()
        event_label_sheet.resolve_sheet_id.return_value = "sheet-1"
        event_label_sheet.read_rows.return_value = [["l1", "Renamed", "#000000", ""]]
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        result = event_labels.sync_from_sheet()

        assert result == [
            EventLabel(id="l1", background_color="#000000", name="Renamed", priority=None)
        ]

    def test_deletes_labels_missing_from_the_sheet(self):
        # A label not present in any sheet row simply isn't included in
        # what's sent to replace_event_labels -- CalendarClient.
        # replace_event_labels is what actually removes it.
        calendar_client = MagicMock()
        calendar_client.replace_event_labels.return_value = []
        event_label_sheet = MagicMock()
        event_label_sheet.resolve_sheet_id.return_value = "sheet-1"
        event_label_sheet.read_rows.return_value = []
        event_labels = make_event_labels(calendar_client, event_label_sheet)

        result = event_labels.sync_from_sheet()

        calendar_client.replace_event_labels.assert_called_once_with([])
        assert result == []
