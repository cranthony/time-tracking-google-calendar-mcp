from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
from utilities.event_label_sheet import EventLabelSheet


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
    def test_defaults_to_gitignored_local_filename(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CREDENTIALS_PATH", raising=False)

        assert config.get_credentials_path() == Path("credentials.json")

    def test_uses_env_var_when_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "/etc/secrets/credentials.json")

        assert config.get_credentials_path() == Path("/etc/secrets/credentials.json")


class TestGetTokenPath:
    def test_defaults_to_gitignored_local_filename(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_TOKEN_PATH", raising=False)

        assert config.get_token_path() == Path("token.json")

    def test_uses_env_var_when_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", "/etc/secrets/token.json")

        assert config.get_token_path() == Path("/etc/secrets/token.json")


class TestGetWorkosAuthkitDomain:
    def test_raises_when_unset(self, monkeypatch):
        monkeypatch.delenv("WORKOS_AUTHKIT_DOMAIN", raising=False)

        with pytest.raises(config.ConfigError):
            config.get_workos_authkit_domain()

    def test_raises_when_empty(self, monkeypatch):
        monkeypatch.setenv("WORKOS_AUTHKIT_DOMAIN", "")

        with pytest.raises(config.ConfigError):
            config.get_workos_authkit_domain()

    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("WORKOS_AUTHKIT_DOMAIN", "https://my-tenant.authkit.app")

        assert config.get_workos_authkit_domain() == "https://my-tenant.authkit.app"


class TestGetMcpResourceUrl:
    def test_prefers_explicit_public_url(self, monkeypatch):
        monkeypatch.setenv("MCP_PUBLIC_URL", "https://explicit.example/mcp")
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://from-render.onrender.com")

        assert config.get_mcp_resource_url() == "https://explicit.example/mcp"

    def test_falls_back_to_render_external_url(self, monkeypatch):
        monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://my-service.onrender.com")

        assert config.get_mcp_resource_url() == "https://my-service.onrender.com/mcp"

    def test_strips_trailing_slash_from_render_external_url(self, monkeypatch):
        monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://my-service.onrender.com/")

        assert config.get_mcp_resource_url() == "https://my-service.onrender.com/mcp"

    def test_raises_when_neither_is_set(self, monkeypatch):
        monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
        monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)

        with pytest.raises(config.ConfigError):
            config.get_mcp_resource_url()


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


class TestBuildEventLabelSheet:
    def test_builds_both_clients_from_one_shared_load_credentials_call(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CALENDAR_ID", "my-calendar-id")
        monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS_PATH", "creds.json")
        monkeypatch.setenv("GOOGLE_OAUTH_TOKEN_PATH", "tok.json")
        creds = object()
        load_credentials_mock = MagicMock(return_value=creds)
        monkeypatch.setattr(config, "load_credentials", load_credentials_mock)
        services = {"calendar": MagicMock(), "sheets": MagicMock()}
        build_mock = MagicMock(side_effect=lambda name, _version, credentials: services[name])
        monkeypatch.setattr(config, "build", build_mock)

        result = config.build_event_label_sheet()

        load_credentials_mock.assert_called_once_with(Path("tok.json"), Path("creds.json"))
        assert isinstance(result, EventLabelSheet)
        assert build_mock.call_count == 2
        assert {call.args[0] for call in build_mock.call_args_list} == {"calendar", "sheets"}
        for call in build_mock.call_args_list:
            assert call.kwargs["credentials"] is creds

    def test_raises_when_calendar_id_unset(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CALENDAR_ID", raising=False)
        monkeypatch.setattr(config, "load_credentials", MagicMock())
        monkeypatch.setattr(config, "build", MagicMock())

        with pytest.raises(config.ConfigError):
            config.build_event_label_sheet()
