# Goal tree restructure and traits: implementation plan

Agreed with Chris, 2026-10-05. People's goals are named here **by id
only**: names stay in the Goals sheet, and `get_goals` resolves them.
Keep it that way in code, tests, commits and PRs.

Three phases, in order; each ships without the next:

1. Tree restructure (data only).
2. Weights that expire (server + client).
3. Traits: event facets written by compaction, a `traits` measure, and a
   Traits tab.

## 1. Tree restructure

**Done 2026-10-05.** New ids: inner zjdtkg, middle ep1sb2, outer tbx8eo,
neighborhood v49aol; Plan tomorrow i3h3z6, Find a partner 0w3o5z, Follow
up with someone new ss5awc, Know my building 09smob. Priorities: self 0,
job 1, inner 2, the rest 3; l0sixx and t813z7 keep priority 3 explicitly
(Chris's choice); oexqm1's own priority was cleared. Overall weights: self
5, job 2, inner 1, future self 1, middle/outer/neighborhood/g537hk 0.

Keep ids (rename in place, move by `parent_id`); archive, never delete.
Unlisted sub-goals move with their parent.

```
Serve self            5f6bss  rename ("Take care of myself")   priority: first
  r4o64w idi9rr l0sixx(from 3zwv2x) bq28vc 2jlr4c v0f79q hr0b45 t813z7(from 3zwv2x)
  2jlr4c Maintain my home  <- gains 8yowmu Host at my place
  NEW Plan tomorrow        count, 1 per day
Serve job             iowa65  rename ("Fulfill my work commitments")   priority: second
  i8nu2d
Serve inner circle    NEW                                       priority: third (raise later)
  q0t2ld zqsxct xvnln4 5nnpld ny42qk hl0aj2
Serve middle circle   NEW
  6r7110 6g7u8p kxybyq 1fyp1e v4oke8 11wkqa midz3p 29jdag 2t267n i9235l 8jxabw
Serve outer circle    NEW
  hwlbna;  NEW Follow up with someone new  count, 1 per 7 days
Serve neighborhood    NEW   (one goal: building and wider neighborhood)
  ppnuv4 y7w450;  NEW Know my building  count, 1 conversation or invite per 30 days
Serve future self     6cvbyv  rename ("Develop myself")
  1xuzjg 7fd1r2 oexqm1 rbkrse jw424q(from 3zwv2x) 8zyoda(inactive) 7v9exw(inactive) 3draq5(inactive)
  NEW Find a partner       count, 1 date per 14 days
g537hk Follow through on my word   top level, Overall weight 0, until phase 3
```

Circle placement is rough on purpose; Chris will move people later.

**Archive** once emptied: 72hq6d, 3zwv2x, 1vj1rk, ly90ub, rilfz4, yraorq,
oio77e, 1atdu6, and the six "Listen to …" goals hzmce9 wggtfi 0vhvsc
m58pzm 3mwita 8z8rn2 (Generous's attention part replaces them). v0buqf
stays until phase 3.

**Priority** is set per top-level goal and inherited, not copied from each
goal's old effective priority; list any child that sets its own and
contradicts the order, for Chris to decide. 8ufbgo (the app) **keeps
priority 0** — Chris will lower it himself when it's ready.

**Overall**: weighted, strongly favoring Serve self (e.g. self 5, job 2,
inner 1, future self 1, others 0 — suppressed with an expiry once phase 2
lands). New top-level goals have no measure (mean of children) for now.
Numbers are placeholders; the structure is what matters.

Use one write path (sheet + `sync_goals_from_sheet`, or `update_goal`).
Done when `get_goals` shows exactly these top-level goals, no event lost a
goal, and labels stay under 200 (~122).

## 2. Weights that expire

A rollup `weights` value may be a number or
`{"weight": 0, "until": "YYYY-MM-DD", "then": 1}`: `weight` before
`until`, `then` from it on. Validate in `measure_problems`. Recorded
ratings are never recomputed. Reflection instructions mention suppressions
that expired since the last reflection, so Chris can extend or let lapse.

**Client** (time-tracker-client): show a suppressed weight and its expiry
on the goal, and let it be set and edited there.

## 3. Traits

### 3.1 Concepts

- **Traits**: Thoughtful, Reliable, Creative, Adventurous, Generous.
  Disciplined is folded into Reliable ("reliable to myself too").
  Configurable in a **Traits** tab: id, name, status (active | off |
  archived), definition, parts (JSON).
- A **part** is a measure with no scope; the goal using the trait supplies
  it (`events_of` = that goal and sub-goals).
- **`traits` measure kind**:
  `{"kind": "traits", "traits": [...] | "all", "weights"?, "window_days": 30}`.
  Rating = weighted mean of the selected traits' scores over the goal's
  events. People goals select all five; self goals can select Reliable.
  Their existing sub-goals (visits, activities) become inputs, not
  averaged children. `only_if` limits rating to days with an event of the
  goal; the window keeps scores stable between them.
- **Not used:** event creation times. Nothing depends on when an event was
  put on the calendar.

### 3.2 Storage

| What | Where | Written by |
|---|---|---|
| Trait definitions | **Traits** tab, metadata sheet (hand-editable) | `create_trait`/`update_trait`, or by hand |
| Per-event facets | Main calendar event, `extendedProperties.private["cascading-time-tracker-facets"]` (JSON, ≤ 1024 chars) | Compaction, when it records a past event that carries a goal using a `traits` measure; editable later with `update_event` |
| What matters to each person | The person goal's description (**Goal Details** tab), a "What matters to them" section: facts, upcoming moments, preferences, each dated | Compaction and reflection, when notes reveal something; Chris by hand |
| History digest for an LLM judgment | Not stored: computed on request from past events' facets | Server |
| Ratings | Goal Health calendar, as today: one assessment per goal per day; `metrics` holds `{"traits": {"<id>": score}}` and the parts | Reflection |
| Trait history | Derived: per day, the mean of that trait's scores across goals rated | `get_trait_history` |

**Facets** (all optional; short, normalized strings):

```jsonc
{
  "with": ["ny42qk"],          // goals of people present
  "for":  [],                  // goals the event was done for while they weren't there (preparation)
  "activity": "salsa social",  // reuse labels from the digest's vocabulary
  "place": "<venue>",
  "creative": 2,               // 0-3: made something together
  "new": "place",              // none | activity | place | both — judged against the digest
  "effort": 1,                 // 0-3: effort beyond showing up (prepared, cooked, hosted, traveled)
  "attention": 3,              // 0-3: quality of attention, from notes
  "why": "she talked through the job offer; phone away"   // one line of evidence
}
```

Why on the event: it travels with the event, survives notes being garbage
collected, and makes every part a plain computation over events — the
same machinery counts and durations use. Notes are already appended to
event descriptions by compaction, so the raw text survives too.

**History digest** (what makes novelty and creativity judgments easy): for
a goal and window (default 180 days), the server groups past facets by
activity and by place with count, first and last date — e.g.
`salsa social ×9 (first 2026-04, last 2026-09-28), volunteering ×2 …`.
Compaction is shown it before writing facets, so it reuses the same labels
and can judge `new` directly.

### 3.3 Parts per trait

| Trait | Core | Computed parts (from facets and events) | Judgment |
|---|---|---|---|
| Thoughtful | Remembers what matters to them | **prep**: events with the goal in `for` in the window; **prep regularity**: share of recent weeks with at least one | Did events and notes reflect what's in "What matters to them"? |
| Reliable | Does what he said, keeps contact going | **continuity**: last event within X days and next within Y (100/50/0); **follow_through** (exists); **cadence** count (exists) | Rarely needed |
| Creative | Makes something together (alone is Serve self) | **together creative**: events with the goal in `with` and `creative ≥ 2` in the window | The `creative` facet itself, judged at compaction |
| Adventurous | New experiences together | **novelty**: events with `new ≠ none` in the window | The `new` facet, judged against the digest |
| Generous | Made an effort for them, including attention — the follow-through of thoughtfulness | **effort paid**: sum over the window of minutes × (1 + effort), counting both `with` and `for` events, against a target; **attention**: mean `attention` of `with` events | The `attention` facet, from notes |

### 3.4 Compaction and reflection

- **Compaction** proposes facets for each past event carrying a
  traits-measured goal (shown the digest and the "What matters to them"
  section), plus additions to that section. They're part of the plan Chris
  approves, written on apply, before notes are garbage collected.
- **Reflection** reads facets, so it mostly proposes: each traits-measured
  goal due that day comes with computed parts filled in and a proposed
  rating; Chris corrects in bulk. No per-trait, per-person questions.
- **Learning loop**: keep confirmed judgments next to computed parts so
  that, later, judgment parts that add nothing can be retired.

### 3.5 Tools

`get_traits`, `create_trait`, `update_trait` (archive to retire),
`get_trait_history`; `prepare_compaction`/`compact_notes` gain facets;
`prepare_reflection`/`record_reflection` extended; a digest tool or a
digest field where compaction and reflection need it. Tool descriptions
are the reflecting assistant's only documentation.

### 3.6 g537hk

Reliable's follow_through part, scoped to each goal, counts what tagging
events with g537hk did. g537hk stays parked (top level, weight 0) until
both hold, then is archived (its history stays):

1. Traits ratings have been confirmed in reflections for about a week, on
   people goals and on the self goals that select Reliable.
2. A check finds every event tagged g537hk in the last 30 days also
   carries a goal whose `traits` measure includes Reliable. Any commitment
   that doesn't (one tagged g537hk alone) is shown to Chris, to give it a
   goal or accept that it stops counting.

Then stop tagging new events with it.

### 3.7 Client (time-tracker-client)

The client reaches the server through its MCP tools, so each screen below
needs the tool it reads or writes.

**Facets**

- Event detail shows the event's facets (who it was with and for,
  activity, place, the four 0–3 scores, the evidence line) and lets them
  be edited; saved through `update_event` (facets become a `PublicEvent`
  field, clearable via `clear_fields`).
- A person goal's page shows its history digest (activities and places
  with counts and first/last dates), its "What matters to them" section
  (edited as the goal description already is), and a timeline of its
  events with their facets, so a trait score can be traced to the events
  behind it.
- A trait score anywhere (goal health, trait history) opens the parts
  and events that produced it.

**Traits**

- A Traits list beside the goal tree: name, status, latest score, trend.
- Create, rename, reword the definition, change status (active, off),
  archive; edit parts (add or remove a part, its parameters and weight,
  judgment rubrics) with the same validation the server applies
  (`trait_problems`, like `measure_problems`), so a bad part is refused
  with a message rather than silently unrated.
- On a goal, pick which traits its `traits` measure selects and their
  weights.

### 3.8 Not now

A goals-by-traits grid in the client; automatic rating without judgment
(the learning loop is the way there).
