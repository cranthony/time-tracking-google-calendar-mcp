# time-tracking-google-calendar-mcp
A Model Context Protocol (MCP) for managing a Google calendar with a set of non-overlapping events representing one person's time

## Setup

Create and activate a virtual environment, then install dependencies.

**macOS/Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

For running tests, install the dev dependencies instead (this also installs `requirements.txt`):

```bash
pip install -r requirements-dev.txt
```

## Project layout

- [`calendar_clients/google_calendar.py`](calendar_clients/google_calendar.py) — Google Calendar API access, behind a `CalendarClient` class and plain `Event`/`Calendar` dataclasses. Kept separate from `server.py` so the Calendar logic can be unit tested without hitting the real API — tests construct a `CalendarClient` around a mocked `service` object instead.
- [`server.py`](server.py) — the MCP server; its tools call into `calendar_clients/google_calendar.py` rather than talking to `googleapiclient`/OAuth directly.
- [`create_calendar.py`](create_calendar.py) — a standalone bootstrap script (not an MCP tool) that creates the dedicated calendar this app needs — see [Calendar access model](#calendar-access-model) below.
- [`calendar_cli.py`](calendar_cli.py) — a dev-only command-line tool for poking at the calendar directly (`list`/`get`) without going through an MCP host — see [Command-line utilities](#command-line-utilities) below.
- [`config.py`](config.py) — reads configuration from environment variables — see [Configuration](#configuration) below.
- [`tests/`](tests/) — unit tests for the above, mocking the Google API rather than hitting it.

## Calendar access model

This app requests only the `calendar.app.created` OAuth scope (see `SCOPES` in [`calendar_clients/google_calendar.py`](calendar_clients/google_calendar.py)) — not the broader `calendar.events`/`calendar.events.owned` scopes. That has real consequences:

- This app can only read and write events on calendars **it has created itself**.
- It has **no access to the user's existing calendars** — not `"primary"`, not any calendar they made by hand in the Calendar UI. API calls against any calendar this app didn't create itself will fail.
- This is deliberate: a compromised or misbehaving instance of this app cannot read or touch anything outside the dedicated calendar(s) it made for itself.

**Bootstrapping:** there's no calendar to operate on until this app creates one. Run [`create_calendar.py`](create_calendar.py) once — it only needs `GOOGLE_OAUTH_CREDENTIALS_PATH`/`GOOGLE_OAUTH_TOKEN_PATH` (see [Configuration](#configuration) below), not `GOOGLE_CALENDAR_ID` — and it prints the new calendar's ID:

```bash
python create_calendar.py "Time Tracking"
```

Then set `GOOGLE_CALENDAR_ID` to the ID it prints. If the app hasn't been used to create a calendar yet — including the very first time you set this up — this is the step to run first.

## MCP tools

[`server.py`](server.py) exposes the calendar as five MCP tools:

| Tool | Signature | Status |
| --- | --- | --- |
| `list_events` | `(min_time, max_time) -> list[Event]` | Implemented |
| `get_event` | `(id) -> Event` | Implemented |
| `update_event` | `(event: Event) -> list[Event]` | Raises `NotImplementedError` |
| `create_event` | `(event: Event) -> list[Event]` | Raises `NotImplementedError` |
| `delete_event` | `(id) -> list[Event]` | Raises `NotImplementedError` |

`update_event`/`create_event`/`delete_event` return the list of events *affected* by the operation (not necessarily just the one event acted on — e.g. a change that resolves an overlap could affect more than one event), which is why their return type is `list[Event]` rather than a single `Event`.

Calendar creation is deliberately *not* an MCP tool — see [Calendar access model](#calendar-access-model) above — so the model can't create new calendars on its own; that's a one-time, human-run bootstrap step via `create_calendar.py`.

### Why tools, not resources

MCP has both **tools** (model-controlled: the model decides when to call one, with whatever arguments it computes, as part of its own reasoning) and **resources** (application-controlled: a human typically browses and attaches one via the host's UI, like Claude Desktop's "Attach from MCP" picker). This server only uses tools, for two reasons:

- **Portability.** Resources depend on the host having built UI (or another bridging mechanism) for the model to reach them at all; a lot of MCP clients — agentic ones especially — only implement tool-calling and skip resources entirely. Tools work everywhere.
- **Fit.** The natural workflow here is model-driven, not human-browsing-driven: the model discovers an event's ID via `list_events`, then immediately wants to act on it — fetch details, update, delete. That's a tool-calling pattern (one tool's output, the `id` field, feeds directly into the next tool's input) with no need for a resource-URI layer in between. Nobody is going to browse a picker UI for an event by its opaque Google Calendar ID.

## Google OAuth credentials

`CalendarClient.from_credentials`, in [`calendar_clients/google_calendar.py`](calendar_clients/google_calendar.py), needs two files, neither of which should ever be committed:

- **`credentials.json`** — the OAuth *client* secret, downloaded once from the [Google Cloud Console](https://console.cloud.google.com/) for the Google Cloud project you register this server under (APIs & Services → Credentials → create an OAuth client ID of type "Desktop app", then download its JSON). This identifies the application, not you as a user — you obtain it yourself and supply its path.
- **`token.json`** — the *user's* actual access + refresh token. You don't create this yourself: the first time `load_credentials` runs without a valid cached token, it opens a browser for you to log into Google and grant access, then writes the resulting credentials to this path. Every later run reads the cached file back and refreshes it as the access token expires; rewriting it back to `token_path` afterward is best-effort — if the path turns out not to be writable (see [Deploying](#deploying) below), the refresh still succeeds in memory, just without being persisted.

Both paths are required arguments (no defaults), so where they live is up to whatever wires up the server. For local development, the filenames `credentials.json` and `token.json` are gitignored, so you can drop both files straight into the project root without risk of committing them.

## Configuration

[`config.py`](config.py) reads the server's configuration from environment variables, rather than anything being hardcoded or committed:

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GOOGLE_CALENDAR_ID` | Yes | — | The calendar to operate on — must be the ID of a calendar this app created itself; see [Calendar access model](#calendar-access-model) above. Run `create_calendar.py` if you don't have one yet. |
| `GOOGLE_OAUTH_CREDENTIALS_PATH` | No | `credentials.json` | Path to the OAuth client secret file — see [Google OAuth credentials](#google-oauth-credentials) above. Defaults to the gitignored local filename; override to something with real protections when deployed (see [Deploying](#deploying)). |
| `GOOGLE_OAUTH_TOKEN_PATH` | No | `token.json` | Path to the cached OAuth user token — see [Google OAuth credentials](#google-oauth-credentials) above. Defaults to the gitignored local filename; override to something with real protections when deployed (see [Deploying](#deploying)). |

To supply these locally, copy [`.env.example`](.env.example) to `.env` and fill it in — `config.py` loads `.env` automatically (via `python-dotenv`) if one is present. `.env` is gitignored, so nothing personal ends up committed.

If this server is launched by an MCP host (Claude Desktop, Claude Code, etc.) instead of run standalone, set these same variables in that host's server config under its `env` field — no `.env` file needed in that case.

## Deploying

On a platform like [Render](https://render.com/), don't put `credentials.json`/`token.json` in the repo or in a regular env var — Render's **Secret Files** feature is built for exactly this: add each file under the service's Environment tab, and Render mounts it at `/etc/secrets/<filename>` at runtime, separate from your source and the regular env var list.

- `GOOGLE_OAUTH_CREDENTIALS_PATH`/`GOOGLE_OAUTH_TOKEN_PATH` default to the gitignored local filenames (`credentials.json`/`token.json`), meant for local development — on Render, set them explicitly to `/etc/secrets/credentials.json`/`/etc/secrets/token.json` to match those mounts.
- Run `python create_calendar.py` locally first — it needs an interactive browser for the OAuth consent flow, so it can't run on Render itself. This both generates `token.json` and creates the dedicated calendar in one step; paste the resulting `token.json` into the Secret File, and set `GOOGLE_CALENDAR_ID` on Render to the ID it printed.
- Secret File mounts may be read-only, so `token.json`'s refresh-and-rewrite (see [Google OAuth credentials](#google-oauth-credentials) above) is best-effort by design — a failed write there just means the next process restart refreshes again from the same cached refresh token, which Google doesn't rotate on a normal refresh.

## Running the server

With the virtual environment activated, run the server directly:

```bash
python server.py
```

Or launch it with the MCP Inspector for interactive testing:

```bash
mcp dev server.py
```

`mcp dev` requires two tools outside the Python virtual environment: **Node.js/npx** (to launch the Inspector UI itself) and **uv** (the Inspector uses it to run the server in an isolated, on-the-fly environment). Neither is something pip/venv can provide — a venv only manages Python packages already on your machine, not other language runtimes or tools.

**Installing Node.js (provides `npx`):**
- **macOS:** `brew install node`
- **Windows:** `winget install OpenJS.NodeJS.LTS` (or download the installer from [nodejs.org](https://nodejs.org))
- **Linux:** use your distro's package manager (e.g. `sudo apt install nodejs npm`) or install via [nvm](https://github.com/nvm-sh/nvm)

**Installing uv:**
- **macOS/Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Windows:** `winget install astral-sh.uv` (or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)

After installing, restart your terminal and verify with:

```bash
npx --version
uv --version
```

If you only need to run the server (not the Inspector), `python server.py` works without Node.js or uv.

See [MCP tools](#mcp-tools) above for what `server.py` currently exposes.

## Command-line utilities

[`calendar_cli.py`](calendar_cli.py) is a dev tool for calling the calendar directly from a terminal, without going through an MCP host — useful for poking around or debugging. It's not named `calendar.py`: that would shadow Python's stdlib `calendar` module, which `google-auth`/`httplib2` (both used by `calendar_clients/google_calendar.py`) import internally. Its one extra dependency, `pytimeparse`, lives in `requirements-dev.txt`, not `requirements.txt` — install the dev dependencies (see [Setup](#setup)) to use it.

```bash
# Events from 1 hour ago to 1 hour from now (the default window)
python calendar_cli.py list

# Events from 30 minutes ago to 2 hours from now
python calendar_cli.py list 30m 2h

# A single event by id
python calendar_cli.py get <event-id>
```

`from`/`to` are each a duration relative to *now* — parsed with [pytimeparse](https://pypi.org/project/pytimeparse/) (e.g. `"1h"`, `"90m"`, `"2d"`, `"1:30"`) — giving a window from `now - from` to `now + to`. Both are optional and default to `1h`.

## Running tests

With the dev dependencies installed:

```bash
pytest
```
