from pathlib import Path
from unittest.mock import MagicMock

from calendar_clients import google_auth
from calendar_clients.google_auth import SCOPES, load_credentials


class TestScopes:
    def test_includes_calendar_and_drive_file(self):
        assert "https://www.googleapis.com/auth/calendar.app.created" in SCOPES
        assert "https://www.googleapis.com/auth/drive.file" in SCOPES


class TestLoadCredentials:
    def _mock_expired_creds(self) -> MagicMock:
        creds = MagicMock()
        creds.valid = False
        creds.expired = True
        creds.refresh_token = "refresh-token"
        creds.to_json.return_value = "{}"
        return creds

    def test_refreshes_and_rewrites_token_path(self, monkeypatch):
        creds = self._mock_expired_creds()
        monkeypatch.setattr(
            google_auth.Credentials,
            "from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        token_path = MagicMock(spec=Path)
        token_path.exists.return_value = True

        result = load_credentials(token_path, Path("credentials.json"))

        assert result is creds
        creds.refresh.assert_called_once()
        token_path.write_text.assert_called_once_with("{}")

    def test_swallows_oserror_when_token_path_is_not_writable(self, monkeypatch, caplog):
        creds = self._mock_expired_creds()
        monkeypatch.setattr(
            google_auth.Credentials,
            "from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        token_path = MagicMock(spec=Path)
        token_path.exists.return_value = True
        token_path.write_text.side_effect = OSError("Read-only file system")

        with caplog.at_level("WARNING", logger="calendar_clients.google_auth"):
            result = load_credentials(token_path, Path("credentials.json"))

        assert result is creds
        creds.refresh.assert_called_once()
        assert any(
            "Could not write refreshed credentials" in record.message
            for record in caplog.records
        )
