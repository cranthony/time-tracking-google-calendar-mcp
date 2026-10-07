# Habits

Status: built in the server: 1. the habits store and tools; 2. habits
as judgment and cancellation subjects; 3. backfilling a habit's
judgments. The app's side is in progress.

A **habit** is something the user wants to do well -- "Practice guitar",
"Cook" -- rated by traits, as a person is. It belongs to the user (self),
and is about one action or action group: only the user's events with
that action, or any action under that group, count toward it.

## Terms

- **Scope.** A habit's events: those with its `action_id`, or with any
  action under it, if it's a group. Worked out from the actions and
  groups as they are when it's used -- when an event is judged, or a
  score worked out -- not stored: moving an action between groups
  changes what's in scope from then on, scores included, as a person's
  scores change when an event's facts do.
- **Traits.** A habit's `traits` are a person's: `{"select": "all" or
  [trait ids], "parts": {trait id: [parts]}}`, both optional. Without
  `select`, every active trait applies; `parts` replace a trait's parts
  for that habit alone -- its own, more specific rubrics. A habit is
  always "with": judgment parts for those an event was done "for" don't
  apply to it. Parts with their own `action` (count, duration,
  continuity, follow-through) narrow within its scope, never widen it.
- **Subject.** Who or what an event is judged for, and a cancellation
  recorded against: a person, by their id, or a habit, by
  `habit:<habit id>`.

## 1. The store and tools

The **Habits** tab of the calendar's metadata spreadsheet
(utilities/habits.py), one row per habit: `id`, `name` (unique),
`action_id` (an action's or a group's id), `status` (`active`,
`archived` or `deleted`; deleted habits are kept, so judgments naming
them still make sense), `note` (what it's for, and what doing it well
looks like: context for judging its events) and `traits`.

| Tool | |
|---|---|
| `get_habits(statuses?)` | The habits, by default active ones, each with its `action_path` ("Creative › Guitar"). Read-only. |
| `get_habit(id_or_name)` | One, suggesting close matches if there's none. Read-only. |
| `create_habit(habit)` | `name` and `action_id` required; `traits` checked as a person's. Returns it, and `created_id`. |
| `update_habit(habit, clear_fields?)` | Sets what's given; `clear_fields` (`note`, `traits`) blanks. Deleting is `status: deleted`. |

A habit's `action_id` is checked when it's written: it has to be an
action (not a deleted one) or a group. One whose action or group is
deleted later is left as it is -- it just has nothing in scope -- and
other habits can still be written.

## 2. Judgments and cancellations

- **Judging.** `prepare_judgments` adds each active habit with an event
  in scope to that event's subjects, engagement "with", with the judgment
  parts of the traits that apply to it (its own `parts` first). Its
  request's history is the user's past events in its scope, and its note
  stands in for a person's notes and what matters to them; the framing
  says it's the user's habit. Judgments are kept on the event as a
  person's are, under `habit:<id>`: `judgments["habit:<id>"][trait
  id][part key]`. `update_event` takes hand-made ones under a habit's id
  too.
- **Cancellations.** A cancel that counts is recorded against each
  active habit whose follow-through part it matches, as the event was
  planned -- the same rule as for people -- one row per habit, under
  `habit:<id>`. `get_habits` and `get_habit` return each habit's
  `cancelled_events`, as the people tools return a person's. The
  compaction timeline's `Follow-through:` list, and a proposal's
  `follow_through`, name it "the <name> habit".

## 3. Backfilling

A habit made, or given a new rubric, has no judgments on events already
compacted. Backfilling them is manual, never automatic:

- `prepare_habit_judgments(habit_id, since?, redo = false)` (by id or
  name; an active habit) returns a `backfill_id` (`habit:<id>@<since>`),
  and the judgments due for that habit on its settled, in-scope events
  from `since` until where history ends (the last compaction), by
  default as far back as its judgment parts average over (`window_days`,
  30 unless said) plus the week of scores shown, in `prepare_judgments`'s
  layout. Read-only. Without `redo` it leaves out what's judged already; with it,
  each comes with its `current` judgment, to judge again.
- `record_judgments(compaction_id, judgments)` takes the `backfill_id`
  in place of a compaction's id, works out the backfill's requests again
  (judged ones included, so a redo's are accepted), checks each judgment
  against them, and writes it, replacing any earlier one. Nothing about
  a backfill is stored: its id says all it is.

It's a tool of its own rather than an argument of `prepare_judgments`:
that one is the last step of a compaction, which isn't complete until
its judgments are recorded; a backfill completes nothing. Follow-through
can't be backfilled: cancellations are recorded only as they happen.

## The app

The app scores habits as it scores people, from the events, their
judgments and the cancellations: a habit's events are the user's in its
scope, its judgments those under `habit:<id>`. Habits are listed and
edited under the user (self).
