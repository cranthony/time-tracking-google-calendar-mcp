from __future__ import annotations

import logging
import threading
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import build_http

from utilities.memory_diagnostics import track

SCOPES = [
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/drive.file",
]
"""Every scope this app ever requests, for either of its two Google API
clients -- shared here (rather than split across calendar_clients/
google_calendar.py and calendar_clients/google_sheets.py) because both
clients are built from the *same* OAuth token (see `load_credentials`
below), so that token has to be requested with the union of what either
client needs, in one place.

calendar.app.created: this app can only read and write events on
calendars it has created itself; it has no access to the user's existing
calendars, including their "primary" calendar. Run create_calendar.py (at
the project root) to create that dedicated calendar and get the ID for
the GOOGLE_CALENDAR_ID environment variable. See the README's "Calendar
access model" section, and
https://developers.google.com/workspace/calendar/api/auth for the scope
reference.

drive.file: for calendar_clients/google_sheets.py's SheetsClient to
create and read/write the calendar metadata spreadsheet
(utilities/calendar_metadata_sheet.py) this app keeps goals, notes and
its compaction journal in. Grants access only to files this app creates
itself (or that are explicitly opened with it via a picker) -- the same
can't-touch-what-it-didn't-make model as calendar.app.created above.
It's the narrowest scope that still works: confirmed against the Sheets
API reference that it's accepted by spreadsheets.create,
spreadsheets.values.get, and spreadsheets.values.update alike, so
SheetsClient never has to call the Drive API directly -- the sheet's id
is found via CalendarClient.get_calendar_metadata (see
calendar_metadata_sheet.ensure_spreadsheet), not by searching Drive.

This is deliberate, for both scopes: a compromised or misbehaving
instance of this app cannot read or touch anything outside what it made
for itself. Upgrading from an older token.json that predates one of these
scopes requires deleting it and re-running the consent flow (see
`load_credentials`)."""

logger = logging.getLogger(__name__)


def load_credentials(token_path: Path, credentials_path: Path) -> Credentials:
    """Load cached OAuth credentials, refreshing or running the consent flow as needed.

    token_path: where the user's OAuth token (access token + refresh token) is
        cached, conventionally as `token.json`. This file does not need to
        exist yet: on first run (or once the cached token can no longer be
        refreshed), this function runs an interactive consent flow that opens
        a browser for the user to log into Google and grant access, then
        writes the resulting credentials here. On every later run, the
        cached token is read back and — if the access token has expired — is
        refreshed and, on a best-effort basis, rewritten to this same path
        (see below). This file contains live user credentials and must
        never be committed.
    credentials_path: path to the OAuth *client* secret file (conventionally
        `credentials.json`), downloaded once from the Google Cloud Console
        for the project this server registers as. It identifies the
        application, not the end user, but must still never be committed.

    The credentials returned here carry every scope in SCOPES -- shared by
    both CalendarClient and SheetsClient (see their respective
    from_credentials) -- so both clients must be built from the same
    load_credentials call (or the same cached token_path) rather than
    independently, or Google will treat them as differently-scoped
    sessions. See config._build_calendar_and_sheets_clients for the pattern.

    Rewriting token_path after a refresh is best-effort: if the path isn't
    writable (e.g. a read-only mount, such as a Render Secret File), the
    write is skipped with a warning rather than raising. This is safe
    because a refresh doesn't change the refresh token itself, only the
    short-lived access token — so an unwritable token_path just means the
    next process start refreshes again from the same cached refresh token,
    rather than reusing an unexpired access token.
    """
    with track("load_credentials"):
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)
            try:
                token_path.write_text(creds.to_json())
            except OSError:
                logger.warning(
                    "Could not write refreshed credentials to %s; continuing "
                    "with in-memory credentials for this run.",
                    token_path,
                )

        return creds


class _PerThreadHttp:
    """An http object for `googleapiclient.discovery.build` that gives each
    thread its own connection. The MCP server runs every (sync) tool on a
    worker thread, so tool calls that arrive together really do run at
    once -- and an httplib2.Http, which a service otherwise holds just one
    of, isn't thread-safe: two threads sharing its TLS socket fail with
    `SSLError: WRONG_VERSION_NUMBER`, or crash the process outright with a
    segfault. googleapiclient only ever calls `request` and `close` on it.

    The credentials are still shared, so a refresh on one thread is seen
    by the rest."""

    def __init__(self, credentials: Credentials):
        self._credentials = credentials
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: list[AuthorizedHttp] = []

    def _http(self) -> AuthorizedHttp:
        http = getattr(self._local, "http", None)
        if http is None:
            http = self._local.http = AuthorizedHttp(self._credentials, http=build_http())
            with self._lock:
                self._all.append(http)
        return http

    def request(self, *args, **kwargs):
        return self._http().request(*args, **kwargs)

    def close(self) -> None:
        with self._lock:
            for http in self._all:
                http.close()
            self._all.clear()


def build_service(service_name: str, version: str, *, credentials: Credentials):
    """`googleapiclient.discovery.build(service_name, version)` authorized
    with `credentials`, safe to call from several threads at once -- see
    `_PerThreadHttp`."""
    return build(service_name, version, http=_PerThreadHttp(credentials))
