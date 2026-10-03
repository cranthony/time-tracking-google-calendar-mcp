# Goals and reflection: design

Status: proposal (2026-10-01), implemented through phase 3, then revised (2026-10-03) as below.

> **Revision (2026-10-03): one cadence, daily, and ratings that flow up the tree.** This supersedes everything below about cadences, periods longer than a day, and how reflections and measures work; the rest stands.
>
> - **Goals have no cadence.** Every active goal is rated once a day, in the daily reflection, if it has a measure or rated sub-goals; one without a measure is rated as the mean of its sub-goals'. The `cadence` column is no longer read (a tab that has one keeps it untouched), and tools take a `day` instead of a cadence and period. Assessments still record `cadence = daily`, so their event ids are unchanged; ones recorded at another cadence are ignored. Other cadences may come back later.
> - **Measures** (utilities/goal_measures.py): `duration` and `count` count over a configurable `interval_days` before the day's end (wall-clock), and with `zero_at_days`, a shortfall falls linearly from 100 when the target was last met to 0 at `zero_at_days` ("visit parents every 2 months"). `subjective` takes a `prompt` and an `interval_days`: it's asked once the interval has passed since it was last answered, and carried over from the day before otherwise; a rating given in passing restarts the interval. `llm` takes a `rubric`, which may use the immediate sub-goals' ratings. `rollup` aggregates the immediate sub-goals' ratings that day: `mean`, `weighted` (unlisted sub-goals weigh 0) or `percentile` (0 = min, 100 = max).
> - **Ratings only flow up**, so a day is rated a level at a time, leaves first. `prepare_reflection(day)` offers only the goals whose rated sub-goals are all confirmed; `record_reflection` confirms a level at once. The confirmed assessments are the reflection's journal: a reflection cut short resumes where it stopped. (An MCP elicitation per question was considered; it isn't needed, and not every client supports it.)
> - **The overall goal** (2026-10-03): every calendar has a goal with id `overall` whose sub-goals are implied to be all the top-level goals, so the overall health rating has every measure option and is rated last in the daily reflection. It holds no label, can't be given to an event, and is always active and top-level. Considered instead: a real root goal (every path would gain a prefix, and it'd take a label) and a separate object (a second copy of measures, history, the cache and reflection). Each listed goal's `minutes_by_statuses` splits its time by the statuses of the goals each event is given among it and its sub-goals (not their ancestors), so the Goals page can total any goal's time, the overall goal's included, for whatever statuses its filter shows, each event once.
> - **Goals' recent time.** `get_goals` reports each goal's minutes (its sub-goals' included) in the 24 hours and the 7 days up to the last compaction (`GoalList.as_of`), and `get_compaction_status` reports that time and the latest compacted note.

This replaces event labels with **goals**: a tree of things you're working toward, each of which can color events (through a Google Calendar event label), carry the priority/fixed-time defaults labels carry today, and be **assessed** for health on a regular cadence. A **reflection** routine walks you through those assessments daily, weekly and monthly. The assessments are stored in Google Calendar, so you can query them historically.

Contents:

1. [Concepts](#1-concepts)
2. [Where everything lives](#2-where-everything-lives)
3. [The Goals tab](#3-the-goals-tab-calendar-metadata-sheet)
4. [Events ↔ goals](#4-events--goals-the-main-calendar)
5. [Label lifecycle: the 200-label limit](#5-label-lifecycle-the-200-label-limit)
6. [Storing health assessments in Google Calendar](#6-storing-health-assessments-in-google-calendar)
7. [Measuring goals](#7-measuring-goals)
8. [At-a-glance health](#8-at-a-glance-health)
9. [MCP tools](#9-mcp-tools)
10. [Compaction with goals](#10-compaction-with-goals)
11. [The reflection process](#11-the-reflection-process)
12. [The Goals page (time-tracker-client)](#12-the-goals-page-time-tracker-client)
13. [Migration from event labels](#13-migration-from-event-labels)
14. [Implementation phases](#14-implementation-phases)
15. [Decisions](#15-decisions)

---

## 1. Concepts

**Goal.** Something you're working toward: "Make a Time Tracker app to help me use my time wisely", "Wake up at 7am every day", "Host friends for home-cooked food and meaningful discussions weekly". A goal can have **sub-goals** ("Learn to cook vegetarian food" → "Learn to cook Tofu Tikka Masala"), nested to any depth. Goals and sub-goals are the same type, so "goal" means both.

Each goal has:

- a short **name** (it's also the calendar label's name, so ≤ 50 characters) and an optional long **description** (Markdown with inline HTML, Mermaid diagrams and images; see §3.2);
- a **status**: `proposed` (suggested, not taken on yet), `active` (being worked on), `inactive` (paused), `completed` (achieved), `archived` (no longer relevant) or `deleted` (shouldn't have existed). Only active goals take up a calendar label slot (§5) and are assessed; goals of every other status keep their history and can be made active again at any time. The app lists proposed, active and inactive goals by default, with a filter for the rest. No event can be given a deleted goal, though events that already have one keep it;
- an optional **cadence**: `daily`, `weekly`, `monthly` or `every_2_months`. This is how often its health is assessed, and the period its at-a-glance health refers to. A goal is assessed at **exactly one** cadence;
- an optional **measure**: how its health is assessed (objective metric, subjective rating, LLM judgement, or a rollup of its sub-goals; see §7);
- an optional **target** and **deadline** for goals that finish ("ship v1 by 2026-12-31");
- the **priority** and **fixed_time** defaults that event labels carry today, inherited by sub-goals that don't set their own.

**Assessment.** One health rating of one goal for one period of its cadence, e.g. "Wake up at 7am, daily, 2026-09-30: 92/100, woke 07:05". Stored as an event on a dedicated calendar (§6).

**Reflection.** A conversation, run by the MCP client (Claude) through the tools in §9, that produces the assessments due for one cadence and period: a day, a week, a month or two months. It may also record a short journal entry.

## 2. Where everything lives

| Data | Where | Why |
| --- | --- | --- |
| The goal tree and each goal's settings | **Goals** tab of the Calendar Metadata sheet | Same pattern as the Event Labels tab: hand-editable, read on every event tool call to resolve priorities |
| Goal descriptions | **Goal Details** tab of the same sheet | Can be long, and are only needed on demand, so they stay out of the Goals tab that every event tool call reads |
| Description images | App-created files in Drive (`drive.file` scope, already granted) | A sheet cell can't hold an image |
| Which goals an event serves | Main calendar: the event's `extendedProperties.private` | Travels with the event and needs no sheet lookup |
| Event color | Main calendar: the event's `eventLabelId` | Derived from its goals. Display only (§4) |
| Assessments and reflection journals | A second, app-created **Goal Health** calendar | Time-indexed and filterable by goal (§6) |
| At-a-glance health | Cache columns in the Goals tab | Lets the Goals page render from one sheet read. It can always be rebuilt from the Goal Health calendar |
| Summary → goals hints for compaction | **Goal Hints** tab | Lets the server suggest goals for an event without the model having to remember them (§10) |

## 3. The Goals tab (Calendar Metadata sheet)

### 3.1 Columns

A new tab, tagged with `sheet-role = goals` and found the same way as the existing tabs (`calendar_metadata_sheet.ensure_tab`). One row per goal, active or not. Columns are read by header name, like `EventLabelSheet.from_row`, so new columns can be added later without a migration.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | string | Immutable short id, e.g. `g7k2qp`: 6 random lowercase letters and digits. Short enough for the model to repeat reliably, and cheap to store in an event's goals property (§4). It isn't the calendar label's id, which must be a UUID; that's `label_id` below. Narrow column, like the label ID today |
| `parent_id` | string | Blank for a top-level goal. Must name another row, and cycles are rejected |
| `name` | string | ≤ 50 characters; becomes the label name. Unique among siblings |
| `active` | `TRUE`/`FALSE` | §5 |
| `label_id` | UUID | This goal's **reserved** calendar label id, kept even while the goal is inactive so that reactivating it restores the same id (§5). New goals get `uuid5(GOALS_NAMESPACE, id)`; migrated labels keep their Google-assigned id |
| `background_color` | hex | Optional; derived from priority when blank, exactly like labels today |
| `priority` | int | Optional; inherited from the nearest ancestor that sets one |
| `fixed_time` | `TRUE`/`FALSE` | Optional; inherited the same way |
| `cadence` | `daily`/`weekly`/`monthly`/`every_2_months` | Optional. Blank means the goal is never assessed; it's only an organizing node for its sub-goals. A goal with a `rollup` measure (§7) still needs a cadence. Changing it later is allowed: old assessments keep the cadence they were recorded at |
| `measure` | JSON | Optional; the measure spec in §7. Kept small (< 1 kB) |
| `target` | string | Optional free-text target ("300 min/week", "v1 shipped") |
| `deadline` | date | Optional |
| `created` | date | Set on creation; anchors the first period that gets assessed |
| `note` | string | Short free text, like a label note today |
| `health` | cache | Latest confirmed rating at the goal's cadence (§8) |
| `health_period` | cache | The period that rating covers, e.g. `week-2026-09-20` |
| `health_trend` | cache | The last 8 ratings at that cadence, oldest first and comma-separated, e.g. `72,80,-,65,90,88,95,92`, where `-` marks a missing or skipped period |

The cache columns are written only by `record_assessments` and `rebuild_goal_health_cache`. If you edit them by hand, the next write overwrites the edit.

Every event tool call reads this tab once (memoized by `cached_sheet_reads`) to resolve priorities, just as it reads the Event Labels tab today. The cost doesn't grow with history: rows are goals, not events.

### 3.2 Descriptions (Goal Details tab)

Tagged with `sheet-role = goal-details`: `goal_id | description`. The description is **Markdown**: it allows inline HTML (sanitized when rendered: no scripts or styles), ```` ```mermaid ```` blocks for diagrams, and images written as `![alt](drive:<fileId>)`.

- **Why Markdown rather than raw HTML.** The model reads and writes Markdown naturally, it diffs well, it's readable in the sheet itself, and Flutter has mature Markdown renderers (`flutter_markdown` plus a Mermaid renderer in a WebView). Markdown that is just inline HTML still works, so you lose nothing by starting with HTML.
- **Size.** A Sheets cell holds at most 50,000 characters. A description longer than that is split across consecutive cells in the same row (`C`, `D`, …) and joined on read. That's far more than a goal description should need, but the split means the limit never fails silently.
- **Images** are uploaded through `attach_goal_image` (§9) into an app-created Drive folder, "Calendar Metadata – goal images", next to the spreadsheet. The `drive.file` scope covers exactly these files. `get_goal_image` serves them back, because the client app holds no Google credentials of its own.

## 4. Events ↔ goals (the main calendar)

An event serves **zero or more goals**, ordered so that the first is its **primary goal**. One dinner can serve both "Host friends weekly" and "Learn Tofu Tikka Masala". Contributing to a goal implicitly contributes to all of its ancestors, so nobody tags "Learn vegetarian cooking" separately.

Stored as one private extended property on the event:

```
extendedProperties.private["cascading-time-tracker-goal_ids"] = "g7k2qp g2m9aa"
```

A space-separated list of ids, primary first. With the existing `cascading-time-tracker-` prefix this uses 31 of the 44 characters a key can have, and the value limit (1024 characters) allows over 140 goals per event. Like `priority` and `min_duration`, it's read and written through `Event`'s `_parse_properties`/`_format_properties`, as a new `Event.goal_ids: list[str] | None`.

**`eventLabelId` is derived from the goals and never set by hand.** On every write, a new `GoalCalendar` (replacing `LabelPriorityCalendar`) sets the event's label from its primary goal. The rule differs between insert and update, because the probe found that Calendar rejects a stale label id on insert but accepts one on patch (§5):

- **Update (patch):** the primary goal's own reserved `label_id` if the event already carries it, **whether or not the goal is active**: the probe showed Calendar accepts re-sending an event's existing removed label, so the event recolors when the goal is reactivated, without being rewritten. Otherwise the same as an insert. (The probe didn't test patching an event with a removed label it *didn't* already have, so the implementation doesn't.)
- **Insert:** the primary goal's `label_id` if the goal is active, otherwise the nearest active ancestor's, otherwise none. This covers events created by reallocation's splits and compaction's `create` too, since every insert goes through `GoalCalendar`. Such an event keeps the ancestor's label until its goals are rewritten.

Two consequences:

- A goal going inactive never stops an event from being written (§5).
- `PublicEvent.event_label_id` becomes read-only (like `effective_priority`). `PublicEvent.goal_ids` is the writable field.

**Priority and fixed time** are resolved the way labels resolve them today, but along the goal chain: the event's own value; otherwise the primary goal's; otherwise that goal's nearest ancestor that sets one. They're exposed as the same `effective_priority`/`effective_is_fixed_time`, so reallocation doesn't change at all. `Event.label_priority`/`label_is_fixed_time` are renamed `goal_priority`/`goal_is_fixed_time`, and are still internal and never written to the API.

**Old events** that have an `eventLabelId` but no goals property (everything written before the migration) are read as `goal_ids = [the goal whose label_id matches]`. That avoids any bulk rewrite of history (§13).

**Pagination.** `CalendarClient.list_events` doesn't currently page, and Calendar returns 250 events per page by default. Anything that lists a week or more (goal metrics, reflections) needs it to follow `nextPageToken`, with `maxResults=2500`. This is a small prerequisite fix.

## 5. Label lifecycle: the 200-label limit

Google Calendar allows at most 200 labels per calendar. The client app already hides a few **unnamed** labels, which represent Calendar's default colors. If those count toward the 200, the real budget is `200 - unnamed`. The tools report the budget rather than assume it.

**Rule: a goal occupies a label exactly while it's active.** `EventLabels.sync_labels`' replace-the-whole-list sync stays as it is; only its input changes. The calendar's label list becomes *the active goals' (`label_id`, `name`, `color`) + any unnamed labels already present* (unnamed labels are always preserved). That gives:

- **Deactivating** a goal removes its label from the calendar and frees a slot. Its `label_id` stays reserved in the sheet.
- **Reactivating** a goal re-adds the label **with the same UUID**. The Calendars reference allows a client-supplied `id` ("must be unique within the calendar and follow UUID format"), and the probe confirmed it. Events keep their now-dangling `eventLabelId`, so the goal's history is linked to the label again without any events being rewritten.
- **Activating past the limit** fails with a `ToolError` that names the budget, before anything is written. (Suggesting which goals to deactivate, e.g. those with no events in the last 60 days, is left for later.)

### What happens to events whose label is removed?

Google's documentation doesn't say, so [`probe_label_lifecycle.py`](../probe_label_lifecycle.py) tested it against the real API on a throwaway calendar (run 2026-10-01):

| Question | Result |
| --- | --- |
| Is a client-chosen UUID kept as the label's id? | **Yes** |
| Does an event keep `eventLabelId` after its label is removed? | **Yes**: the dangling id is still returned |
| Can you patch an unrelated field on such an event (`eventLabelVersion=1`, label omitted from the body)? | **Yes**, and the dangling id is kept |
| Can you patch such an event while re-sending the stale `eventLabelId`? | **Yes** |
| Can you **insert** a new event with a stale `eventLabelId`? | **No**: HTTP 400 "Invalid event label id … does not exist" |
| Can you re-add a label with the same UUID (and a new color)? | **Yes**, and all three old events report that id again |
| Does `privateExtendedProperty=key=value` filtering work? | **Yes**: exact match, and only the tagged event was returned |

The probe checked the API only. Whether the Calendar UI paints the re-added label's new color on the old events wasn't observed (that needs a `--pause` run), but the events carry the label's id, which is all the color depends on.

**So deactivation and reactivation touch no events at all.** The only constraint is on inserts, and §4's insert rule handles it: never insert with an inactive goal's label id.

> **This was a bug before goals (fixed in phase 0).** Reallocation splits an event by cloning it (`continuation = preceding.clone()` in `utilities/reallocation.py`) and inserting the clone. If the original's label had since been deleted, the clone carried the stale `eventLabelId` and the insert failed with a 400, partway through applying the plan. Now:
> - `ReallocatingCalendar` drops a removed label from a split continuation before inserting it.
> - It refuses a new event with an unknown label up front, before anything is applied.
> - `NoteCompactor` does the same for compaction plans: a `create` decision naming an unknown label fails the dry run, and any other created event drops a removed label.
>
> `GoalCalendar`'s insert rule replaces these checks in phase 1.

## 6. Storing health assessments in Google Calendar

### 6.1 Verdict: yes, on a dedicated, app-created calendar

Google Calendar works well as a time-series store for this, provided three things hold:

- **A separate calendar.** The main calendar's invariant is *non-overlapping events representing one person's time*. Reallocation and compaction both depend on it, so all-day assessment events can't live there. `calendar.app.created` lets the app create a second calendar, **Goal Health**, whose id is recorded on the main calendar with the existing `set_calendar_metadata` marker (`[cascading-time-tracker:goal-health-calendar=<id>]`). It's created lazily on first use, and also by `create_calendar.py`. The Time Tracker app is where you'll view this history, so the calendar is meant to stay out of Google Calendar's way: on creation the app tries to hide it (`calendarList` entry `hidden: true`). Whether the `calendar.app.created` scope permits that is unverified. If it doesn't, the calendar simply appears in Google Calendar and you can untick it once. Nothing depends on it being hidden, and each event's title is still readable there (below).
- **One event per (goal, period),** as an all-day event that spans the period: one day for daily, Sunday–Saturday for weekly (so the default Sunday-morning reflection comes right after the week ends, §11.2), the calendar month for monthly, and an aligned pair of months (Jan–Feb, Mar–Apr, …, Nov–Dec) for every two months. Calendar's own time-range query (`timeMin`/`timeMax` overlap) then gives "every assessment touching September" without any extra work.
- **Structured fields in `extendedProperties.private`,** which Calendar can filter on server-side, with the human narrative in `description`.

### 6.2 Assessment event shape

```jsonc
{
  "id": "a3k9…",                       // deterministic, see 6.3
  "summary": "🟢 Wake up at 7am · 2026-09-30 · 92",
  "description": "Woke 07:05; target 07:00 with 10 min grace → 92. Bed at 23:40 the night before.",
  "start": {"date": "2026-09-30"}, "end": {"date": "2026-10-01"},
  "transparency": "transparent",
  "extendedProperties": {"private": {
    "cascading-time-tracker-kind":     "assessment",
    "cascading-time-tracker-goal":     "g7k2qp",
    "cascading-time-tracker-cadence":  "daily",             // the goal's cadence when assessed
    "cascading-time-tracker-period":   "2026-09-30",        // week-2026-09-27, 2026-09, 2026-09..10 (Sep–Oct)
    "cascading-time-tracker-rating":   "92",                // 0–100, or "skip"
    "cascading-time-tracker-method":   "metric",            // metric | subjective | llm | rollup
    "cascading-time-tracker-status":   "confirmed",         // proposed | confirmed
    "cascading-time-tracker-metrics":  "{\"wake\":\"07:05\",\"target\":\"07:00\"}",
    "cascading-time-tracker-assessed": "2026-10-01T08:12:00-04:00",
    "cascading-time-tracker-schema":   "1"
  }}
}
```

- **Rating scale:** an integer 0–100, shown in three bands: 0–39 🔴, 40–69 🟡, 70–100 🟢. The band thresholds are display-only constants, so changing them later rewrites no history. `skip` explicitly records "not assessed / not applicable this period", so a reflection that ran can be told apart from one that never happened. Ratings are integers so they're easy to filter on and average.
- **`metrics`** holds the raw measured values as compact JSON (≤ 1024 characters per value). The rating's one-line explanation is a property of its own, `cascading-time-tracker-explanation`, and `description` holds only the rationale.
- **`status`:** `proposed` or `confirmed`. **Nothing is confirmed outside a reflection, measured ratings included** (§7). A `proposed` assessment may be written ahead of time, e.g. by a scheduled agent (§11.5), but only `confirmed` ratings feed the at-a-glance health and history charts.
- Total size per event is far below Calendar's limit (300 properties, 32 kB).

**Reflection journal entries** are events on the same calendar with `kind = reflection`, `cadence` and `period` (no `goal`). The journal text is in `description`. Any "next time I'll…" intentions go in a `cascading-time-tracker-intentions` property as JSON, for the next reflection to read back.

**Description limit: 8,192 characters, truncated silently.** The probe wrote 8,000-, 32,000- and 128,000-character descriptions: the first came back intact, and both longer ones came back cut to exactly 8,192 characters, with no error. (It used ASCII, so whether the limit counts characters or bytes for non-ASCII text is untested.) So `record_reflection` and `record_assessments` reject a journal or rationale over **8,000 characters** with a `ToolError`, leaving room for a header line. After every write they compare the description read back with what was sent, and fail loudly on a mismatch rather than lose text. If longer journals turn out to matter, the overflow could go in a **Reflection Journal** sheet tab keyed by the reflection event's id, but that's not planned.

> **The same limit affected compaction (fixed in phase 0).** `utilities/note_compaction.py`'s `_annotate` appended note text to an event's description with no length check. It now rejects a plan that would grow a description past `MAX_DESCRIPTION_BYTES` (8,192, counted as UTF-8 bytes), naming the notes to leave out with `ignore_notes`.

### 6.3 Idempotent writes

The app chooses each event's id itself. Calendar accepts a caller-chosen id of 5–1024 characters from `a-v` and `0-9`; compaction already relies on this for the events it creates (`_new_event_id` in `utilities/note_compactor.py`). The id is the **lowercase, unpadded base32hex encoding** of the UTF-8 string `"<goal>|<cadence>|<period>"`, for example `g7k2qp|daily|2026-09-30`, which encodes to `csrmmcjhe1u68ob9dhsnochg68r2qc1p5kpj0`. That's 37 characters, and the longest case (`every_2_months`) is 53, well under the 1024 limit. Today each cadence's period ids happen to have a distinct format (`2026-09-30`, `week-2026-09-27`, `2026-09`, `2026-09..10`), but the cadence stays in the id so a future cadence whose period names overlap an existing one's can't collide with it.

- **No collisions.** This is an encoding, not a hash, so nothing is dropped. Base32hex maps every byte string to a distinct output, and it decodes back to the same string. Goal ids, cadences and period ids never contain `|`, so two different (goal, cadence, period) triples always give different strings, and so different event ids.
- **Readable back.** Decoding an id gives back its goal, cadence and period, which helps when debugging.
- **Why not compaction's plain concatenation?** Compaction builds ids like `cmp<compaction_id>s001` because its parts already use only allowed characters. Period ids contain `-`, `W` and `.`, which Calendar doesn't allow, and base32hex sidesteps that.

Recording an assessment is an **upsert**: try `insert`, and on 409 `patch`. Re-rating a period overwrites it instead of duplicating it, and a retried write is harmless, so no write-ahead journal is needed. A goal id is never reused, so ids can't collide across goals.

### 6.4 Queries

| Question | Calendar query |
| --- | --- |
| A goal's history | `events.list(calendarId=health, privateExtendedProperty="cascading-time-tracker-goal=g7k2qp", timeMin, timeMax, singleEvents=true, maxResults=2500)` |
| Everything for a period (a reflection's own context, a month's summary) | `events.list(calendarId=health, timeMin, timeMax, maxResults=2500)`, then group by goal in memory |
| A goal plus its descendants | One query per goal, or the period query filtered in memory. Google's docs disagree on whether repeating `privateExtendedProperty` means AND or OR, so the design never repeats it |

**Volume check:** 50 goals assessed daily for 5 years is about 91,000 events, well within what a calendar holds. One goal's daily history for a year is 365 events, a single request. Summary stats for the Goals page never query the calendar at all (§8).

**Alternatives considered:**

- **The Sheet.** It would need row budgets or garbage collection like the notes tab, and gets slow to scan as it grows.
- **One calendar per goal.** That runs into calendar-creation quotas and clutters the calendar list.
- **One event per period holding every goal's rating.** Calendar can't filter that by goal, and it hits the 1024-character value limit as goals multiply.

## 7. Measuring goals

The `measure` column holds a small JSON spec. The kinds below are a starting set, chosen so that the two you asked for, *objective event duration* and *subjective or LLM judgement confirmed in reflection*, are both first-class. The registry is open-ended, so more kinds can be added later without a schema change. A goal has one measure, applied at its one cadence, so the spec has no period of its own: "300 minutes" on a weekly goal means 300 minutes per week.

| `kind` | Example | Computed from | Proposed rating (0–100) |
| --- | --- | --- | --- |
| `duration` | `{"kind":"duration","target_min":300}` | Total minutes of non-cancelled events in the period whose goals include this goal or a descendant | `min(100, round(100 × minutes / target))` |
| `count` | `{"kind":"count","target":1}` (hosting friends) | Number of such events | `min(100, round(100 × count / target))` |
| `wake_time` | `{"kind":"wake_time","target":"07:00","grace_min":10,"zero_at_min":60}` | End of the previous `is_end_of_day_sleep` event | 100 within the grace; falls linearly to 0 at `zero_at_min` late. For a weekly or longer cadence, the mean over the period's days |
| `subjective` | `{"kind":"subjective","prompt":"How meaningful were the conversations?"}` | Asked during reflection | You give the number |
| `llm` | `{"kind":"llm","rubric":"…"}` | The model reads the period's events, notes and journal, and proposes a rating with a rationale | You confirm or change it |
| `rollup` | `{"kind":"rollup","agg":"min"}` | The children's confirmed ratings whose periods fall within this goal's period (`min` or `mean`) | Computed |

A spec is checked whenever it's saved (utilities/goal_measures.py), so that a typo such as `target_mins` is refused rather than leaving the goal silently unmeasured. Each kind takes only the fields shown, its numbers must be positive (`grace_min` may be 0, and `zero_at_min` must exceed it), and `wake_time`'s `target` is `HH:MM`. Syncing hand edits checks every goal's measure; creating or updating a goal checks only the measure being set, so an old invalid one can't block edits to other goals.

**Measured ratings are never confirmed automatically.** `measure_goals` (§9) computes the metric and rollup kinds **without writing anything**, and every rating, measured or not, is confirmed only during a reflection. To make agreeing with a measured rating effortless, each one carries a deterministic, one-line **`explanation`** built from the inputs and the formula, so you can check it at a glance:

- "Woke 07:05; target 07:00 with 10 min grace → 92"
- "4h 10m of 5h target → 83"
- "Hosted 0 of 1 dinners → 0"
- "Min of 3 sub-goals (92, 75, 60) → 60"

The reflection shows the explanations together, so a typical answer is "yes" to all of them, or "yes, except bump cooking to 70 because I was sick". The explanation is stored in its own property, and any note you add is the assessment's `description`.

**Double counting** is intended: a dinner that serves two goals counts in full toward both. Durations answer "how much time went toward X", not "how is my time partitioned".

## 8. At-a-glance health

A goal's at-a-glance health is its **latest confirmed rating at its own cadence**, plus how current that rating is:

- **current**: the last period that has fully ended has a confirmed rating;
- **stale**: one or more fully ended periods have no confirmed rating yet, and the Goals page shows how many ("2 weeks unassessed"). Measured goals are stale too until a reflection confirms them, which is intended: the dot reflects what you've agreed with;
- **in progress**: for `duration`/`count` goals, the running value for the current period ("3h 10m of 5h this week"), computed live on the goal's detail view only.

`record_assessments` updates the `health`, `health_period` and `health_trend` cache columns, so the Goals page (and `get_goals`) can show a colored dot plus an 8-period sparkline from a single sheet read. `rebuild_goal_health_cache` recomputes the columns from the Goal Health calendar if they ever drift, for example after hand edits or a failed write.

## 9. MCP tools

These **replace** `create_event_label`, `update_event_label` and `sync_event_labels_from_sheet`. As with labels today, every goal-mutating tool returns the full resulting goal list, so there's no separate "list" tool that secretly writes. Reading has its own tool here, `get_goals`, because it no longer needs to sync labels just to read the tree.

### 9.1 Types

```python
Cadence = Literal["daily", "weekly", "monthly", "every_2_months"]

@dataclass(kw_only=True)
class Goal:
    id: str | None = None              # None only when creating
    parent_id: str | None = None
    name: str | None = None
    active: bool | None = None
    background_color: str | None = None
    priority: int | None = None
    fixed_time: bool | None = None
    cadence: Cadence | None = None
    measure: dict | None = None        # §7
    target: str | None = None
    deadline: date | None = None
    note: str | None = None
    # read-only, ignored on input:
    label_id: str | None = None
    path: str | None = None            # "Host friends › Learn vegetarian cooking › Tofu tikka"
    health: GoalHealth | None = None

@dataclass
class GoalHealth:                      # §8
    rating: int | None                 # 0–100, latest confirmed
    period: str | None
    trend: str                         # "72,80,-,65,90,88,95,92"
    stale_periods: int

@dataclass
class Assessment:
    goal_id: str
    cadence: Cadence                   # the goal's cadence; validated
    period: str                        # "2026-09-30" | "week-2026-09-27" | "2026-09" | "2026-09..10"
    rating: int | Literal["skip"]      # 0–100
    method: Literal["metric", "subjective", "llm", "rollup"]
    status: Literal["proposed", "confirmed"] = "proposed"   # read-only on input, see 9.4
    explanation: str | None = None     # one line; set by measure_goals for metric/rollup (§7)
    metrics: dict | None = None
    rationale: str | None = None       # your or the model's added words

@dataclass
class GoalList:
    goals: list[Goal]                  # flat, parents before children
    label_slots_used: int
    label_slots_total: int
```

### 9.2 Goal management

| Tool | Signature | Notes |
| --- | --- | --- |
| `get_goals` | `(statuses: list[GoalStatus] \| None = None) -> GoalList` | Read-only. The tree with cached health. By default proposed, active and inactive goals; completed, archived and deleted ones only when asked for, to keep the model's context small |
| `create_goal` | `(goal: Goal, description: str \| None = None) -> GoalList` | Allocates `id` and `label_id`. If `active` (the default), adds the label; fails past the label budget |
| `update_goal` | `(goal: Goal, clear_fields: list[GoalField] \| None = None) -> GoalList` | Same merge/clear semantics as `update_event_label` today. Changing `active` adds or removes the label (§5). Changing `parent_id` re-parents the goal (cycles rejected) |
| `get_goal_description` | `(goal_id: str) -> str` | Markdown |
| `set_goal_description` | `(goal_id: str, description: str) -> None` | Replaces the whole description |
| `attach_goal_image` | `(goal_id: str, data_base64: str, mime_type: str, alt: str) -> str` | Uploads to the Drive folder and returns the `![alt](drive:<id>)` snippet to paste into the description |
| `get_goal_image` | `(file_id: str) -> ImageContent` | Only serves files in the app's goal-images folder |
| `sync_goals_from_sheet` | `() -> GoalList` | Pushes the active goals' labels to the calendar after hand edits to the tab, like `sync_event_labels_from_sheet` today. Unlike today, a row missing from the sheet **doesn't** delete history. Its label is removed, as if it were deactivated |

There's deliberately **no `delete_goal`**: assessments and events refer to goal ids forever. A goal created by mistake can be deleted by hand from the sheet; its label is removed on the next sync, and any events tagged with it read as "unknown goal" and get no label.

### 9.3 Events

`list_events`, `get_event`, `create_event` and `update_event` are unchanged except for `PublicEvent`:

- `goal_ids: list[str] | None`: writable. Unknown or typo'd ids are rejected with a `ToolError` that lists the closest matches.
- `goal_names: list[str]`: read-only, so results are readable without a `get_goals` call.
- `event_label_id`: becomes read-only (derived, §4).
- `effective_priority`/`effective_is_fixed_time`: unchanged in meaning, now resolved through goals.

### 9.4 Health

| Tool | Signature | Notes |
| --- | --- | --- |
| `measure_goals` | `(cadence: Cadence, period: str \| None = None, goal_ids: list[str] \| None = None) -> list[Assessment]` | Read-only. Proposed assessments, each with its `explanation`, for the metric and rollup goals with that cadence (default period: the most recent fully ended one) |
| `record_assessments` | `(assessments: list[Assessment]) -> list[Assessment]` | Upserts assessment events (§6.3) as **`proposed`**, whatever `status` says. Usable outside a reflection, e.g. "rate today's cooking 70", and the next reflection presents it for confirmation. Never touches the health cache, which only reflects confirmed ratings |
| `get_goal_history` | `(goal_ids: list[str], cadence: Cadence \| None = None, start: date \| None = None, end: date \| None = None) -> list[Assessment]` | The default range is the last 12 periods at each goal's cadence |
| `rebuild_goal_health_cache` | `() -> GoalList` | Repair (§8) |

### 9.5 Reflection

| Tool | Signature | Notes |
| --- | --- | --- |
| `prepare_reflection` | `(cadence: Cadence, period: str \| None = None) -> ReflectionContext` | Read-only. See §11 |
| `record_reflection` | `(cadence: Cadence, period: str, assessments: list[Assessment], journal: str \| None = None, intentions: list[str] \| None = None, dry_run: bool = True) -> ReflectionResult` | **The only way a rating becomes `confirmed`.** The dry run validates and returns a preview table (goal, rating, explanation). A real run writes the assessments as confirmed, plus the journal event, and refreshes the health cache |

### 9.6 Compaction (changed, not new)

- `EventDecision` (`keep`/`create`): `event_label_id` is replaced by `goal_ids: list[str] | None` (on `keep`, `None` means unchanged and `[]` clears them).
- `prepare_compaction`'s `CompactionContext` gains `goals` (the active goals as `id`, `path` and `name` only: compact on purpose) and, for each event, `goal_ids` plus `suggested_goal_ids` (§10).
- The `timeline` shows goals (§10).

## 10. Compaction with goals

**Goal: tagging events with goals should cost the user almost nothing.** Compaction is already the moment when each past event is looked at, so goals are attached there, mostly automatically.

1. **The server suggests goals, deterministically.** For each past event without goals, `prepare_compaction` suggests the goals of the latest event with the same title (case and spacing ignored) in the previous 4 weeks, leaving out deleted goals. Otherwise it suggests nothing: silence means no goal, mirroring "silence means on schedule". Recurring events need nothing extra, since an instance carries its series' goals. *(Built this way instead of the separate **Goal Hints** tab first planned: deriving hints from recent events needs no extra tab or write path, and keeps itself up to date.)*
2. **The model applies suggestions without asking**, and proposes a goal for a `create` decision only when the notes clearly imply one. It asks only when it's genuinely unsure between two goals. The instructions returned by `prepare_compaction` say this explicitly.
3. **The timeline shows goals after each event's title** (built that way rather than as a third lane, so it fits a narrow screen), with changes marked, so a glance confirms them:

```
 TIME  NOTES                                 EVENTS                                  GOALS
17:00                                        ┌ Work · on schedule                    ◆ Time Tracker
18:15  ● Leaving for salsa class early t… ── ├ Salsa prep · new                      ◇ Dance
18:30                                        ├ Google Salsa class · on schedule      ◆ Dance
19:30                                        ├ Dinner w/ Sam & Priya                 ◇ Host friends, ◇ Tofu tikka
20:10  ● Done with dinner ────────────────── └ Dinner ends · 10m late (planned 20:00)
                                             ├ Reading · starts 10m late             ◆ Reading
20:30  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  ┄┄ now
```

   `◆` means already set and `◇` means newly suggested and about to be applied. It's the same text-first approach as today's timeline (`compaction_timeline.py` gains a `goals` field on `TimelineEvent` and the lane in `render`), so it works in any MCP client. A client that draws the structured `timeline` can render the same data as colored chips.
4. **A footer line ties the day back to goals**, which is the bridge into the daily reflection:

```
Goals today: Time Tracker 3h20 · Dance 1h45 · Host friends 2h10 (weekly target met ✓) · Wake 7am: 07:05
```

Correcting a goal is one sentence to the model ("dinner wasn't Tofu tikka, it was just hosting"). The model resubmits the dry run, and the corrected summary → goals mapping goes into Goal Hints on commit.

## 11. The reflection process

A skeleton that mirrors compaction's prepare → preview → commit flow, run by the MCP client.

### 11.1 Which goals a reflection covers

Each goal is assessed at exactly one cadence, so a reflection at cadence **C** for period **P** **rates** only the active goals whose cadence is **C**:

- **daily**: daily goals, for that day;
- **weekly**: weekly goals, for that Sunday–Saturday week (its period id is `week-` plus the date of its Sunday);
- **monthly**: monthly goals, for that calendar month;
- **every two months**: goals with the `every_2_months` cadence, for that aligned pair of months.

A longer reflection also **reviews** the finer-cadence goals without rating them again. The context includes each one's confirmed ratings within P (e.g. a daily goal's 7 ratings in a weekly reflection, shown as a mean and sparkline). That gives the conversation the longer view, prompts a look at trends, and can feed intentions, but it never creates a second rating at another frequency.

### 11.2 When reflections happen

- **Daily**: offered right after the last compaction that closes a day ("day closed; ready for a 2-minute reflection?"), since compacted events make the metrics accurate. You can always skip it.
- **Weekly**: **Sunday morning** by default (configurable), covering the Sunday–Saturday week that ended the night before; or the next time you talk to the assistant after then. It comes after Saturday's daily reflection, so the week's daily ratings are already confirmed when it reviews them.
- **Monthly / every two months**: on the first such session after the period ends.

`prepare_reflection` with no `period` picks the **oldest fully ended, unreflected period** at that cadence (one with no `kind = reflection` event), like compaction's oldest-day-first. A backlog is worked through one period at a time. You can also mark a period as skipped in bulk, so a two-week vacation doesn't become a chore.

### 11.3 `ReflectionContext`

```python
@dataclass
class ReflectionContext:
    cadence: Cadence
    period: str
    period_start: date
    period_end: date
    due: list[DueGoal]              # goals rated (same cadence, §11.1), each with path,
                                    # measure, target, deadline, a ≤300-char description
                                    # excerpt, its last 6 confirmed ratings, and a proposed
                                    # Assessment with explanation (metric/rollup) or None
                                    # (subjective/llm)
    reviewed: list[GoalReview]      # finer-cadence goals: confirmed ratings within the period
    already_recorded: list[Assessment]   # proposed ratings written earlier, to confirm
    goal_time: dict[str, int]       # minutes per goal in the period (includes descendants)
    events_digest: str              # compact per-day list of events with goals
    notes_digest: str               # compacted notes' text in the period
    previous_intentions: list[str]  # from the previous reflection at this cadence
    uncompacted_notes: int          # >0 → warn: metrics may be off until compaction runs
    instructions: str
```

### 11.4 The conversation (encoded in `instructions`)

1. **Open with the measured goals.** One line per goal: band, proposed rating and its `explanation` ("🟢 Wake 7am: 92, woke 07:05; target 07:00 with 10 min grace"). Ask for agreement in bulk ("agree with these 5?"). Change only what you object to, and record any reason you give as the `rationale`.
2. **Subjective goals:** ask the goal's `prompt`, **one goal at a time**, for a 0–100 number. Accept "skip", and accept words ("pretty good"), proposing a number back for you to confirm.
3. **LLM goals:** propose a rating with a one-sentence rationale grounded in `events_digest`/`notes_digest`, and ask for confirmation.
4. **Proposed ratings recorded earlier** (`already_recorded`): present them for confirmation the same way as step 1.
5. **For a weekly or longer reflection,** summarize the `reviewed` finer-cadence goals in a line or two, without rating them.
6. **Look back at `previous_intentions`:** did they happen?
7. **Ask (optionally) for a short journal entry** (≤ 8,000 characters, §6.2) and up to 3 intentions for the next period.
8. **Call `record_reflection(dry_run=True)`,** show the preview table, then commit. Only then do the ratings become `confirmed`.

A daily reflection should take under two minutes: usually one bulk confirmation, plus one or two subjective questions.

### 11.5 Extension points (left open on purpose)

- New `measure` kinds register a function `(goal, period, events, notes) -> Assessment | None`.
- An LLM assessment can also be precomputed by a scheduled agent that writes it with `record_assessments`, so it's `proposed`. The next reflection still has to confirm it.
- Intentions could later become calendar events. Out of scope for now.

## 12. The Goals page (time-tracker-client)

This replaces `EventLabelsScreen`, `EventLabelsRepository`, `EventLabelDialog` and the `EventLabel` model in the Flutter app.

- **List:** an indented tree with a disclosure chevron for each goal. Each row shows the color swatch, name, a health dot plus an 8-period sparkline drawn from `health_trend`, its cadence chip, and a "stale" badge when needed. A filter at the top switches between **Active** (default), **Inactive** and **All**, next to a label budget readout (`143 / 189 labels`).
- **Active toggle** on each row (a swipe or menu action). Deactivating shows a confirmation that explains history is kept. Activating past the budget shows the tool's error, with its suggested goals to deactivate.
- **Goal detail:** a rendered description (Markdown, Mermaid, Drive images through `get_goal_image`); target, deadline and measure; a history chart from `get_goal_history` (0–100 bars per period at the goal's cadence, with the band thresholds drawn as reference lines; proposed-but-unconfirmed ratings shown hollow); the in-progress value for duration/count goals; "Add sub-goal".
- **Edit dialog:** today's label dialog (name, color, priority, fixed time, note), plus parent, cadence, measure (a form for each kind) and target/deadline. The description is edited in a Markdown editor with a preview tab and an image-attach button.
- **Events page:** shows goal chips on each event (from `goal_names`), and the event dialog lets you pick goals with a typeahead over active goals.

## 13. Migration from event labels

The migration runs once and automatically, the first time `Goals` is constructed for a calendar (the same lazy-ensure pattern as `EventLabels.__init__`):

1. Create the Goals tab. Each named Event Labels row becomes a top-level, **active** goal: a new short `id`, `label_id` = the existing label id (no events change, and the colors stay identical), with name, color, priority, fixed_time and note copied across. Unnamed labels aren't migrated; they stay on the calendar untouched.
2. Rename the old tab to "Event Labels (migrated)", so it's kept for reference. It keeps its tag, but nothing reads it once a goals tab exists. The goals tab is tagged only after its rows are written, so an interrupted migration just runs again.
3. Events need no rewrite: their goals are read from `eventLabelId` when the goals property is missing (§4). They gain the property the next time any tool writes them.
4. The tool removal is a breaking change to the MCP surface. Ship the client's Goals page in the same release window, since the client calls `update_event_label` today.

You can then reorganize: give migrated goals parents, cadences and measures, add new goals, and deactivate category-like ones you don't want to assess.

## 14. Implementation phases

| Phase | Server (this repo) | Client |
| --- | --- | --- |
| 0 (done) | Ran `probe_label_lifecycle.py` (§5). `list_events` pages through results. Fixed the two latent bugs the probe exposed: the stale label id on inserts (§5) and silent description truncation in compaction (§6.2) | – |
| 1 (done): Goals replace labels | `GoalSheet`/`Goals` (replacing `EventLabelSheet`/`EventLabels`), `Event.goal_ids`, `GoalCalendar` (replacing `LabelPriorityCalendar`), the migration, the goal and event tools in §9.2–9.3, removal of the label tools | Goals page (list, toggle, edit), event goal chips |
| 2 (done): Health storage | The Goal Health calendar, `record_assessments`, `get_goal_history`, cache columns, `measure_goals` (duration/count/wake_time/rollup) | Health dots, sparklines, history chart |
| 3 (done): Compaction + reflection | `prepare_reflection`/`record_reflection`; goal suggestions and marks in compaction | – (the reflection runs in the MCP client) |
| 4: Rich descriptions | Goal Details tab, Drive images, the description tools | Markdown/Mermaid rendering and editor |

Each phase is shippable on its own. Phase 1 alone is a strict improvement over labels: hierarchy, active/inactive, unlimited goals over time.

## 15. Decisions

Decided 2026-10-02:

1. **Longest cadence: every two months**, in aligned pairs of months (Jan–Feb, …, Nov–Dec), with period ids like `2026-09..10`. The enum value is `every_2_months`, spelled out so it can't be misread as twice a month.
2. **Rating scale: 0–100**, shown in three bands (0–39 🔴, 40–69 🟡, 70–100 🟢).
3. **One cadence per goal.** Longer reflections review finer-cadence goals but don't rate them again (§11.1). If a goal ever needs a second frequency, `measure` could become a list keyed by cadence.
4. **Goal Health calendar: hidden if the scope allows it**, and not something the design relies on. History is viewed in the Time Tracker app.
5. **Nothing is confirmed outside a reflection,** measured ratings included. Measured ratings carry a one-line explanation so agreeing with them is quick (§7).

No open questions remain at the design level. The thresholds and mapping formulas in §7 are starting points to tune once real data exists.
