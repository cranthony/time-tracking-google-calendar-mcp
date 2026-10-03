import contextlib
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from calendar_clients import google_auth
from calendar_clients.google_auth import SCOPES, load_credentials


@pytest.fixture(autouse=True)
def _no_op_memory_tracking(monkeypatch):
    """load_credentials wraps its body in `track(...)` (see
    utilities/memory_diagnostics.py, which has its own dedicated tests) --
    replaced here with a no-op so these tests don't pay for real
    RSS/tracemalloc/objgraph work on every call."""
    monkeypatch.setattr(google_auth, "track", lambda label: contextlib.nullcontext())


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

    def test_tracks_memory_around_itself(self, monkeypatch):
        creds = self._mock_expired_creds()
        monkeypatch.setattr(
            google_auth.Credentials,
            "from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        token_path = MagicMock(spec=Path)
        token_path.exists.return_value = True
        tracked_labels = []
        monkeypatch.setattr(
            google_auth,
            "track",
            lambda label: tracked_labels.append(label) or contextlib.nullcontext(),
        )

        load_credentials(token_path, Path("credentials.json"))

        assert tracked_labels == ["load_credentials"]

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


class TestBuildService:
    def _http(self, monkeypatch) -> google_auth._PerThreadHttp:
        # Each AuthorizedHttp a stand-in, so nothing touches the network.
        monkeypatch.setattr(google_auth, "AuthorizedHttp", lambda creds, http: MagicMock(credentials=creds))
        return google_auth._PerThreadHttp(object())

    def test_builds_the_service_with_a_per_thread_http(self, monkeypatch):
        build_mock = MagicMock()
        monkeypatch.setattr(google_auth, "build", build_mock)
        creds = object()

        assert google_auth.build_service("sheets", "v4", credentials=creds) is build_mock.return_value

        assert build_mock.call_args.args == ("sheets", "v4")
        http = build_mock.call_args.kwargs["http"]
        assert isinstance(http, google_auth._PerThreadHttp)
        assert http._credentials is creds

    def test_reuses_one_connection_within_a_thread(self, monkeypatch):
        http = self._http(monkeypatch)

        http.request("https://a")
        http.request("https://b")

        assert len(http._all) == 1
        assert http._all[0].request.call_count == 2

    def test_gives_each_thread_its_own_connection(self, monkeypatch):
        http = self._http(monkeypatch)
        barrier = threading.Barrier(3)

        def call():
            http.request("https://a")
            barrier.wait()  # keep every thread alive, so none's is reused

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len({id(h) for h in http._all}) == 3
        assert all(h.request.call_count == 1 for h in http._all)

    def test_close_closes_every_threads_connection(self, monkeypatch):
        http = self._http(monkeypatch)
        thread = threading.Thread(target=http.request, args=("https://a",))
        thread.start()
        thread.join()
        http.request("https://b")
        opened = list(http._all)

        http.close()

        assert len(opened) == 2
        assert all(h.close.called for h in opened)
