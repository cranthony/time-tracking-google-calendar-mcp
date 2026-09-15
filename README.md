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

- [`calendar_clients/google_auth.py`](calendar_clients/google_auth.py) — the OAuth plumbing shared by both API clients below: `SCOPES` (every scope either one needs) and `load_credentials` (loads/refreshes/runs the consent flow). Split out on its own so `CalendarClient` and `SheetsClient` can each be built from one shared token rather than each owning (and duplicating) credential loading.
- [`calendar_clients/google_calendar.py`](calendar_clients/google_calendar.py) — Google Calendar API access, behind a `CalendarClient` class and plain `Event`/`Calendar`/`EventLabel` dataclasses. Kept separate from `server.py` so the Calendar logic can be unit tested without hitting the real API — tests construct a `CalendarClient` around a mocked `service` object instead. `CalendarClient` is a thin, pure API wrapper (list/get/create/update/delete, plus event-label management); its `EventLabel` is a *raw* label, exactly as Calendar represents one — see [Event label sheets](#event-label-sheets) below for the richer `EventLabel` most of this app actually uses.
- [`calendar_clients/google_sheets.py`](calendar_clients/google_sheets.py) — Google Sheets API access, behind a `SheetsClient` class — the same thin-wrapper role as `CalendarClient`, but for spreadsheets: create one, read/write rows, narrow a column. Never touches the Drive API directly — a spreadsheet's id is looked up via `CalendarClient.get_calendar_metadata` instead (see [Event label sheets](#event-label-sheets) below). Knows nothing about event labels.
- [`utilities/reallocation.py`](utilities/reallocation.py) — the reallocation algorithm: how creating an event makes room for itself by reclaiming time from lower-priority events and free time in its day. Has no Calendar API dependency of its own — see the module docstring.
- [`utilities/reallocating_calendar.py`](utilities/reallocating_calendar.py) — the glue between the two above: `ReallocatingCalendar` wraps a `CalendarClient`-shaped calendar (see `label_priority_calendar.py` below) with reallocation-aware `create_event`/`update_event`, the shared entry point both `server.py` and `calendar_cli.py` use.
- [`utilities/label_priority_calendar.py`](utilities/label_priority_calendar.py) — `LabelPriorityCalendar` wraps a `CalendarClient` with an `EventLabels`, filling in an event's `priority` from its event label whenever it's read (`list_events`/`get_event`) with no explicit priority of its own. `ReallocatingCalendar` is built on top of this rather than a plain `CalendarClient` directly, so reallocation naturally treats an event label's priority as the fallback — see [Event label sheets](#event-label-sheets) below.
- [`utilities/event_label_sheet.py`](utilities/event_label_sheet.py) — a thin, per-spreadsheet API: `EventLabelSheet.create` (a spreadsheet, given its `SheetsClient`) and, bound to one already-known spreadsheet id, `read`/`write`/`append` its rows as this module's own `EventLabel` dataclass (`id`/`name`/`background_color`/`priority`). Has no idea which calendar (if any) a given spreadsheet belongs to.
- [`utilities/event_labels.py`](utilities/event_labels.py) — `EventLabels`, the application-level policy that ties a `CalendarClient` to *its* event label sheet: finds (or creates) it via `CalendarClient.get_calendar_metadata`/`set_calendar_metadata`, and combines `CalendarClient`'s raw labels with `EventLabelSheet`'s rows for `server.py` and most of `calendar_cli.py` to work with — layered on top of both the way `ReallocatingCalendar` is layered on top of `CalendarClient` alone. See [Event label sheets](#event-label-sheets) below.
- [`utilities/memory_diagnostics.py`](utilities/memory_diagnostics.py) — `track(label)`, a context manager wrapping an operation (every MCP tool, `load_credentials`) to log RSS, `tracemalloc` growth, and `objgraph` object-count growth since the previously tracked operation — for narrowing down what's driving this process's memory usage. `tracemalloc` itself only gets started once RSS first crosses half of this process's own memory limit, read from the `MEMORY_LIMIT_BYTES` environment variable if set, else from the cgroup enforcing it (`memory.max`/`memory.limit_in_bytes`) — so its overhead is only paid once things are heading toward an OOM kill.
- [`server.py`](server.py) — the MCP server; its tools call into `calendar_clients/google_calendar.py` rather than talking to `googleapiclient`/OAuth directly.
- [`workos_auth.py`](workos_auth.py) — verifies bearer tokens issued by WorkOS AuthKit, for when `server.py` is hosted remotely over `streamable-http` instead of run locally over stdio — see [Deploying](#deploying) below.
- [`create_calendar.py`](create_calendar.py) — a standalone bootstrap script (not an MCP tool) that creates the dedicated calendar this app needs, and its event label sheet — see [Calendar access model](#calendar-access-model) below.
- [`calendar_cli.py`](calendar_cli.py) — a dev-only command-line tool for poking at the calendar directly (`list`/`get`/`update_properties`/`create`) without going through an MCP host — see [Command-line utilities](#command-line-utilities) below.
- [`config.py`](config.py) — reads configuration from environment variables — see [Configuration](#configuration) below.
- [`render.yaml`](render.yaml) — a Render Blueprint for hosting this remotely — see [Deploying](#deploying) below.
- [`tests/`](tests/) — unit tests for the above, mocking the Google API (and WorkOS's JWKS) rather than hitting them.

## Calendar access model

This app requests only the `calendar.app.created` OAuth scope (see `SCOPES` in [`calendar_clients/google_auth.py`](calendar_clients/google_auth.py)) — not the broader `calendar.events`/`calendar.events.owned` scopes. That has real consequences:

- This app can only read and write events on calendars **it has created itself**.
- It has **no access to the user's existing calendars** — not `"primary"`, not any calendar they made by hand in the Calendar UI. API calls against any calendar this app didn't create itself will fail.
- This is deliberate: a compromised or misbehaving instance of this app cannot read or touch anything outside the dedicated calendar(s) it made for itself.

`SCOPES` also includes `drive.file`, for [event label sheets](#event-label-sheets) — the same "can't touch anything it didn't make" model as `calendar.app.created`, but for Google Sheets: this app can only see/create spreadsheets it made itself, never the user's other Drive files. It's required by the Sheets API's own `spreadsheets.create` even though this app never calls the Drive API directly — a sheet's id is looked up from the calendar itself, not by searching Drive (see [Event label sheets](#event-label-sheets)). If you're upgrading an install whose `token.json` predates this scope, delete it and let the consent flow run again (see [Google OAuth credentials](#google-oauth-credentials) below) — the old token won't carry the new scope.

**Bootstrapping:** there's no calendar to operate on until this app creates one. Run [`create_calendar.py`](create_calendar.py) once — it only needs `GOOGLE_OAUTH_CREDENTIALS_PATH`/`GOOGLE_OAUTH_TOKEN_PATH` (see [Configuration](#configuration) below), not `GOOGLE_CALENDAR_ID` — and it prints the new calendar's ID, then creates that calendar's event label sheet in the same step (see [Event label sheets](#event-label-sheets) below) and prints its URL too:

```bash
python create_calendar.py "Time Tracking"
```

Then set `GOOGLE_CALENDAR_ID` to the ID it prints. If the app hasn't been used to create a calendar yet — including the very first time you set this up — this is the step to run first.

If `GOOGLE_CALENDAR_ID` is *already* set when you run it, `create_calendar.py` doesn't create a new calendar at all — it just creates an event label sheet for that already-configured calendar instead (useful if you set this app up before event label sheets existed). Either way, sheet creation is a one-time, human-run bootstrap step just like the calendar itself — there's deliberately no MCP tool or CLI command for it (see [MCP tools](#mcp-tools) and [Event label sheets](#event-label-sheets) below); it fails if that calendar already has one tracked.

## MCP tools

[`server.py`](server.py) exposes the calendar as MCP tools:

| Tool | Signature |
| --- | --- |
| `list_events` | `(min_time, max_time) -> list[PublicEvent]` |
| `get_event` | `(id) -> PublicEvent` |
| `update_event` | `(event: PublicEvent) -> list[PublicEvent]` |
| `create_event` | `(event: PublicEvent) -> list[PublicEvent]` |
| `delete_event` | `(id) -> list[PublicEvent]` |
| `create_event_label` | `(label: EventLabel) -> list[EventLabel]` |
| `update_event_label` | `(label: EventLabel) -> list[EventLabel]` |
| `sync_event_labels_from_sheet` | `() -> list[EventLabel]` |

`update_event`/`create_event`/`delete_event` all return a `list[PublicEvent]` rather than a single `PublicEvent`, since `update_event`/`create_event` can affect more than the one event acted on (see below). `delete_event` doesn't call the Calendar API's own delete — it patches the event's `status` to `"cancelled"` (via `CalendarClient.update_event`), the same way reallocation cancels an event to make room for another. This matches `Event.status`'s own documented recommendation to cancel rather than delete an instance of a recurring event, and always returns exactly that one event, wrapped in a single-element list for a consistent return type across all three.

`create_event`/`update_event` both make room for the event via `utilities/reallocating_calendar.py`'s `ReallocatingCalendar` (see [Project layout](#project-layout) above): each fetches the roughly 24 hours of events starting at the event's own `start` (via `ReallocatingCalendar.list_day_events`, truncated after the first event marked `is_end_of_day_sleep`, if any — that's "the day" `utilities/reallocation.py` operates on), then calls `reallocate_for_new_event` and applies whatever it returns (creating or moving the event itself, and updating — shrinking, moving, splitting, or cancelling — whatever else needed to make room, each via the plain `CalendarClient`). `update_event` (`ReallocatingCalendar.update_event`) additionally excludes the event's own prior position from that day's events first, since `reallocate_for_new_event` refuses a day already containing the event being moved -- and passes its id as `list_day_events`'s `ignore_id`, so truncating at the first `is_end_of_day_sleep` event doesn't key off the event's own not-yet-applied prior position when it's itself that day's sleep block (e.g. stretching a morning sleep block later). Its `PublicEvent` needs at least one of `start`/`end` set (not necessarily both) — whichever is left `None` is filled in from the event's current value before reallocating, reusing that same `list_day_events` fetch rather than a second one: if the event is already in it, its current value is read from there; if not (e.g. the one value given put it on a different day than its prior position), a direct `get_event` gets it instead. A `ValueError` from reallocation is surfaced as a `ToolError`.

Every event tool uses `PublicEvent` (defined in `server.py`), not `Event`, as its input/output type — `Event` minus whatever fields are named in `INTERNAL_EVENT_FIELDS`, plus `is_cancelled` (which has no `Event` equivalent — it's derived from the hidden `status` field). Agents communicating with this MCP only see the fields in `PublicEvent`. `calendar_cli.py` still operates on `Event` directly and has full access to every field, since it's a human-run dev tool, not something the agent talks to.

`list_events`/`get_event` still don't surface a cancelled event at all: `list_events` omits it, and `get_event` raises a `ToolError`. But when an operation like `create_event` cancels an event as a side effect of making room, or `delete_event` cancels the event it was asked to remove, that cancellation is a direct result of the agent's own action, so it's worth surfacing rather than hiding — its `PublicEvent` comes back with the rest of its fields intact and `is_cancelled=True`. `is_cancelled` only ever moves from `False` to `True`; setting it `False` has no effect, since there's no way to un-cancel an event through this API.

Calendar creation is deliberately *not* an MCP tool — see [Calendar access model](#calendar-access-model) above — so the model can't create new calendars on its own; that's a one-time, human-run bootstrap step via `create_calendar.py`.

`create_event_label`/`update_event_label`/`sync_event_labels_from_sheet` manage this calendar's custom event labels via `utilities/event_labels.py`'s `EventLabel` (through `EventLabels`, not `CalendarClient` directly — see [Event label sheets](#event-label-sheets) below) — no `PublicEvent`-style wrapper, since a label has no internal-only fields to hide from the agent. These manage the labels *defined on the calendar*; assigning one to a specific event is `PublicEvent.event_label_id` (see [Event colors](#event-colors) below), a separate field from these tools. There's no `list_event_labels` or `delete_event_label` tool: all three of the tools above return the *entire* resulting label list (not just the one label touched), which is also how you see the current ones — a dedicated "list" tool would just be `sync_event_labels_from_sheet` under a misleading name (see [Event label sheets](#event-label-sheets) below) — and deleting means removing a label's row from the event label sheet and calling `sync_event_labels_from_sheet`, since the sheet is what's authoritative. `update_event_label`'s `label.id` must already exist, and whichever of its other fields are left `None` keep their current value.

`EventLabel.priority` gives a label the same priority-coloring concept `Event.colorId` already has, but expressed as an arbitrary hex color instead of one of the 11 fixed ones — with one important difference from `Event.priority`: **Google Calendar's label resource has no field for it at all.** Passing `priority` to `create_event_label`/`update_event_label` only derives that call's `background_color` (via the public `calendar_clients.google_calendar.color_for_priority`) when `background_color` is left unset; it isn't persisted anywhere Calendar can hand back later. The only place a label's priority is actually remembered is a synced **event label sheet** — see [Event label sheets](#event-label-sheets) below; every label tool's result has `priority` filled in from that sheet, `None` for a label with no matching row.

Every label write reads the full label list, changes it in memory, and writes the whole thing back (the API has no way to touch a single label in place), which is a lost-update race if two callers do this concurrently. `CalendarClient.replace_event_labels` (what `EventLabels` builds every one of these tools on) guards against that with the calendar's own `etag`: it's sent back as an `If-Match` precondition on the write, so a write based on a label list that's since changed fails with `EventLabelConflictError` (surfaced as a `ToolError`) instead of silently overwriting the other change. Confirmed empirically against the real API, since Google's docs only document `If-Match` for Events, not Calendars.

There's deliberately no `create_event_label_sheet` MCP tool or CLI command — see [Event label sheets](#event-label-sheets) below for how a sheet actually comes to exist.

### Why tools, not resources

MCP has both **tools** (model-controlled: the model decides when to call one, with whatever arguments it computes, as part of its own reasoning) and **resources** (application-controlled: a human typically browses and attaches one via the host's UI, like Claude Desktop's "Attach from MCP" picker). This server only uses tools, for two reasons:

- **Portability.** Resources depend on the host having built UI (or another bridging mechanism) for the model to reach them at all; a lot of MCP clients — agentic ones especially — only implement tool-calling and skip resources entirely. Tools work everywhere.
- **Fit.** The natural workflow here is model-driven, not human-browsing-driven: the model discovers an event's ID via `list_events`, then immediately wants to act on it — fetch details, update, delete. That's a tool-calling pattern (one tool's output, the `id` field, feeds directly into the next tool's input) with no need for a resource-URI layer in between. Nobody is going to browse a picker UI for an event by its opaque Google Calendar ID.

## Event label sheets

Since Google Calendar has no field for an event label's priority, this app keeps it in a Google Sheet instead — one row per label, four columns: **ID** (deliberately narrow — nobody's expected to care what it is, just that it's there), **Name**, **Background Color**, **Priority**. Two layers work together here, the same split as `CalendarClient`/`ReallocatingCalendar` elsewhere in this project:

- [`utilities/event_label_sheet.py`](utilities/event_label_sheet.py)'s `EventLabelSheet` owns the sheet itself: creating it (header row, narrowed ID column), finding it again, and reading/writing its rows as plain `list[str]` rows. It knows nothing about what a row *means* as a label.
- [`utilities/event_labels.py`](utilities/event_labels.py)'s `EventLabels` is the layer everything else actually talks to: it combines `CalendarClient`'s raw labels with `EventLabelSheet`'s rows into a richer `EventLabel` (`id`, `name`, `background_color`, and — unlike `calendar_clients/google_calendar.py`'s raw `EventLabel` — `priority`), and is what `create_event_label`/`sync_event_labels_from_sheet`/etc. (see [MCP tools](#mcp-tools) above) and `create_label`/`sync_labels`/etc. (see [Command-line utilities](#command-line-utilities) below) are built on.

Finding the sheet again doesn't rely on searching Drive (there's no reliable way to pick the "right" one among however many sheets this app might create, short of a "most recently modified" guess). Instead, the spreadsheet's id is recorded directly on the calendar itself, via `CalendarClient.get_calendar_metadata`/`set_calendar_metadata`: since the Calendars resource has no dedicated field for arbitrary app metadata (confirmed against the API reference — unlike Events' `extendedProperties`), these embed a small `[cascading-time-tracker:key=value]` marker as its own line in the calendar's `description`, clearly delimited from whatever human-readable text is already there, and guarded by the calendar's `etag` exactly like the event-label writes above. A single deterministic lookup, not a heuristic.

**Creating the sheet is implicit, not a separate step:** constructing `EventLabels` for a calendar (`EventLabels.__init__`, what every one of `build_event_labels`'s callers — the MCP tools, `calendar_cli.py`'s non-`_raw_` label commands — do first) checks for a tracked sheet and, if there isn't one yet, creates it right there: a new spreadsheet, pre-populated with the calendar's *current* labels (Priority left blank — Calendar doesn't know one), recorded on the calendar. [`create_calendar.py`](create_calendar.py) (see [Calendar access model](#calendar-access-model) above) triggers this proactively right after creating the calendar (or for an already-configured one) purely so its URL gets printed for you right away — nothing else *requires* running it first; the first `create_event_label`/`update_event_label`/`sync_event_labels_from_sheet` call would create the sheet on the fly just the same.

**There's deliberately no "list labels" tool or command.** Every operation below reconciles the calendar with the sheet (the sheet always wins wherever they disagree) and returns the *entire* resulting label list, not just whatever it specifically touched — so any of them doubles as "show me the current labels", and a dedicated list operation would just be `sync_labels` under a name that hides that it also writes:

- **`sync_event_labels_from_sheet`/`sync_labels`** (`EventLabels.sync_labels`) makes the calendar's labels match its tracked sheet exactly: a row with a blank ID becomes a new label (and that row's ID cell is then filled in with the real, Google-assigned ID, so syncing again doesn't create a duplicate); a row whose ID matches an existing label fully overwrites its name/color/priority; **any existing label whose ID isn't present in the sheet at all is deleted.** This is also the *only* way to delete a label — remove its row and sync (or just call this with no sheet edits pending, to see the current labels).
- **`create_event_label`/`create_label`** (`EventLabels.create_label`) appends a new row to the sheet, then syncs.
- **`update_event_label`/`update_label`** (`EventLabels.update_label`) requires `label.id` to already be in the sheet, merges whichever of its other fields aren't `None` onto that row (leaving the rest as they were), then syncs.
- Every one of the above takes or derives a `priority`, used only to derive `background_color` (via the public `calendar_clients.google_calendar.color_for_priority`) wherever a row's own `background_color` is left unset — priority is never stored anywhere except as a plain column value in the sheet itself.

**An event's own `priority` always wins, but falls back to its label's when unset.** [`utilities/label_priority_calendar.py`](utilities/label_priority_calendar.py)'s `LabelPriorityCalendar` fills in `priority` from the event label named by `event_label_id` (via `EventLabels.label_priorities`, a read-only sheet lookup) whenever it reads an event (`list_events`/`get_event`) that doesn't already have one of its own. `ReallocatingCalendar` (see [Project layout](#project-layout) above) is layered on top of this instead of a plain `CalendarClient`, so reallocation sees this fallback priority too when deciding what to reclaim time from.

## Event colors

[`Event.to_api_body()`](calendar_clients/google_calendar.py) (used by every path that writes an event — the MCP tools, `calendar_cli.py`, and reallocation, all via `CalendarClient.create_event`/`update_event`) automatically sets `colorId` from the event's `priority`, so priority is visible at a glance in the Google Calendar UI without a separate step:

| Priority | Color | `colorId` |
| --- | --- | --- |
| <=0 | Graphite (gray) | `"8"` |
| 1 | Banana (yellow) | `"5"` |
| 2 | the calendar's default color | unset |
| >=3 | Sage (soft green) | `"2"` |

Note that calendar colors are superseded by the colors corresponding to the event's label. `event_label_id` (the API's own `eventLabelId` field) assigns one of this calendar's event labels (see [Event label sheets](#event-label-sheets) above) to a specific event — it's a plain field on `Event`/`PublicEvent` like any other, so `create_event`/`update_event` (and `calendar_cli.py`'s `create`/`update`/`update_properties`, via `event_label_id=<label-id>`) can set it directly; there's no validation that the id actually names an existing label, so a stale or made-up one is only caught by the Calendar API itself.

## Google OAuth credentials

`calendar_clients/google_auth.py`'s `load_credentials` — used by both `CalendarClient.from_credentials` and `SheetsClient.from_credentials` (and by `config.build_event_labels`, which loads it once and shares it between the two — see [Project layout](#project-layout) above) — needs two files, neither of which should ever be committed:

- **`credentials.json`** — the OAuth *client* secret, downloaded once from the [Google Cloud Console](https://console.cloud.google.com/) for the Google Cloud project you register this server under (APIs & Services → Credentials → create an OAuth client ID of type "Desktop app", then download its JSON). This identifies the application, not you as a user — you obtain it yourself and supply its path.
- **`token.json`** — the *user's* actual access + refresh token, carrying every scope in `SCOPES` (Calendar and Drive/Sheets both). You don't create this yourself: the first time `load_credentials` runs without a valid cached token, it opens a browser for you to log into Google and grant access, then writes the resulting credentials to this path. Every later run reads the cached file back and refreshes it as the access token expires; rewriting it back to `token_path` afterward is best-effort — if the path turns out not to be writable (see [Deploying](#deploying) below), the refresh still succeeds in memory, just without being persisted.

Both paths are required arguments (no defaults), so where they live is up to whatever wires up the server. For local development, the filenames `credentials.json` and `token.json` are gitignored, so you can drop both files straight into the project root without risk of committing them.

## Configuration

[`config.py`](config.py) reads the server's configuration from environment variables, rather than anything being hardcoded or committed:

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GOOGLE_CALENDAR_ID` | Yes | — | The calendar to operate on — must be the ID of a calendar this app created itself; see [Calendar access model](#calendar-access-model) above. Run `create_calendar.py` if you don't have one yet. |
| `GOOGLE_OAUTH_CREDENTIALS_PATH` | No | `credentials.json` | Path to the OAuth client secret file — see [Google OAuth credentials](#google-oauth-credentials) above. Defaults to the gitignored local filename; override to something with real protections when deployed (see [Deploying](#deploying)). |
| `GOOGLE_OAUTH_TOKEN_PATH` | No | `token.json` | Path to the cached OAuth user token — see [Google OAuth credentials](#google-oauth-credentials) above. Defaults to the gitignored local filename; override to something with real protections when deployed (see [Deploying](#deploying)). |
| `MCP_TRANSPORT` | No | `stdio` | `stdio` runs the server as a subprocess a local MCP host spawns directly (Claude Desktop's local config, `mcp dev`). `streamable-http` runs it as a standalone HTTP server instead — set this when hosting it remotely (see [Deploying](#deploying)); the three variables below only matter in that case. |
| `WORKOS_AUTHKIT_DOMAIN` | Only for `streamable-http` | — | The WorkOS AuthKit domain (e.g. `https://your-tenant.authkit.app`) that issues and signs bearer tokens for callers of this server — see [Deploying](#deploying). Unrelated to the Google OAuth variables above: this controls who's allowed to talk to *this* server, not this server's own credential to talk to Google. |
| `MCP_PUBLIC_URL` | No | derived from `RENDER_EXTERNAL_URL` | This server's own public MCP endpoint URL (e.g. `https://your-service.onrender.com/mcp`) — used as the OAuth resource identifier and the audience bearer tokens must carry. On Render this is derived automatically from `RENDER_EXTERNAL_URL` (which Render sets for you); set it explicitly elsewhere. |
| `PORT` | No | `10000` | The port `streamable-http` binds to (always alongside host `0.0.0.0`, as every Render web service requires). Render sets this for you automatically. |
| `MEMORY_LIMIT_BYTES` | No | read from the cgroup enforcing it | *Not read via `config.py`* — read directly by [`utilities/memory_diagnostics.py`](utilities/memory_diagnostics.py). This process's own memory limit, in bytes; only needed if the cgroup value can't be read (see that module). |

To supply these locally, copy [`.env.example`](.env.example) to `.env` and fill it in — `config.py` loads `.env` automatically (via `python-dotenv`) if one is present. `.env` is gitignored, so nothing personal ends up committed.

If this server is launched by an MCP host (Claude Desktop, Claude Code, etc.) instead of run standalone, set these same variables in that host's server config under its `env` field — no `.env` file needed in that case.

## Deploying

Hosting this remotely (e.g. on [Render](https://render.com/)) involves **two independent OAuth concerns** — don't confuse them:

1. **This server's own credential to talk to Google** (`credentials.json`/`token.json`, already covered above) — who *this app* is, as far as Google Calendar is concerned.
2. **Who's allowed to talk to *this server*** — once it's reachable at a public URL instead of spawned locally as a subprocess, that URL alone is no longer enough access control. This is what `MCP_TRANSPORT=streamable-http` and WorkOS AuthKit below are for.

A [`render.yaml`](render.yaml) blueprint is included, covering the build/start command and plain env vars; two things still need to be done by hand in Render's dashboard regardless (a blueprint can't express either):

**1. Google Calendar credentials** (unchanged from before): don't put `credentials.json`/`token.json` in the repo or a regular env var — Render's **Secret Files** feature is built for exactly this: add each file under the service's Environment tab, and Render mounts it at `/etc/secrets/<filename>` at runtime, separate from your source and the regular env var list.
- Run `python create_calendar.py` locally first — it needs an interactive browser for the OAuth consent flow, so it can't run on Render itself. This both generates `token.json` and creates the dedicated calendar in one step; paste the resulting `token.json` into the Secret File, and set `GOOGLE_CALENDAR_ID` on Render to the ID it printed.
- Secret File mounts may be read-only, so `token.json`'s refresh-and-rewrite (see [Google OAuth credentials](#google-oauth-credentials) above) is best-effort by design — a failed write there just means the next process restart refreshes again from the same cached refresh token, which Google doesn't rotate on a normal refresh.

**2. A WorkOS account, configured for AuthKit**, to authenticate *callers* of this server. This app never issues tokens or holds a password itself — over `streamable-http` it's only an OAuth *Resource Server*, checking that a request's bearer token was really issued by WorkOS for this server ([`workos_auth.py`](workos_auth.py) does the actual JWT/JWKS verification). WorkOS AuthKit is the *Authorization Server*: it's what Claude.ai's connector UI talks to when it discovers this server needs auth, dynamically registers itself as a client, and runs you through the login/consent flow — none of that is code in this repo. **You don't create or configure any OAuth application/client yourself** — Claude.ai registers its own at connect time, which is the entire point of Dynamic Client Registration/CIMD below.

To set that up:

1. Sign up at [workos.com](https://workos.com/) — there's nothing else to create; skip straight to the dashboard settings below.
2. Under **Connect → Configuration**, turn on **"Allow MCP clients to authenticate using Dynamic Client Registration (DCR) or Client ID Metadata Document (CIMD)"** — this is required: it's off by default, and it's what lets Claude.ai register itself as a client automatically. This same page is where you add this server's deployed URL plus `/mcp` (e.g. `https://your-service.onrender.com/mcp`) as a **Resource Indicator** — mark it as the default one.
3. Find your AuthKit domain under the **Domains** section of the dashboard sidebar (a separate section from Connect, easy to miss). In a Staging environment WorkOS auto-assigns one that looks like `https://your-tenant.authkit.app`; in a Production environment you'll instead see a **Configure AuthKit domain** button to set one up. Set `WORKOS_AUTHKIT_DOMAIN` on Render to that value.

**Staging is fine to stay on** for a personal tool like this one — don't feel pushed toward Production just because it's the other option. Per WorkOS's own [Staging vs. production environments](https://workos.com/docs/authkit/environments) docs, Staging is "fully functional and secure," and the only things gated behind Production are a custom AuthKit domain (cosmetic — `*.authkit.app` works identically otherwise) and billing details. Production's SLA/"customer-facing traffic" language in WorkOS's terms is aimed at real multi-tenant SaaS products serving external customers out of staging by mistake; it doesn't really describe a single hobbyist authenticating only themselves. The billing requirement is also moot in practice for this project either way — what actually gets charged is SSO/Directory Sync connections, features this project never touches (it only uses Connect/OAuth).

Don't try to test this by visiting the AuthKit domain directly in a browser — that's AuthKit's own generic hosted sign-in page (it'll ask you to log in and then complain about a missing sign-in callback), a completely different, unrelated flow that really does expect a manually-configured application. It has nothing to do with the MCP/DCR setup above. The real test is connecting from Claude.ai (below): Claude.ai itself constructs the correct authorization request, with its own dynamically-registered client id and redirect URI, so there's nothing to configure or test by hand first.

With both in place, set `MCP_TRANSPORT=streamable-http` (already in `render.yaml`) and deploy. `MCP_PUBLIC_URL` doesn't need setting explicitly on Render — it's derived from `RENDER_EXTERNAL_URL`, which Render provides automatically.

Local development is entirely unaffected by any of this: `python server.py` with no `MCP_TRANSPORT` set (the default) still runs over stdio, and none of `WORKOS_AUTHKIT_DOMAIN`/`MCP_PUBLIC_URL` is read in that case.

### Connecting from Claude.ai

Once deployed, add it under claude.ai's **Settings → Connectors → Add custom connector**, using your server's URL plus `/mcp` (e.g. `https://your-service.onrender.com/mcp`). Claude.ai takes it from there — discovering that the server requires auth, finding your WorkOS AuthKit project, registering itself as a client, and walking you through the login/consent flow — before it can call any tool.

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

# Set one or more properties on an existing event
python calendar_cli.py update_properties <event-id> priority=1 location="Room A"

# Move/resize an existing event, reallocating time from its day as needed
python calendar_cli.py update <event-id> start=2026-01-01T09:30:00-05:00 end=2026-01-01T10:00:00-05:00

# Create a new event, reallocating time from its day as needed
python calendar_cli.py create summary="Focus block" start=2026-01-01T09:00:00-05:00 end=2026-01-01T10:00:00-05:00 priority=1

# Delete an event by id
python calendar_cli.py delete <event-id>

# List this calendar's custom event labels, as Calendar itself stores
# them (no priority -- see create_label/sync_labels below)
python calendar_cli.py list_raw_labels

# Create a new event label -- background_color is required
python calendar_cli.py create_raw_label background_color="#8e24aa" name="Design Work"

# Update an existing event label's color and/or name
python calendar_cli.py update_raw_label <label-id> background_color="#d50000"

# Delete an event label by id
python calendar_cli.py delete_raw_label <label-id>

# Create a new event label (also prints every current label, with
# priority filled in from the event label sheet -- there's no separate
# "list" command; sync_labels below works too, with no changes to apply)
python calendar_cli.py create_label background_color="#8e24aa" name="Design Work"

# Create a label from a priority alone -- background_color is derived from it
python calendar_cli.py create_label name="Design Work" priority=1

# Update an existing event label's color, name, and/or priority
python calendar_cli.py update_label <label-id> background_color="#d50000"

# Sync event labels from this calendar's tracked event label sheet --
# blank-ID rows create labels, matching-ID rows overwrite them, and
# missing rows delete them (see create_calendar.py for creating the sheet)
python calendar_cli.py sync_labels
```

`from`/`to` are each a duration relative to *now* — parsed with [pytimeparse](https://pypi.org/project/pytimeparse/) (e.g. `"1h"`, `"90m"`, `"2d"`, `"1:30"`) — giving a window from `now - from` to `now + to`. Both are optional and default to `1h`.

`update_properties` takes one or more `key=value` pairs, where each `key` is an `Event` attribute (`summary`, `start`, `end`, `description`, `location`, `min_duration`, `is_fixed_duration`, `priority` — not `id`, since changing it would repoint the patch at a different event). It builds an `Event` with just those attributes set (everything else `None`) and patches it straight in, without fetching the event first — Calendar's `patch` semantics mean any attribute you don't mention is left exactly as it was server-side. `start`/`end` take an ISO 8601 datetime with a UTC offset (e.g. `2026-01-01T09:00:00-05:00`, or a trailing `Z`); `min_duration` takes a pytimeparse duration like `from`/`to` above; `is_fixed_duration` takes `true`/`false` (also `1`/`0`, `yes`/`no`).

`update` takes the same `key=value` pairs as `update_properties` (at least one of `start`/`end` is required this time — whichever is omitted is kept as the event's current value) and calls `ReallocatingCalendar.update_event` — the same reallocation-aware path `server.py`'s `update_event` MCP tool uses (see [MCP tools](#mcp-tools) above) — reallocating time from the rest of the event's day as needed to make room for its new position. Prints every event the call created or changed, not just the moved one. Use `update_properties` instead for a plain patch that doesn't move the event (e.g. just renaming it) — `update` always needs a real position to make room for, since that's what reallocation acts on.

`create` takes the same `key=value` pairs as `update_properties` (`summary`, `start`, and `end` are required this time) and builds a new `Event` from them, then calls `ReallocatingCalendar.create_event` — the same reallocation-aware creation path `server.py`'s `create_event` MCP tool uses (see [MCP tools](#mcp-tools) above). Prints every event the call created or changed, not just the new one.

For a recurring event, the id from `list`/`get` names one specific *instance*.  To change a property for the entire series of recurring events, use the `recurring_event_id` that's visible from `get`.  See Google's [recurring events guide](https://developers.google.com/workspace/calendar/api/guides/recurringevents) for more on how instances and recurring events relate.

`list_raw_labels`/`create_raw_label`/`update_raw_label`/`delete_raw_label` manage this calendar's custom event labels exactly as Google Calendar's API represents them (`CalendarClient.list_event_labels`/`create_event_label`/`update_event_label`/`delete_event_label`, `calendar_clients/google_calendar.py`'s `EventLabel`) — a richer, arbitrary-hex-color alternative to `Event.colorId`'s 11 fixed colors, but with no concept of priority: Calendar itself has no field for one, so `create_raw_label`/`update_raw_label` take only `background_color=value`/`name=value` pairs, and `background_color` is required for `create_raw_label`. Prefer `create_label`/`update_label`/`sync_labels` below unless you specifically want to bypass priority-derived colors and the event label sheet.

`create_label`/`update_label` take the same kind of `key=value` pairs as `update_properties`, plus `priority` (`background_color`/`name`/`priority`). Along with `sync_labels` below, they manage the same labels as `utilities/event_labels.py`'s richer `EventLabel` (via `EventLabels`), and all three print the *entire* resulting label list, not just the one label touched — see [MCP tools](#mcp-tools) and [Event label sheets](#event-label-sheets) above for how `priority`/`background_color` interact, why `priority` doesn't stick around on its own, and why there's no dedicated `list_labels` command. `update_label` requires an existing `label_id`. There's no `delete_label` either — delete a row from the event label sheet directly, then run `sync_labels`. See Google's [event labels guide](https://developers.google.com/workspace/calendar/api/guides/labels).

`sync_labels` always syncs from this calendar's tracked event label sheet and prints the resulting labels — see [Event label sheets](#event-label-sheets) above for how that sheet comes to exist in the first place.

## Running tests

With the dev dependencies installed:

```bash
pytest
```
