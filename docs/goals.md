# Goals

Goals are outcomes the user means to reach and can mark done. "Spend 40
hours a week at work" has no "done", so it's a habit; "ship the Goals
pane" is a goal. Goals aren't tied to actions, habits or places, though
a goal can name the people it's done with or for: events are tagged with
them explicitly, apart from everything else about the event, and most
events have none.

## A goal

| Field | |
| --- | --- |
| `title` | |
| `parent_id` | The goal it's a piece of, to any depth; none for a top-level goal. |
| `required` | Whether its parent needs it done to be done. |
| `expected_minutes` | Optional. How long it's expected to take, compared with the time spent purely as a guide for estimating; never enforced. |
| `deadline` | A date ("2026-11-01", due at the end of that day) or a date and time ("2026-11-01T17:00"), in the calendar's time zone. Needed while active; optional, and empty by default, while inactive. |
| `status` | `inactive`, `active`, `done`, `missed` (its deadline passed without it) or `obsolete` (no longer wanted). New goals are active unless made inactive. A missed goal can be reopened with a new deadline; its history keeps each deadline it missed, so a goal can be missed and retried any number of times. |
| `history` | Every change of status and when, oldest first, starting with the status it was made in, each with the `deadline` in force at that change (if it had one; older entries may have none). A change the server records itself -- a status set without a history, a create without one -- carries the goal's deadline after the change; a history given whole keeps its entries' deadlines as given. Entries can be backdated or removed; the history must stay in order and end with the status. A goal started when it first became active, and was finished when it last became done, missed or obsolete. |
| `description`, `definition_of_done` | HTML. |
| `attribution_hints` | A guide, for a model, to telling which events are for the goal. |
| `with_ids` | The people the user does it with (person ids). |
| `for_ids` | The people it's done for, who aren't part of doing it: a promise to them, a gift, a favor. No one is in both, and neither names "self". |
| `id`, `version` | As every kind the app writes: made once by the client's id, updates and deletes by version. |

## The rules

The server keeps these for the whole set of goals after each change, and
refuses a change that breaks one, naming the goals in the way. It never
moves or changes another goal to make things fit.

- An active goal has a deadline.
- A goal starts (first becomes active) before its deadline. A sub-goal may
  start before its parent: groundwork often comes first.
- An open sub-goal (inactive or active) is due no later than its parent,
  and expected to take no longer (each sub-goal on its own, not their
  total). A parent with no expected time puts no limit on its sub-goals.
- An inactive goal has no active sub-goals: deactivating a goal means
  deactivating (or moving) its active sub-goals in the same change, and
  activating a sub-goal means activating its parents.
- A done, missed or obsolete goal has no open sub-goals: closing a goal
  means marking each of its open sub-goals done, missed or obsolete, or
  moving it under another goal (or the top level), in the same change.
  The app doesn't offer to mark a goal done while a required sub-goal is
  open; a missed or obsolete required sub-goal doesn't hold it up.
- A done, missed or obsolete goal can be reopened, unless its parent is closed.
  Reopening a missed goal is a retry: one update sets its new deadline and
  gives its history ending `missed` (with the deadline it missed) then
  `active` or `inactive` (whichever it was before, with the new deadline),
  both at the same moment. That's how the app records a retry, and the
  history keeps every deadline missed along the way.
- A goal can be deleted even when events are tagged with it (the app
  warns that it can't be undone). Its sub-goals are deleted or moved in
  the same change.

`update_goals` takes creates, updates and deletes together, so any of
these -- closing a goal while moving its sub-goals under a new top-level
goal made for them, say -- is one change.

## Events' goal shares

An event's `goals` are its goal shares: each a goal and how much of the
event it takes:

- `minutes`: a fixed share. Fixed shares are carved out of the event first.
- `percent`: a share of what the fixed shares leave.
- neither: the whole event.

The fixed minutes must fit in the event, and the percents add up to no
more than 100; a write that sets an event's goals is refused otherwise.
An event shortened since its goals were set isn't refused: read back, its
minutes are cut down to fit, in order. Tags on goals deleted since are
left out when an event is read, and dropped when its goals are next set.

They're kept in the event's private properties as `goal_shares`, as
text: `a b=45m c=30%`. (The goals that actions replaced left `goal_ids`
on old events, and a Goals tab; these are kept apart from both, in
`goal_shares` and the Outcome Goals tab.)

A series' goals are set on the series, and its events have them.

### Time toward a goal

The user's app works out the time spent toward each goal. From one
event, a goal gets its own share; a parent gets its own share and every
share of its sub-goals (at any depth), never more than the event's
length, and the whole event if it or any of them takes the whole event.
So a parent tagged for 20 minutes of an hour, with its sub-goal tagged
for 30, gets 50 minutes; with the sub-goal taking the whole event, the
parent gets the hour.

## The assistant

The goal tools are `get_goals`, `get_goal`, `create_goal` and
`update_goals`; events' goals are set with `update_event`, `create_event`
and `update_recurrence`.

The assistant tags an event with a goal only when that goal is open
(inactive or active), has attribution hints, and the event clearly
matches them; it never guesses for a goal without hints, and leaves an
event untagged when it's unclear (the user can still ask it to tag one
by hand). It tags the most specific goal that fits -- a sub-goal rather
than its parent -- and gives minutes or a percent when only part of the
event was for the goal. The tool docs say so where events' goals are
set (`update_event`, `create_event`, `get_goals`).

Compaction is where it does this, as it settles each past event's
facts: `prepare_compaction` lists the open goals (`goals`: each one's
id, title, path, status, deadline, and its hints and people if it has
them) beside the actions, people and locations, and `compact_notes`
takes goal shares (`goals`) on its updates and creates -- only for goals
with hints. A proposal keeps them through its revisions, shows them on
its events and changes, and writes them when it's applied; a share that
names no goal, or doesn't fit its event, is refused. Tagging an event
with a goal, Claude adds the goal's `with_ids` to the event's people
unless the notes say otherwise.

Compaction also proposes new goals: when a note says the user wants or
means to do something, or told someone they would, `compact_notes`'
`new_goals` adds one (with a `new:` ref, like a new action or person),
naming the people it's with or for -- new people by their refs. Never
one an open goal already covers. For the user alone, only something
longer term: most "I want to" notes are done right away. For someone
else, however small: a promise, offer, favor or plan for a person
("send Kopal a bread-bowl recipe") is a goal for them, or with them if
they'll do it together, unless the notes show it done; so is an idea of
the user's for helping someone with a goal of theirs or something
they're struggling with, but not a friend's own goal with no idea of
the user's attached. A note prefixed `todo:`, `for <person>:`,
`with <person>:` or `idea for <person>:` is a definite goal: the person
named is who it's for or with, and the rest is its title, phrased as an
outcome. It's inactive. A goal for the user alone has a deadline only
when the notes give one; a goal with or for someone always has one --
the notes' or, failing that, about a week after the note, which the
recap's `unsure` says was assumed -- so it shows on the goals-due strip.
It's created when the proposal is applied (an open goal with the same
title is reused), or sooner if the user settles it -- made active then,
with a deadline, if they like; see
[compaction proposals](compaction-proposals.md).
