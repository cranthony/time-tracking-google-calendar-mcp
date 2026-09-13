import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import create_calendar
from calendar_clients.google_calendar import Calendar


class TestCreateCalendar:
    def test_creates_calendar_and_returns_parsed_result(self):
        fake_creds = MagicMock()
        fake_service = MagicMock()
        fake_service.calendars.return_value.insert.return_value.execute.return_value = {
            "id": "new-cal-id",
            "summary": "Time Tracking",
        }

        with (
            patch.object(create_calendar, "get_token_path", return_value=Path("token.json")),
            patch.object(
                create_calendar, "get_credentials_path", return_value=Path("credentials.json")
            ),
            patch.object(
                create_calendar, "load_credentials", return_value=fake_creds
            ) as load_creds,
            patch.object(create_calendar, "build", return_value=fake_service) as build_service,
        ):
            result = create_calendar.create_calendar("Time Tracking")

        assert result == Calendar(id="new-cal-id", summary="Time Tracking")
        load_creds.assert_called_once_with(Path("token.json"), Path("credentials.json"))
        build_service.assert_called_once_with("calendar", "v3", credentials=fake_creds)
        fake_service.calendars.return_value.insert.assert_called_once_with(
            body={"summary": "Time Tracking"}
        )

    def test_includes_description_when_given(self):
        fake_service = MagicMock()
        fake_service.calendars.return_value.insert.return_value.execute.return_value = {
            "id": "new-cal-id",
            "summary": "Time Tracking",
            "description": create_calendar.DEFAULT_DESCRIPTION,
        }

        with (
            patch.object(create_calendar, "get_token_path", return_value=Path("token.json")),
            patch.object(
                create_calendar, "get_credentials_path", return_value=Path("credentials.json")
            ),
            patch.object(create_calendar, "load_credentials", return_value=MagicMock()),
            patch.object(create_calendar, "build", return_value=fake_service),
        ):
            result = create_calendar.create_calendar(
                "Time Tracking", create_calendar.DEFAULT_DESCRIPTION
            )

        assert result.description == create_calendar.DEFAULT_DESCRIPTION
        fake_service.calendars.return_value.insert.assert_called_once_with(
            body={"summary": "Time Tracking", "description": create_calendar.DEFAULT_DESCRIPTION}
        )


class TestCreateEventLabelSheetForCalendar:
    def test_delegates_to_build_event_labels(self):
        event_labels = MagicMock()
        event_labels.create_sheet.return_value = "sheet-1"

        with patch.object(create_calendar, "build_event_labels", return_value=event_labels) as build:
            result = create_calendar.create_event_label_sheet_for_calendar("cal-1")

        assert result == "sheet-1"
        build.assert_called_once_with("cal-1")
        event_labels.create_sheet.assert_called_once_with()


class TestMain:
    def test_creates_calendar_and_its_event_label_sheet_when_unconfigured(self, capsys, monkeypatch):
        monkeypatch.delenv("GOOGLE_CALENDAR_ID", raising=False)
        monkeypatch.setattr(sys, "argv", ["create_calendar.py"])
        new_calendar = Calendar(id="new-cal-id", summary="Time Tracking")

        with (
            patch.object(create_calendar, "create_calendar", return_value=new_calendar) as create,
            patch.object(
                create_calendar, "create_event_label_sheet_for_calendar", return_value="sheet-1"
            ) as create_sheet,
        ):
            create_calendar.main()

        create.assert_called_once_with(create_calendar.DEFAULT_SUMMARY, create_calendar.DEFAULT_DESCRIPTION)
        create_sheet.assert_called_once_with("new-cal-id")
        out = capsys.readouterr().out
        assert "new-cal-id" in out
        assert "https://docs.google.com/spreadsheets/d/sheet-1/edit" in out

    def test_only_adds_a_sheet_when_calendar_id_already_configured(self, capsys, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "existing-cal-id")
        monkeypatch.setattr(sys, "argv", ["create_calendar.py"])

        with (
            patch.object(create_calendar, "create_calendar") as create,
            patch.object(
                create_calendar, "create_event_label_sheet_for_calendar", return_value="sheet-1"
            ) as create_sheet,
        ):
            create_calendar.main()

        create.assert_not_called()
        create_sheet.assert_called_once_with("existing-cal-id")
        out = capsys.readouterr().out
        assert "existing-cal-id" in out
        assert "https://docs.google.com/spreadsheets/d/sheet-1/edit" in out

    def test_exits_with_the_error_when_a_sheet_is_already_tracked(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "existing-cal-id")
        monkeypatch.setattr(sys, "argv", ["create_calendar.py"])

        with patch.object(
            create_calendar,
            "create_event_label_sheet_for_calendar",
            side_effect=ValueError("already tracked"),
        ):
            with pytest.raises(SystemExit):
                create_calendar.main()
