from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials
from calendar_clients.google_calendar import CalendarClient
from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet
from utilities.compaction_journal import CompactionJournal
from utilities.event_labels import EventLabels
from utilities.noted_time_sheet import NotedTimeSheet

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def get_calendar_id() -> str:
    """The Google Calendar to operate on, from the GOOGLE_CALENDAR_ID
    environment variable. There is no default. This must be the ID of a
    calendar this app has created itself (run create_calendar.py) — see the
    README's "Calendar access model". "primary" and other pre-existing
    calendars will not work.
    """
    value = os.environ.get("GOOGLE_CALENDAR_ID")
    if not value:
        raise ConfigError(
            "GOOGLE_CALENDAR_ID is not set. Set it to the ID of a calendar "
            "this app has created itself (see the README's \"Calendar "
            'access model" — "primary" will not work).'
        )
    return value


def get_credentials_path() -> Path:
    """Path to the OAuth client secret file, from
    GOOGLE_OAUTH_CREDENTIALS_PATH (default: credentials.json, a gitignored
    filename for local development). When deployed, override this to
    somewhere with real protections, e.g. a Render Secret File mount
    (/etc/secrets/credentials.json). See the README's "Google OAuth
    credentials" and "Deploying" sections."""
    return Path(os.environ.get("GOOGLE_OAUTH_CREDENTIALS_PATH", "credentials.json"))


def get_token_path() -> Path:
    """Path to the cached OAuth user token, from GOOGLE_OAUTH_TOKEN_PATH
    (default: token.json, a gitignored filename for local development).
    When deployed, override this to somewhere with real protections, e.g.
    a Render Secret File mount (/etc/secrets/token.json). See the README's
    "Google OAuth credentials" and "Deploying" sections."""
    return Path(os.environ.get("GOOGLE_OAUTH_TOKEN_PATH", "token.json"))


def get_workos_authkit_domain() -> str:
    """The WorkOS AuthKit domain (e.g. https://your-tenant.authkit.app) that
    issues and signs bearer tokens for this server's HTTP transport, from
    the WORKOS_AUTHKIT_DOMAIN environment variable. Only needed when
    MCP_TRANSPORT=streamable-http -- stdio (local dev, mcp dev) never reads
    this. See the README's "Deploying" section."""
    value = os.environ.get("WORKOS_AUTHKIT_DOMAIN")
    if not value:
        raise ConfigError(
            "WORKOS_AUTHKIT_DOMAIN is not set. Set it to your WorkOS AuthKit "
            'domain (e.g. "https://your-tenant.authkit.app") -- see the '
            'README\'s "Deploying" section.'
        )
    return value


def get_mcp_resource_url() -> str:
    """The public URL this server's MCP endpoint is reachable at, used both
    as the OAuth resource identifier and the audience bearer tokens must
    carry. Prefers MCP_PUBLIC_URL; falls back to Render's own auto-injected
    RENDER_EXTERNAL_URL (set on every Render web service) plus "/mcp". Only
    needed when MCP_TRANSPORT=streamable-http."""
    explicit = os.environ.get("MCP_PUBLIC_URL")
    if explicit:
        return explicit
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        return f"{render_url.rstrip('/')}/mcp"
    raise ConfigError(
        "Neither MCP_PUBLIC_URL nor RENDER_EXTERNAL_URL is set. Set "
        "MCP_PUBLIC_URL to this server's public MCP endpoint URL (e.g. "
        '"https://your-service.onrender.com/mcp") -- see the README\'s '
        '"Deploying" section.'
    )


def build_calendar_client() -> CalendarClient:
    """Construct a CalendarClient from environment configuration (and a
    local .env file, if present)."""
    return CalendarClient.from_credentials(
        token_path=get_token_path(),
        credentials_path=get_credentials_path(),
        calendar_id=get_calendar_id(),
    )


def _build_calendar_and_sheets_clients(
    calendar_id: str | None = None,
) -> tuple[CalendarClient, SheetsClient]:
    """A CalendarClient and a SheetsClient sharing one loaded set of
    credentials -- rather than each independently calling
    CalendarClient.from_credentials/SheetsClient.from_credentials (which
    would load, and potentially refresh and rewrite, token_path twice
    for what's really one OAuth session -- see calendar_clients/
    google_auth.py's SCOPES, which covers both clients' needs
    together).

    calendar_id: defaults to get_calendar_id() (the calendar configured
    via GOOGLE_CALENDAR_ID). Pass one explicitly for a calendar that
    isn't configured (yet) -- e.g. create_calendar.py building this
    calendar's event label sheet right after creating it, before
    GOOGLE_CALENDAR_ID has been set to its id."""
    creds = load_credentials(get_token_path(), get_credentials_path())
    calendar_client = CalendarClient(
        build("calendar", "v3", credentials=creds), calendar_id or get_calendar_id()
    )
    sheets_client = SheetsClient(build("sheets", "v4", credentials=creds))
    return calendar_client, sheets_client


def build_event_labels(calendar_id: str | None = None) -> EventLabels:
    """Construct an EventLabels from environment configuration (and a
    local .env file, if present). See `_build_calendar_and_sheets_clients`
    for `calendar_id`. Constructing this ensures the calendar has an event
    label sheet, creating one (pre-populated with its current labels) if
    it didn't already -- see `EventLabels.__init__`."""
    calendar_client, sheets_client = _build_calendar_and_sheets_clients(calendar_id)
    return EventLabels(calendar_client, sheets_client)


def build_noted_time_sheet(calendar_id: str | None = None) -> NotedTimeSheet:
    """Construct a NotedTimeSheet from environment configuration (and a
    local .env file, if present). See `_build_calendar_and_sheets_clients`
    for `calendar_id`. Constructing this ensures the calendar has a
    metadata spreadsheet and a noted-times tab (with its header row),
    creating whichever doesn't already exist -- see
    `NotedTimeSheet.ensure`."""
    calendar_client, sheets_client = _build_calendar_and_sheets_clients(calendar_id)
    spreadsheet_id, _is_new_spreadsheet = calendar_metadata_sheet.ensure_spreadsheet(
        calendar_client, sheets_client
    )
    return NotedTimeSheet.ensure(sheets_client, spreadsheet_id)


def build_compaction_journal(calendar_id: str | None = None) -> CompactionJournal:
    """Construct a CompactionJournal from environment configuration (and a
    local .env file, if present). See `_build_calendar_and_sheets_clients`
    for `calendar_id`. Constructing this ensures the calendar has a
    metadata spreadsheet and a compactions tab (with its header row),
    creating whichever doesn't already exist -- see
    `CompactionJournal.ensure`."""
    calendar_client, sheets_client = _build_calendar_and_sheets_clients(calendar_id)
    spreadsheet_id, _is_new_spreadsheet = calendar_metadata_sheet.ensure_spreadsheet(
        calendar_client, sheets_client
    )
    return CompactionJournal.ensure(sheets_client, spreadsheet_id)
