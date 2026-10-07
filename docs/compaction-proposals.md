# Compaction proposals

Status: built in the server; the app's side is in progress.

A compaction becomes a **proposal** the user confirms: "this is what
happened from `from` to `through`." Claude prepares it; the user reviews
it in the app, editing it or leaving notes for Claude; only the user's
confirmation applies it. Notes are an aid to building it, not a
requirement: a day with no notes is proposed as planned.

A scheduled Claude routine drives it. Each run, in order: makes pending
judgments; finishes a confirmed proposal whose apply failed; revises a
proposal with open feedback; extends a proposal awaiting review to now;
or, with none open, proposes one from the last confirmed time to now. A
conversation with Claude can do any of these sooner.

## Terms

- **Proposal**: one compaction under review, with a stable id. At most
  one is open at a time.
- **Revision**: one version of a proposal, numbered from 1. Every change
  -- Claude's, the user's, an extension, a recheck -- makes a new one.
  Only the newest (*current*) revision can be confirmed or applied.
- **Window**: `from` (the last applied proposal's `through`) to `through`
  (the `now` the revision was planned at). Everything before the last
  applied `through` is history.
- **Claude decisions**: what Claude says happened differently from the
  plan (keep with changes, create, cancel, merge), as `compact_notes`
  takes them today. An event no decision mentions happened as planned.
- **User ledger**: the user's edits to the proposal, one entry each,
  only ever appended to. Kept per proposal, not per revision.
- **Feedback**: a note from the user for Claude to act on, kept per
  proposal. The server adds its own when it needs Claude (a failed
  recheck or apply).
- **Key**: the id of an event a proposal creates, which doesn't exist
  until it's applied: `<proposal>c<n>` for Claude's creates,
  `<proposal>u<n>` (its ledger entry's id) for the user's. Edits and
  `as_planned` name a created event by its key, as they name others by
  event id. Claude keeps a create's key by sending it back.
- **Ids**: a proposal's id is 12 hex digits; a revision's is
  `<proposal>r<n>`, its later days `<proposal>r<n>d2`, and so on;
  feedback `<proposal>f<n>`. Only `0-9a-v`: created events' Calendar ids
  are made from the revision's.

## How a revision is built

A revision's outcome is always: the calendar as it is now, with Claude's
decisions applied, then the whole user ledger on top. A user entry
overrides Claude's decision on the same event field by field (an entry
that only moves an end keeps the facts Claude set); `as_planned` clears
Claude's decision on that event. The planner then works out the calendar
writes, deterministically, refusing any overlap (nothing is moved to
make room) or change to history, as today.

Since Claude never sends the user's entries and the server always lays
the whole ledger on top, no revision can leave a user edit out.

Notes work the same way. A note that doesn't set an edge has its text
added to the event it falls within, unless Claude ignores it
(`ignore_notes`) or adds it to another event (`annotate_notes`). The
user's note edits override either, note by note -- `annotate` (where it
falls, or with a named event) or `ignore` -- and `as_planned` on a note
puts it back as Claude had it. A note that sets an event's start or end
isn't added to any description unless annotated: with the event named,
or, naming none, with the event whose edge it sets. Note edits never
touch an edge: only an event edit moves one. A note's text is never
added to a description twice.

The actions, people and locations Claude's decisions add (by `new:`
ref) are created only when the proposal is applied. The user can settle
one sooner, without confirming the rest: `create` it now (with its name,
a person's context or a location's hint corrected), say it's one
that's `existing`, or `drop` it. Every revision from then on names it by
its id, or leaves it out of the events' actions and facts, wherever the
decisions named its ref; Claude may keep sending the ref, and isn't
refused for one the user created. `as_planned` on a ref unsettles it.
One created stays, whatever becomes of the proposal.

## Contract

Every write names the revision it started from (`revision`). Writes
never interleave (they hold the write lock, in one process), but either
side may have started from a revision that's since been replaced; the
rules below say what happens then. A refused call writes nothing.

### `prepare_compaction` (Claude; read-only)

- Returns the window from the last applied `through` to now, with or
  without notes, and its events and timeline as today. At most a week:
  a longer gap takes more than one proposal.
- With a proposal open, also returns `proposal`: its current revision's
  Claude decisions (`updates`, `creates` with their keys, `cancels`,
  `ignore_notes`, `annotate_notes`), the user ledger, every feedback
  item, and `user_seq` (the last ledger entry included).

### `compact_notes` (Claude; plans only)

Proposes, revises or extends: `compact_notes(updates, creates, cancels,
ignore_notes, proposal_id?, revision?, replies?, new_actions?,
new_people?, new_locations?, annotate_notes?)`. `replies` is
`[{feedback_id, reply}]`; `annotate_notes` is `[{note_id, event_id}]`
(an event's id, or a create's key from the open proposal). Each call
sends all of Claude's decisions, planned to now; a create with a `key`
keeps it.

- `dry_run=False` is removed: Claude can't apply anything.
- With no proposal open, starts one (revision 1). With one open, a call
  without its `proposal_id` is refused.
- Refused while the proposal is applying.
- Every feedback item open and added before Claude's starting revision
  was written needs a reply. Replies to items withdrawn since are
  ignored; items added since may be answered, or stay open.
- Claude's decisions can't override a user entry, except on an event
  or note named by a feedback item this call answers, and then only
  entries up to its starting revision's `user_seq`. A newer user entry stands, and
  the reply is marked superseded by it.
- Built on the current revision, whatever revision it started from. If
  that overlaps the user's newer entries, it's refused: prepare again.

### `get_proposal` (app, Claude; read-only)

`get_proposal(proposal_id?, since_revision?)`: the current revision of
the open proposal (or the one named), planned again from the calendar
as it is now:

- `id`, `revision`, `state` (`awaiting_review`, `awaiting_claude`,
  `applying`, `applied`, `abandoned`), `window_start`, `through`, `by`,
  `reason`, `created`.
- `events`: every event of its days, as the revision leaves them: `id`
  (event id or key), `summary`, `start`, `end`, `description`,
  `location`, `priority`, `action_ids`, `facts`, `status` (`on_schedule`, `adjusted`, `new`,
  `cancelled`, `merged`, `planned` -- the future, untouched),
  `planned_start`/`planned_end`, `decided_by` (`claude`, `user` or
  none), `history_until`, `is_end_of_day_sleep`, and for a cancelled
  one `counts_against_follow_through` and `follow_through` (who it
  counts against; to flip it, cancel it again). `None`, with `problem`
  saying why, when it no longer plans (confirming then hands it to
  Claude).
- `notes`: `id`, `timestamp`, `description`, `use` (`edge`, `annotates`,
  `ignored`, or `unused` -- no text, or no event to add it to),
  `event_id` (the event, or key, it's added to -- for an `edge`, whose
  edge it sets), `edge_of` (the event whose start or end it sets, if
  any, annotated or not) and `decided_by` (`claude`, `user`, or none
  for a note just added where it falls).
- `changes` (the calendar writes), `warnings`, `timeline`, `additions`
  (those still to create on apply) and `settled_additions` (`ref`,
  `use`, `id`: those the user settled).
- `user_edits`: the ledger (`id`, `edit`, `status` `active`,
  `inapplicable` or `replaced`, `created`, `base_revision`).
- `feedback`: `id`, `text`, `event_id`, `note_id`, `at`, `by` (`user`/`server`),
  `created`, `status` (`open`, `answered`, `withdrawn`), `reply`,
  `answered_in`.
- `changed_since`: with `since_revision`, the ids and keys whose outcome
  changed after it (the union of each later revision's `changed`).

### `amend_proposal` (app)

`amend_proposal(proposal_id, revision, updates?, creates?, cancels?,
as_planned?, notes?, additions?)`: `updates`/`creates`/`cancels` as
update_event takes them -- an update is `{event, clear_fields?}` (the
event's id may be a key), a create an event -- each update and create
also taking `start_note`/`end_note` (a note that sets that edge);
`notes` is `[{note_id, use: annotate | ignore, event_id?}]`;
`additions` is `[{ref, use: create | existing | drop, id?, name?,
context?, hint?}]`; `as_planned` event ids, keys, note ids or refs. Returns the new revision, as `get_proposal` does, with
`replaced`: the ids of events whose newer Claude change it overrode.

- Appends one ledger entry per edit and writes a new revision with
  `by: user`, built on the current revision.
- From an outdated revision: still applied on the current one; the
  response names each of Claude's newer changes it replaced.
- Refused as a whole if an edit names an event or note outside the
  window or gone, adds a note to an event of another day, would make a
  description too long, if the result overlaps, or if it changes
  history. The response
  returns the current revision and the refused edits.
- A `description` is the event's whole description, as the user wrote
  it from what `get_proposal` shows, notes and all: no notes or
  `annotate` text are added to it, and it replaces Claude's `annotate`.
  A note whose text it has counts as added; one whose text it dropped,
  as ignored. `location` and `priority` set the event's own;
  `clear_fields` clears a description, location or facts, but not a
  priority. Refused: `is_cancelled` (that's `cancels`), `judgments`,
  and an update without an id.
- An addition the user creates is created once the revision is known
  to plan, so a refused amend creates nothing. One settled with a ref
  the proposal doesn't add, an `existing` id that isn't there, or an
  id with any other use, is refused.
- Edits set actions and facts (location, people, notes on people) as
  `compact_notes` does, naming existing ones by id. A new action,
  person or location is created first (`create_action`,
  `create_person`, `create_location`), then named; the `new:` refs of
  `new_actions` and the like are only for Claude's additions, created on
  apply. An edit naming one that's been deleted is refused like any
  invalid edit.
- Refused while the proposal is applying.

### `add_proposal_note` / `withdraw_proposal_note` (app)

- `add_proposal_note(proposal_id, text, event_id?, at?, note_id?)` returns the
  feedback item, its id `<proposal>f<n>`.
- `withdraw_proposal_note(feedback_id)`: open -> withdrawn; already
  withdrawn -> no change, success; answered -> refused, naming the
  revision that answered it; unknown, or the proposal finished ->
  refused.

### `confirm_proposal` (app only)

`confirm_proposal(proposal_id, revision)` returns `{status, message,
proposal}`, `status` one of `applied`, `rechecked`, `needs_claude`.

- Refused unless `revision` is current and no feedback is open.
- Rechecks: plans the revision again on the calendar as it is now.
  - Same writes: applies them (status `applying`), then stamps its
    notes and makes `through` the new start of history.
  - Different writes: writes a new revision (`recheck`) to confirm
    again.
  - Can't be planned: adds server feedback for Claude.
- Only the app calls it. It's a tool of its own so the user can deny it
  to Claude in Claude's connector settings -- for conversations and the
  routine alike.

### `finish_proposal` (app, Claude)

`finish_proposal(proposal_id)` returns as `confirm_proposal` does
(`applied`, or `rebuilt`).

- A confirmed revision that stopped partway is resumed: writes already
  done are skipped. It was approved when confirmed, so finishing needs
  no new approval, and Claude may do it.
- A write that can never succeed (Calendar: not found, gone) marks the
  revision `failed`, and the server writes a new revision (`apply
  failed`), its first warning saying what stopped it and what couldn't
  carry over, for the user to confirm again. If what's left can't be
  planned, server feedback hands it to Claude instead. Days already
  applied stay applied; the new revision's `from` is where they end.
- The new revision replays the decisions on the calendar as it is now:

  | Decision | Write done | Write not done |
  |---|---|---|
  | Update | Already matches: no write | Planned again |
  | Create | Dropped: the event exists as decided; edits naming its key follow it to the created event | Stays a create |
  | Cancel | Dropped; its follow-through is recorded when the apply fails | Stays a cancel |
  | Names an event that's gone | Dropped; listed in the feedback | Dropped; listed in the feedback |

  A user entry naming an event that's gone stays in the ledger, marked
  inapplicable. A note is never added to an event's description twice,
  so replaying a revision whose notes were partly written is safe.

### Others

- `abandon_compaction(proposal_id)` abandons the proposal. Writes
  already made stay.
- `get_compaction_status` adds `proposal`: the open one's `id`,
  `revision`, `state`, `window_start`, `through` and `open_feedback`
  (a count). `last_compaction` is the last applied `through`.
- `prepare_judgments`/`record_judgments` (after apply) and
  `update_event` (history ends at the last applied `through`) work as
  today.

## Journal rows

Same tab and columns:

```
compaction_id | step | kind | event_id | before | after | status | detail
```

| `kind` | `compaction_id` | `step` | Other columns |
|---|---|---|---|
| `compaction` | revision-day id: `<proposal>r<n>`, then `<proposal>r<n>d2`… for later days | 0 | `status` (below); `detail`: today's (`now` = `through`, `note_ids`, `warnings`, `ignore_notes`, `note_targets` (note -> event), `batch`/`day`, `additions`) plus `proposal`, `revision`, `from`, `created`, `base` (starting revision), `user_seq`, `by` (`claude`/`user`/`server`), `reason` (`proposed`, `extended`, `revised for notes`, `user edit`, `recheck`, `apply failed`), `changed` (event ids whose outcome changed from the previous revision), `claude_ignore_notes`, `claude_note_targets` and `claude_additions` (Claude's own, before the user's note edits and settled additions; the first day's `additions` are those left to create) |
| `decision` | revision-day id | 0 | A decision the day was planned with -- Claude's and the user's merged -- as JSON in `before`; `event_id` repeats its event |
| `claude_decision` | revision's first day | 0 | One of Claude's own decisions, as JSON in `before`: what the next revision starts from |
| `update`/`create`/`cancel` | revision-day id | 1… | One calendar write: `event_id` (a create's key, for a create), the event's state `before` and `after`, `pending`/`done`, reason in `detail` |
| `user_decision` | proposal id | n (id `<proposal>u<n>`) | The edit as JSON in `before` (a decision, `as_planned`, a `note` edit, whose `event_id` column holds the note's id, or an `addition`, whose `event_id` column holds its ref); `status` `active`, `inapplicable`, or `replaced` (by Claude answering the user's feedback on that event); `detail`: when, and the revision it started from |
| `feedback` | proposal id | n (id `<proposal>f<n>`) | `before`: text, `at`, `note_id`, author (`user`/`server`), when; `after`: reply and answering revision; `status` `open`, `answered` or `withdrawn` |

`compaction` row statuses:

| Status | Meaning |
|---|---|
| `proposed` | The current revision, awaiting review (or Claude, if feedback is open) |
| `superseded` | Replaced by a newer revision; never applied |
| `applying` | Confirmed; writes in progress |
| `applied` | Writes done; notes not stamped yet |
| `stamped` | Done; its `through` is the start of history |
| `failed` | Stopped by a write that can't succeed; some writes may be done |
| `abandoned` | Given up |

The proposal's own state is derived: `applying`/`stamped`/`abandoned`
from its current revision, otherwise awaiting Claude while any feedback
is open, otherwise awaiting review.

## Garbage collection

Runs as each compaction step starts (before it reads, so nothing it
deletes is read again), and deletes rows anywhere in the tab, bottom
first, not only from the top. Every id lives in a row's own cells, so deleting rows never changes
one.

Kept:

- An open proposal's current revision, ledger and feedback.
- Its last 3 superseded revisions whole, for debugging; of older ones,
  only the `compaction` rows (their `changed` lists).
- A `failed` revision whole until its proposal finishes: its `before`
  states are the only record of the writes that went through.
- The most recently stamped revision, as today.

Deletable: the rest of superseded revisions; once a proposal is
stamped, its ledger, feedback and superseded rows (its applied rows age
out as today); everything of an abandoned proposal.

Superseded rows are safe to delete because they were never applied, and
nothing reads them: revisions are built from the current one and the
ledger, override checks use `user_seq`, stale confirms need only the
revision number, and "changed since" uses the kept `changed` lists.
