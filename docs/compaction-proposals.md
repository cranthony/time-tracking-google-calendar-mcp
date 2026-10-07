# Compaction proposals

Status: design, not built.

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

## Contract

Every write names the revision it started from (`revision`). Writes
never interleave (they hold the write lock, in one process), but either
side may have started from a revision that's since been replaced; the
rules below say what happens then. A refused call writes nothing.

### `prepare_compaction` (Claude; read-only)

- Returns the window from the last applied `through` to now, with or
  without notes, and its events and timeline as today.
- With a proposal open, also returns its current revision: Claude's
  decisions, the user ledger, every feedback item with its id and
  status, and `user_seq` (the last ledger entry included).

### `compact_notes` (Claude; plans only)

Proposes, revises or extends. Takes Claude's decisions, `proposal_id`
and `revision` (when one is open), and `replies` (feedback id -> text).

- `dry_run=False` is removed: Claude can't apply anything.
- With no proposal open, starts one (revision 1). With one open, a call
  without its `proposal_id` is refused.
- Refused while the proposal is applying.
- Every feedback item open at Claude's starting revision needs a reply.
  Items withdrawn since are ignored; items added since stay open.
- Claude's decisions can't override a user entry, except on an event
  named by a feedback item this call answers, and then only entries up
  to its starting revision's `user_seq`. A newer user entry stands, and
  the reply is marked superseded by it.
- Built on the current revision, whatever revision it started from. If
  that overlaps the user's newer entries, it's refused: prepare again.

### `get_proposal` (app, Claude; read-only)

The current revision: window, status, timeline, calendar writes,
ledger, feedback with replies, and the events changed since any earlier
revision the caller names (the union of each revision's `changed`).

### `amend_proposal` (app)

Takes `proposal_id`, `revision`, and `updates`/`creates`/`cancels`
(as `compact_notes` takes them) and `as_planned` (event ids).

- Appends one ledger entry per edit and writes a new revision with
  `by: user`, built on the current revision.
- From an outdated revision: still applied on the current one; the
  response names each of Claude's newer changes it replaced.
- Refused as a whole if an edit names an event outside the window or
  gone, if the result overlaps, or if it changes history. The response
  returns the current revision and the refused edits.
- Edits set actions and facts (location, people, notes on people) as
  `compact_notes` does, naming existing ones by id. A new action,
  person or location is created first (`create_action`,
  `create_person`, `create_location`), then named; the `new:` refs of
  `new_actions` and the like are only for Claude's additions, created on
  apply. An edit naming one that's been deleted is refused like any
  invalid edit.
- Refused while the proposal is applying.

### `add_proposal_note` / `withdraw_proposal_note` (app)

- `add_proposal_note(proposal_id, text, event_id?, at?)` returns the
  feedback id, `<proposal>-f<n>`.
- `withdraw_proposal_note(feedback_id)`: open -> withdrawn; already
  withdrawn -> no change, success; answered -> refused, naming the
  revision that answered it; unknown, or the proposal finished ->
  refused.

### `confirm_proposal` (app only)

Takes `proposal_id` and `revision`.

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

### Finishing an apply (app, Claude)

- A confirmed revision that stopped partway is resumed: writes already
  done are skipped. It was approved when confirmed, so finishing needs
  no new approval, and Claude may do it.
- A write that can never succeed (Calendar: not found, gone) marks the
  revision `failed`, and the server writes a new revision (`apply
  failed`) with server feedback listing what couldn't carry over, for
  the user to confirm again. Days already applied stay applied; the
  new revision's `from` is where they end.
- The new revision replays the decisions on the calendar as it is now:

  | Decision | Write done | Write not done |
  |---|---|---|
  | Update | Already matches: no write | Planned again |
  | Create | Becomes a keep of the event it created | Stays a create |
  | Cancel | Kept, marked done; follow-through still recorded on apply | Stays a cancel |
  | Names an event that's gone | Dropped; listed in the feedback | Dropped; listed in the feedback |

  A user entry naming an event that's gone stays in the ledger, marked
  inapplicable.

### Unchanged

`abandon_compaction` (abandons the open proposal), `record_judgments`
(after apply), and `update_event` (history ends at the last applied
`through`) work as today.

## Journal rows

Same tab and columns:

```
compaction_id | step | kind | event_id | before | after | status | detail
```

| `kind` | `compaction_id` | `step` | Other columns |
|---|---|---|---|
| `compaction` | revision-day id: `<proposal>-r<n>`, then `-r<n>d2`… for later days | 0 | `status` (below); `detail`: today's (`now` = `through`, `note_ids`, `warnings`, `ignore_notes`, `batch`/`day`, `additions`) plus `proposal`, `revision`, `from`, `base` (starting revision), `user_seq`, `by` (`claude`/`user`/`server`), `reason` (`proposed`, `extended`, `revised for notes`, `user edit`, `recheck`, `apply failed`), `changed` (event ids whose outcome changed from the previous revision) |
| `decision` | revision-day id | 0 | Claude's decision, as JSON in `before`; `event_id` repeats its event |
| `update`/`create`/`cancel` | revision-day id | 1… | One calendar write: the event's state `before` and `after`, `pending`/`done`, reason in `detail` |
| `user_decision` | proposal id | n (id `<proposal>-u<n>`) | The edit as JSON in `before` (a decision, or `as_planned`); `status` `active` or `inapplicable`; `detail`: when, and the revision it started from |
| `feedback` | proposal id | n (id `<proposal>-f<n>`) | `before`: text, `at`, author (`user`/`server`), when; `after`: reply and answering revision; `status` `open`, `answered` or `withdrawn` |

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

Runs whenever a revision is written, and deletes rows anywhere in the
tab (several ranges in one request, bottom first), not only from the
top. Every id lives in a row's own cells, so deleting rows never changes
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
