from pathlib import Path
from unittest.mock import MagicMock, patch

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
