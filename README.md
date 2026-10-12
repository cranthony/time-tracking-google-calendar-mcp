# time-tracking-google-calendar-mcp
A Model Context Protocol (MCP) for managing a Google calendar with a set of non-overlapping events representing one person's time

## Setup

The server is written in Rust, in [`rust/`](rust/). Install Rust with [rustup](https://rustup.rs), then build it:

```bash
cargo build --release --manifest-path rust/Cargo.toml
```

That builds the server (`ctt-server`) and the command-line tools (`create_calendar`, `calendar_cli`, `probe`) into `rust/target/release/`. On Windows, use the GNU toolchain (`rustup default stable-x86_64-pc-windows-gnu`) with a MinGW-w64 toolchain on `PATH` (e.g. `winget install BrechtSanders.WinLibs.POSIX.UCRT`), or the MSVC one with Visual Studio's build tools.

The [conformance scenarios](#tests) are run by a Python harness; to run them, install its dependencies in a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

## Project layout

All of it is in [`rust/src/`](rust/src/) (`lib.rs` lists the modules the server and the tools share):

- [`main.rs`](rust/src/main.rs) — the server: loads `.env`, builds the [`App`](rust/src/app.rs) (configuration, the Google clients, and each store, built the first time a tool needs it), and serves MCP over stdio or streamable HTTP ([`mcp/`](rust/src/mcp/): the protocol, argument checks against each tool's input schema, and the HTTP transport with WorkOS bearer-token checks, CORS and the OAuth proxy for browser clients).
- [`tools/`](rust/src/tools/) — the MCP tools, each taking its checked arguments and returning its result as JSON. Their names, descriptions and input/output schemas, which tools/list sends, are derived from the code ([`contract.rs`](rust/src/contract.rs): each tool's doc comment and its parameters' and result's types, via `#[tool]` and `#[derive(Schema)]` from [`derive/`](rust/derive/)). [`contract/tools.json`](contract/tools.json) is a snapshot of them that a test keeps current: after a deliberate change, `CTT_UPDATE_CONTRACT=1 cargo test` rewrites it, and its diff shows what clients will see change.
- [`google/`](rust/src/google/) — Google API access: the OAuth token (refreshed as it expires, and the consent flow the command-line tools run for a first one), [`calendar.rs`](rust/src/google/calendar.rs) (`CalendarClient`: events, labels, the calendar's own metadata) and [`sheets.rs`](rust/src/google/sheets.rs) (`SheetsClient`). Inside a tool call, a `list_events` within a range already listed is answered from memory (`CachedListings`), and so is a tab read again (`CachedReads`); any write forgets what it touches, and nothing is kept between calls, so hand edits are always seen. Sheets requests retry with backoff on 429 (its read quota is 60 a minute).
- [`event.rs`](rust/src/event.rs) and [`facts.rs`](rust/src/facts.rs) — an event as the server sees it, its facts, and how both are written to Calendar.
- [`sheet/`](rust/src/sheet/) — the calendar's metadata spreadsheet and its tabs (see [Calendar metadata sheets](#calendar-metadata-sheets)), and tabs of one item per row, read and written by header name.
- [`stores/`](rust/src/stores/) — what's kept: actions and groups, people and circles, locations, traits, habits, cancellations, the noted-times tab, the compaction schedule, the compaction journal; and [`event_changes.rs`](rust/src/stores/event_changes.rs) (batches of event changes, refused if they'd overlap), [`action_calendar.rs`](rust/src/stores/action_calendar.rs) (events read and written through their actions) and [`recurrences.rs`](rust/src/stores/recurrences.rs) (recurring series).
- [`compaction/`](rust/src/compaction/) — compacting notes: the planner ([`plan.rs`](rust/src/compaction/plan.rs)), proposals, what a compaction adds, judgments, and [`compactor.rs`](rust/src/compaction/compactor.rs), which ties them to the journal; [`timeline.rs`](rust/src/timeline.rs) draws them.
- [`health.rs`](rust/src/health.rs) — health metrics for `get_health`.
- [`bin/`](rust/src/bin/) — the command-line tools: [`create_calendar`](rust/src/cli/create_calendar.rs), [`calendar_cli`](rust/src/cli/calendar_cli.rs) and [`probe`](rust/src/bin/probe.rs) (see [Command-line utilities](#command-line-utilities)).

The server began as a Python one, and was ported to Rust call for call: some names (and the error texts) still read the way Python wrote them.

Also here: [`conformance/`](conformance/) (the [tests](#tests)), [`scripts/`](scripts/) and [`render.yaml`](render.yaml) (see [Deploying](#deploying)).

## Calendar access model

This app requests only the `calendar.app.created` OAuth scope (see `SCOPES` in [`rust/src/google/mod.rs`](rust/src/google/mod.rs)) — not the broader `calendar.events`/`calendar.events.owned` scopes. That has real consequences:

- This app can only read and write events on calendars **it has created itself**.
- It has **no access to the user's existing calendars** — not `"primary"`, not any calendar they made by hand in the Calendar UI. API calls against any calendar this app didn't create itself will fail.
- This is deliberate: a compromised or misbehaving instance of this app cannot read or touch anything outside the dedicated calendar(s) it made for itself.

`SCOPES` also includes `drive.file`, for the [calendar metadata sheet](#calendar-metadata-sheets) — the same "can't touch anything it didn't make" model as `calendar.app.created`, but for Google Sheets: this app can only see/create spreadsheets it made itself, never the user's other Drive files. It's required by the Sheets API's own `spreadsheets.create` even though this app never calls the Drive API directly — a spreadsheet's id is looked up from the calendar itself, not by searching Drive (see [Calendar metadata sheets](#calendar-metadata-sheets)). If you're upgrading an install whose `token.json` predates this scope, delete it and let the consent flow run again (see [Google OAuth credentials](#google-oauth-credentials) below) — the old token won't carry the new scope.

**Bootstrapping:** there's no calendar to operate on until this app creates one. Run [`create_calendar`](rust/src/cli/create_calendar.rs) once (see [Command-line utilities](#command-line-utilities)) — it only needs `GOOGLE_OAUTH_CREDENTIALS_PATH`/`GOOGLE_OAUTH_TOKEN_PATH` (see [Configuration](#configuration) below), not `GOOGLE_CALENDAR_ID` — and it prints the new calendar's ID, then ensures that calendar's metadata spreadsheet (Actions and uncompacted time notes tabs both provisioned) in the same step (see [Calendar metadata sheets](#calendar-metadata-sheets) below) and prints its URL too:

```bash
create_calendar "Time Tracking"
```

Then set `GOOGLE_CALENDAR_ID` to the ID it prints. If the app hasn't been used to create a calendar yet — including the very first time you set this up — this is the step to run first.

If `GOOGLE_CALENDAR_ID` is *already* set when you run it, `create_calendar` doesn't create a new calendar at all — it just ensures the metadata spreadsheet and its tabs for that already-configured calendar instead (useful if you set this app up before the spreadsheet, or one of its tabs, existed). Either way, provisioning the spreadsheet/tabs themselves is a one-time, human-run bootstrap step just like the calendar itself — there's deliberately no MCP tool or CLI command for *that* (though there is for adding data to them once they exist — see [MCP tools](#mcp-tools) and [Calendar metadata sheets](#calendar-metadata-sheets) below).

## MCP tools

The server exposes the calendar as MCP tools ([`rust/src/tools/`](rust/src/tools/); a snapshot of their schemas is [`contract/tools.json`](contract/tools.json)):

| Tool | Signature |
| --- | --- |
| `list_events` | `(min_time, max_time) -> list[PublicEvent]` |
| `get_event` | `(id) -> PublicEvent` |
| `update_event` | `(updates: list[EventUpdate] \| None, creates: list[PublicEvent] \| None, cancels: list[EventCancel] \| None, shifts: list[EventShift] \| None, allow_compacted_changes: bool = False, dry_run: bool = False) -> EventChangesResult` |
| `create_event` | `(events: list[PublicEvent], dry_run: bool = False) -> EventChangesResult` |
| `delete_event` | `(cancels: list[EventCancel], allow_compacted_changes: bool = False, dry_run: bool = False) -> EventChangesResult` |
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
| `get_habits` | `(statuses: list[HabitStatus] \| None) -> list[ListedHabit]` |
| `get_habit` | `(id_or_name: str) -> ListedHabit` |
| `create_habit` | `(habit: Habit) -> CreatedHabit` |
| `update_habit` | `(habit: Habit, clear_fields: list[HabitField] \| None) -> ListedHabit` |
| `get_traits` | `(statuses: list[TraitStatus] \| None) -> list[ListedTrait]` |
| `create_trait` | `(trait: Trait) -> Trait` |
| `update_trait` | `(trait: Trait, clear_fields: list[TraitField] \| None) -> Trait` |
| `get_compaction_status` | `() -> CompactionStatus` |
| `get_compaction_schedule_hints` | `() -> CompactionScheduleHints` |
| `set_compaction_schedule_hints` | `(hints: list[ScheduleHint]) -> CompactionScheduleHints` |
| `get_health` | `(tool_name: str \| None = None, samples: bool = True) -> HealthReport` |
| `note` | `(noted_time: NotedTime) -> NoteWithId` |
| `get_notes` | `(include_compacted: bool = False) -> list[NoteWithId]` |
| `edit_note` | `(note_id: str, timestamp: datetime \| None, description: str \| None) -> NoteWithId` |
| `delete_note` | `(note_id: str) -> NotedTime` |
| `prepare_compaction` | `() -> CompactionContext` |
| `compact_notes` | `(updates: list[CompactionUpdate] \| None, creates: list[CompactionCreate] \| None, cancels: list[EventCancel] \| None, ignore_notes: list[str] \| None, proposal_id: str \| None, revision: int \| None, replies: list[FeedbackReply] \| None, new_actions: list[NewAction] \| None, new_people: list[NewPerson] \| None, new_locations: list[NewLocation] \| None, annotate_notes: list[NoteAnnotation] \| None) -> CompactionResult` |
| `get_proposal` | `(proposal_id: str \| None, since_revision: int \| None) -> Proposal` |
| `amend_proposal` | `(proposal_id: str, revision: int, updates: list[ProposalUpdate] \| None, creates: list[ProposalCreate] \| None, cancels: list[EventCancel] \| None, as_planned: list[str] \| None, notes: list[NoteEdit] \| None, additions: list[AdditionChoice] \| None) -> Proposal` |
| `add_proposal_note` | `(proposal_id: str, text: str, event_id: str \| None, at: datetime \| None, note_id: str \| None) -> Feedback` |
| `withdraw_proposal_note` | `(feedback_id: str) -> Feedback` |
| `confirm_proposal` | `(proposal_id: str, revision: int) -> ProposalResult` |
| `finish_proposal` | `(proposal_id: str) -> ProposalResult` |
| `prepare_judgments` | `(compaction_id: str \| None, redo: bool = False) -> JudgmentsDue` |
| `prepare_habit_judgments` | `(habit_id: str, since: datetime \| None, redo: bool = False) -> HabitJudgmentsDue` |
| `record_judgments` | `(compaction_id: str, judgments: list[Judgment]) -> JudgmentsResult` |
| `abandon_compaction` | `(proposal_id: str) -> CompactionResult` |
| `set_time_zone` | `(time_zone: str) -> str` |

**Batches of event changes.** `create_event`, `update_event` and `delete_event` each take several events at once, as one batch ([`rust/src/stores/event_changes.rs`](rust/src/stores/event_changes.rs)): `update_event` takes `updates` (an `EventUpdate`: the event's id and the fields to set, with its own `clear_fields`), `creates`, `cancels` (an `EventCancel`: an event id and `counts_against_follow_through`, required — a commitment dropped, or just a change of plan) and `shifts` (an `EventShift`: event ids and `minutes`, moving each by the same amount — each an update); `create_event` takes only creates, and `delete_event` only cancels. An event can be in only one of them. **Nothing is moved to make room:** every event the batch creates or updates must end after it starts and must not overlap any other event, on the calendar or in the batch, so whatever's in the way is moved, shortened or cancelled in the same call. A batch that breaks this changes nothing, and the error says everything needed to send a valid one at once: the rule, every problem, and the calendar from the earliest time the batch touches through the end of that night, as the batch would leave it, its events starred and history marked. Overlaps between events the batch doesn't move or create are left alone, and an update that doesn't move an event isn't checked. Each returns an `EventChangesResult`: the events changed, and a `timeline` of them beside the events around them, drawn as compaction draws its plans; `dry_run` returns that without writing anything. Cancelling patches the event's `status` to `"cancelled"` rather than deleting it, as `Event.status` recommends for a recurring event's instance. **History is protected:** everything before the last compaction (its `now`, `get_compaction_status`'s `last_compaction`) is history. An event that started before then can't be changed or cancelled, and no event can be created, or moved, to start before then, unless `allow_compacted_changes` is set — which the tools say to set only when you've explicitly approved changing history — except that an event still going on then may run on (an update moving only its end, to no earlier than the last compaction). Refusals are logged like compaction's (see [Rejections are logged](#compacting-notes)).


Every event tool uses `PublicEvent`, not the server's whole event, as its input/output type — the event minus its internal fields, plus `is_cancelled` (which has no `Event` equivalent — it's derived from the hidden `status` field). `is_end_of_day_sleep` and `recurring_event_id` are visible but read-only, like the `effective_*` fields below: `PublicEvent.to_event` ignores them, so `update_event`/`create_event` can't change them. `recurring_event_id` is assigned by Google, and `is_end_of_day_sleep` decides where compaction's days end, so a wrong mark would quietly change what each compaction takes on; it's set by hand with `calendar_cli update_properties`. Agents communicating with this MCP only see the fields in `PublicEvent`. `calendar_cli` still operates on the whole event and has full access to every field, since it's a human-run dev tool, not something the agent talks to.


Every event tool's `PublicEvent`s also carry a read-only `effective_priority`: the priority that applies, i.e. `Event`'s property of the same name: the event's own `priority`, falling back, when it has none, to the highest priority among all its actions (each action's own, or its nearest group's — filled in via `rust/src/stores/action_calendar.rs`'s `fill_in_from_actions`; see [Actions](#actions) below). `priority` itself stays the event's own value, and `update_event`/`create_event` ignore `effective_priority`, so sending a listed event straight back to `update_event` never copies its actions' priority onto the event — which would otherwise stop it following later changes to its actions.

`PublicEvent.action_ids` are what was done at an event, the first setting its color; it's how `create_event`/`update_event` tie an event to actions (`[]` removes them), and ids that aren't actions are refused with the closest matches suggested. `action_names` (their names, in the same order) and `event_label_id` (derived from the first action) are read-only. `PublicEvent.facts` are what compaction established about a past event (see [Compacting notes](#compacting-notes)); `update_event` can replace them whole, or clear them.

`list_events`/`get_event` still don't surface a cancelled event at all: `list_events` omits it, and `get_event` raises a `ToolError`. But when an operation like `create_event` cancels an event as a side effect of making room, or `delete_event` cancels the event it was asked to remove, that cancellation is a direct result of the agent's own action, so it's worth surfacing rather than hiding — its `PublicEvent` comes back with the rest of its fields intact and `is_cancelled=True`. `is_cancelled` is read-only: `update_event` refuses to set it, so `delete_event` is the one way to cancel an event — and it says whether the cancellation counts against follow-through (`counts_against_follow_through`, `False` by default: a change of plan). There's no way to un-cancel an event through this API.

Calendar creation is deliberately *not* an MCP tool — see [Calendar access model](#calendar-access-model) above — so the model can't create new calendars on its own; that's a one-time, human-run bootstrap step via `create_calendar`.

The action, group, people, circle, location, habit and trait tools manage what events are tagged with and what traits are read from — see [Actions](#actions), [People, circles and locations](#people-circles-and-locations) and [Traits](#traits) below. Each kind has tools to list them all, get one by id or name (suggesting close matches when there's none), create one (returning the new id as `created_id`) and update one (whichever fields are given are set, and those in `clear_fields` blanked; ids never change). Action groups, circles and locations have no status, so they have delete tools; actions, people and habits are archived or deleted by status instead, so events naming them still make sense.

The `note` tool records a new time note (`rust/src/stores/notes.rs`'s `NotedTime`: a required `timestamp`, and an optional free-text `description` of what it marks) by appending it to this calendar's noted-times tab (via `NotedTimeSheet.append`, which writes only the new row — see [Calendar metadata sheets](#calendar-metadata-sheets) below), returning the note as recorded along with its id (`NoteWithId`: the note's timestamp and sheet row together, e.g. `2026-01-01T09:05:00+00:00#5`). A caller can't set a note's `compaction_id`; only compaction does. `get_notes` lists the notes that haven't been compacted yet, sorted by timestamp and each with its id (or all of them with `include_compacted`).

`edit_note` corrects an uncompacted note by id — a new `timestamp` and/or `description` (whichever is left out keeps its value; an empty `description` clears it) — and returns it with its id, which changes if its timestamp did. `delete_note` removes one, by blanking its row rather than deleting it, so no other note's id changes. Both refuse a stale id (the note was edited or removed since it was listed), an already-compacted note (change the calendar event it became instead), and a note in a compaction that's partway through being applied — that compaction stamps its notes last, so changing one midway would get the changed note stamped. A proposal that included the note is planned again when it's confirmed (confirming re-checks the notes), for the user to confirm that instead. Notes are otherwise turned into calendar changes by compacting them — see [Compacting notes](#compacting-notes) below.

Every label write reads the full label list, changes it in memory, and writes the whole thing back (the API has no way to touch a single label in place), which is a lost-update race if two callers do this concurrently. `CalendarClient.replace_event_labels` (what `Actions` builds every label change on) guards against that with the calendar's own `etag`: it's sent back as an `If-Match` precondition on the write, so a write based on a label list that's since changed fails with `EventLabelConflictError` (surfaced as a `ToolError`) instead of silently overwriting the other change. Confirmed empirically against the real API, since Google's docs only document `If-Match` for Events, not Calendars.

There's deliberately no MCP tool or CLI command that creates the metadata tabs: each store creates its tab the first time it's used.

### Why tools, not resources

MCP has both **tools** (model-controlled: the model decides when to call one, with whatever arguments it computes, as part of its own reasoning) and **resources** (application-controlled: a human typically browses and attaches one via the host's UI, like Claude Desktop's "Attach from MCP" picker). This server only uses tools, for two reasons:

- **Portability.** Resources depend on the host having built UI (or another bridging mechanism) for the model to reach them at all; a lot of MCP clients — agentic ones especially — only implement tool-calling and skip resources entirely. Tools work everywhere.
- **Fit.** The natural workflow here is model-driven, not human-browsing-driven: the model discovers an event's ID via `list_events`, then immediately wants to act on it — fetch details, update, delete. That's a tool-calling pattern (one tool's output, the `id` field, feeds directly into the next tool's input) with no need for a resource-URI layer in between. Nobody is going to browse a picker UI for an event by its opaque Google Calendar ID.

## Compacting notes

Notes are jotted down as things happen ("leaving for salsa early to prep", "done with dinner"), and they're sparse: you note what's notable, not every event boundary. Compacting realigns the day's planned events to those notes: the past becomes fact, and whatever the notes show running into what follows moves it, as the plan says. **Silence means on schedule.** A past event that no note contradicts is recorded exactly as planned. A missing start or end note never makes an event disappear or merge into its neighbor.

Reading free-form text is a job for a model, and the MCP client already is one — so the server has no LLM of its own. Instead the work is split. The client compares the notes to the plan and decides what changed; the server validates those decisions and does everything risky deterministically, previewably, and recoverably.

**The flow.** A compaction is a *proposal* the user confirms: "this is what happened, from the last compaction to now". Claude proposes it — on a schedule, as a Claude routine, or in a conversation — and the user reviews it in the app, where they confirm it, edit it, or leave Claude feedback, which Claude answers with a revised proposal. Only the user's confirmation applies it. The design, and the contract the tools enforce, are in [docs/compaction-proposals.md](docs/compaction-proposals.md).

1. **`prepare_compaction`** (read-only) returns every day from the last compaction up to now, notes or not — each note with an id that is its timestamp and sheet row together (`2026-01-01T09:05:00+00:00#5`) and a shortlist of nearby planned events as `candidates` — plus those days' planned events, a **`timeline`** showing the two side by side (below), and instructions for the next step. A backlog of more than a week takes more than one round, oldest first; notes written after now wait for the next.
2. **`compact_notes(updates, creates, cancels)`** takes the model's decisions, as `update_event` takes a batch: an update for each event the notes show happened differently (or settle the facts of), a create for each that wasn't planned, and a cancel — counting against follow-through or not — for each that didn't happen. It validates them, plans the changes, writes the plan to the journal as a proposal (or, with the open proposal's `proposal_id` and the `revision` it started from, a new revision of it), and returns it with the resulting `timeline` — **without touching the calendar**. Claude can't apply it.
3. **In the app**, the user reviews the proposal (`get_proposal`) and confirms it (`confirm_proposal`), edits it (`amend_proposal`: their edits are laid over Claude's decisions in every later revision), or leaves Claude a note (`add_proposal_note`), which blocks confirming until Claude's next revision answers it. Confirming plans it again: if anything changed since, that's a new revision to confirm instead; otherwise it's applied.

**Several days at once, a day at a time.** One compaction takes on every day from the last compaction up to now, and you review them together, but each day is planned on its own, oldest first, and the timeline heads each with its date. A day after the first starts where the one before it ends, and is planned against the calendar as that day's plan would leave it. Every day but the last is wholly in the past, so its plan only records what happened; only the last day, the one you're in, has a future to reschedule. **The night is the border.** Each night between two days gets one decision, made with the earlier day, and its end — when you woke up — is where one day ends and the next begins. The model judges which note, if any, marks waking up: making it the night's `end_note` moves the border to it, so a 05:30 "woke up early" ends the earlier day there, and an 08:30 "finally up" takes every note up to it into the earlier day. A note in the night that doesn't end it ("can't sleep") is just added to the night's description. If you slept in, the next day still starts at the planned wake-up time, so the morning the night now runs over is settled there: a past event under it, or one still to come that it reaches, is an overlap the plan names for the model to resolve, never silently pushed along. A night you didn't sleep at all is cancelled, and its two days become one long day. Applying the compaction goes a day at a time too: each day's changes, then its notes stamped, before the next. If it stops partway, the days already done stay done.

**Which events are offered: the compaction window.** The first day is the one the last compaction ran in (before there's been one, the one the oldest uncompacted note falls in, or today). A day starts when the last end-of-day sleep event that began before it ends, or at the note itself if it was written before then (you woke up early), and runs to the end of the next end-of-day sleep event. A day can take several compactions, so the events offered — the *compaction window* — start at whichever is later: the day's start, or the last stamped compaction's `now` (for a later day of the same compaction, the day before it's end). Whatever an earlier compaction already settled isn't offered again — and nothing after it is skipped, notes or not: a day that went to plan is confirmed as planned. The one event that ended within 15 minutes before the compaction window starts is offered too, so an event the last compaction closed off ("done reading", or last night's sleep) can still be stretched. The latest compacted note, however long ago it was written, comes along as `previous_note` — context only, for what was going on as the window began — and the timeline shows it too, beside when the last compaction ran.

**Decisions** (`updates`, `creates` and `cancels`, which the server turns into `EventDecision`s):

- an update (`CompactionUpdate`, an `event_id`): it happened. Each edge stays where it was planned unless the update moves it: `start_note`/`end_note` (a note id) sets that edge to the note's time and links the note to it; `start`/`end` gives an explicit time (alongside a note, when the note gives a time relative to itself — "leaving 15 minutes early"). It also takes `summary` (rename it) and `annotate` (add text to its description). An update that moves a *future* event is a direct reschedule — "move lunch to 12:15 and adjust the afternoon" — and whatever it now runs into has to be moved too, in the same call.
- a cancel (`EventCancel`, an `event_id` and `counts_against_follow_through`): it didn't happen — counting against follow-through if you dropped it, not if the plan changed for another reason.
- a create (`CompactionCreate`: `summary`, a start and an end — each a time or a note): something unplanned happened.
- **Not remembering where one event ended and the next began** is an update growing one (renamed after both, if you like) and a cancel of the other that doesn't count. The model is told to do this only after you've confirmed it — never just because the notes are sparse.

**Actions and facts.** Compaction establishes each past event's facts, which traits are judged from later: what the user was doing (its **actions**), where it was, who was there, who it was for, and a subjective note on each person who was there (`self` for the user) — whatever might help judge it later. `prepare_compaction` sends every active and proposed action (their groups left out), every active person and every location, to settle them from. Updates and creates take `action_ids` (the first setting the event's color; for an update, leaving them out keeps them, and `[]` clears them) and `facts` ([`rust/src/facts.rs`](rust/src/facts.rs): `location_id`, `with_ids`, `for_ids` and `notes`, replacing the event's facts whole). For a past event with no actions, `suggested_action_ids` are those of the latest event with the same title in the previous 4 weeks. When an action, a person or a place isn't there yet, the model adds it in the same call — `new_actions`, `new_people` and `new_locations`, each with a `ref` like `new:ukulele` that the updates and creates use in place of its id — rather than by separate tools, so the user confirms them with the plan; they're created when it's applied (or found again, if a commit is resumed), and every ref is replaced by the real id as events are written. The user can settle one sooner in the app, without confirming the rest (`amend_proposal`'s `additions`): create it now, say it's one already there, or drop it. Facts are stored in the event's private extended properties as JSON, split across several when they're long. Unknown or deleted actions, people and locations that aren't there, and refs that aren't added are all refused at the dry run.

Notes that don't set an edge have their text added to the description of the event they fall within (with their time), so they survive compaction. `compact_notes`'s `ignore_notes` lists any that shouldn't be added, and `annotate_notes` any that belong with another event; the user can change either for a note in the app (`amend_proposal`'s `notes`). Google Calendar silently cuts a description longer than 8,192 characters (`MAX_DESCRIPTION_BYTES`, counted as UTF-8 bytes to be safe), so a plan that would grow one past that is rejected, naming the notes to leave out.

**What happens to the rest of the calendar** (all in `rust/src/compaction/plan.rs`'s `plan_compaction`). Every resulting past event — decided, or untouched and so on schedule — is settled: once the compaction is stamped, everything before its `now` is history. Nothing is written on an event to say so, so an event that happened as planned isn't written at all. **Only what's happened is settled:** an event still going on is settled up to now — its start, and its lasting until now, are fact — and it can run on past its planned end; the next compaction says where it ended. A later compaction keeps to what an earlier one settled: it can't move the start of an event that started before the last compaction, end it before then, or cancel or merge it away (refused as `compacted`), though its end can run later; nor can any event it places run back over one before its window (refused as an `overlap`). `prepare_compaction` gives such an event a `history_until`: how far it's settled. Each event a compaction settles is also given the priority its actions give it then, as its own `priority`, unless it has one: actions' priorities change often, and what happened keeps the priority it had. **Nothing is moved to make room.** If dinner's noted end runs into the reading planned after it, the plan is refused — the same refusal, from the same code, as a batch of event changes (see [MCP tools](#mcp-tools)): every problem, and the day as it would leave it — and the model says which edge gives way in the same call, asking you if the notes don't tell it. That goes for events still to come as much as past ones, and for end-of-day sleep events too: a note can't silently eat into last night's sleep.

**Moving bedtime** (the day's own end-of-day sleep event) moves where the day ends. A later bedtime just leaves the evening free; an earlier one needs whatever runs past it ended or cancelled in the same call. Its end — your wake-up time — is the start of the *next* day. When the next day is in the same compaction, moving the wake-up moves the border between them, as above. When it isn't, compacting this day never adjusts it: moving only its start changes only the bedtime, and if its end does move, the plan carries a warning to check the next morning's events (and move them with `update_event` if they now overlap).

**The timeline** (`rust/src/timeline.rs`) is the notes and events in time order, so you can see which note moved which event and which notes just describe what was going on. It's plain data — each note with the edges it set or the event it was added to, each event with its planned and resulting times and a status (`on_schedule`, `adjusted`, `new`, `cancelled`, `merged`, `planned`) — for a client that can draw it, plus `text`, a fixed-width rendering for one that can't — one column, 40 characters wide so it reads on a phone, each moment's notes followed by the event edges at it:

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

**Safety.** Before anything is applied, the whole proposal — the decisions, and every step with its before-state — is written to the **Compactions** tab (see [Calendar metadata sheets](#calendar-metadata-sheets)). Confirming first checks the notes and calendar still match what was proposed, then applies each step and checks it off; a failure partway leaves the journal at exactly that point, and `finish_proposal` finishes only what's left. A write that can never succeed (its event was deleted meanwhile) marks the revision `failed`, and what's left of it is proposed again, for the user to confirm. Created events get deterministic ids (Calendar accepts a caller-chosen id), so a retried create can't duplicate. Each day's notes are stamped compacted only after everything for that day applied, so nothing is lost if it dies. While a compaction is unfinished a new one is refused until it's finished or `abandon_compaction`ed (steps already applied stay applied).

**Changing a proposal.** Nothing touches the calendar until it's confirmed, so a proposal you don't like costs nothing: edit it in the app, or tell Claude what's wrong — a note in the app, or in a conversation — and it calls `compact_notes` again with corrected decisions. Every change is a new revision, and only the newest can be confirmed, so a stale one can't be applied by mistake. Once a compaction has been applied, its events are ordinary events: adjust them with `update_event` like any other.

**Rejections are logged.** Every compaction or event tool call refused with a `ChangeError` (a `CompactionError` is one) — `compact_notes`, `get_proposal`, `amend_proposal`, `add_proposal_note`, `withdraw_proposal_note`, `confirm_proposal`, `finish_proposal`, `edit_note`, `delete_note`, `prepare_judgments`, `record_judgments`, `abandon_compaction`, `create_event`, `update_event`, `delete_event` — logs one warning line naming the tool, the categories of mistake it held, and the full message: `compaction rejected: tool=compact_notes categories=overlap,unknown_note: ...`. Each problem is tagged where it's found (`rust/src/compaction/plan.rs`'s `Problem`) — `overlap`, `unknown_note`, `unknown_event`, `duplicate_decision`, `nonpositive_length`, `malformed_decision`, `note_after_now`, `description_too_long`, `compacted`, `duplicate_change`, `unknown_event`, `facts`, `actions`, `additions`, `stale`, `open_compaction`, `judgment`, and a few for refused state changes — so the logs show which instructions a model gets wrong, and how often.

There's no CLI command for this: comparing the notes to the plan needs a model, which is what the MCP client is.

## Calendar metadata sheets

Each calendar this app manages has one shared **calendar metadata spreadsheet** ([`rust/src/sheet/mod.rs`](rust/src/sheet/mod.rs)), titled "Calendar Metadata" — one tab per kind of Sheet-backed data this app keeps for that calendar. Today that's:

- **Actions** and **Action Groups**, **People** and **Circles**, **Locations** — one row per item, read by header name (so you can reorder columns or add your own; see [Actions](#actions) and [People, circles and locations](#people-circles-and-locations) below). Ids are short and assigned; a list or object (a person's circles, their traits) is JSON in its cell.
- **Traits** — one row per trait: id, name, status, definition and parts (JSON). See [Traits](#traits) below.
- **Compaction Schedule** — when the user's scheduled routines (compacting notes, answering the notes left on a proposal) usually run: one row per hint, its `time` ("HH:MM", 24-hour, in the calendar's time zone) and an optional `label` ([`rust/src/stores/schedule.rs`](rust/src/stores/schedule.rs)). Nothing runs at them: a routine sets them with `set_compaction_schedule_hints`, the whole list at once, and the user's app reads them with `get_compaction_schedule_hints` to fetch soon after each, so it has what the routine made. They can be edited here by hand too.
- **Health** — the server's health metrics ([`rust/src/health.rs`](rust/src/health.rs)), for `get_health` to give back to graph: one row per series, its samples compactly encoded (about a kilobyte for a hundred). Each tool's last 100 calls -- when each ended, how long the tool's own work took, how long the whole request took (auth and transport included, when it came over HTTP), and whether it succeeded; the server's resident memory after each of its last 100 calls, whichever tool; and its last 100 starts (there's one server, so each after the first is a restart -- a crash or a deploy). A call is recorded in memory, a few microseconds' work; a background thread writes what changed every 30 seconds, and as the server exits, holding a lock of its own rather than the one tools' writes hold, so it never makes a tool wait. A `source` column leaves room for the app's own series.
- **Noted Times** — time notes: three columns, **Timestamp**, **Description** (optional) and **Compaction ID** (blank until the note has been compacted; see [Compacting notes](#compacting-notes)). A note's id is its timestamp and row together, and stamping a note verifies its row still holds that timestamp — which also catches a row shifted by garbage collection (below), not just a hand edit.
- **Compactions** — the write-ahead journal of note compactions: one row per fact (each proposal's revisions, each decision, each calendar change with its before/after state and whether it's been applied, and the user's edits and feedback). See `rust/src/stores/journal.rs` and [docs/compaction-proposals.md](docs/compaction-proposals.md).

Both the Noted Times and Compactions tabs only ever grow, so each keeps itself under a row budget (50 for notes, 100 for compactions) by physically deleting old, no-longer-needed rows once it goes over — trimming it to half that (25 and 50), so the very next append doesn't set it off again. That keeps both small enough to read whole, in one request each, every time (Google Sheets throttles read requests to 60 a minute). `NotedTimeSheet.garbage_collect` (run before `note` appends a new one) deletes already-*compacted* notes, always keeping the one with the latest timestamp (the next compaction shows it as context), and `CompactionJournal.garbage_collect` (run as each compaction step starts) deletes finished compactions and proposals, oldest first and wherever they are in the tab, always preserving the open proposal and the most recently *stamped* compaction (its `now` anchors the next round — see [Compacting notes](#compacting-notes)); of the open proposal's replaced revisions, it keeps the newest three whole and only the first row of older ones. The notes tab stops at the first row it can't safely delete, so an unusually large uncompacted backlog, or an unusually long-lived proposal, can leave a tab over budget rather than lose something still needed. Deleting rows also shrinks the tab's grid, and a write past the end of the grid fails, so whenever garbage collection leaves a tab with fewer than 1000 rows in all (`calendar_metadata_sheet.MIN_TAB_ROWS`, what Sheets gives a new tab), empty rows are added at the bottom to make up the difference.

**Finding a tab doesn't rely on its title or position.** Both the spreadsheet and each tab within it are located by a stable tag instead, so renaming a tab (or the spreadsheet itself), or reordering tabs, never breaks the app's ability to find the right one again:

- The spreadsheet's id is recorded directly on the calendar itself, via `CalendarClient.get_calendar_metadata`/`set_calendar_metadata`: since the Calendars resource has no dedicated field for arbitrary app metadata (confirmed against the API reference — unlike Events' `extendedProperties`), these embed a small `[cascading-time-tracker:key=value]` marker as its own line in the calendar's `description`, clearly delimited from whatever human-readable text is already there, and guarded by the calendar's `etag` exactly like the event-label writes below.
- Each tab is tagged with Google Sheets [developer metadata](https://developers.google.com/workspace/sheets/api/guides/metadata) — a `sheet-role` key (`actions`, `uncompacted-time-notes`, ...) attached to that tab's `sheetId`, `PROJECT`-scoped so only this app's own OAuth client can see or query it. `SheetsClient.create_sheet_metadata`/`find_sheet_id` are the low-level calls; `calendar_metadata_sheet.ensure_tab` is what everything else uses — find the tagged tab, or create (and tag) a new one if there isn't one yet.

**A tab's title and color are purely a visible cue for you, not how the app finds it again.** Every tab this app manages gets a human-readable title and the same tab color, set once at creation, so you can tell at a glance which tabs it's using when you open the spreadsheet — but since the actual lookup is by tag, you're free to rename a tab (or the spreadsheet) afterwards without breaking anything.

## Actions

Actions replaced goals: an action is a verb for what the user is doing in a moment ("play guitar", "eat a meal"), and events are tagged with them. Groups roll actions up ("Creative" → "Guitar" → "play guitar") for targets and finding one's way around, but aren't actions themselves: no event can be tagged with one, and actions are always the leaves.

- **Each action reserves one event label.** Active actions always hold theirs; proposed ones (made by the assistant and not yet reviewed) hold theirs while there's room among the calendar's 200; archived and deleted ones don't, and get the same label back if they're made active again. A label is named after its action and colored with its own `background_color`, else its nearest group's, else its priority's color (its own or inherited, from the [priority labels](#event-colors)); that's its `effective_color`, and the priority its events take is its `effective_priority`. Labels that aren't any action's — Calendar's own unnamed ones, and the labels of the goals actions replaced — are left alone, but for the four [priority labels](#event-colors). A change that would need more labels than the calendar has room for for its active actions is refused before anything is written.
- **Events do actions.** `Event.action_ids` (a private extended property) records which; the first decides the event's label (`rust/src/stores/action_calendar.rs`). Calendar rejects *inserting* an event with a label it doesn't have, but accepts re-sending one an existing event already has, so an update keeps an archived action's own label on an event that already carries it. An event never given actions but carrying an action's label (one picked in Calendar, say) is read as doing that action.
- **Priority is inherited.** An action without its own priority takes its nearest group's, and an event without its own takes the highest (lowest-numbered) among its actions'. `Event.action_priority` holds what it inherits (filled in on read, never written), and `effective_priority` — what compaction and the time summaries read — combines the two.
- **Names are unique**, ignoring case: among actions (whatever their status) and, separately, among groups — an action may share a group's name. Deleting a group moves its actions and groups up into its own enclosing group.

## People, circles and locations

- **People** are who the user spends time with: a name, a `context` that tells them apart ("met at salsa" — a name and context together are unique), a status (`active`, `archived` or `deleted`), the `circles` they're in, `what_matters` to them, and their `traits` (which traits apply to them, and their own parts for any — see [Traits](#traits)). The user is always one of them, with the id `self`, whether or not the People tab has a row for them.
- **Circles** are groups people belong to ("Family"), a person belonging to any number; names are unique. Deleting a circle takes its people out of it.
- **Follow-through counts what the user cancelled**, not what's cancelled on the calendar (a deleted series, a reshuffled plan, a merge). A cancellation is recorded ([`rust/src/stores/cancellations.rs`](rust/src/stores/cancellations.rs)) in the **Cancellations** tab, one row per person per event, when a compaction's `cancel` decision (it didn't happen) is applied, or `delete_event` is called with `counts_against_follow_through` — and only for the people a follow-through part matches, as the event was planned: the user and everyone it was with (`with_ids`) for a `with` part, everyone it was for (`for_ids`) for a `for` part, and only an event of the part's `action`, if it has one. The compaction timeline lists each such cancellation at the end of its day, under `Follow-through:` (`✗`, with whom it counts against). A row recorded by mistake can be deleted from the tab by hand; rows are kept for 490 days. `get_people` and `get_person` return each person with theirs (`cancelled_events`, newest first).
- **Locations** are where events happen: a unique name and a `hint` for recognizing when an event or note refers to it.
- **Habits** ([`rust/src/stores/habits.rs`](rust/src/stores/habits.rs); the design is in [`docs/habits.md`](docs/habits.md)) are what the user wants to do well, rated by traits as a person is: a unique name, the action or action group whose events they're about (`action_id` — only the user's events with that action, or any action under that group, are in scope, as the actions and groups are when it's used), a status (`active`, `archived` or `deleted`), a `note` (what it's for, and what doing it well looks like) and `traits`, as a person's: every active trait by default, and their own parts — their own rubrics — for any. They live in the **Habits** tab. An event in a habit's scope is judged for it as for a person there, under `habit:<id>` (see Traits below), and a cancel that counts is recorded against it as against the user; `get_habits` returns each habit's `cancelled_events`. A habit made, or given a new rubric, has its settled events judged only when the user asks: `prepare_habit_judgments` gives a backfill of them (a `backfill_id`, and what's due, as `prepare_judgments` gives a compaction's), and `record_judgments` records them by that id.

## Traits

How the user wants to be with people (and with themselves): Thoughtful, Reliable, Creative, Adventurous and Generous to start with. Nothing about a trait is hard-coded.

- **The Traits tab** of the metadata spreadsheet holds each trait's id (a slug of its first name, never changed), name, status (`active`, `off` or `archived`), definition and parts (JSON). It's created and seeded the first time it's needed, can be edited by hand, and `create_trait`/`update_trait` check a trait before saving it ([`rust/src/stores/traits.rs`](rust/src/stores/traits.rs)'s `trait_problems`), so a bad part is refused rather than silently unrated.
- **Parts.** Each part is one of `judgment` (each event judged by the assistant against a `rubric`, on a scale of `ratings` such as `{"0": "routine", ..., "3": "adventurous"}`, from the `facts` it names: `action`, `action_history`, `location`, `location_history` (the history ones with a `lookback_days`, 30 by default), `general_notes`, `person_notes` and `what_matters` (what's important to the person, from their `what_matters`)), `continuity`, `count`, `duration` or `follow_through`. Every part can take a `weight` and an `engagement_type`: `with` (the default; the events the person was at) or `for` (what was done for them while they weren't there). `continuity`, `count`, `duration` and `follow_through` can take an `action` (an action or action group id), to count only its events. `get_traits` describes each.
- **Per person.** A person's `traits` ([`rust/src/stores/people.rs`](rust/src/stores/people.rs)) select which traits apply to them (`"select": "all"` or a list of trait ids) and can replace a trait's parts for them alone (`"parts": {"reliable": [...]}`), so each person keeps their own cadences. Without it, every active trait applies, as the Traits tab has it.
- **Facts.** Traits are read off the facts compaction establishes for each event (see [Compacting notes](#compacting-notes)).
- **Scores.** The client scores each person's traits itself, from the calendar: their events' judgments, facts and actions, and their cancellations (`get_people`). The server keeps no scores.
- **Judgments complete a compaction.** Once its facts are written, each event with facts is judged ([`rust/src/compaction/judgments.rs`](rust/src/compaction/judgments.rs)) for each person it was about — the user (`self`) and everyone there, for a trait's `with` judgment parts; everyone it was done for, for its `for` parts — against every judgment part of the traits that apply to them — and for each of the user's habits it's in the scope of, for their `with` parts, under `habit:<id>`, its note standing in for what matters. Nothing about judging is sent before the user approves the plan. The applied compaction's result carries the plan's final `timeline` and, beside it, the judgments due (`judgments`), in one compact list: each event with the people it was about and the parts to judge for each (`<trait id>/<part key>`), then each of those parts once — its rubric and ratings, keyed `<trait id>/<part key>`, or `…@<person id>` for a person's own parts — and each person's history (what they did and where) over the days their parts look back. The events are judged as that timeline shows them, and each judgment's request id is `<event id>/<person id>/<trait id>/<part key>`. The model makes them all itself, never asking the user — a rating and one succinct line of reasoning each — and records them with `record_judgments`; the compaction isn't complete until every one is, and `prepare_compaction` and `get_compaction_status` say so (`judgments_pending`). They're kept on the event (`Event.judgments`, JSON in private properties like facts) by person, trait and part, with the scale they were rated on; nothing rolls them up yet. `prepare_judgments(redo=True)` hands them back, with the judgments made, to redo one with more context, and `update_event` can overwrite a rating by hand.

**`get_compaction_status()`** says when notes were last compacted into the calendar, and which note was the latest compacted.

## Event colors

An event's color is its label's. An event doing actions gets its first action's label (see [Actions](#actions) above); one without an action's label gets its priority's: four labels, "Priority 0" to "Priority 3", reserved for that ([`rust/src/stores/groups.rs`](rust/src/stores/groups.rs)) -- its own priority's, or else its actions', or else priority 2's. They have fixed ids, are made the first time they're needed, and count against the calendar's 200 labels. They start out as:

| Priority | Color |
| --- | --- |
| <=0 | `#e1e1e1` (gray) |
| 1 | `#fbd75b` (yellow) |
| 2 (and none) | `#a4bdfc` (the calendar's default blue) |
| >=3 | `#7ae7bf` (soft green) |

**`get_priority_colors()`** lists them; **`update_priority_color(priority, color)`** recolors one, and with it every action's label that takes its color from that priority (an action or group with no color of its own, nor its groups'), returning those actions as `affected_actions`.

The server never writes an event's own `colorId`: it overrides the label's color, and writing one -- even clearing it -- can drop the event's label.

`event_label_id` (the API's own `eventLabelId` field) is derived from the event's actions (see [Actions](#actions) above) and is read-only to the MCP tools; `calendar_cli`'s `update_properties` can still set it directly (`event_label_id=<label-id>`). Calendar itself tolerates an existing event pointing at a label that's since been removed (it keeps the id, and the event can still be updated), but it rejects *inserting* an event with one. So a created event's label always follows its actions, and one a compaction plan creates that names a removed label quietly drops it rather than failing the whole commit. A `create` decision in compaction naming an unknown label fails its dry run. Updating an event doesn't check its label.

## Google OAuth credentials

The server's OAuth token ([`rust/src/google/mod.rs`](rust/src/google/mod.rs)), shared by its Calendar and Sheets clients, needs two files, neither of which should ever be committed:

- **`credentials.json`** — the OAuth *client* secret, downloaded once from the [Google Cloud Console](https://console.cloud.google.com/) for the Google Cloud project you register this server under (APIs & Services → Credentials → create an OAuth client ID of type "Desktop app", then download its JSON). This identifies the application, not you as a user — you obtain it yourself and supply its path.
- **`token.json`** — the *user's* actual access + refresh token, carrying every scope in `SCOPES` (Calendar and Drive/Sheets both). You don't create this yourself: the first time a command-line tool (`create_calendar`, say) runs without a usable token, it opens a browser for you to log into Google and grant access, then writes the resulting credentials to this path. (The server never does: it needs a token already there.) Every later run reads the cached file back and refreshes it as the access token expires; rewriting it back to `token_path` afterward is best-effort — if the path turns out not to be writable (see [Deploying](#deploying) below), the refresh still succeeds in memory, just without being persisted.

Where they live is set by the [configuration](#configuration). For local development, the filenames `credentials.json` and `token.json` are gitignored, so you can drop both files straight into the project root without risk of committing them.

## Configuration

The server reads its configuration from environment variables ([`rust/src/app.rs`](rust/src/app.rs), and [`rust/src/mcp/http.rs`](rust/src/mcp/http.rs) for the HTTP ones), rather than anything being hardcoded or committed:

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `GOOGLE_CALENDAR_ID` | Yes | — | The calendar to operate on — must be the ID of a calendar this app created itself; see [Calendar access model](#calendar-access-model) above. Run `create_calendar` if you don't have one yet. |
| `GOOGLE_OAUTH_CREDENTIALS_PATH` | No | `credentials.json` | Path to the OAuth client secret file — see [Google OAuth credentials](#google-oauth-credentials) above. Defaults to the gitignored local filename; override to something with real protections when deployed (see [Deploying](#deploying)). |
| `GOOGLE_OAUTH_TOKEN_PATH` | No | `token.json` | Path to the cached OAuth user token — see [Google OAuth credentials](#google-oauth-credentials) above. Defaults to the gitignored local filename; override to something with real protections when deployed (see [Deploying](#deploying)). |
| `MCP_TRANSPORT` | No | `stdio` | `stdio` runs the server as a subprocess a local MCP host spawns directly (Claude Desktop's local config, say). `streamable-http` runs it as a standalone HTTP server instead — set this when hosting it remotely (see [Deploying](#deploying)); the variables below only matter in that case. |
| `WORKOS_AUTHKIT_DOMAIN` | Only for `streamable-http` | — | The WorkOS AuthKit domain (e.g. `https://your-tenant.authkit.app`) that issues and signs bearer tokens for callers of this server — see [Deploying](#deploying). Unrelated to the Google OAuth variables above: this controls who's allowed to talk to *this* server, not this server's own credential to talk to Google. |
| `MCP_ALLOWED_USER_IDS` | Only for `streamable-http` | — | Comma-separated WorkOS user ids (`user_...`) allowed to use the server. Tokens for anyone else are rejected, and the server refuses to start without this — see [Deploying](#deploying). |
| `MCP_PUBLIC_URL` | No | derived from `RENDER_EXTERNAL_URL` | This server's own public MCP endpoint URL (e.g. `https://your-service.onrender.com/mcp`) — used as the OAuth resource identifier and the audience bearer tokens must carry. On Render this is derived automatically from `RENDER_EXTERNAL_URL` (which Render sets for you); set it explicitly elsewhere. |
| `MCP_CORS_ALLOWED_ORIGINS` | No | — | Comma-separated browser origins (e.g. `https://tracker.example.com`) allowed to call the `streamable-http` endpoint cross-origin, for a web-based MCP client like the Time Tracker web app. `http://localhost` and `http://127.0.0.1` on any port are always allowed, so local development needs nothing set. Safe to widen: the server authenticates by bearer token, never cookies, so a cross-origin page can't borrow the user's access. |
| `PORT` | No | `10000` | The port `streamable-http` binds to (always alongside host `0.0.0.0`, as every Render web service requires). Render sets this for you automatically. |

To supply these locally, copy [`.env.example`](.env.example) to `.env` and fill it in — the server and the tools read a `.env` in their working directory if there is one (a variable already set wins). `.env` is gitignored, so nothing personal ends up committed.

If this server is launched by an MCP host (Claude Desktop, Claude Code, etc.) instead of run standalone, set these same variables in that host's server config under its `env` field — no `.env` file needed in that case.

## Tests

```bash
cargo test --manifest-path rust/Cargo.toml
cargo build --manifest-path rust/Cargo.toml
python -m pytest -n auto
```

`cargo test` runs the Rust unit tests. The rest is the conformance scenarios: [`conformance/`](conformance/) holds scenarios that drive the server the way a client does, over MCP, against [`conformance/fake_google.py`](conformance/fake_google.py): an in-memory fake of the parts of Google Calendar and Sheets this server uses, served over HTTP and faithful to what the [probes](#command-line-utilities) found Google does. When a scenario ends, three things are compared with its golden record in `conformance/golden/`: every tool result, every request the server sent Google, and Google's state afterwards. The records were written by the original Python server, and the Rust one was ported to match them exactly; they're the spec of what the server does. If two servers send Google the same requests, Google does the same for both, so this holds even where the fake gets Google wrong. They need no credentials or configuration.

The scenarios run the debug build in `rust/target` (or else the release one); set `CTT_SERVER` to a command line to run another, and `CTT_SERVER_LOG` to a file to keep its logs. `CTT_ACTUAL_DIR` writes each scenario's record as this run made it, to compare whole. After a deliberate change of behaviour, rewrite the records with `CTT_UPDATE_GOLDEN=1`, and read the diff. The server under test runs with these variables set, and none of them is set outside the scenarios:

- `GOOGLE_API_ROOT`: where Google's APIs are (the fake's address).
- `CTT_NOW`: a fixed time the server takes as now.
- `CTT_DETERMINISTIC_IDS`: makes generated ids a fixed sequence (see [`rust/src/determinism.rs`](rust/src/determinism.rs)).
- `CTT_HEALTH_WRITER=off`: no background writes to the Health tab, which would interleave with the tools' requests.

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs all of it for every pull request into `main` and every push to it (`conformance (Rust server)`, the check `main`'s ruleset requires).

## Deploying

Hosting this remotely (e.g. on [Render](https://render.com/)) involves **two independent OAuth concerns** — don't confuse them:

1. **This server's own credential to talk to Google** (`credentials.json`/`token.json`, already covered above) — who *this app* is, as far as Google Calendar is concerned.
2. **Who's allowed to talk to *this server*** — once it's reachable at a public URL instead of spawned locally as a subprocess, that URL alone is no longer enough access control. This is what `MCP_TRANSPORT=streamable-http` and WorkOS AuthKit below are for.

A [`render.yaml`](render.yaml) blueprint is included, covering the build/start command and plain env vars. The build and start commands are [`scripts/render-build.sh`](scripts/render-build.sh) and [`scripts/render-start.sh`](scripts/render-start.sh) (set in the dashboard as `bash scripts/render-build.sh`/`bash scripts/render-start.sh` if the service wasn't created from the blueprint), so changing how the server builds or starts is a commit, not a dashboard edit. Two things still need to be done by hand in Render's dashboard regardless (a blueprint can't express either):

**1. Google Calendar credentials** (unchanged from before): don't put `credentials.json`/`token.json` in the repo or a regular env var — Render's **Secret Files** feature is built for exactly this: add each file under the service's Environment tab, and Render mounts it at `/etc/secrets/<filename>` at runtime, separate from your source and the regular env var list.
- Run `create_calendar` locally first — it needs an interactive browser for the OAuth consent flow, so it can't run on Render itself. This both generates `token.json` and creates the dedicated calendar in one step; paste the resulting `token.json` into the Secret File, and set `GOOGLE_CALENDAR_ID` on Render to the ID it printed.
- Secret File mounts may be read-only, so `token.json`'s refresh-and-rewrite (see [Google OAuth credentials](#google-oauth-credentials) above) is best-effort by design — a failed write there just means the next process restart refreshes again from the same cached refresh token, which Google doesn't rotate on a normal refresh.

**2. A WorkOS account, configured for AuthKit**, to authenticate *callers* of this server. This app never issues tokens or holds a password itself — over `streamable-http` it's only an OAuth *Resource Server*, checking that a request's bearer token was really issued by WorkOS for this server ([`rust/src/mcp/http.rs`](rust/src/mcp/http.rs) does the actual JWT/JWKS verification). WorkOS AuthKit is the *Authorization Server*: it's what Claude.ai's connector UI talks to when it discovers this server needs auth, dynamically registers itself as a client, and runs you through the login/consent flow — none of that is code in this repo. **You don't create or configure any OAuth application/client yourself** — Claude.ai registers its own at connect time, which is the entire point of Dynamic Client Registration/CIMD below.

To set that up:

1. Sign up at [workos.com](https://workos.com/) — there's nothing else to create; skip straight to the dashboard settings below.
2. Under **Connect → Configuration**, turn on **"Allow MCP clients to authenticate using Dynamic Client Registration (DCR) or Client ID Metadata Document (CIMD)"** — this is required: it's off by default, and it's what lets Claude.ai register itself as a client automatically. This same page is where you add this server's deployed URL plus `/mcp` (e.g. `https://your-service.onrender.com/mcp`) as a **Resource Indicator** — mark it as the default one.
3. Find your AuthKit domain under the **Domains** section of the dashboard sidebar (a separate section from Connect, easy to miss). In a Staging environment WorkOS auto-assigns one that looks like `https://your-tenant.authkit.app`; in a Production environment you'll instead see a **Configure AuthKit domain** button to set one up. Set `WORKOS_AUTHKIT_DOMAIN` on Render to that value.
4. Find your own WorkOS user id on the **Users** page of the dashboard (it looks like `user_01ABC...`), and set `MCP_ALLOWED_USER_IDS` on Render to it. This is what keeps the server yours: a token WorkOS issued only proves *someone* signed in to your AuthKit environment, and depending on your WorkOS settings, anyone may be able to sign up there. The server rejects tokens for any other user (and logs their id, which is another way to find yours: try connecting, then check Render's logs), and refuses to start if this isn't set. To also stop strangers creating accounts at all, turn off sign-ups in WorkOS's Authentication settings.

**Staging is fine to stay on** for a personal tool like this one — don't feel pushed toward Production just because it's the other option. Per WorkOS's own [Staging vs. production environments](https://workos.com/docs/authkit/environments) docs, Staging is "fully functional and secure," and the only things gated behind Production are a custom AuthKit domain (cosmetic — `*.authkit.app` works identically otherwise) and billing details. Production's SLA/"customer-facing traffic" language in WorkOS's terms is aimed at real multi-tenant SaaS products serving external customers out of staging by mistake; it doesn't really describe a single hobbyist authenticating only themselves. The billing requirement is also moot in practice for this project either way — what actually gets charged is SSO/Directory Sync connections, features this project never touches (it only uses Connect/OAuth).

Don't try to test this by visiting the AuthKit domain directly in a browser — that's AuthKit's own generic hosted sign-in page (it'll ask you to log in and then complain about a missing sign-in callback), a completely different, unrelated flow that really does expect a manually-configured application. It has nothing to do with the MCP/DCR setup above. The real test is connecting from Claude.ai (below): Claude.ai itself constructs the correct authorization request, with its own dynamically-registered client id and redirect URI, so there's nothing to configure or test by hand first.

With both in place, set `MCP_TRANSPORT=streamable-http` (already in `render.yaml`) and deploy. `MCP_PUBLIC_URL` doesn't need setting explicitly on Render — it's derived from `RENDER_EXTERNAL_URL`, which Render provides automatically.

Local development is entirely unaffected by any of this: the server with no `MCP_TRANSPORT` set (the default) runs over stdio, and none of `WORKOS_AUTHKIT_DOMAIN`/`MCP_PUBLIC_URL` is read in that case.

### Browser-based clients

A client running in a web page (like the Time Tracker web app) can't talk to AuthKit's registration and token endpoints directly: AuthKit answers them without CORS headers, so the browser hides the responses. For those clients only, this server offers `POST /oauth/register` and `POST /oauth/token`, which forward the request to the same endpoints under `WORKOS_AUTHKIT_DOMAIN` and return AuthKit's response unchanged (see [`rust/src/mcp/http.rs`](rust/src/mcp/http.rs)). Nothing to configure in WorkOS: the web client still registers itself through DCR, just via this server. Its origin does need to be allowed by `MCP_CORS_ALLOWED_ORIGINS` (localhost always is). Claude.ai and the native apps never use these routes.

### Connecting from Claude.ai

Once deployed, add it under claude.ai's **Settings → Connectors → Add custom connector**, using your server's URL plus `/mcp` (e.g. `https://your-service.onrender.com/mcp`). Claude.ai takes it from there — discovering that the server requires auth, finding your WorkOS AuthKit project, registering itself as a client, and walking you through the login/consent flow — before it can call any tool.

## Running the server

```bash
cargo run --release --manifest-path rust/Cargo.toml --bin ctt-server
```

With no `MCP_TRANSPORT` set it serves MCP over stdio, for a local MCP host to run as a subprocess: point the host's config at `rust/target/release/ctt-server` (with the [configuration](#configuration) in its `env`, or a `.env` in its working directory). See [MCP tools](#mcp-tools) above for what it exposes.

## Command-line utilities

The tools are built with the server (see [Setup](#setup)), into `rust/target/release/` (or run one with `cargo run --release --manifest-path rust/Cargo.toml --bin <tool> -- <args>`). Each reads the same [configuration](#configuration), a `.env` included.

[`create_calendar`](rust/src/cli/create_calendar.rs) makes the calendar this app needs — see [Calendar access model](#calendar-access-model).

[`probe <name>`](rust/src/bin/probe.rs) runs one of the probes: each finds out empirically, against Google, how its APIs behave where their docs don't say, or checks the server's own writes, on a scratch calendar it deletes afterward (`--keep` keeps it). What each found is in its comments.

- `cancelled_events [--days N] [--raw]` — what Google Calendar returns for your cancelled events. Nothing reads them from Calendar any more (`list_events` leaves them out, and the Cancellations tab records the ones that count). It lists the configured calendar's last days with and without `showDeleted`, and only reads.
- `clear_fields` — that clearing an event's fields (`clear_fields`) removes them, on events and series.
- `label_lifecycle [--pause]` — what an event does when its label is removed, then added back.
- `metadata_tabs` — the actions, groups, people, circles and locations tabs end to end, and facts on events. It leaves its scratch spreadsheet in Drive, for you to trash by hand: this app can't.
- `series_edits`, `series_deletes`, `series_splits` — which of a recurring series' instances an edit or delete reaches, and ending a series Google split itself.
- `server` — the server itself, over MCP against Google, on a scratch calendar: the stores, events, a whole compaction (a proposal, an edit, confirming, judging), a habit backfill and health. Run it before deploying a change you're unsure of.

[`calendar_cli`](rust/src/cli/calendar_cli.rs) is a dev tool for calling the calendar directly from a terminal, without going through an MCP host — useful for poking around or debugging.

```bash
# Events from 1 hour ago to 1 hour from now (the default window)
calendar_cli list

# Events from 30 minutes ago to 2 hours from now
calendar_cli list 30m 2h

# A single event by id
calendar_cli get <event-id>

# Set one or more properties on an existing event
calendar_cli update_properties <event-id> priority=1 location="Room A"

# Move/resize an existing event (refused if it would overlap another)
calendar_cli update <event-id> start=2026-01-01T09:30:00-05:00 end=2026-01-01T10:00:00-05:00

# Create a new event (refused if it would overlap another)
calendar_cli create summary="Focus block" start=2026-01-01T09:00:00-05:00 end=2026-01-01T10:00:00-05:00 priority=1

# Delete an event by id
calendar_cli delete <event-id>

# List this calendar's custom event labels, as Calendar itself stores
# them (each action in play holds one -- see the action commands below)
calendar_cli list_raw_labels

# Create a new event label -- background_color is required
calendar_cli create_raw_label background_color="#8e24aa" name="Design Work"

# Update an existing event label's color and/or name
calendar_cli update_raw_label <label-id> background_color="#d50000"

# Delete an event label by id
calendar_cli delete_raw_label <label-id>

# List the proposed and active actions (--status for others, --all for every status)
calendar_cli list_actions

# Create an action (its id is assigned; list_actions shows it)
calendar_cli create_action name="Play guitar" status=active priority=1

# Update an action's properties -- archiving frees its label
calendar_cli update_action <action-id> status=archived

# Blank properties instead (here, so it's in no group)
calendar_cli update_action <action-id> --clear group_id

# Tie an event to actions (the first sets its color), which also sets its label
calendar_cli update_properties <event-id> action_ids=<action-id>,<other-action-id>

# Record a note for right now -- description is optional
calendar_cli note 0s "Started focus block"

# A note for 30 minutes ago, with no description
calendar_cli note 30m

# List the notes that haven't been compacted yet, each with its id
calendar_cli get_notes

# Correct a note's description, or move it to 20 minutes ago / an exact time
calendar_cli edit_note "2026-01-01T09:05:00+00:00#5" --description "Started standup"
calendar_cli edit_note "2026-01-01T09:05:00+00:00#5" --ago 20m
calendar_cli edit_note "2026-01-01T09:05:00+00:00#5" --at 2026-01-01T09:10:00-04:00

# Delete a note
calendar_cli delete_note "2026-01-01T09:05:00+00:00#5"
```

`from`/`to` are each a duration relative to *now*, as [pytimeparse](https://pypi.org/project/pytimeparse/) reads them (e.g. `"1h"`, `"90m"`, `"2d"`, `"1:30"`), giving a window from `now - from` to `now + to`. Both are optional and default to `1h`.

`update_properties` takes one or more `key=value` pairs, each an event attribute (`summary`, `start`, `end`, `description`, `location`, `status`, `priority`, `is_end_of_day_sleep`, `event_label_id`, `action_ids`, `facts`, `judgments` — not `id`, since changing it would repoint the patch at a different event), and patches just those straight in, without fetching the event first: anything not mentioned is left as it was. `start`/`end` take an ISO 8601 datetime with a UTC offset (e.g. `2026-01-01T09:00:00-05:00`, or a trailing `Z`). `facts` is JSON as stored, e.g. `'{"location":"l7k2qp","with":["p3x9aa"],"notes":{"self":"tired"}}'`, and replaces the event's facts whole.

`update` takes the same pairs (at least one of `start`/`end`; whichever is omitted is kept) and moves the event the way the `update_event` tool does: nothing else is moved to make room, so a new time that would overlap another event is refused, printing every problem and the events around them, and exiting non-zero. `create` takes them too (`summary`, `start` and `end` required) and creates the event the way `create_event` does. Both print the event as written.

For a recurring event, the id from `list`/`get` names one specific *instance*. To change a property for the entire series, use the `recurring_event_id` that `get` shows. See Google's [recurring events guide](https://developers.google.com/workspace/calendar/api/guides/recurringevents) for how instances and series relate.

`list_raw_labels`/`create_raw_label`/`update_raw_label`/`delete_raw_label` manage this calendar's event labels exactly as Google Calendar's API represents them — with no concept of priority: Calendar itself has no field for one, so they take only `background_color=value`/`name=value`. Labels are normally managed through actions, which leave any label that isn't an action's alone.

`list_actions` (proposed and active actions; `--status <status>`, repeatable, or `--all`), `create_action` and `update_action <action_id>` manage this calendar's actions, the same way the MCP tools do (see [Actions](#actions)), each printing the resulting actions one per line: id, status, and path. They take `key=value` pairs for an action's fields (`name`, `group_id`, `status`, `background_color`, `priority`, `note`); `update_action` also takes `--clear <attribute>`, repeatable.

`note` takes how long ago the note is for (a duration, like `list`'s; `0s` for right now) and an optional description. `get_notes` lists every uncompacted note with its id. `edit_note <note_id>` takes `--ago` (a duration before now) or `--at` (an ISO 8601 time, local if it has no UTC offset), and/or `--description` (`""` clears it); `delete_note <note_id>` removes the note. Both refuse a compacted note, a stale id, or a note in a compaction that's partway through being applied, printing why and exiting non-zero.
