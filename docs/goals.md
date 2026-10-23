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
| `deadline` | As set: `{at}` -- a date ("2026-11-01", due at the end of that day) or a date and time ("2026-11-01T17:00"), in the calendar's time zone -- or measured from something, and kept so while the goal is open: `{before_parent: {days \| minutes}}`, `{after_start: {days \| minutes}}` (from when it becomes active: days, the end of that many days after the day it started) or `{after_goal: {id, days \| minutes}}`. Days keep a date a date; minutes give a date and time. Setting a date unties it; so does moving a goal measured from its parent's to the top level. |
| `due` | Read-only: when the deadline falls, worked out; none until a goal measured from its start starts. Kept in the tab's `deadline` column, with what it's measured from in `deadline_rule`. |
| `activate_when` | Optional, for an inactive goal: what it waits for before it's activated -- `after` (every one of `{goal_closed: id}`, done, missed or obsolete, and `{at: date or date and time}`), then `delay` (`"next_day"`, the start of the day after, or `{minutes}`), and `hold_while` (`[{active_at_least: n, priority?: {at_or_above \| exactly: a priority or "own"}}]`: held back while that many other goals of that priority are active, its own parents and sub-goals not counted). Cleared once it's activated, by the server or by hand. |
| `expected_start` | Read-only, for a goal waiting: when it's expected to be activated -- what it waits for at its due (a goal measured from a start that hasn't come, from when that's expected), then its delay. None while it's held, or something it waits for has no due yet. |
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

- An open goal has a deadline: an active one, a due; an inactive one may
  be measured from its start. (Goals made before this rule may have none
  until they're next changed.)
- A deadline isn't measured, through others, from its own, and a goal
  doesn't wait, through others, for itself, or for one of its parents to
  close (it can't close while the goal is open).
- A goal starts (first becomes active) before its deadline. A sub-goal may
  start before its parent: groundwork often comes first.
- An open sub-goal (inactive or active) is due no later than its parent,
  and expected to take no longer (each sub-goal on its own, not their
  total). A parent with no expected time puts no limit on its sub-goals.
- An inactive goal may have active sub-goals, but activating a goal --
  by hand or as it waited to be -- activates its inactive parents too,
  each dated then, and clears what each waited for.
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

## Waiting to be activated

The server activates an inactive goal whose `activate_when` is ready --
every trigger come, then the delay -- and not held, whenever goals are
read or changed: dated when it became ready (held, the latest a goal
stopped being active since, if later), never before its last change.
Of those ready, the highest priority goes first, then the soonest due,
each counted in the holds of the next. A read shows the goals so; the
next change of goals writes them. One that would start past its own
deadline, or with a parent that has none, keeps waiting. A missed goal
tried again stays open, so what waits for it waits on. Deleting a goal
takes it out of what waits for it, and a deadline measured from it keeps
the date it had.

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

The assistant tags an event with a goal only when that goal was open
(inactive or active) when the event started -- open now, or closed
after the event started, since the user often makes a goal and closes
it the same day -- and the event clearly matches it: its attribution
hints, or with none its title. It never guesses, and leaves an event
untagged when it's unclear (the user can still ask it to tag one
by hand). It tags the most specific goal that fits -- a sub-goal rather
than its parent -- and gives minutes or a percent when only part of the
event was for the goal. The tool docs say so where events' goals are
set (`update_event`, `create_event`, `get_goals`).

Compaction is where it does this, as it settles each past event's
facts: `prepare_compaction` lists the open goals, and those closed since
the earliest event it offers started (`goals`: each one's id, title,
path, status, deadline, `closed_at` if it's closed, and its hints and
people if it has them) beside the actions, people and locations, and
`compact_notes` takes goal shares (`goals`) on its updates and creates.
A proposal keeps them through its revisions, shows them on its events
and changes, and writes them when it's applied; a share that names no
goal, doesn't fit its event, or newly tags an event with a goal that
closed before the event started, is refused. Tagging an event
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
named is who it's for or with, and the rest is its title, phrased as
what the user will do. A title is always that: the active voice, from
the user's side ("Fix the fence", "Send Mom the photos", "Encourage
Gary to make a project with AI"), never the outcome as a state ("Fence
fixed", "Photos sent to Mom"). Its description says where it came
from -- the note's date and words -- and why, when the notes say why,
never a reason they don't; its definition of done says how anyone
would know it happened ("Mom has the photos"). The goal is inactive.
Every goal has a deadline -- the notes' or, failing that, about a week
after the note, which the recap's `unsure` says was assumed -- so it
shows on the goals-due strip.
It's created when the proposal is applied (an open goal with the same
title is reused), or sooner if the user settles it -- made active then,
with a deadline, if they like; see
[compaction proposals](compaction-proposals.md).

Compaction also proposes changes to goals there are, with
`compact_notes`' `goal_changes` -- each a goal's id, a status (`done`,
`missed` or `obsolete`), a time in the window (`at`; the proposal's
`through` if left out) and the notes it came from -- so that, with goals
as the user's task list, a note like "sent Kopal the recipe" marks that
goal done without the user doing it by hand. When a note says a goal's
outcome was reached, it proposes `done` at the note's time; when a note
says the user dropped it, `obsolete`. Then a triage pass: `prepare_compaction`'s
`goals` flag each goal whose deadline fell inside the window
(`due_in_window`), and every one of them that no note or event shows
done is proposed `missed` at its deadline, with a point in the recap's
`unsure` that it may have been done without a note. A goal the notes
and the deadline say nothing about is left alone; a goal with open
sub-goals isn't closed unless the notes settle them too (the server
refuses otherwise, naming them, as `update_goals` would); and a change
for a goal the user already settled is left out. Claude never proposes a
retry itself. The user settles each change in review -- applying it
now, dropping it, or changing it, including turning a `missed` into a
retry with a `retry_deadline` (the goal's history gains `missed` with
the old deadline then its prior status with the new one, both at the
change's time, and it stays open) -- and the rest are made when the
proposal is applied, each as one update with the change's time as the
history time and the deadline then in force as its stamp. One the goal
rules refuse by then is dropped and reported in the proposal's
warnings, for the user to redo.
