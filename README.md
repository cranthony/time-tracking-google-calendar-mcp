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
- [`calendar_clients/google_calendar.py`](calendar_clients/google_calendar.py) — Google Calendar API access, behind a `CalendarClient` class and plain `Event`/`Calendar`/`EventLabel` dataclasses. Kept separate from `server.py` so the Calendar logic can be unit tested without hitting the real API — tests construct a `CalendarClient` around a mocked `service` object instead. `CalendarClient` is a thin, pure API wrapper (list/get/create/update/delete, plus event-label management); its `EventLabel` is a *raw* label, exactly as Calendar represents one — labels are managed through actions (see [Actions](#actions) below). Inside `cached_calendar_listings()` — which `server.py` wraps every MCP tool call in, alongside `cached_sheet_reads()` (below) — a `list_events` within a range already listed in the same call (same calendar, same `show_deleted`) is answered from memory, filtered to its own range; any write through any `CalendarClient` forgets every listing first. Compaction lists overlapping stretches of the same days many times in one step, so `NoteCompactor` lists them all once up front and the rest come from that: a two-day dry run makes one Calendar listing instead of six.
- [`calendar_clients/google_sheets.py`](calendar_clients/google_sheets.py) — Google Sheets API access, behind a `SheetsClient` class — the same thin-wrapper role as `CalendarClient`, but for spreadsheets: create one, add/rename/color a tab, read/write rows (optionally addressed to a tab by its sheetId, so there's no title lookup to spend a read request on), narrow a column, tag a tab with developer metadata and find it again by that tag. Never touches the Drive API directly — a spreadsheet's id is looked up via `CalendarClient.get_calendar_metadata` instead (see [Calendar metadata sheets](#calendar-metadata-sheets) below). Knows nothing about actions, time notes, or any other meaning attached to a tab. Every request retries with backoff (for up to about a minute) when Sheets answers 429 — its read quota is only 60 requests per minute per user — and on nothing else, since some requests aren't safe to repeat. `server.py` also wraps every MCP tool call in `cached_sheet_reads()`, so a tab read more than once in the same call (a compaction step reads the notes tab and the journal several times) costs one request, not several; any write to that tab forgets it, and nothing is kept between calls, so edits you make in the spreadsheet by hand are always seen by the next call.
- [`utilities/reallocation.py`](utilities/reallocation.py) — the reallocation algorithm: how creating an event makes room for itself by reclaiming time from lower-priority events and free time in its day. Has no Calendar API dependency of its own — see the module docstring.
- [`utilities/reallocating_calendar.py`](utilities/reallocating_calendar.py) — the glue between the two above: `ReallocatingCalendar` wraps a `CalendarClient`-shaped calendar (see `action_calendar.py` below) with reallocation-aware `create_event`/`update_event`, the shared entry point both `server.py` and `calendar_cli.py` use.
- [`utilities/action_calendar.py`](utilities/action_calendar.py) — `ActionCalendar` wraps a `CalendarClient` with an `Actions`: on read (`list_events`/`get_event`) it fills in an event's actions (from its label, for an event never given any) and the `action_priority` it inherits from them; on write it derives the event's label from its first action. The event's own `priority` is never touched. `ReallocatingCalendar` is built on top of this, and reallocation reads only `Event.effective_priority` (the event's own priority, falling back to its actions') — see [Actions](#actions) below.
- [`utilities/calendar_metadata_sheet.py`](utilities/calendar_metadata_sheet.py) — owns the *spreadsheet* a calendar's Sheet-backed data (actions, people, uncompacted time notes, ...) lives in: `ensure_spreadsheet` finds or creates it, `ensure_tab` finds or creates/tags one of its tabs by role, and `create_tab` creates one but only tags it once its first rows are written. See [Calendar metadata sheets](#calendar-metadata-sheets) below.
- [`utilities/row_sheet.py`](utilities/row_sheet.py) — `RowSheet`, a tab of one dataclass per row, read and written by header name (columns it doesn't know are kept as they are): what the actions, action groups, people, circles and locations tabs are built on.
- [`utilities/actions.py`](utilities/actions.py) and [`utilities/action_groups.py`](utilities/action_groups.py) — `Actions`: the actions and the groups that roll them up, kept in step with the calendar's event labels. `ActionTree` answers the read-only questions events need (an action's priority, which action holds a label). See [Actions](#actions) below.
- [`utilities/people.py`](utilities/people.py) and [`utilities/locations.py`](utilities/locations.py) — `People` (people, the user always among them as `self`, and circles) and `Locations`. See [People, circles and locations](#people-circles-and-locations) below.
- [`utilities/traits.py`](utilities/traits.py) — `Traits`, how the user wants to be, each rated from parts. See [Traits](#traits) below.
- [`utilities/facts.py`](utilities/facts.py) — an event's `Facts`: where it was, who it was with and for, and a note on each person there, as compaction establishes them.
- [`utilities/noted_time_sheet.py`](utilities/noted_time_sheet.py) — a thin, per-tab API: `NotedTimeSheet.ensure` (the noted-times tab of a given calendar metadata spreadsheet, via `calendar_metadata_sheet.ensure_tab`) and, bound to one already-known tab, `read`/`read_with_rows`/`append`/`mark_compacted`/`garbage_collect` its rows as this module's own `NotedTime` dataclass (`timestamp`/`description`/`compaction_id`). Compacting a note stamps its `compaction_id` rather than deleting it; a note's row, together with its timestamp, forms its id, stable unless `garbage_collect` shifts it (see [Compacting notes](#compacting-notes) below). A noted time has no Calendar API counterpart to reconcile with, so there's no layer above this one. See [Calendar metadata sheets](#calendar-metadata-sheets) below.
- [`utilities/note_compaction.py`](utilities/note_compaction.py) — `plan_compaction`, a pure function (no API access) that turns a day's notes, plus a model's decisions about which events they show happened differently, into the calendar changes that realign the day to them. See [Compacting notes](#compacting-notes) below.
- [`utilities/compaction_timeline.py`](utilities/compaction_timeline.py) — `Timeline`, the view of a compaction (each day's notes and its events in time order), as data and as narrow fixed-width text.
- [`utilities/compaction_journal.py`](utilities/compaction_journal.py) — `CompactionJournal`, the write-ahead journal tab that records each approved compaction and its progress, so a failed one can be resumed exactly.
- [`utilities/compaction_marker.py`](utilities/compaction_marker.py) — `CompactionMarker`, the bright red, 5-minute event in Google Calendar that ends at the last compaction. See [Compacting notes](#compacting-notes) below.
- [`utilities/note_compactor.py`](utilities/note_compactor.py) — `NoteCompactor`, which ties the notes tab, the calendar, the planner and the journal together behind the `prepare_compaction`/`compact_notes`/`abandon_compaction` tools; [`utilities/compaction_additions.py`](utilities/compaction_additions.py) has the actions, people and locations a compaction adds as it goes.
- [`utilities/memory_diagnostics.py`](utilities/memory_diagnostics.py) — `track(label)`, a context manager wrapping an operation (every MCP tool, `load_credentials`) to log RSS, `tracemalloc` growth, and `objgraph` object-count growth since the previously tracked operation — for narrowing down what's driving this process's memory usage. `tracemalloc` itself is only traced while RSS is at or above half of this process's own memory limit (read from the `MEMORY_LIMIT_BYTES` environment variable if set, else from the cgroup enforcing it, `memory.max`/`memory.limit_in_bytes`) -- started the moment it first crosses that threshold, stopped the moment it first drops back below (no hysteresis, and always logged one last time right before stopping) -- so its overhead is only paid while things are actually near an OOM kill.
- [`server.py`](server.py) — the MCP server; its tools call into `calendar_clients/google_calendar.py` rather than talking to `googleapiclient`/OAuth directly.
- [`workos_auth.py`](workos_auth.py) — verifies bearer tokens issued by WorkOS AuthKit, for when `server.py` is hosted remotely over `streamable-http` instead of run locally over stdio — see [Deploying](#deploying) below.
- [`create_calendar.py`](create_calendar.py) — a standalone bootstrap script (not an MCP tool) that creates the dedicated calendar this app needs, and its calendar metadata spreadsheet (Actions and uncompacted time notes tabs both provisioned) — see [Calendar access model](#calendar-access-model) below.
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

**Bootstrapping:** there's no calendar to operate on until this app creates one. Run [`create_calendar.py`](create_calendar.py) once — it only needs `GOOGLE_OAUTH_CREDENTIALS_PATH`/`GOOGLE_OAUTH_TOKEN_PATH` (see [Configuration](#configuration) below), not `GOOGLE_CALENDAR_ID` — and it prints the new calendar's ID, then ensures that calendar's metadata spreadsheet (Actions and uncompacted time notes tabs both provisioned) in the same step (see [Calendar metadata sheets](#calendar-metadata-sheets) below) and prints its URL too:

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
| `update_event` | `(event: PublicEvent, clear_fields: list[EventField] \| None, reallocate: bool = True) -> list[PublicEvent]` |
| `create_event` | `(event: PublicEvent, reallocate: bool = True) -> list[PublicEvent]` |
| `delete_event` | `(id) -> list[PublicEvent]` |
| `get_actions` | `(statuses: list[ActionStatus] \| None) -> ActionList` |
| `get_action` | `(id_or_name: str) -> ListedAction` |
| `create_action` | `(action: Action) -> CreatedAction` |
| `update_action` | `(action: Action, clear_fields: list[ActionField] \| None) -> ActionChanges` |
| `get_action_groups` | `() -> list[ListedActionGroup]` |
| `get_action_group` | `(id_or_name: str) -> ListedActionGroup` |
| `create_action_group` | `(group: ActionGroup) -> CreatedActionGroup` |
| `update_action_group` | `(group: ActionGroup, clear_fields: list[ActionGroupField] \| None) -> ActionGroupChanges` |
| `delete_action_group` | `(group_id: str) -> DeletedActionGroup` |
| `get_people` | `(statuses: list[PersonStatus] \| None) -> list[ListedPerson]` |
| `get_person` | `(id_or_name: str) -> ListedPerson` |
| `create_person` | `(person: Person) -> CreatedPerson` |
| `update_person` | `(person: Person, clear_fields: list[PersonField] \| None) -> ListedPerson` |
| `get_circles` | `() -> list[ListedCircle]` |
| `get_circle` | `(id_or_name: str) -> ListedCircle` |
| `create_circle` | `(circle: Circle) -> CreatedCircle` |
| `update_circle` | `(circle: Circle, clear_fields: list[CircleField] \| None) -> ListedCircle` |
| `delete_circle` | `(circle_id: str) -> DeletedCircle` |
| `get_locations` | `() -> list[Location]` |
| `get_location` | `(id_or_name: str) -> Location` |
| `create_location` | `(location: Location) -> CreatedLocation` |
| `update_location` | `(location: Location, clear_fields: list[LocationField] \| None) -> Location` |
| `delete_location` | `(location_id: str) -> Location` |
| `get_traits` | `(statuses: list[TraitStatus] \| None) -> list[ListedTrait]` |
| `create_trait` | `(trait: Trait) -> Trait` |
| `update_trait` | `(trait: Trait, clear_fields: list[TraitField] \| None) -> Trait` |
| `get_compaction_status` | `() -> CompactionStatus` |
| `note` | `(noted_time: NotedTime) -> NoteWithId` |
| `get_notes` | `(include_compacted: bool = False) -> list[NoteWithId]` |
| `edit_note` | `(note_id: str, timestamp: datetime \| None, description: str \| None) -> NoteWithId` |
| `delete_note` | `(note_id: str) -> NotedTime` |
| `prepare_compaction` | `() -> CompactionContext` |
| `compact_notes` | `(decisions: list[EventDecision] \| None, ignore_notes: list[str] \| None, compaction_id: str \| None, dry_run: bool = True, new_actions: list[NewAction] \| None, new_people: list[NewPerson] \| None, new_locations: list[NewLocation] \| None) -> CompactionResult` |
| `prepare_judgments` | `(compaction_id: str \| None, redo: bool = False) -> JudgmentsDue` |
| `record_judgments` | `(compaction_id: str, judgments: list[Judgment]) -> JudgmentsResult` |
| `get_trait_scores` | `(person_id: str \| None, start: date \| None, end: date \| None) -> list[TraitScoreRow]` |
| `rebuild_trait_scores` | `(start: date, end: date) -> list[TraitScoreRow]` |
| `abandon_compaction` | `(compaction_id: str) -> CompactionResult` |
| `set_time_zone` | `(time_zone: str) -> str` |

`update_event`/`create_event`/`delete_event` all return a `list[PublicEvent]` rather than a single `PublicEvent`, since `update_event`/`create_event` can affect more than the one event acted on (see below). `delete_event` doesn't call the Calendar API's own delete — it patches the event's `status` to `"cancelled"` (via `CalendarClient.update_event`), the same way reallocation cancels an event to make room for another. This matches `Event.status`'s own documented recommendation to cancel rather than delete an instance of a recurring event, and always returns exactly that one event, wrapped in a single-element list for a consistent return type across all three.

`create_event`/`update_event` both make room for the event via `utilities/reallocating_calendar.py`'s `ReallocatingCalendar` (see [Project layout](#project-layout) above): each fetches the roughly 24 hours of events starting at the event's own `start` (via `ReallocatingCalendar.list_day_events`, truncated after the first event marked `is_end_of_day_sleep` that ends after the event does, if any, so the tail of the night before doesn't end the day — that's "the day" `utilities/reallocation.py` operates on), then calls `reallocate_for_new_event` and applies whatever it returns (creating or moving the event itself, and updating — shrinking, moving, splitting, or cancelling — whatever else needed to make room, each via the plain `CalendarClient`). `update_event` (`ReallocatingCalendar.update_event`) additionally excludes the event's own prior position from that day's events first, since `reallocate_for_new_event` refuses a day already containing the event being moved -- and passes its id as `list_day_events`'s `ignore_id`, so truncating at the first `is_end_of_day_sleep` event doesn't key off the event's own not-yet-applied prior position when it's itself that day's sleep block (e.g. stretching a morning sleep block later). An update that doesn't move the event — neither `start` nor `end` set, or both the same as now — changes only its other fields, so it's patched straight in: nothing is reallocated, and the rest of the day isn't read or checked, so an event can be edited even on a day whose events already overlap. Otherwise, whichever of `start`/`end` is left `None` is filled in from the event's current value before reallocating, reusing that same `list_day_events` fetch rather than a second one: if the event is already in it, its current value is read from there; if not (e.g. the one value given put it on a different day than its prior position), a direct `get_event` gets it instead. `create_event`/`update_event` with `reallocate` false still compute that plan, but write nothing if it would change anything besides placing the event where it was asked for: it raises `ReallocationNeeded` instead, naming each change. An event that starts inside a fixed-time event is refused (`StartsInsideFixedTime`), saying when it can start. A `ValueError` from reallocation is surfaced as a `ToolError`.

Every event tool uses `PublicEvent` (defined in `server.py`), not `Event`, as its input/output type — `Event` minus whatever fields are named in `INTERNAL_EVENT_FIELDS`, plus `is_cancelled` (which has no `Event` equivalent — it's derived from the hidden `status` field). `is_end_of_day_sleep` and `recurring_event_id` are visible but read-only, like the `effective_*` fields below: `PublicEvent.to_event` ignores them, so `update_event`/`create_event` can't change them. `recurring_event_id` is assigned by Google, and `is_end_of_day_sleep` decides where reallocation's day ends, so a wrong mark would quietly change what later updates shrink, move or cancel; it's set by hand with `calendar_cli.py update_properties`. Agents communicating with this MCP only see the fields in `PublicEvent`. `calendar_cli.py` still operates on `Event` directly and has full access to every field, since it's a human-run dev tool, not something the agent talks to.

**`is_fixed_time`** marks an event whose `start`/`end` must never change, not just its duration (`is_fixed_duration`) — the same `min_duration`-equals-own-duration treatment applies (see `Event.is_fixed_time`'s own docstring), so a fixed-time event is never shrunk during reallocation either. Unlike every other field, `reallocate_for_new_event` (`utilities/reallocation.py`) actively *enforces* it across a whole reallocation: if displacing a fixed-time event turns out to be unavoidable in a single pass (e.g. it was the only thing standing in a new event's way), the algorithm detects that afterward and re-runs itself to put it straight back at its exact original position — reclaiming whatever's now there instead, which may itself displace *another* fixed-time event, repairing in turn, until everything settles (or a genuine conflict between two fixed-time events raises a `ValueError`). See that module's "Fixed time" section for the full mechanism. A cancelled fixed-time event is the one outcome that's never undone.

Every event tool's `PublicEvent`s also carry a read-only `effective_priority`: the priority reallocation actually uses, i.e. `Event`'s property of the same name: the event's own `priority`, falling back, when it has none, to the highest priority among all its actions (each action's own, or its nearest group's — filled in via `utilities/action_calendar.py`'s `fill_in_from_actions`; see [Actions](#actions) below). `priority` itself stays the event's own value, and `update_event`/`create_event` ignore `effective_priority`, so sending a listed event straight back to `update_event` never copies its actions' priority onto the event — which would otherwise stop it following later changes to its actions. `is_fixed_time` is only ever the event's own.

`PublicEvent.action_ids` are what was done at an event, the first setting its color; it's how `create_event`/`update_event` tie an event to actions (`[]` removes them), and ids that aren't actions are refused with the closest matches suggested. `action_names` (their names, in the same order) and `event_label_id` (derived from the first action) are read-only. `PublicEvent.facts` are what compaction established about a past event (see [Compacting notes](#compacting-notes)); `update_event` can replace them whole, or clear them.

`list_events`/`get_event` still don't surface a cancelled event at all: `list_events` omits it, and `get_event` raises a `ToolError`. But when an operation like `create_event` cancels an event as a side effect of making room, or `delete_event` cancels the event it was asked to remove, that cancellation is a direct result of the agent's own action, so it's worth surfacing rather than hiding — its `PublicEvent` comes back with the rest of its fields intact and `is_cancelled=True`. `is_cancelled` only ever moves from `False` to `True`; setting it `False` has no effect, since there's no way to un-cancel an event through this API.

Calendar creation is deliberately *not* an MCP tool — see [Calendar access model](#calendar-access-model) above — so the model can't create new calendars on its own; that's a one-time, human-run bootstrap step via `create_calendar.py`.

The action, group, people, circle, location and trait tools manage what events are tagged with and what traits are read from — see [Actions](#actions), [People, circles and locations](#people-circles-and-locations) and [Traits](#traits) below. Each kind has tools to list them all, get one by id or name (suggesting close matches when there's none), create one (returning the new id as `created_id`) and update one (whichever fields are given are set, and those in `clear_fields` blanked; ids never change). Action groups, circles and locations have no status, so they have delete tools; actions and people are archived or deleted by status instead, so events naming them still make sense.

The `note` tool records a new time note (`utilities/noted_time_sheet.py`'s `NotedTime`: a required `timestamp`, and an optional free-text `description` of what it marks) by appending it to this calendar's noted-times tab (via `NotedTimeSheet.append`, which writes only the new row — see [Calendar metadata sheets](#calendar-metadata-sheets) below), returning the note as recorded along with its id (`NoteWithId`: the note's timestamp and sheet row together, e.g. `2026-01-01T09:05:00+00:00#5`). A caller can't set a note's `compaction_id`; only compaction does. `get_notes` lists the notes that haven't been compacted yet, sorted by timestamp and each with its id (or all of them with `include_compacted`).

`edit_note` corrects an uncompacted note by id — a new `timestamp` and/or `description` (whichever is left out keeps its value; an empty `description` clears it) — and returns it with its id, which changes if its timestamp did. `delete_note` removes one, by blanking its row rather than deleting it, so no other note's id changes. Both refuse a stale id (the note was edited or removed since it was listed), an already-compacted note (change the calendar event it became instead), and a note in a compaction that's partway through being applied — that compaction stamps its notes last, so changing one midway would get the changed note stamped. A dry-run plan that included the note can't be committed afterward (committing re-checks the notes); just run a new dry run. Notes are otherwise turned into calendar changes by compacting them — see [Compacting notes](#compacting-notes) below.

Every label write reads the full label list, changes it in memory, and writes the whole thing back (the API has no way to touch a single label in place), which is a lost-update race if two callers do this concurrently. `CalendarClient.replace_event_labels` (what `Actions` builds every label change on) guards against that with the calendar's own `etag`: it's sent back as an `If-Match` precondition on the write, so a write based on a label list that's since changed fails with `EventLabelConflictError` (surfaced as a `ToolError`) instead of silently overwriting the other change. Confirmed empirically against the real API, since Google's docs only document `If-Match` for Events, not Calendars.

There's deliberately no MCP tool or CLI command that creates the metadata tabs: each store creates its tab the first time it's used.

### Why tools, not resources

MCP has both **tools** (model-controlled: the model decides when to call one, with whatever arguments it computes, as part of its own reasoning) and **resources** (application-controlled: a human typically browses and attaches one via the host's UI, like Claude Desktop's "Attach from MCP" picker). This server only uses tools, for two reasons:

- **Portability.** Resources depend on the host having built UI (or another bridging mechanism) for the model to reach them at all; a lot of MCP clients — agentic ones especially — only implement tool-calling and skip resources entirely. Tools work everywhere.
- **Fit.** The natural workflow here is model-driven, not human-browsing-driven: the model discovers an event's ID via `list_events`, then immediately wants to act on it — fetch details, update, delete. That's a tool-calling pattern (one tool's output, the `id` field, feeds directly into the next tool's input) with no need for a resource-URI layer in between. Nobody is going to browse a picker UI for an event by its opaque Google Calendar ID.

## Compacting notes

**The last compaction is marked in Google Calendar**, as one bright red (Tomato) event, 5 minutes long, ending when it ran. It's on a small calendar of its own, **Compactions** (created the first time, in the main calendar's time zone and colored red; its id is kept on the main calendar), so nothing that reads events — listing, compacting, reallocating — ever sees it, and the main calendar's events never overlap it. There's only ever one: it has a fixed id, so each compaction moves it (restoring it if it was deleted by hand), and each move deletes anything else on that calendar. Moving it is best effort: a failure is a warning on the compaction's result, never a failed compaction, and the next one (or committing again) puts it right.

Notes are jotted down as things happen ("leaving for salsa early to prep", "done with dinner"), and they're sparse: you note what's notable, not every event boundary. Compacting realigns the day's planned events to those notes: the past becomes fact, and the future reflows around it. **Silence means on schedule.** A past event that no note contradicts is recorded exactly as planned. A missing start or end note never makes an event disappear or merge into its neighbor.

Reading free-form text is a job for a model, and the MCP client already is one — so the server has no LLM of its own. Instead the work is split. The client compares the notes to the plan and decides what changed; the server validates those decisions and does everything risky deterministically, previewably, and recoverably.

**The flow** (three tools):

1. **`prepare_compaction`** (read-only) returns every day of uncompacted notes up to now — each note with an id that is its timestamp and sheet row together (`2026-01-01T09:05:00+00:00#5`) and a shortlist of nearby planned events as `candidates` — plus those days' planned events, a **`timeline`** showing the two side by side (below), and instructions for the next step. A backlog of more than a week takes more than one round, oldest first; notes written after now wait for the next.
2. **`compact_notes(decisions)`** takes the model's decisions: one `EventDecision` per event the notes show happened differently. It validates them, plans the changes, writes the plan to the journal, and returns it with a `compaction_id` and the resulting `timeline` — **without touching the calendar**. The model shows you the timeline.
3. **`compact_notes(compaction_id=..., dry_run=False)`** applies it, once you've agreed.

**Several days at once, a day at a time.** One compaction takes on every day of notes up to now, and you review them together, but each day is planned on its own, oldest first, and the timeline heads each with its date. A day after the first starts where the one before it ends, and is planned against the calendar as that day's plan would leave it. Every day but the last is wholly in the past, so its plan only records what happened and reflows nothing; only the last day, the one you're in, has a future to reflow. **The night is the border.** Each night between two days gets one decision, made with the earlier day, and its end — when you woke up — is where one day ends and the next begins. The model judges which note, if any, marks waking up: making it the night's `end_note` moves the border to it, so a 05:30 "woke up early" ends the earlier day there, and an 08:30 "finally up" takes every note up to it into the earlier day. A note in the night that doesn't end it ("can't sleep") is just added to the night's description. If you slept in, the next day still starts at the planned wake-up time, and is compacted even with no notes of its own, so the morning the night now runs over is settled there: a past event under it is an overlap the plan names for the model to resolve, never silently pushed along, and whatever is still to come reflows after it. A night you didn't sleep at all is cancelled, and its two days become one long day. Applying the compaction goes a day at a time too: each day's changes, then its notes stamped, before the next. If it stops partway, the days already done stay done.

**Which events are offered: the compaction window.** A day is the one the oldest uncompacted note (of those not already in an earlier day) falls in. It starts when the last end-of-day sleep event that began before that note ends, or at the note itself if it was written before then (you woke up early). It runs to the end of the next end-of-day sleep event. A day can take several compactions, so the events offered — the *compaction window* — start at whichever is later: the day's start, or the last stamped compaction's `now` (for a later day of the same compaction, the day before it's end). Whatever an earlier compaction already settled isn't offered again. The one event that ended within 15 minutes before the compaction window starts is offered too, so an event the last compaction closed off ("done reading", or last night's sleep) can still be stretched. The latest compacted note, however long ago it was written, comes along as `previous_note` — context only, for what was going on as the window began — and the timeline shows it too, beside when the last compaction ran.

**Decisions** (`EventDecision.action`):

- `keep` (an `event_id`): it happened. Each edge stays where it was planned unless the decision moves it: `start_note`/`end_note` (a note id) sets that edge to the note's time and links the note to it; `start`/`end` gives an explicit time (alongside a note, when the note gives a time relative to itself — "leaving 15 minutes early"). `keep` also takes `summary` (rename it) and `annotate` (add text to its description). A `keep` that moves a *future* event is a direct reschedule — "move lunch to 12:15 and adjust the afternoon" — pinned there, with the rest of the day reflowing around it in the same plan.
- `cancel` (an `event_id`): it didn't happen.
- `create` (`summary`, a start and an end — each a time or a note — and optionally `event_label_id`): something unplanned happened.
- `merge` (an `event_id`, `into` another): fold one event into another, for when you don't remember where one ended and the next began. The target grows to cover both and is titled after both (unless renamed); the merged one is cancelled. The model is told to do this only after you've confirmed it — never just because the notes are sparse.

**Actions and facts.** Compaction establishes each past event's facts, which traits are judged from later: what the user was doing (its **actions**), where it was, who was there, who it was for, and a subjective note on each person who was there (`self` for the user) — whatever might help judge it later. `prepare_compaction` sends every active and proposed action (their groups left out), every active person and every location, to settle them from. `keep` and `create` take `action_ids` (the first setting the event's color; for `keep`, leaving them out keeps them, and `[]` clears them) and `facts` ([`utilities/facts.py`](utilities/facts.py): `location_id`, `with_ids`, `for_ids` and `notes`, replacing the event's facts whole). For a past event with no actions, `suggested_action_ids` are those of the latest event with the same title in the previous 4 weeks. When an action, a person or a place isn't there yet, the model adds it in the same call — `new_actions`, `new_people` and `new_locations`, each with a `ref` like `new:ukulele` that the decisions use in place of its id — rather than by separate tools, so the user confirms them with the plan; they're created when it's applied (or found again, if a commit is resumed), and every ref is replaced by the real id as events are written. Facts are stored in the event's private extended properties as JSON, split across several when they're long. Unknown or deleted actions, people and locations that aren't there, and refs that aren't added are all refused at the dry run.

Notes that don't set an edge have their text added to the description of the event they fall within (with their time), so they survive compaction. `compact_notes`'s `ignore_notes` lists any that shouldn't be added. Google Calendar silently cuts a description longer than 8,192 characters (`MAX_DESCRIPTION_BYTES`, counted as UTF-8 bytes to be safe), so a plan that would grow one past that is rejected, naming the notes to leave out.

**What happens to the rest of the calendar** (all in `utilities/note_compaction.py`'s `plan_compaction`). Every resulting past event — decided, or untouched and so on schedule — is pinned (`is_fixed_time`), so no later reallocation moves history. **Past events can't overlap.** If dinner's noted end runs into the reading planned after it, the plan is rejected and the error names both events. The model then has to say which edge gives way, asking you if the notes don't tell it. End-of-day sleep events count too: a note can't silently eat into last night's sleep. Everything else — the future, and anything still in progress — reflows around the facts using `utilities/reallocation.py`, simulated in memory so the plan can be previewed.

**Moving bedtime** (the day's own end-of-day sleep event) works differently, since nothing in the day comes after it for the rest to reflow into: it moves where the day ends. A later bedtime just leaves the evening free; an earlier one shortens whatever runs past it to end there and cancels what starts after it (or can't be shortened that far), and a fixed-time event in the way is an error. Its end — your wake-up time — is the start of the *next* day. When the next day is in the same compaction, moving the wake-up moves the border between them, as above. When it isn't, compacting this day never adjusts it: moving only its start changes only the bedtime, and if its end does move, the plan carries a warning to check the next morning's events (and move them with `update_event` if they now overlap).

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

`→` marks an edge the note just above it set, and `↳` the event a note's text was added to. A past event with no tag happened as planned; `+`/`−` is how late or early an edge was, and `⇢`/`⇠` how far a whole event moved. Each event's actions go on the line under it (`◆` had, `◇` being given), then its facts — where, with whom, for whom, then a line per person's note (`▸` had, `▹` being set) — each action's total time follows the events, and a short legend ends it. Long lines wrap, indented under their text.

**Safety.** Before anything is applied, the whole approved plan — the decisions, and every step with its before-state — is written to the **Compactions** tab (see [Calendar metadata sheets](#calendar-metadata-sheets)). Committing first checks the notes and calendar still match what was previewed, then applies each step and checks it off; a failure partway leaves the journal at exactly that point, and calling `compact_notes` with the same `compaction_id` again finishes only what's left. Created events get deterministic ids (Calendar accepts a caller-chosen id), so a retried create can't duplicate. Each day's notes are stamped compacted only after everything for that day applied, so nothing is lost if it dies. While a compaction is unfinished a new one is refused until it's resumed or `abandon_compaction`ed (steps already applied stay applied).

**Rejecting or changing a plan.** Nothing touches the calendar until the commit, so a plan you don't like costs nothing: tell the model what's wrong, it corrects the decisions, and it calls `compact_notes` again with them — no need to call `prepare_compaction` again unless the notes or calendar changed. Each new dry run replaces the earlier unapplied plans (they're marked `abandoned`), so a stale one can't be committed by mistake. Once a compaction has been applied, its events are ordinary events: adjust them with `update_event` like any other.

**Rejections are logged.** Every compaction tool call refused with a `CompactionError` (`compact_notes`, `edit_note`, `delete_note`, `prepare_judgments`, `record_judgments`, `abandon_compaction`) logs one warning line naming the tool, the categories of mistake it held, and the full message: `compaction rejected: tool=compact_notes categories=overlap,unknown_note: ...`. Each problem is tagged where it's found (`utilities/note_compaction.py`'s `Problem`) — `overlap`, `unknown_note`, `unknown_event`, `duplicate_decision`, `nonpositive_length`, `malformed_decision`, `note_after_now`, `description_too_long`, `fixed_time_conflict`, `reflow_failed`, `facts`, `actions`, `additions`, `stale`, `open_compaction`, `judgment`, and a few for refused state changes — so the logs show which instructions a model gets wrong, and how often.

There's no CLI command for this: comparing the notes to the plan needs a model, which is what the MCP client is.

## Calendar metadata sheets

Each calendar this app manages has one shared **calendar metadata spreadsheet** ([`utilities/calendar_metadata_sheet.py`](utilities/calendar_metadata_sheet.py)), titled "Calendar Metadata" — one tab per kind of Sheet-backed data this app keeps for that calendar. Today that's:

- **Actions** and **Action Groups**, **People** and **Circles**, **Locations** — one row per item, read by header name (so you can reorder columns or add your own; see [Actions](#actions) and [People, circles and locations](#people-circles-and-locations) below). Ids are short and assigned; a list or object (a person's circles, their traits) is JSON in its cell.
- **Traits** — one row per trait: id, name, status, definition and parts (JSON). See [Traits](#traits) below.
- **Noted Times** — time notes: three columns, **Timestamp**, **Description** (optional) and **Compaction ID** (blank until the note has been compacted; see [Compacting notes](#compacting-notes)). A note's id is its timestamp and row together, and stamping a note verifies its row still holds that timestamp — which also catches a row shifted by garbage collection (below), not just a hand edit.
- **Compactions** — the write-ahead journal of note compactions: one row per fact (the compaction itself, each decision, each calendar change with its before/after state and whether it's been applied). See `utilities/compaction_journal.py`.

Both the Noted Times and Compactions tabs only ever grow, so each keeps itself under a row budget (50 for notes, 100 for compactions) by physically deleting old, no-longer-needed rows from the top once it goes over — trimming it to half that (25 and 50), so the very next append doesn't set it off again. That keeps both small enough to read whole, in one request each, every time (Google Sheets throttles read requests to 60 a minute). `NotedTimeSheet.garbage_collect` (run before `note` appends a new one) deletes already-*compacted* notes, always keeping the one with the latest timestamp (the next compaction shows it as context), and `CompactionJournal.garbage_collect` (run when `prepare_compaction` starts) deletes fully-finished compaction blocks, always preserving whichever compaction is still open or merely planned and the most recently *stamped* one (its `now` anchors the next compaction round — see [Compacting notes](#compacting-notes)). Both stop at the first row they can't safely delete, so an unusually large uncompacted backlog, or an unusually long-lived compaction, can leave a tab over budget rather than lose something still needed. Deleting rows also shrinks the tab's grid, and a write past the end of the grid fails, so whenever garbage collection leaves a tab with fewer than 1000 rows in all (`calendar_metadata_sheet.MIN_TAB_ROWS`, what Sheets gives a new tab), empty rows are added at the bottom to make up the difference.

**Finding a tab doesn't rely on its title or position.** Both the spreadsheet and each tab within it are located by a stable tag instead, so renaming a tab (or the spreadsheet itself), or reordering tabs, never breaks the app's ability to find the right one again:

- The spreadsheet's id is recorded directly on the calendar itself, via `CalendarClient.get_calendar_metadata`/`set_calendar_metadata`: since the Calendars resource has no dedicated field for arbitrary app metadata (confirmed against the API reference — unlike Events' `extendedProperties`), these embed a small `[cascading-time-tracker:key=value]` marker as its own line in the calendar's `description`, clearly delimited from whatever human-readable text is already there, and guarded by the calendar's `etag` exactly like the event-label writes below.
- Each tab is tagged with Google Sheets [developer metadata](https://developers.google.com/workspace/sheets/api/guides/metadata) — a `sheet-role` key (`actions`, `uncompacted-time-notes`, ...) attached to that tab's `sheetId`, `PROJECT`-scoped so only this app's own OAuth client can see or query it. `SheetsClient.create_sheet_metadata`/`find_sheet_id` are the low-level calls; `calendar_metadata_sheet.ensure_tab` is what everything else uses — find the tagged tab, or create (and tag) a new one if there isn't one yet.

**A tab's title and color are purely a visible cue for you, not how the app finds it again.** Every tab this app manages gets a human-readable title and the same tab color, set once at creation, so you can tell at a glance which tabs it's using when you open the spreadsheet — but since the actual lookup is by tag, you're free to rename a tab (or the spreadsheet) afterwards without breaking anything.

## Actions

Actions replaced goals: an action is a verb for what the user is doing in a moment ("play guitar", "eat a meal"), and events are tagged with them. Groups roll actions up ("Creative" → "Guitar" → "play guitar") for targets and finding one's way around, but aren't actions themselves: no event can be tagged with one, and actions are always the leaves.

- **Each action reserves one event label.** Active actions always hold theirs; proposed ones (made by the assistant and not yet reviewed) hold theirs while there's room among the calendar's 200; archived and deleted ones don't, and get the same label back if they're made active again. A label is named after its action and colored with its own `background_color`, else its nearest group's, else its priority's color (its own or inherited); that's its `effective_color`, and the priority its events take is its `effective_priority`. Labels that aren't any action's — Calendar's own unnamed ones, and the labels of the goals actions replaced — are left alone. A change that would need more labels than the calendar has room for for its active actions is refused before anything is written.
- **Events do actions.** `Event.action_ids` (a private extended property) records which; the first decides the event's label (`utilities/action_calendar.py`). Calendar rejects *inserting* an event with a label it doesn't have, but accepts re-sending one an existing event already has, so an update keeps an archived action's own label on an event that already carries it. An event never given actions but carrying an action's label (one picked in Calendar, say) is read as doing that action.
- **Priority is inherited.** An action without its own priority takes its nearest group's, and an event without its own takes the highest (lowest-numbered) among its actions'. `Event.action_priority` holds what it inherits (filled in on read, never written), and `effective_priority` — what reallocation and compaction read — combines the two.
- **Names are unique**, ignoring case: among actions (whatever their status) and, separately, among groups — an action may share a group's name. Deleting a group moves its actions and groups up into its own enclosing group.

## People, circles and locations

- **People** are who the user spends time with: a name, a `context` that tells them apart ("met at salsa" — a name and context together are unique), a status (`active`, `archived` or `deleted`), the `circles` they're in, `what_matters` to them, and their `traits` (which traits apply to them, and their own parts for any — see [Traits](#traits)). The user is always one of them, with the id `self`, whether or not the People tab has a row for them.
- **Circles** are groups people belong to ("Family"), a person belonging to any number; names are unique. Deleting a circle takes its people out of it.
- **Locations** are where events happen: a unique name and a `hint` for recognizing when an event or note refers to it.

## Traits

How the user wants to be with people (and with themselves): Thoughtful, Reliable, Creative, Adventurous and Generous to start with. Nothing about a trait is hard-coded.

- **The Traits tab** of the metadata spreadsheet holds each trait's id (a slug of its first name, never changed), name, status (`active`, `off` or `archived`), definition and parts (JSON). It's created and seeded the first time it's needed, can be edited by hand, and `create_trait`/`update_trait` check a trait before saving it ([`utilities/traits.py`](utilities/traits.py)'s `trait_problems`), so a bad part is refused rather than silently unrated.
- **Parts.** Each part is one of `judgment` (each event judged by the assistant against a `rubric`, on a scale of `ratings` such as `{"0": "routine", ..., "3": "adventurous"}`, from the `facts` it names: `action`, `action_history`, `location`, `location_history` (the history ones with a `lookback_days`, 30 by default), `general_notes`, `person_notes` and `what_matters` (what's important to the person, from their `what_matters`)), `continuity`, `count`, `duration` or `follow_through`. Every part can take a `weight` and an `engagement_type`: `with` (the default; the events the person was at) or `for` (what was done for them while they weren't there). `continuity`, `count`, `duration` and `follow_through` can take an `action` (an action or action group id), to count only its events. `get_traits` describes each.
- **Per person.** A person's `traits` ([`utilities/people.py`](utilities/people.py)) select which traits apply to them (`"select": "all"` or a list of trait ids) and can replace a trait's parts for them alone (`"parts": {"reliable": [...]}`), so each person keeps their own cadences. Without it, every active trait applies, as the Traits tab has it.
- **Facts.** Traits are read off the facts compaction establishes for each event (see [Compacting notes](#compacting-notes)).
- **Scores.** Once a compaction is complete (its judgments recorded), every day it settled is rolled up ([`utilities/trait_rollup.py`](utilities/trait_rollup.py)) into each active person's trait scores, kept in the **Trait Scores** tab: one row per person per day, with each trait's score (0-100) and each part's, and how it was reached. A part reads the person's events -- every event, for the user; for anyone else, those naming them in `with_ids` (or `for_ids`, for a `for` part), planned events being tagged with their people ahead of time, so a cancelled one counts against follow-through. Judgment parts score the mean of their ratings (rating / scale) over a trailing `window_days` (30 by default); continuity, count, duration and follow-through are computed from the events ([`utilities/trait_scores.py`](utilities/trait_scores.py)); a trait is the weighted mean of its parts, leaving out any with nothing to score it by. A day is a calendar date in the calendar's time zone, scored once it's over; the tab keeps the last 400 days. `get_trait_scores` reads them, and `rebuild_trait_scores` rolls any span up again -- to backfill, or after a judgment is redone or a trait changes.
- **Judgments complete a compaction.** Once its facts are written, each event with facts is judged ([`utilities/judgments.py`](utilities/judgments.py)) for each person it was about — the user (`self`) and everyone there, for a trait's `with` judgment parts; everyone it was done for, for its `for` parts — against every judgment part of the traits that apply to them. The applied compaction's result carries the requests (`judgments`): each with its rubric, ratings, the facts its part names resolved for that event and person (its actions, its location, their history together over the part's lookback, the event's notes, the notes on the person — the user's own for a `for` engagement), and a framing for one person. The model makes them all itself, never asking the user — a rating and one succinct line of reasoning each — and records them with `record_judgments`; the compaction isn't complete until every one is, and `prepare_compaction` and `get_compaction_status` say so (`judgments_pending`). They're kept on the event (`Event.judgments`, JSON in private properties like facts) by person, trait and part, with the scale they were rated on; nothing rolls them up yet. `prepare_judgments(redo=True)` hands them back, with the judgments made, to redo one with more context, and `update_event` can overwrite a rating by hand.

**`get_compaction_status()`** says when notes were last compacted into the calendar, and which note was the latest compacted.

## Event colors

[`Event.to_api_body()`](calendar_clients/google_calendar.py) (used by every path that writes an event — the MCP tools, `calendar_cli.py`, and reallocation, all via `CalendarClient.create_event`/`update_event`) automatically sets `colorId` from the event's `priority`, so priority is visible at a glance in the Google Calendar UI without a separate step:

| Priority | Color | `colorId` |
| --- | --- | --- |
| <=0 | Graphite (gray) | `"8"` |
| 1 | Banana (yellow) | `"5"` |
| 2 | the calendar's default color | unset |
| >=3 | Sage (soft green) | `"2"` |

Note that calendar colors are superseded by the colors corresponding to the event's label. `event_label_id` (the API's own `eventLabelId` field) is derived from the event's actions (see [Actions](#actions) above) and is read-only to the MCP tools; `calendar_cli.py`'s `update_properties` can still set it directly (`event_label_id=<label-id>`). Calendar itself tolerates an existing event pointing at a label that's since been removed (it keeps the id, and the event can still be updated), but it rejects *inserting* an event with one. So creating an event with a label this calendar doesn't have fails up front, before reallocation changes anything, and an event split off another by reallocation (or compaction) quietly drops a removed label rather than failing the whole call. A `create` decision in compaction naming an unknown label fails its dry run. Updating an event doesn't check its label.

## Google OAuth credentials

`calendar_clients/google_auth.py`'s `load_credentials` — used by both `CalendarClient.from_credentials` and `SheetsClient.from_credentials` (and by `config`'s builders, which load it once and share it between the two — see [Project layout](#project-layout) above) — needs two files, neither of which should ever be committed:

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

[`probes/metadata_tabs.py`](probes/metadata_tabs.py) runs the actions, action groups, people, circles and locations tabs end to end against Google, on a scratch calendar it deletes afterward. It leaves the scratch spreadsheet in Drive, titled "CTT tabs probe (safe to delete)", for you to trash by hand:

```bash
python -m probes.metadata_tabs
```

[`probes/cancelled_events.py`](probes/cancelled_events.py) checks what Google Calendar returns for your cancelled events, which `follow_through` trait parts depend on: it lists the configured calendar's last `--days` (default 14) days with and without `showDeleted`, and prints, for each cancelled event, which fields it kept (start, end, `action_ids`, label, ...). It only reads. `--raw` prints each one's JSON too:

```bash
python -m probes.cancelled_events --days 30
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
# them (each action in play holds one -- see the action commands below)
python calendar_cli.py list_raw_labels

# Create a new event label -- background_color is required
python calendar_cli.py create_raw_label background_color="#8e24aa" name="Design Work"

# Update an existing event label's color and/or name
python calendar_cli.py update_raw_label <label-id> background_color="#d50000"

# Delete an event label by id
python calendar_cli.py delete_raw_label <label-id>

# List the proposed and active actions (--status for others, --all for every status)
python calendar_cli.py list_actions

# Create an action (its id is assigned; list_actions shows it)
python calendar_cli.py create_action name="Play guitar" status=active priority=1

# Update an action's properties -- archiving frees its label
python calendar_cli.py update_action <action-id> status=archived

# Blank properties instead (here, so it's in no group)
python calendar_cli.py update_action <action-id> --clear group_id

# Tie an event to actions (the first sets its color), which also sets its label
python calendar_cli.py update_properties <event-id> action_ids=<action-id>,<other-action-id>

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

`list_raw_labels`/`create_raw_label`/`update_raw_label`/`delete_raw_label` manage this calendar's custom event labels exactly as Google Calendar's API represents them (`CalendarClient.list_event_labels`/`create_event_label`/`update_event_label`/`delete_event_label`, `calendar_clients/google_calendar.py`'s `EventLabel`) — a richer, arbitrary-hex-color alternative to `Event.colorId`'s 11 fixed colors, but with no concept of priority: Calendar itself has no field for one, so `create_raw_label`/`update_raw_label` take only `background_color=value`/`name=value` pairs, and `background_color` is required for `create_raw_label`. Labels are normally managed through actions (below), which leave any label that isn't an action's alone.

`list_actions` (proposed and active actions; `--status <status>`, repeatable, for others, or `--all`), `create_action` and `update_action <action_id>` manage this calendar's actions, the same way the MCP tools do (see [Actions](#actions) above), and each prints the resulting actions, one per line: id, status, and path. They take `key=value` pairs for an action's fields (`name`, `group_id`, `status`, `background_color`, `priority`, `note`); `update_action` also takes `--clear <attribute>`, repeatable. `update_properties`'s `action_ids=a1,a2` sets an event's actions, and the label they imply.

`note` takes a positional `ago` (required -- like `from`/`to` above, a pytimeparse duration, e.g. `"1h"`, `"90m"`, `"0s"` for right now, resolved via `resolve_note_timestamp` the same way `list`'s window is resolved via `resolve_window`) and an optional positional `description` (quote it if it contains spaces), builds a `utilities/noted_time_sheet.py` `NotedTime` from them, and appends it to this calendar's noted-times tab via `NotedTimeSheet.append` — see [MCP tools](#mcp-tools) and [Calendar metadata sheets](#calendar-metadata-sheets) above. `get_notes` lists every uncompacted note with its id (via `NotedTimeSheet.read_with_rows`). `edit_note <note_id>` takes `--ago` (a duration before now, like `note`'s) or `--at` (an ISO 8601 time, local if it has no UTC offset) for a new time, and/or `--description` (`""` clears it); `delete_note <note_id>` removes the note. Both refuse a compacted note, a stale id, or a note in a compaction that's partway through being applied, printing why and exiting non-zero — see `edit_note`/`delete_note` under [MCP tools](#mcp-tools).


## Running tests

With the dev dependencies installed:

```bash
pytest
```
