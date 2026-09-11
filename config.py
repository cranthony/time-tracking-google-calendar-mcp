from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from calendar_clients.google_calendar import CalendarClient

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
    GOOGLE_OAUTH_CREDENTIALS_PATH (default: /etc/secrets/credentials.json,
    matching a Render Secret File mount). See the README's "Google OAuth
    credentials" and "Deploying" sections."""
    return Path(
        os.environ.get("GOOGLE_OAUTH_CREDENTIALS_PATH", "/etc/secrets/credentials.json")
    )


def get_token_path() -> Path:
    """Path to the cached OAuth user token, from GOOGLE_OAUTH_TOKEN_PATH
    (default: /etc/secrets/token.json, matching a Render Secret File mount).
    See the README's "Google OAuth credentials" and "Deploying" sections."""
    return Path(os.environ.get("GOOGLE_OAUTH_TOKEN_PATH", "/etc/secrets/token.json"))


def build_calendar_client() -> CalendarClient:
    """Construct a CalendarClient from environment configuration (and a
    local .env file, if present)."""
    return CalendarClient.from_credentials(
        token_path=get_token_path(),
        credentials_path=get_credentials_path(),
        calendar_id=get_calendar_id(),
    )
