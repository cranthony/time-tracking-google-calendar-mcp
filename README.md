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
- [`calendar_clients/google_calendar.py`](calendar_clients/google_calendar.py) — Google Calendar API access, behind a `CalendarClient` class and plain `Event`/`Calendar`/`EventLabel` dataclasses. Kept separate from `server.py` so the Calendar logic can be unit tested without hitting the real API — tests construct a `CalendarClient` around a mocked `service` object instead. `CalendarClient` is a thin, pure API wrapper (list/get/create/update/delete, plus event-label management); its `EventLabel` is a *raw* label, exactly as Calendar represents one — labels are managed through goals (see [Goals](#goals) below).
- [`calendar_clients/google_sheets.py`](calendar_clients/google_sheets.py) — Google Sheets API access, behind a `SheetsClient` class — the same thin-wrapper role as `CalendarClient`, but for spreadsheets: create one, add/rename/color a tab, read/write rows (optionally addressed to a tab by its sheetId, so there's no title lookup to spend a read request on), narrow a column, tag a tab with developer metadata and find it again by that tag. Never touches the Drive API directly — a spreadsheet's id is looked up via `CalendarClient.get_calendar_metadata` instead (see [Calendar metadata sheets](#calendar-metadata-sheets) below). Knows nothing about goals, time notes, or any other meaning attached to a tab. Every request retries with backoff (for up to about a minute) when Sheets answers 429 — its read quota is only 60 requests per minute per user — and on nothing else, since some requests aren't safe to repeat. `server.py` also wraps every MCP tool call in `cached_sheet_reads()`, so a tab read more than once in the same call (a compaction step reads the notes tab and the journal several times) costs one request, not several; any write to that tab forgets it, and nothing is kept between calls, so edits you make in the spreadsheet by hand are always seen by the next call.
- [`utilities/reallocation.py`](utilities/reallocation.py) — the reallocation algorithm: how creating an event makes room for itself by reclaiming time from lower-priority events and free time in its day. Has no Calendar API dependency of its own — see the module docstring.
- [`utilities/reallocating_calendar.py`](utilities/reallocating_calendar.py) — the glue between the two above: `ReallocatingCalendar` wraps a `CalendarClient`-shaped calendar (see `goal_calendar.py` below) with reallocation-aware `create_event`/`update_event`, the shared entry point both `server.py` and `calendar_cli.py` use.
- [`utilities/goal_periods.py`](utilities/goal_periods.py) — periods of days, one kind per cadence, and their ids (`2026-09-30`, `week-2026-09-27`, `2026-09`, `2026-09..10`). Pure date arithmetic. Goals are only rated daily, so only daily periods are used today.
- [`utilities/goal_measures.py`](utilities/goal_measures.py) — the kinds of measure a goal can have, and the checks a measure passes before it's saved. See [Goal health](#goal-health) below.
- [`utilities/health_days.py`](utilities/health_days.py) — how a day's assessments and reflection are kept together in one Goal Health event (or a few, if they don't fit in one), and the `Assessment` they're made of.
- [`utilities/health_summary.py`](utilities/health_summary.py) — a day's goal health in brief, a line per goal grouped by priority: the reflection's summary, and the description of the day's Goal Health event.
- [`utilities/goal_health.py`](utilities/goal_health.py) — `GoalHealth`: recording and reading goals' daily assessments on the Goal Health calendar, measuring goals from the main calendar, and keeping the goals tab's health columns up to date. See [Goal health](#goal-health) below.
- [`utilities/goal_time.py`](utilities/goal_time.py) — how many minutes went toward each goal (its sub-goals' included) within a window of time: each goal's last 24 hours and 7 days in `get_goals`, and a reflected day's time per goal.
- [`utilities/reflection.py`](utilities/reflection.py) — `Reflections`: preparing and recording a reflection, the conversation that confirms a period's ratings. See [Reflections](#reflections) below.
- [`utilities/goal_calendar.py`](utilities/goal_calendar.py) — `GoalCalendar` wraps a `CalendarClient` with a `Goals`: on read (`list_events`/`get_event`) it fills in an event's goals (from its label, for an event written before goals existed) and the `goal_priority`/`goal_is_fixed_time` it inherits from them; on write it derives the event's label from its goals. The event's own `priority`/`is_fixed_time` are never touched. `ReallocatingCalendar` is built on top of this, and reallocation reads only `Event.effective_priority`/`effective_is_fixed_time` (the event's own value, falling back to its goal's) — see [Goals](#goals) below.
- [`utilities/calendar_metadata_sheet.py`](utilities/calendar_metadata_sheet.py) — owns the *spreadsheet* a calendar's Sheet-backed data (goals, uncompacted time notes, ...) lives in: `ensure_spreadsheet` finds or creates it, `ensure_tab` finds or creates/tags one of its tabs by role, and `create_tab` creates one but only tags it once its first rows are written. See [Calendar metadata sheets](#calendar-metadata-sheets) below.
- [`utilities/goal_sheet.py`](utilities/goal_sheet.py) — a thin, per-tab API: `GoalSheet.find`/`create` (the goals tab of a given calendar metadata spreadsheet) and, bound to it, `read`/`write` its rows as this module's own `Goal` dataclass. Columns are matched by header, so ones it doesn't know are kept as they are.
- [`utilities/goals.py`](utilities/goals.py) — `Goals`, the application-level policy that ties a `CalendarClient` to *its* goals tab: creating and updating goals, keeping the calendar's event labels in sync with the active ones, and migrating a calendar's event labels into goals the first time. `GoalTree` answers the read-only questions (a goal's path, what it inherits, which label an event gets). See [Goals](#goals) below.
- [`utilities/noted_time_sheet.py`](utilities/noted_time_sheet.py) — a thin, per-tab API mirroring `goal_sheet.py`: `NotedTimeSheet.ensure` (the noted-times tab of a given calendar metadata spreadsheet, via `calendar_metadata_sheet.ensure_tab`) and, bound to one already-known tab, `read`/`read_with_rows`/`append`/`mark_compacted`/`garbage_collect` its rows as this module's own `NotedTime` dataclass (`timestamp`/`description`/`compaction_id`). Compacting a note stamps its `compaction_id` rather than deleting it; a note's row, together with its timestamp, forms its id, stable unless `garbage_collect` shifts it (see [Compacting notes](#compacting-notes) below). Unlike a goal, a noted time has no Calendar API counterpart to reconcile with, so there's no `Goals`-equivalent layer above this one. See [Calendar metadata sheets](#calendar-metadata-sheets) below.
- [`utilities/note_compaction.py`](utilities/note_compaction.py) — `plan_compaction`, a pure function (no API access) that turns a day's notes, plus a model's decisions about which events they show happened differently, into the calendar changes that realign the day to them. See [Compacting notes](#compacting-notes) below.
- [`utilities/compaction_timeline.py`](utilities/compaction_timeline.py) — `Timeline`, the view of a compaction (the day's notes and its events in time order), as data and as narrow fixed-width text.
- [`utilities/compaction_journal.py`](utilities/compaction_journal.py) — `CompactionJournal`, the write-ahead journal tab that records each approved compaction and its progress, so a failed one can be resumed exactly.
- [`utilities/compaction_marker.py`](utilities/compaction_marker.py) — `CompactionMarker`, the bright red, 5-minute event in Google Calendar that ends at the last compaction. See [Compacting notes](#compacting-notes) below.
- [`utilities/note_compactor.py`](utilities/note_compactor.py) — `NoteCompactor`, which ties the notes tab, the calendar, the planner and the journal together behind the `prepare_compaction`/`compact_notes`/`abandon_compaction` tools.
- [`utilities/memory_diagnostics.py`](utilities/memory_diagnostics.py) — `track(label)`, a context manager wrapping an operation (every MCP tool, `load_credentials`) to log RSS, `tracemalloc` growth, and `objgraph` object-count growth since the previously tracked operation — for narrowing down what's driving this process's memory usage. `tracemalloc` itself is only traced while RSS is at or above half of this process's own memory limit (read from the `MEMORY_LIMIT_BYTES` environment variable if set, else from the cgroup enforcing it, `memory.max`/`memory.limit_in_bytes`) -- started the moment it first crosses that threshold, stopped the moment it first drops back below (no hysteresis, and always logged one last time right before stopping) -- so its overhead is only paid while things are actually near an OOM kill.
- [`server.py`](server.py) — the MCP server; its tools call into `calendar_clients/google_calendar.py` rather than talking to `googleapiclient`/OAuth directly.
- [`workos_auth.py`](workos_auth.py) — verifies bearer tokens issued by WorkOS AuthKit, for when `server.py` is hosted remotely over `streamable-http` instead of run locally over stdio — see [Deploying](#deploying) below.
- [`create_calendar.py`](create_calendar.py) — a standalone bootstrap script (not an MCP tool) that creates the dedicated calendar this app needs, and its calendar metadata spreadsheet (goals and uncompacted time notes tabs both provisioned) — see [Calendar access model](#calendar-access-model) below.
- [`calendar_cli.py`](calendar_cli.py) — a dev-only command-line tool for poking at the calendar directly (`list`/`get`/`update_properties`/`create`) without going through an MCP host — see [Command-line utilities](#command-line-utilities) below.
- [`config.py`](config.py) — reads configuration from environment variables — see [Configuration](#configuration) below.
- [`render.yaml`](render.yaml) — a Render Blueprint for hosting this remotely — see [Deploying](#deploying) below.
- [`tests/`](tests/) — unit tests for the above, mocking the Google API (and WorkOS's JWKS) rather than hitting them.

## Calendar access model

This app requests only the `calendar.app.created` OAuth scope (see `SCOPES` in [`calendar_clients/google_auth.py`](calendar_clients/google_auth.py)) — not the broader `calendar.events`/`calendar.events.owned` scopes. That has real consequences:

- This app can only read and write events on calendars **it has created itself**.
- It has **no access to the user's existing calendars** — not `"primary"`, not any calendar they made by hand in the Calendar UI. API calls against any calendar this app didn't create itself will fail.
- This is deliberate: a compromised or misbehaving instance of this app cannot read or touch anything outside the dedicated calendar(s) it made for itself.

`SCOPES` also includes `drive.file`, for the [calendar metadata sheet](#calendar-metadata-sheets) — the same "can't touch anything it didn't make" model as `calendar.app.created`, but for Google Sheets: this app can only see/create spreadsheets it made itself, never the user's other Drive files. It's required by the Sheets API's own `spreadsheets.create` even though this app never calls the Drive API directly — a spreadsheet's id is looked up from the calendar itself, not by searching Drive (see [Calendar metadata sheets](#calendar-metadata-sheets)). If you're upgrading an install whose `token.json` predates this scope, delete it and let the consent flow run again (see [Google OAuth credentials](#google-oauth-credentials) below) — the old token won't carry the new scope.

**Bootstrapping:** there's no calendar to operate on until this app creates one. Run [`create_calendar.py`](create_calendar.py) once — it only needs `GOOGLE_OAUTH_CREDENTIALS_PATH`/`GOOGLE_OAUTH_TOKEN_PATH` (see [Configuration](#configuration) below), not `GOOGLE_CALENDAR_ID` — and it prints the new calendar's ID, then ensures that calendar's metadata spreadsheet (goals and uncompacted time notes tabs both provisioned) in the same step (see [Calendar metadata sheets](#calendar-metadata-sheets) below) and prints its URL too:

```bash
python create_calendar.py "Time Tracking"
```

Then set `GOOGLE_CALENDAR_ID` to the ID it prints. If the app hasn't been used to create a calendar yet — including the very first time you set this up — this is the step to run first.

If `GOOGLE_CALENDAR_ID` is *already* set when you run it, `create_calendar.py` doesn't create a new calendar at all — it just ensures the metadata spreadsheet and its tabs for that already-configured calendar instead (useful if you set this app up before the spreadsheet, or one of its tabs, existed). Either way, provisioning the spreadsheet/tabs themselves is a one-time, human-run bootstrap step just like the calendar itself — there's deliberately no MCP tool or CLI command for *that* (though there is for adding data to them once they exist — see [MCP tools](#mcp-tools) and [Calendar metadata sheets](#calendar-metadata-sheets) below).

## MCP tools

[`server.py`](server.py) exposes the calendar as MCP tools:

| Tool | Signature |
| --- | --- |
| `list_events` | `(min_time, max_time) -> list[PublicEvent]` |
| `get_event` | `(id) -> PublicEvent` |
| `update_event` | `(event: PublicEvent) -> list[PublicEvent]` |
| `create_event` | `(event: PublicEvent) -> list[PublicEvent]` |
| `delete_event` | `(id) -> list[PublicEvent]` |
| `get_goals` | `(statuses: list[GoalStatus] \| None) -> GoalList` |
| `create_goal` | `(goal: Goal) -> CreatedGoal` |
| `update_goal` | `(goal: Goal, clear_fields: list[GoalField] \| None) -> GoalList` |
| `sync_goals_from_sheet` | `() -> GoalList` |
| `measure_goals` | `(day: date \| None, goal_ids: list[str] \| None) -> list[Assessment]` |
| `record_assessments` | `(assessments: list[Assessment]) -> list[Assessment]` |
| `get_goal_history` | `(goal_ids: list[str], start: date \| None, end: date \| None) -> list[Assessment]` |
| `rebuild_goal_health_cache` | `() -> GoalList` |
| `prepare_reflection` | `(day: date \| None) -> ReflectionContext` |
| `record_reflection` | `(day: date, assessments: list[Assessment], proposed: list[str] \| None, dry_run: bool = True) -> ReflectionResult` |
| `get_compaction_status` | `() -> CompactionStatus` |
| `note` | `(noted_time: NotedTime) -> NoteWithId` |
| `get_notes` | `(include_compacted: bool = False) -> list[NoteWithId]` |
| `edit_note` | `(note_id: str, timestamp: datetime \| None, description: str \| None) -> NoteWithId` |
| `delete_note` | `(note_id: str) -> NotedTime` |
| `prepare_compaction` | `() -> CompactionContext` |
| `compact_notes` | `(decisions: list[EventDecision] \| None, ignore_notes: list[str] \| None, compaction_id: str \| None, dry_run: bool = True) -> CompactionResult` |
| `abandon_compaction` | `(compaction_id: str) -> CompactionResult` |
| `set_time_zone` | `(time_zone: str) -> str` |

`update_event`/`create_event`/`delete_event` all return a `list[PublicEvent]` rather than a single `PublicEvent`, since `update_event`/`create_event` can affect more than the one event acted on (see below). `delete_event` doesn't call the Calendar API's own delete — it patches the event's `status` to `"cancelled"` (via `CalendarClient.update_event`), the same way reallocation cancels an event to make room for another. This matches `Event.status`'s own documented recommendation to cancel rather than delete an instance of a recurring event, and always returns exactly that one event, wrapped in a single-element list for a consistent return type across all three.

`create_event`/`update_event` both make room for the event via `utilities/reallocating_calendar.py`'s `ReallocatingCalendar` (see [Project layout](#project-layout) above): each fetches the roughly 24 hours of events starting at the event's own `start` (via `ReallocatingCalendar.list_day_events`, truncated after the first event marked `is_end_of_day_sleep`, if any — that's "the day" `utilities/reallocation.py` operates on), then calls `reallocate_for_new_event` and applies whatever it returns (creating or moving the event itself, and updating — shrinking, moving, splitting, or cancelling — whatever else needed to make room, each via the plain `CalendarClient`). `update_event` (`ReallocatingCalendar.update_event`) additionally excludes the event's own prior position from that day's events first, since `reallocate_for_new_event` refuses a day already containing the event being moved -- and passes its id as `list_day_events`'s `ignore_id`, so truncating at the first `is_end_of_day_sleep` event doesn't key off the event's own not-yet-applied prior position when it's itself that day's sleep block (e.g. stretching a morning sleep block later). Its `PublicEvent` needs at least one of `start`/`end` set (not necessarily both) — whichever is left `None` is filled in from the event's current value before reallocating, reusing that same `list_day_events` fetch rather than a second one: if the event is already in it, its current value is read from there; if not (e.g. the one value given put it on a different day than its prior position), a direct `get_event` gets it instead. A `ValueError` from reallocation is surfaced as a `ToolError`.

Every event tool uses `PublicEvent` (defined in `server.py`), not `Event`, as its input/output type — `Event` minus whatever fields are named in `INTERNAL_EVENT_FIELDS`, plus `is_cancelled` (which has no `Event` equivalent — it's derived from the hidden `status` field). `is_end_of_day_sleep` and `recurring_event_id` are visible but read-only, like the `effective_*` fields below: `PublicEvent.to_event` ignores them, so `update_event`/`create_event` can't change them. `recurring_event_id` is assigned by Google, and `is_end_of_day_sleep` decides where reallocation's day ends, so a wrong mark would quietly change what later updates shrink, move or cancel; it's set by hand with `calendar_cli.py update_properties`. Agents communicating with this MCP only see the fields in `PublicEvent`. `calendar_cli.py` still operates on `Event` directly and has full access to every field, since it's a human-run dev tool, not something the agent talks to.

**`is_fixed_time`** marks an event whose `start`/`end` must never change, not just its duration (`is_fixed_duration`) — the same `min_duration`-equals-own-duration treatment applies (see `Event.is_fixed_time`'s own docstring), so a fixed-time event is never shrunk during reallocation either. Unlike every other field, `reallocate_for_new_event` (`utilities/reallocation.py`) actively *enforces* it across a whole reallocation: if displacing a fixed-time event turns out to be unavoidable in a single pass (e.g. it was the only thing standing in a new event's way), the algorithm detects that afterward and re-runs itself to put it straight back at its exact original position — reclaiming whatever's now there instead, which may itself displace *another* fixed-time event, repairing in turn, until everything settles (or a genuine conflict between two fixed-time events raises a `ValueError`). See that module's "Fixed time" section for the full mechanism. A cancelled fixed-time event is the one outcome that's never undone.

Every event tool's `PublicEvent`s also carry two read-only fields, `effective_priority` and `effective_is_fixed_time`: the values reallocation actually uses, i.e. `Event`'s properties of the same name: the event's own `priority`/`is_fixed_time`, falling back to its primary goal's (or that goal's nearest ancestor's) when it has none of its own (filled in via `utilities/goal_calendar.py`'s `fill_in_from_goals` — see [Goals](#goals) below). `priority`/`is_fixed_time` themselves stay the event's own values, and `update_event`/`create_event` ignore the `effective_*` fields, so sending a listed event straight back to `update_event` never copies its goal's values onto the event — which would otherwise stop it following later changes to its goal.

`PublicEvent.goal_ids` are the goals an event serves, primary goal first; it's how `create_event`/`update_event` tie an event to goals (`[]` removes them), and ids that aren't goals are refused with the closest matches suggested. `goal_names` (their names, in the same order) and `event_label_id` (derived from the primary goal) are read-only.

`list_events`/`get_event` still don't surface a cancelled event at all: `list_events` omits it, and `get_event` raises a `ToolError`. But when an operation like `create_event` cancels an event as a side effect of making room, or `delete_event` cancels the event it was asked to remove, that cancellation is a direct result of the agent's own action, so it's worth surfacing rather than hiding — its `PublicEvent` comes back with the rest of its fields intact and `is_cancelled=True`. `is_cancelled` only ever moves from `False` to `True`; setting it `False` has no effect, since there's no way to un-cancel an event through this API.

Calendar creation is deliberately *not* an MCP tool — see [Calendar access model](#calendar-access-model) above — so the model can't create new calendars on its own; that's a one-time, human-run bootstrap step via `create_calendar.py`.

`get_goals`/`create_goal`/`update_goal`/`sync_goals_from_sheet` manage this calendar's goals — see [Goals](#goals) below. Each returns a `GoalList`: the goals with the statuses asked for (by default proposed, active and inactive), parents before children, each with its `path` from the top of the tree, plus how many of the calendar's 200 event labels are in use; `create_goal`'s also gives the new goal's id, as `created_id`. `update_goal` sets whichever fields are given and blanks those named in `clear_fields`; `id` and `label_id` are assigned and never change. A goal's `status` is one of `proposed` (suggested, not taken on yet), `active` (being worked on), `inactive` (paused), `completed` (achieved), `archived` (no longer relevant) or `deleted` (shouldn't have existed: no event can be given it, though events that already have it keep it). There's no `delete_goal`: setting `deleted` keeps the goal's history and its events' links to it.

The `note` tool records a new time note (`utilities/noted_time_sheet.py`'s `NotedTime`: a required `timestamp`, and an optional free-text `description` of what it marks) by appending it to this calendar's noted-times tab (via `NotedTimeSheet.append`, which writes only the new row — see [Calendar metadata sheets](#calendar-metadata-sheets) below), returning the note as recorded along with its id (`NoteWithId`: the note's timestamp and sheet row together, e.g. `2026-01-01T09:05:00+00:00#5`). A caller can't set a note's `compaction_id`; only compaction does. `get_notes` lists the notes that haven't been compacted yet, sorted by timestamp and each with its id (or all of them with `include_compacted`).

`edit_note` corrects an uncompacted note by id — a new `timestamp` and/or `description` (whichever is left out keeps its value; an empty `description` clears it) — and returns it with its id, which changes if its timestamp did. `delete_note` removes one, by blanking its row rather than deleting it, so no other note's id changes. Both refuse a stale id (the note was edited or removed since it was listed), an already-compacted note (change the calendar event it became instead), and a note in a compaction that's partway through being applied — that compaction stamps its notes last, so changing one midway would get the changed note stamped. A dry-run plan that included the note can't be committed afterward (committing re-checks the notes); just run a new dry run. Notes are otherwise turned into calendar changes by compacting them — see [Compacting notes](#compacting-notes) below.

Every label write reads the full label list, changes it in memory, and writes the whole thing back (the API has no way to touch a single label in place), which is a lost-update race if two callers do this concurrently. `CalendarClient.replace_event_labels` (what `Goals` builds every label change on) guards against that with the calendar's own `etag`: it's sent back as an `If-Match` precondition on the write, so a write based on a label list that's since changed fails with `EventLabelConflictError` (surfaced as a `ToolError`) instead of silently overwriting the other change. Confirmed empirically against the real API, since Google's docs only document `If-Match` for Events, not Calendars.

There's deliberately no MCP tool or CLI command that creates the goals tab — see [Goals](#goals) below for how it comes to exist.

### Why tools, not resources

MCP has both **tools** (model-controlled: the model decides when to call one, with whatever arguments it computes, as part of its own reasoning) and **resources** (application-controlled: a human typically browses and attaches one via the host's UI, like Claude Desktop's "Attach from MCP" picker). This server only uses tools, for two reasons:

- **Portability.** Resources depend on the host having built UI (or another bridging mechanism) for the model to reach them at all; a lot of MCP clients — agentic ones especially — only implement tool-calling and skip resources entirely. Tools work everywhere.
- **Fit.** The natural workflow here is model-driven, not human-browsing-driven: the model discovers an event's ID via `list_events`, then immediately wants to act on it — fetch details, update, delete. That's a tool-calling pattern (one tool's output, the `id` field, feeds directly into the next tool's input) with no need for a resource-URI layer in between. Nobody is going to browse a picker UI for an event by its opaque Google Calendar ID.

## Compacting notes

**The last compaction is marked in Google Calendar**, as one bright red (Tomato) event, 5 minutes long, ending when it ran. It's on a small calendar of its own, **Compactions** (created the first time, in the main calendar's time zone and colored red; its id is kept on the main calendar), so nothing that reads events — listing, compacting, reallocating, counting goals' time, reflecting — ever sees it, and the main calendar's events never overlap it. There's only ever one: it has a fixed id, so each compaction moves it (restoring it if it was deleted by hand), and each move deletes anything else on that calendar. Moving it is best effort: a failure is a warning on the compaction's result, never a failed compaction, and the next one (or committing again) puts it right.

Notes are jotted down as things happen ("leaving for salsa early to prep", "done with dinner"), and they're sparse: you note what's notable, not every event boundary. Compacting realigns the day's planned events to those notes: the past becomes fact, and the future reflows around it. **Silence means on schedule.** A past event that no note contradicts is recorded exactly as planned. A missing start or end note never makes an event disappear or merge into its neighbor.

Reading free-form text is a job for a model, and the MCP client already is one — so the server has no LLM of its own. Instead the work is split. The client compares the notes to the plan and decides what changed; the server validates those decisions and does everything risky deterministically, previewably, and recoverably.

**The flow** (three tools):

1. **`prepare_compaction`** (read-only) returns one day of uncompacted notes — each with an id that is its timestamp and sheet row together (`2026-01-01T09:05:00+00:00#5`) and a shortlist of nearby planned events as `candidates` — plus that day's planned events, a **`timeline`** showing the two side by side (below), and instructions for the next step. A backlog spanning several days takes one round per day, oldest first.
2. **`compact_notes(decisions)`** takes the model's decisions: one `EventDecision` per event the notes show happened differently. It validates them, plans the changes, writes the plan to the journal, and returns it with a `compaction_id` and the resulting `timeline` — **without touching the calendar**. The model shows you the timeline.
3. **`compact_notes(compaction_id=..., dry_run=False)`** applies it, once you've agreed.

**Which events are offered: the compaction window.** The day is the one the oldest uncompacted note falls in. It starts when the last end-of-day sleep event that began before that note ends, or at the note itself if it was written before then (you woke up early). It runs to the end of the next end-of-day sleep event. A day can take several compactions, so the events offered — the *compaction window* — start at whichever is later: the day's start, or the last stamped compaction's `now`. Whatever an earlier compaction already settled isn't offered again. The one event that ended within 15 minutes before the compaction window starts is offered too, so an event the last compaction closed off ("done reading", or last night's sleep) can still be stretched. The latest compacted note, however long ago it was written, comes along as `previous_note` — context only, for what was going on as the window began — and the timeline shows it too, beside when the last compaction ran.

**Decisions** (`EventDecision.action`):

- `keep` (an `event_id`): it happened. Each edge stays where it was planned unless the decision moves it: `start_note`/`end_note` (a note id) sets that edge to the note's time and links the note to it; `start`/`end` gives an explicit time (alongside a note, when the note gives a time relative to itself — "leaving 15 minutes early"). `keep` also takes `summary` (rename it) and `annotate` (add text to its description). A `keep` that moves a *future* event is a direct reschedule — "move lunch to 12:15 and adjust the afternoon" — pinned there, with the rest of the day reflowing around it in the same plan.
- `cancel` (an `event_id`): it didn't happen.
- `create` (`summary`, a start and an end — each a time or a note — and optionally `event_label_id`): something unplanned happened.
- `merge` (an `event_id`, `into` another): fold one event into another, for when you don't remember where one ended and the next began. The target grows to cover both and is titled after both (unless renamed); the merged one is cancelled. The model is told to do this only after you've confirmed it — never just because the notes are sparse.

**Goals.** `keep` and `create` take `goal_ids` (primary first) to set the goals an event serves; for `keep`, leaving them out keeps them, and `[]` clears them. A goals-only change is an ordinary update, and the event's label follows its goals when it's written. `prepare_compaction` lists the active goals, each event's goals, and, for a past event serving none, `suggested_goal_ids`: those of the latest event with the same title in the previous 4 weeks. Recurring events need nothing extra, since an instance's goals come with its series. The model is told to apply suggestions without asking, to set goals on a `create` when the notes clearly imply them, and to ask only when torn between two. The timeline marks each event's goals after its title — `◆` for one it already serves, `◇` for one it's being given (or, before deciding, one suggested) — and ends with each goal's total time. Unknown goals, and deleted ones an event doesn't already have, are refused at the dry run.

Notes that don't set an edge have their text added to the description of the event they fall within (with their time), so they survive compaction. `compact_notes`'s `ignore_notes` lists any that shouldn't be added. Google Calendar silently cuts a description longer than 8,192 characters (`MAX_DESCRIPTION_BYTES`, counted as UTF-8 bytes to be safe), so a plan that would grow one past that is rejected, naming the notes to leave out.

**What happens to the rest of the calendar** (all in `utilities/note_compaction.py`'s `plan_compaction`). Every resulting past event — decided, or untouched and so on schedule — is pinned (`is_fixed_time`), so no later reallocation moves history. **Past events can't overlap.** If dinner's noted end runs into the reading planned after it, the plan is rejected and the error names both events. The model then has to say which edge gives way, asking you if the notes don't tell it. End-of-day sleep events count too: a note can't silently eat into last night's sleep. Everything else — the future, and anything still in progress — reflows around the facts using `utilities/reallocation.py`, simulated in memory so the plan can be previewed.

**Moving bedtime** (the day's own end-of-day sleep event) works differently, since nothing in the day comes after it for the rest to reflow into: it moves where the day ends. A later bedtime just leaves the evening free; an earlier one shortens whatever runs past it to end there and cancels what starts after it (or can't be shortened that far), and a fixed-time event in the way is an error. Its end — your wake-up time — is the start of the *next* day, which compacting this one never adjusts: moving only its start changes only the bedtime, and if its end does move, the plan carries a warning to check the next morning's events (and move them with `update_event` if they now overlap).

**The timeline** (`utilities/compaction_timeline.py`) is the notes and events in time order, so you can see which note moved which event and which notes just describe what was going on. It's plain data — each note with the edges it set or the event it was added to, each event with its planned and resulting times and a status (`on_schedule`, `adjusted`, `reflowed`, `new`, `cancelled`, `merged`, `planned`) — for a client that can draw it, plus `text`, a fixed-width rendering for one that can't — one column, 40 characters wide so it reads on a phone, each moment's notes followed by the event edges at it:

```
17:00 ┌ Work
18:15 ● Leaving for salsa early to prep
     →├ Salsa prep · new
18:30 ├ Google Salsa class
19:00 ● Learned the cross-body lead
        ↳ Google Salsa class
19:30 ├ Dinner
20:10 ● Done with dinner
     →└ Dinner ends · +10m (was 20:00)
     →├ Reading · +10m (was 20:00)
20:30 ┄┄ now ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
21:00 └ Reading ends
```

`→` marks an edge the note just above it set, and `↳` the event a note's text was added to. A past event with no tag happened as planned; `+`/`−` is how late or early an edge was, and `⇢`/`⇠` how far a whole event moved. Each event's goals go on the line under it, each goal's total time follows the events, and a short legend ends it. Long lines wrap, indented under their text.

**Safety.** Before anything is applied, the whole approved plan — the decisions, and every step with its before-state — is written to the **Compactions** tab (see [Calendar metadata sheets](#calendar-metadata-sheets)). Committing first checks the notes and calendar still match what was previewed, then applies each step and checks it off; a failure partway leaves the journal at exactly that point, and calling `compact_notes` with the same `compaction_id` again finishes only what's left. Created events get deterministic ids (Calendar accepts a caller-chosen id), so a retried create can't duplicate. The notes are stamped compacted only after everything applied, so nothing is lost if it dies. While a compaction is unfinished a new one is refused until it's resumed or `abandon_compaction`ed (steps already applied stay applied).

**Rejecting or changing a plan.** Nothing touches the calendar until the commit, so a plan you don't like costs nothing: tell the model what's wrong, it corrects the decisions, and it calls `compact_notes` again with them — no need to call `prepare_compaction` again unless the notes or calendar changed. Each new dry run replaces the earlier unapplied plans (they're marked `abandoned`), so a stale one can't be committed by mistake. Once a compaction has been applied, its events are ordinary events: adjust them with `update_event` like any other.

There's no CLI command for this: comparing the notes to the plan needs a model, which is what the MCP client is.

## Calendar metadata sheets

Each calendar this app manages has one shared **calendar metadata spreadsheet** ([`utilities/calendar_metadata_sheet.py`](utilities/calendar_metadata_sheet.py)), titled "Calendar Metadata" — one tab per kind of Sheet-backed data this app keeps for that calendar. Today that's:

- **Goals** — one row per goal, active or not: **id** and **label_id** (both narrow — nobody's expected to care what they are, just that they're there), **parent_id**, **name**, **status** (`proposed`, `active`, `inactive`, `completed`, `archived` or `deleted`), **background_color**, **priority**, **fixed_time**, **measure** (JSON) and **note**, then three columns the app keeps up to date itself — **health**, **health_period** and **health_trend** (see [Goal health](#goal-health) below), added the first time they're needed. Columns are matched by header name, so you can reorder them or add your own. See [Goals](#goals) below. Columns earlier versions wrote and nothing uses any more — **target**, **deadline**, **cadence** (goals are all rated daily now), the **active** (`TRUE`/`FALSE`) column **status** replaced, and **created** — are removed the next time the tab is written, shifting the columns after them left.
- **Noted Times** — time notes: three columns, **Timestamp**, **Description** (optional) and **Compaction ID** (blank until the note has been compacted; see [Compacting notes](#compacting-notes)). A note's id is its timestamp and row together, and stamping a note verifies its row still holds that timestamp — which also catches a row shifted by garbage collection (below), not just a hand edit.
- **Compactions** — the write-ahead journal of note compactions: one row per fact (the compaction itself, each decision, each calendar change with its before/after state and whether it's been applied). See `utilities/compaction_journal.py`.

Both the Noted Times and Compactions tabs only ever grow, so each keeps itself under a row budget (50 for notes, 100 for compactions) by physically deleting old, no-longer-needed rows from the top once it goes over — trimming it to half that (25 and 50), so the very next append doesn't set it off again. That keeps both small enough to read whole, in one request each, every time (Google Sheets throttles read requests to 60 a minute). `NotedTimeSheet.garbage_collect` (run before `note` appends a new one) deletes already-*compacted* notes, always keeping the one with the latest timestamp (the next compaction shows it as context), and `CompactionJournal.garbage_collect` (run when `prepare_compaction` starts) deletes fully-finished compaction blocks, always preserving whichever compaction is still open or merely planned and the most recently *stamped* one (its `now` anchors the next compaction round — see [Compacting notes](#compacting-notes)). Both stop at the first row they can't safely delete, so an unusually large uncompacted backlog, or an unusually long-lived compaction, can leave a tab over budget rather than lose something still needed. Deleting rows also shrinks the tab's grid, and a write past the end of the grid fails, so whenever garbage collection leaves a tab with fewer than 1000 rows in all (`calendar_metadata_sheet.MIN_TAB_ROWS`, what Sheets gives a new tab), empty rows are added at the bottom to make up the difference.

**Finding a tab doesn't rely on its title or position.** Both the spreadsheet and each tab within it are located by a stable tag instead, so renaming a tab (or the spreadsheet itself), or reordering tabs, never breaks the app's ability to find the right one again:

- The spreadsheet's id is recorded directly on the calendar itself, via `CalendarClient.get_calendar_metadata`/`set_calendar_metadata`: since the Calendars resource has no dedicated field for arbitrary app metadata (confirmed against the API reference — unlike Events' `extendedProperties`), these embed a small `[cascading-time-tracker:key=value]` marker as its own line in the calendar's `description`, clearly delimited from whatever human-readable text is already there, and guarded by the calendar's `etag` exactly like the event-label writes below.
- Each tab is tagged with Google Sheets [developer metadata](https://developers.google.com/workspace/sheets/api/guides/metadata) — a `sheet-role` key (`goals`, `uncompacted-time-notes`, ...) attached to that tab's `sheetId`, `PROJECT`-scoped so only this app's own OAuth client can see or query it. `SheetsClient.create_sheet_metadata`/`find_sheet_id` are the low-level calls; `calendar_metadata_sheet.ensure_tab` is what everything else uses — find the tagged tab, or create (and tag) a new one if there isn't one yet.

**A tab's title and color are purely a visible cue for you, not how the app finds it again.** Every tab this app manages gets a human-readable title and the same tab color, set once at creation, so you can tell at a glance which tabs it's using when you open the spreadsheet — but since the actual lookup is by tag, you're free to rename a tab (or the spreadsheet) afterwards without breaking anything.

## Goals

Goals replaced event labels: a goal is something you're working toward ("Host friends weekly"), and goals form a tree ("Learn vegetarian cooking" → "Learn Tofu Tikka Masala"). See [`docs/goals-design.md`](docs/goals-design.md) for the full design, including the health assessments and reflections still to come.

- **Each goal reserves one event label, and holds it exactly while it's active.** Every change to the goals makes the calendar's labels the active goals' — each named after its goal, colored with its own `background_color`, else its nearest ancestor's, else (as a last resort) its priority's color, its own or inherited; listed goals carry that as `effective_color` — plus any unnamed labels, which are Calendar's own default colors and always left alone; any other label is removed. A goal's status is one of `proposed` (suggested, not taken on yet), `active` (being worked on), `inactive` (paused), `completed` (achieved), `archived` (no longer relevant) or `deleted` (shouldn't have existed: no event can be given it, though events that already have it keep it); any but active frees its label (a calendar holds at most 200) but keeps its history, and making it active again re-adds the label under the same id, which the goal's past events still carry, so their color comes back. A change that would need more than 200 labels is refused before anything is written.
- **Events serve goals.** `Event.goal_ids` (a private extended property, primary goal first) records which; serving a goal implicitly serves its ancestors. An event's label is derived from its primary goal whenever its goals are written (`utilities/goal_calendar.py`): the goal's own label if it's active, otherwise its nearest active ancestor's, otherwise none. Calendar rejects *inserting* an event with a label it doesn't have, but accepts re-sending one an existing event already has, so an update keeps an inactive goal's own label on an event that already carries it.
- **Priority and fixed time are inherited.** A goal without its own priority/fixed_time takes its nearest ancestor's, and an event without its own takes its primary goal's. `Event.goal_priority`/`goal_is_fixed_time` hold what it inherits (filled in on read, never written to the API), and `effective_priority`/`effective_is_fixed_time` — all reallocation and compaction read — combine the two. Writing back an event reallocation moved never copies its goal's values onto it.
- **The goals tab is authoritative.** Edit it by hand if you like, then run `sync_goals_from_sheet` (or `calendar_cli.py sync_goals`) to apply the edits to the calendar's labels; every tool validates the whole tab (names unique among siblings, parents that exist, no cycles, ...) before writing anything. Sync refuses to run against an empty goals tab while the calendar still has named labels, rather than delete them all.

**Creating the goals tab is implicit.** Constructing `Goals` for a calendar (`Goals.__init__`, which every goal tool and the event tools do first) finds its goals tab, or, the first time, creates one by migrating its labels: one active, top-level goal per named label on the calendar, keeping the label's id, name and color, so no event needs rewriting and every color stays the same. Events written before goals have only a label; they're read as serving the goal that owns it, and gain their own `goal_ids` the next time anything writes them. The goals tab is only tagged as such once its rows are written, so an interrupted migration just runs again. [`create_calendar.py`](create_calendar.py) does this up front purely so it can print the spreadsheet's URL.

The noted-times tab is simpler, since a noted time has no Calendar API counterpart to reconcile with: [`utilities/noted_time_sheet.py`](utilities/noted_time_sheet.py)'s `NotedTimeSheet` (`NotedTimeSheet.ensure` finds or creates/tags the tab, with just its header row) is what `server.py`/`calendar_cli.py` talk to directly — no `Goals`-equivalent layer above it. The `note`/`get_notes` MCP tools and the `note`/`get_notes` CLI commands (same names, different surfaces — see [MCP tools](#mcp-tools) above and [Command-line utilities](#command-line-utilities) below) call `NotedTimeSheet.append`/`read_with_rows` respectively (and `edit_note`/`delete_note` call `NotedTimeSheet.edit`/`delete`, through `utilities/note_compactor.py`'s guard against changing a note mid-compaction); `note` also creates the spreadsheet/tab on the fly if neither exists yet.

## Goal health

Every active goal is rated once a day, in the daily reflection — there are no other cadences — if it has a **measure**, or sub-goals that are rated (one without a measure is rated as the mean of its sub-goals'). A day runs from waking on it to waking the next, in the main calendar's own time zone (set with `set_time_zone`, which also moves the Goal Health calendar's). "Today" is the date you last woke on, so until the night's end-of-day sleep ends it's still yesterday, however late you're up. An **assessment** is a 0–100 rating (or `skip`) of one goal for one day, with how it was reached (`metric`, `subjective`, `llm` or `rollup`), an optional one-line `explanation`, the measured `metrics`, and a `rationale`. See [`docs/goals-design.md`](docs/goals-design.md) sections 6–8.

- **Where they're stored.** On a second, app-created **Goal Health** calendar (made the first time it's needed, in the main calendar's time zone, and hidden from Google Calendar's list if the app's scope allows; its id is kept on the main calendar). Each day's assessments and its reflection share one all-day event on that day ([`utilities/health_days.py`](utilities/health_days.py)): everything is in its private extended properties, one per goal, so recording a goal's day again replaces its assessment. Its title is `📝 Reflection · <day>` (or `📊 Goal health · <day>` before a reflection) with the overall goal's rating, and its description is the journal and, like the reflection's summary ([`utilities/health_summary.py`](utilities/health_summary.py)), a line for each goal given its own priority, grouped by priority, only for looking at; the other goals are only in the properties. A day that doesn't fit in one event (Calendar allows 300 properties of 32 kB in all) goes on in more, titled `(1/X)`, `(2/X)` and so on. `get_goal_history` reads whole days, then picks out the goals asked for. Calendars from before this kept an event per assessment: [`migrate_goal_health.py`](migrate_goal_health.py) moves them over once (see [Command-line utilities](#command-line-utilities)).
- **Proposed until a reflection confirms them.** `record_assessments` always writes `proposed` assessments, whatever their `status` says; only a reflection confirms one. Only confirmed ratings count toward a goal's health.
- **Changed ratings explain themselves.** A measured or rolled-up rating's explanation ends in `→ <rating>`. If the rating written is a different one (changed in a reflection, say), the explanation is dropped, leaving the rationale (the reason for the change) to say why, or replaced by `Changed from <rating>` if there's no rationale, so the day's line doesn't contradict itself or repeat the reason. The metrics are kept.
- **Measures** ([`utilities/goal_measures.py`](utilities/goal_measures.py) has the details):
  - `{"kind": "duration", "target_min": 300, "interval_days": 7}` — minutes of the goal's events (its sub-goals' included) over the last `interval_days` (default 1) of wall-clock time before the day ended; `{"kind": "count", "target": 1}` — how many. While the target's met, 100; a shortfall is rated in proportion, or, with `zero_at_days`, by how long ago the target was last met: "visit my parents every 2 months" is `{"kind": "count", "target": 1, "interval_days": 60, "zero_at_days": 90}`, 100 within 60 days of a visit, falling linearly to 0 at 90.
  - `{"kind": "time_constraint", "edge": "start", "target": "09:30", "grace_min": 10, "zero_at_min": 60}` — when the day's first event of the goal started (or, with `edge` `end`, its last ended): 100 by the target plus the grace, falling to 0 at `zero_at_min` minutes late; with `when` `after`, the same for being too early. A day with no such events is rated 0; add `"only_if": {}` (below) to skip it instead. "Up by 07:00" measures a "get up" goal's events.
  - `{"kind": "time_window", "from": "11:30", "to": "13:30", "grace_min": 0, "zero_at_min": 60}` — whether one of the day's events of the goal fell in the window. Each event is as far outside it as its closest edge, so one overlapping the window at all is in, and lunch from 14:00 to 14:30 is 30 minutes out. The day is rated by its closest event: 100 up to the grace, falling to 0 at `zero_at_min` minutes out. A day with no such events is rated 0. "Lunch between 11:30 and 13:30" can measure the events of a general goal like "Eat well" (with `events_of`), so whichever meal falls closest to the window counts as lunch.
  - Duration, count, time constraints and time windows look at the goal's own events and its sub-goals'; `events_of` names another goal to look at instead, as though it were that goal: "work 40 hours a week", under "Fulfil my work commitment", is `{"kind": "duration", "target_min": 2400, "interval_days": 7, "events_of": "<the parent's id>"}`, and no event needs tagging with it. `include_sub_goals: false` leaves out that goal's sub-goals' events. (A measure from before `events_of`, naming goals in `goal_ids`, is still measured, but has to switch to `events_of` to be saved again.)
  - `{"kind": "subjective", "prompt": "How's my mood?", "interval_days": 7}` — the prompt is asked in a reflection once `interval_days` (default 1) have passed since it was last answered; on the days between, the day before's rating carries over. Giving a rating in passing (`record_assessments`) restarts the interval.
  - `{"kind": "llm", "rubric": "..."}` — the model proposes a rating against the rubric, which may refer to the goal's immediate sub-goals' ratings.
  - `{"kind": "rollup", "agg": "mean"}` — from the goal's immediate sub-goals' ratings that day: `mean`, `weighted` (with `weights`, sub-goal id → weight; a sub-goal not listed, such as one added since, weighs 0) or `percentile` (with `percentile`, 0 for the lowest, 100 for the highest).
  - Any measure can take `"only_if": {"events_of": "<goal id>", "include_sub_goals": true}` (both optional, meaning what they do above). Without `events_of` it looks at the same events as the measure, so `{}` means the measure's own `events_of` and `include_sub_goals` if it has them, or else the goal itself and its sub-goals: `{"kind": "time_constraint", "edge": "start", "target": "08:00", "events_of": "<Eat well's id>", "only_if": {}}` skips days without a meal instead of rating them 0. It's rated only on days with at least one such event; on the rest it's rated `skip`, without asking its prompt or judging its rubric. "How did practice go?" is `{"kind": "subjective", "prompt": "How did practice go?", "only_if": {"events_of": "<Piano's id>"}}`. A subjective measure's `interval_days` passes over those skipped days: they don't count as answers, and aren't carried over.
- **The overall goal.** Every calendar has one goal above the rest, with id `overall`: its sub-goals are implied to be every top-level goal, so it's rated like any goal (by a measure of its own, or the mean of the top-level goals'), and rated last in each reflection. Its minutes are the time spent on any goal, each event once, and a weighted rollup weighs top-level goals. It holds no event label, can't be given to an event, is always active and top-level, and is always listed first. It's written to the goals tab the first time anything is; until then it's supplied.
- **Ratings only flow up.** A goal's rating can come from its sub-goals', never the other way around, so a parent above a question that hasn't been answered has only a provisional rating until it is.
- **Measured goals.** `measure_goals` proposes ratings for the goals the calendar can answer (duration, count, time_constraint, time_window, and rollups whose sub-goals are rated), without writing anything, each with an explanation like "3h 40m of 5h in the last 7 days → 73".
- **At a glance.** The goals tab's **health** (the latest confirmed rating; a skip doesn't replace it), **health_period** (the latest day rated) and **health_trend** (the last 8 days' ratings, `-` for none) are a cache of the Goal Health calendar, kept up to date as assessments are confirmed; `rebuild_goal_health_cache` recomputes them. Listed goals also say how many days have ended unrated since the last rated one (`stale_days`), and how many minutes went toward them and their sub-goals in the 24 hours and the 7 days (`minutes_24h`, `minutes_7d`, of wall-clock time) up to the last compaction (`GoalList.as_of`), since the calendar is only settled fact up to then. Each listed goal's `minutes_by_statuses` splits its time by the statuses of the goals each event is given among it and its sub-goals (not their ancestors: an event given only an inactive sub-goal counts toward its active parent as inactive time), so a client can total a goal's time for whatever statuses it shows, each event once. `GoalList.minutes_by_statuses` is the overall goal's.

## Reflections

A **reflection** is a short conversation, run by the MCP client, that rates every rated goal for one day — and it's the only way a rating is confirmed. See [`docs/goals-design.md`](docs/goals-design.md) section 11.

- **Filled in automatically.** Every rating the calendar can give is filled in without asking: measured goals, `only_if` skips, subjective ratings carried over between askings, and rollups all the way up to the overall goal. Only goals that need judgement are **questions**, all asked at once: an `llm` goal (the model rates it against its rubric, and asks only when the day's events and notes don't say) and a subjective goal whose prompt is due.
- **`prepare_reflection(day?)`** (read-only), with no day named, offers the most recent completed days not fully reflected on. For a day, it returns the **questions** (each with its recent ratings and its sub-goals' ratings), active goals with nothing to rate them by (**unmeasured**), minutes per goal (sub-goals' time included), a digest of the day's events and notes, how many notes are still uncompacted (the calendar may not reflect them yet), and instructions for the conversation.
- **`record_reflection(day, assessments, proposed?)`** takes the answers (or any rating being changed, with the reason as its rationale) and works out everything else. By default it's a dry run that writes nothing and returns the **summary**: provisional where it still waits on answers (left out of their parents' rollups, marked `~`), or on an llm rating listed in `proposed` that the model is checking with the user. With `dry_run=False`, every final rating is confirmed in one write (refreshing the goals' health), with whether the day is **complete**; provisional ratings are never written, so a reflection cut short resumes with only the unanswered questions. Measured ratings and rollups are worked out afresh each time, so a correction rolls up into its parents; answered and hand-changed ratings are kept.
- **The summary** has a line for each top-level goal and each goal given its own priority, grouped by priority (0 first; a top-level goal without one counts as 2) and best rated first within each, with goals that have nothing to rate them by last. Each line folds in the sub-goals without a line of their own, naming its lowest-rated ones (or saying how it was measured), and an arrow marks a move of 10 points or more since the day before.
- A day's reflection used to take a journal and intentions for tomorrow; those recorded before are kept on its Goal Health event, but reflections no longer ask for them.

**`get_compaction_status()`** says when notes were last compacted into the calendar, and which note was the latest compacted.

## Event colors

[`Event.to_api_body()`](calendar_clients/google_calendar.py) (used by every path that writes an event — the MCP tools, `calendar_cli.py`, and reallocation, all via `CalendarClient.create_event`/`update_event`) automatically sets `colorId` from the event's `priority`, so priority is visible at a glance in the Google Calendar UI without a separate step:

| Priority | Color | `colorId` |
| --- | --- | --- |
| <=0 | Graphite (gray) | `"8"` |
| 1 | Banana (yellow) | `"5"` |
| 2 | the calendar's default color | unset |
| >=3 | Sage (soft green) | `"2"` |

Note that calendar colors are superseded by the colors corresponding to the event's label. `event_label_id` (the API's own `eventLabelId` field) is derived from the event's goals (see [Goals](#goals) above) and is read-only to the MCP tools; `calendar_cli.py`'s `update_properties` can still set it directly (`event_label_id=<label-id>`). Calendar itself tolerates an existing event pointing at a label that's since been removed (it keeps the id, and the event can still be updated), but it rejects *inserting* an event with one. So creating an event with a label this calendar doesn't have fails up front, before reallocation changes anything, and an event split off another by reallocation (or compaction) quietly drops a removed label rather than failing the whole call. A `create` decision in compaction naming an unknown label fails its dry run. Updating an event doesn't check its label.

## Google OAuth credentials

`calendar_clients/google_auth.py`'s `load_credentials` — used by both `CalendarClient.from_credentials` and `SheetsClient.from_credentials` (and by `config.build_goals`, which loads it once and shares it between the two — see [Project layout](#project-layout) above) — needs two files, neither of which should ever be committed:

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
| `MCP_TRANSPORT` | No | `stdio` | `stdio` runs the server as a subprocess a local MCP host spawns directly (Claude Desktop's local config, `mcp dev`). `streamable-http` runs it as a standalone HTTP server instead — set this when hosting it remotely (see [Deploying](#deploying)); the variables below only matter in that case. |
| `WORKOS_AUTHKIT_DOMAIN` | Only for `streamable-http` | — | The WorkOS AuthKit domain (e.g. `https://your-tenant.authkit.app`) that issues and signs bearer tokens for callers of this server — see [Deploying](#deploying). Unrelated to the Google OAuth variables above: this controls who's allowed to talk to *this* server, not this server's own credential to talk to Google. |
| `MCP_ALLOWED_USER_IDS` | Only for `streamable-http` | — | Comma-separated WorkOS user ids (`user_...`) allowed to use the server. Tokens for anyone else are rejected, and the server refuses to start without this — see [Deploying](#deploying). |
| `MCP_PUBLIC_URL` | No | derived from `RENDER_EXTERNAL_URL` | This server's own public MCP endpoint URL (e.g. `https://your-service.onrender.com/mcp`) — used as the OAuth resource identifier and the audience bearer tokens must carry. On Render this is derived automatically from `RENDER_EXTERNAL_URL` (which Render sets for you); set it explicitly elsewhere. |
| `MCP_CORS_ALLOWED_ORIGINS` | No | — | Comma-separated browser origins (e.g. `https://tracker.example.com`) allowed to call the `streamable-http` endpoint cross-origin, for a web-based MCP client like the Time Tracker web app. `http://localhost` and `http://127.0.0.1` on any port are always allowed, so local development needs nothing set. Safe to widen: the server authenticates by bearer token, never cookies, so a cross-origin page can't borrow the user's access. |
| `PORT` | No | `10000` | The port `streamable-http` binds to (always alongside host `0.0.0.0`, as every Render web service requires). Render sets this for you automatically. |
| `MEMORY_LIMIT_BYTES` | No | read from the cgroup enforcing it | *Not read via `config.py`* — read directly by [`utilities/memory_diagnostics.py`](utilities/memory_diagnostics.py). This process's own memory limit, in bytes; only needed if the cgroup value can't be read (see that module). |

To supply these locally, copy [`.env.example`](.env.example) to `.env` and fill it in — `config.py` loads `.env` automatically (via `python-dotenv`) if one is present. `.env` is gitignored, so nothing personal ends up committed.

If this server is launched by an MCP host (Claude Desktop, Claude Code, etc.) instead of run standalone, set these same variables in that host's server config under its `env` field — no `.env` file needed in that case.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests need no credentials or configuration. [`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs them on Python 3.11 and 3.13 for every pull request into `main` and every push to it. To block merging until they pass, make the `pytest (Python 3.11)` and `pytest (Python 3.13)` checks required for `main` in the repository's branch protection settings (Settings → Branches, or Rules → Rulesets).

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
4. Find your own WorkOS user id on the **Users** page of the dashboard (it looks like `user_01ABC...`), and set `MCP_ALLOWED_USER_IDS` on Render to it. This is what keeps the server yours: a token WorkOS issued only proves *someone* signed in to your AuthKit environment, and depending on your WorkOS settings, anyone may be able to sign up there. The server rejects tokens for any other user (and logs their id, which is another way to find yours: try connecting, then check Render's logs), and refuses to start if this isn't set. To also stop strangers creating accounts at all, turn off sign-ups in WorkOS's Authentication settings.

**Staging is fine to stay on** for a personal tool like this one — don't feel pushed toward Production just because it's the other option. Per WorkOS's own [Staging vs. production environments](https://workos.com/docs/authkit/environments) docs, Staging is "fully functional and secure," and the only things gated behind Production are a custom AuthKit domain (cosmetic — `*.authkit.app` works identically otherwise) and billing details. Production's SLA/"customer-facing traffic" language in WorkOS's terms is aimed at real multi-tenant SaaS products serving external customers out of staging by mistake; it doesn't really describe a single hobbyist authenticating only themselves. The billing requirement is also moot in practice for this project either way — what actually gets charged is SSO/Directory Sync connections, features this project never touches (it only uses Connect/OAuth).

Don't try to test this by visiting the AuthKit domain directly in a browser — that's AuthKit's own generic hosted sign-in page (it'll ask you to log in and then complain about a missing sign-in callback), a completely different, unrelated flow that really does expect a manually-configured application. It has nothing to do with the MCP/DCR setup above. The real test is connecting from Claude.ai (below): Claude.ai itself constructs the correct authorization request, with its own dynamically-registered client id and redirect URI, so there's nothing to configure or test by hand first.

With both in place, set `MCP_TRANSPORT=streamable-http` (already in `render.yaml`) and deploy. `MCP_PUBLIC_URL` doesn't need setting explicitly on Render — it's derived from `RENDER_EXTERNAL_URL`, which Render provides automatically.

Local development is entirely unaffected by any of this: `python server.py` with no `MCP_TRANSPORT` set (the default) still runs over stdio, and none of `WORKOS_AUTHKIT_DOMAIN`/`MCP_PUBLIC_URL` is read in that case.

### Browser-based clients

A client running in a web page (like the Time Tracker web app) can't talk to AuthKit's registration and token endpoints directly: AuthKit answers them without CORS headers, so the browser hides the responses. For those clients only, this server offers `POST /oauth/register` and `POST /oauth/token`, which forward the request to the same endpoints under `WORKOS_AUTHKIT_DOMAIN` and return AuthKit's response unchanged (see [`oauth_proxy.py`](oauth_proxy.py)). Nothing to configure in WorkOS: the web client still registers itself through DCR, just via this server. Its origin does need to be allowed by `MCP_CORS_ALLOWED_ORIGINS` (localhost always is). Claude.ai and the native apps never use these routes.

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

[`migrate_goal_health.py`](migrate_goal_health.py) is a one-time script for a calendar whose Goal Health calendar predates one event per day: it folds each day's old per-goal assessment events and reflection event into that day's event, reads it back, and only then deletes the old ones. Run it without arguments to see what it would do, then with `--apply` while nothing else (the deployed server included) is writing:

```bash
python migrate_goal_health.py
python migrate_goal_health.py --apply
```

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
# them (each active goal owns one -- see the goal commands below)
python calendar_cli.py list_raw_labels

# Create a new event label -- background_color is required
python calendar_cli.py create_raw_label background_color="#8e24aa" name="Design Work"

# Update an existing event label's color and/or name
python calendar_cli.py update_raw_label <label-id> background_color="#d50000"

# Delete an event label by id
python calendar_cli.py delete_raw_label <label-id>

# List the proposed, active and inactive goals (--status for others, --all for every status)
python calendar_cli.py list_goals

# Create a goal, and a sub-goal under it (ids are assigned; list_goals shows them)
python calendar_cli.py create_goal name="Learn vegetarian cooking" priority=1
python calendar_cli.py create_goal name="Tofu tikka masala" parent_id=<goal-id> measure='{"kind":"subjective","prompt":"How did it go?"}'

# Update a goal's properties -- any status but active frees its label but keeps its history
python calendar_cli.py update_goal <goal-id> status=inactive note="Paused for the summer"

# Blank properties instead (here, so it becomes a top-level goal again)
python calendar_cli.py update_goal <goal-id> --clear parent_id

# Apply hand edits to the Goals tab to the calendar's labels
python calendar_cli.py sync_goals

# Tie an event to goals (primary first), which also sets its label
python calendar_cli.py update_properties <event-id> goal_ids=<goal-id>,<other-goal-id>

# Record a note for right now -- description is optional
python calendar_cli.py note 0s "Started focus block"

# A note for 30 minutes ago, with no description
python calendar_cli.py note 30m

# List the notes that haven't been compacted yet, each with its id
python calendar_cli.py get_notes

# Correct a note's description, or move it to 20 minutes ago / an exact time
python calendar_cli.py edit_note "2026-01-01T09:05:00+00:00#5" --description "Started standup"
python calendar_cli.py edit_note "2026-01-01T09:05:00+00:00#5" --ago 20m
python calendar_cli.py edit_note "2026-01-01T09:05:00+00:00#5" --at 2026-01-01T09:10:00-04:00

# Delete a note
python calendar_cli.py delete_note "2026-01-01T09:05:00+00:00#5"
```

`from`/`to` are each a duration relative to *now* — parsed with [pytimeparse](https://pypi.org/project/pytimeparse/) (e.g. `"1h"`, `"90m"`, `"2d"`, `"1:30"`) — giving a window from `now - from` to `now + to`. Both are optional and default to `1h`.

`update_properties` takes one or more `key=value` pairs, where each `key` is an `Event` attribute (`summary`, `start`, `end`, `description`, `location`, `min_duration`, `is_fixed_duration`, `is_fixed_time`, `priority` — not `id`, since changing it would repoint the patch at a different event). It builds an `Event` with just those attributes set (everything else `None`) and patches it straight in, without fetching the event first — Calendar's `patch` semantics mean any attribute you don't mention is left exactly as it was server-side. `start`/`end` take an ISO 8601 datetime with a UTC offset (e.g. `2026-01-01T09:00:00-05:00`, or a trailing `Z`); `min_duration` takes a pytimeparse duration like `from`/`to` above; `is_fixed_duration`/`is_fixed_time` take `true`/`false` (also `1`/`0`, `yes`/`no`) -- see [MCP tools](#mcp-tools) above for what `is_fixed_time` does during reallocation.

`update` takes the same `key=value` pairs as `update_properties` (at least one of `start`/`end` is required this time — whichever is omitted is kept as the event's current value) and calls `ReallocatingCalendar.update_event` — the same reallocation-aware path `server.py`'s `update_event` MCP tool uses (see [MCP tools](#mcp-tools) above) — reallocating time from the rest of the event's day as needed to make room for its new position. Prints every event the call created or changed, not just the moved one. Use `update_properties` instead for a plain patch that doesn't move the event (e.g. just renaming it) — `update` always needs a real position to make room for, since that's what reallocation acts on.

`create` takes the same `key=value` pairs as `update_properties` (`summary`, `start`, and `end` are required this time) and builds a new `Event` from them, then calls `ReallocatingCalendar.create_event` — the same reallocation-aware creation path `server.py`'s `create_event` MCP tool uses (see [MCP tools](#mcp-tools) above). Prints every event the call created or changed, not just the new one.

For a recurring event, the id from `list`/`get` names one specific *instance*.  To change a property for the entire series of recurring events, use the `recurring_event_id` that's visible from `get`.  See Google's [recurring events guide](https://developers.google.com/workspace/calendar/api/guides/recurringevents) for more on how instances and recurring events relate.

`list_raw_labels`/`create_raw_label`/`update_raw_label`/`delete_raw_label` manage this calendar's custom event labels exactly as Google Calendar's API represents them (`CalendarClient.list_event_labels`/`create_event_label`/`update_event_label`/`delete_event_label`, `calendar_clients/google_calendar.py`'s `EventLabel`) — a richer, arbitrary-hex-color alternative to `Event.colorId`'s 11 fixed colors, but with no concept of priority: Calendar itself has no field for one, so `create_raw_label`/`update_raw_label` take only `background_color=value`/`name=value` pairs, and `background_color` is required for `create_raw_label`. Labels are normally managed through goals (below): a raw label that isn't an active goal's is removed the next time goals sync.

`list_goals` (proposed, active and inactive goals; `--status <status>`, repeatable, for others, or `--all`), `create_goal`, `update_goal <goal_id>` and `sync_goals` manage this calendar's goals, the same way the MCP tools do (see [Goals](#goals) above), and each prints the resulting goals, one per line: id, status, and path. `create_goal`/`update_goal` take `key=value` pairs for a goal's fields (`name`, `parent_id`, `status`, `background_color`, `priority`, `fixed_time`, `measure` as JSON, `note`); `update_goal` also takes `--clear <attribute>`, repeatable. `update_properties`'s `goal_ids=g1,g2` sets an event's goals, and the label they imply.

`note` takes a positional `ago` (required -- like `from`/`to` above, a pytimeparse duration, e.g. `"1h"`, `"90m"`, `"0s"` for right now, resolved via `resolve_note_timestamp` the same way `list`'s window is resolved via `resolve_window`) and an optional positional `description` (quote it if it contains spaces), builds a `utilities/noted_time_sheet.py` `NotedTime` from them, and appends it to this calendar's noted-times tab via `NotedTimeSheet.append` — see [MCP tools](#mcp-tools) and [Calendar metadata sheets](#calendar-metadata-sheets) above. `get_notes` lists every uncompacted note with its id (via `NotedTimeSheet.read_with_rows`). `edit_note <note_id>` takes `--ago` (a duration before now, like `note`'s) or `--at` (an ISO 8601 time, local if it has no UTC offset) for a new time, and/or `--description` (`""` clears it); `delete_note <note_id>` removes the note. Both refuse a compacted note, a stale id, or a note in a compaction that's partway through being applied, printing why and exiting non-zero — see `edit_note`/`delete_note` under [MCP tools](#mcp-tools).


## Running tests

With the dev dependencies installed:

```bash
pytest
```
