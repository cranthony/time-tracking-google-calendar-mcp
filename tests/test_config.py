from pathlib import Path
from unittest.mock import patch

import pytest

import config


class TestGetCalendarId:
    def test_raises_when_unset(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CALENDAR_ID", raising=False)

        with pytest.raises(config.ConfigError):
            config.get_calendar_id()

    def test_raises_when_empty(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "")

        with pytest.raises(config.ConfigError):
            config.get_calendar_id()

    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "my-calendar-id")

        assert config.get_calendar_id() == "my-calendar-id"


class TestGetCredentialsPath:
    def test_defaults_to_render_secret_file_path(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CREDENTIALS_PATH", raising=False)

        assert config.get_credentials_path() == Path("/etc/secrets/credentials.json")

    def test_uses_env_var_when_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "creds.json")

        assert config.get_credentials_path() == Path("creds.json")


class TestGetTokenPath:
    def test_defaults_to_render_secret_file_path(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_TOKEN_PATH", raising=False)

        assert config.get_token_path() == Path("/etc/secrets/token.json")

    def test_uses_env_var_when_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", "tok.json")

        assert config.get_token_path() == Path("tok.json")


class TestBuildCalendarClient:
    def test_passes_config_through_to_from_credentials(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "my-calendar-id")
        monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "creds.json")
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", "tok.json")

        with patch.object(
            config.CalendarClient, "from_credentials", return_value="a-client"
        ) as from_credentials:
            result = config.build_calendar_client()

        assert result == "a-client"
        from_credentials.assert_called_once_with(
            token_path=Path("tok.json"),
            credentials_path=Path("creds.json"),
            calendar_id="my-calendar-id",
        )

    def test_raises_when_calendar_id_unset(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CALENDAR_ID", raising=False)

        with pytest.raises(config.ConfigError):
            config.build_calendar_client()
