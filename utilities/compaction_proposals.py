"""Compaction proposals: what the user confirms (see docs/compaction-
proposals.md for the design and the contract the tools enforce).

A proposal says "this is what happened from `window_start` to `through`".
Claude writes it (`compact_notes`), the user reviews it in the app --
editing it (`amend_proposal`) or leaving feedback for Claude
(`add_proposal_note`) -- and only the user's confirmation applies it.
Every change makes a new *revision*; only the newest can be confirmed.

This module is the pure part: the user's edits and feedback as they're
kept (each in a row of the compaction journal -- see utilities/
compaction_journal.py), the shapes the tools hand back, and how a
revision's decisions are made from Claude's and the user's (`merge`).
utilities/note_compactor.py does the rest.

**How a revision's decisions are made.** Claude's decisions, then every
active user edit on top, in the order they were made. An edit to an
event overrides Claude's decision on it field by field -- one that only
moves an end keeps the facts Claude set -- and `as_planned` clears
whatever was decided about the event. Since Claude never sends the
user's edits and every revision lays all of them on top, no revision
can leave one out. Events a proposal creates have no id until it's
applied, so they're named by a *key*: `<proposal>c<n>` for Claude's,
the edit's own id (`<proposal>u<n>`) for the user's.

Notes work the same way. Claude says which to ignore and which to add to
a particular event (rather than the one they fall within); the user's
note edits (`NoteEdit`) override that, note by note, and `as_planned`
on a note puts it back as Claude had it.

So do the actions, people and locations Claude's decisions add (see
utilities/compaction_additions.py), which are only created when the
proposal is applied. The user can settle one sooner (`AdditionChoice`):
create it now, say it's one already there, or drop it -- and every
revision from then on names it by its id, or leaves it out, wherever
the decisions named its ref. `as_planned` on a ref puts it back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from utilities.compaction_timeline import Timeline
from utilities.compaction_additions import REF_PREFIX
from utilities.facts import Facts
from utilities.note_compaction import CompactionChange, EventDecision, NoteUse

ProposalState = Literal["awaiting_review", "awaiting_claude", "applying", "applied", "abandoned"]
EditStatus = Literal["active", "inapplicable", "replaced"]
FeedbackStatus = Literal["open", "answered", "withdrawn"]

_EDIT_FIELDS = (
    "summary", "start", "end", "start_note", "end_note", "annotate", "action_ids", "facts",
    "description", "location", "priority",
)


def new_proposal_id(hex_id: str) -> str:
    """A proposal's id: 12 hex digits. Only `0-9a-v` may appear in a
    proposal's ids, since created events' Calendar ids are made from its
    revisions' (see `revision_id`)."""
    return hex_id[:12]


def revision_id(proposal: str, revision: int) -> str:
    """The compaction id of a revision's first day (later days add
    `d2`, `d3`... -- see utilities/compaction_journal.py)."""
    return f"{proposal}r{revision}"


def claude_key(proposal: str, number: int) -> str:
    return f"{proposal}c{number}"


def edit_id(proposal: str, seq: int) -> str:
    return f"{proposal}u{seq}"


def feedback_id(proposal: str, seq: int) -> str:
    return f"{proposal}f{seq}"


def is_note_id(name: str | None) -> bool:
    """Whether `name` is a note's id (`<timestamp>#<row>`) rather than an
    event's or a key."""
    return name is not None and "#" in name


def is_ref(name: str | None) -> bool:
    """Whether `name` is an addition's ref ("new:ukulele")."""
    return name is not None and name.startswith(REF_PREFIX)


def is_key(proposal: str, name: str | None) -> bool:
    """Whether `name` is a key of `proposal`'s -- a created event's -- and
    not an event id."""
    return name is not None and re.fullmatch(rf"{re.escape(proposal)}[cu]\d+", name) is not None


def claude_key_number(proposal: str, name: str | None) -> int | None:
    match = re.fullmatch(rf"{re.escape(proposal)}c(\d+)", name or "")
    return int(match.group(1)) if match else None


def split_feedback_id(name: str) -> tuple[str, int] | None:
    """(proposal, seq) of a feedback id, or `None` if it isn't one."""
    match = re.fullmatch(r"([0-9a-f]{12})f(\d+)", name or "")
    return (match.group(1), int(match.group(2))) if match else None


@dataclass(kw_only=True)
class UserEdit:
    """One of the user's edits to a proposal, as `amend_proposal` made it.
    Kept for the whole proposal, in the order made (`seq`)."""

    id: str
    """`<proposal>u<seq>`. For a create, also the new event's key."""

    seq: int
    event_id: str | None
    """The event (or key, or note) it edits; `None` for a create."""

    edit: dict[str, Any]
    """What it says, as a decision's JSON: `action` is `keep`, `create`,
    `cancel` or `as_planned`, with the fields a decision of that kind
    takes -- or `note`, with `use` (`annotate` or `ignore`) and, to add
    the note to a particular event, its `event_id`."""

    status: EditStatus = "active"
    """`inapplicable`: the event it names is gone. `replaced`: Claude
    changed the event in answer to the user's feedback on it."""

    created: datetime
    base_revision: int
    """The revision the user was looking at when they made it."""


@dataclass(kw_only=True)
class Feedback:
    """A note for Claude on a proposal: the user's, or the server's when it
    needs Claude (a revision that no longer plans)."""

    id: str
    """`<proposal>f<seq>`."""

    seq: int
    text: str
    event_id: str | None = None
    """The event (or key) it's about, if it's about one."""

    note_id: str | None = None
    """The note it's about, if it's about one."""

    at: datetime | None = None
    """The time it's about, if it's about one."""

    by: Literal["user", "server"] = "user"
    created: datetime
    status: FeedbackStatus = "open"
    reply: str | None = None
    """Claude's answer, once it's answered."""

    answered_in: int | None = None
    """The revision that answered it."""

    superseded_by: list[str] | None = None
    """User edits made after Claude's answer started, which stand over
    what it did to the event."""


@dataclass(kw_only=True)
class NoteEdit:
    """The user's say on what a note is for: `annotate` -- add it to the
    event it falls within, or to `event_id` (an event's id, or a created
    one's key) -- or `ignore` it."""

    note_id: str
    use: Literal["annotate", "ignore"]
    event_id: str | None = None

    def edit(self) -> dict[str, Any]:
        return {
            "action": "note",
            "use": self.use,
            **({"event_id": self.event_id} if self.event_id is not None else {}),
        }


@dataclass(kw_only=True)
class AdditionChoice:
    """The user's say on an action, person or location Claude's decisions
    add (by its `ref`): `create` it now -- with any of `name`, `context`
    (a person's) and `hint` (a location's) corrected -- say it's one
    that's `existing` (its `id`), or `drop` it from the events."""

    ref: str
    use: Literal["create", "existing", "drop"]
    id: str | None = None
    name: str | None = None
    context: str | None = None
    hint: str | None = None


@dataclass(kw_only=True)
class AdditionSettled:
    """An addition the user settled, as a proposal shows it: its `id`, or
    `None` if they dropped it."""

    ref: str
    use: Literal["create", "existing", "drop"]
    id: str | None = None


@dataclass(kw_only=True)
class FeedbackReply:
    """Claude's answer to one feedback item: what it changed, or a question
    back if it couldn't tell what was meant."""

    feedback_id: str
    reply: str


@dataclass(kw_only=True)
class ProposalEvent:
    """One event as a revision leaves it."""

    id: str
    """Its event id, or -- for one the proposal creates -- its key."""

    summary: str | None
    start: datetime | None
    end: datetime | None
    """Where it ends up; for a cancelled or merged one, where it was."""

    status: Literal["on_schedule", "adjusted", "new", "cancelled", "merged", "planned"]
    """`planned`: in the future, untouched."""

    planned_start: datetime | None = None
    planned_end: datetime | None = None
    """Where it is on the calendar now; `None` for a new one."""

    description: str | None = None
    """What it'll be written with: its own description and the notes
    added to it."""

    location: str | None = None
    priority: int | None = None
    """Its own priority, if it has one."""

    action_ids: list[str] | None = None
    facts: Facts | None = None
    decided_by: Literal["claude", "user"] | None = None
    history_until: datetime | None = None
    """How much of it is history already, if it started before the
    proposal's window: its start, and its lasting until then."""

    is_end_of_day_sleep: bool = False
    counts_against_follow_through: bool | None = None
    """For a cancelled one: whether its cancel counts against the
    follow-through of whoever it was planned with (the user dropped it),
    or not (the plan changed). To flip it, cancel it again with the
    other value."""

    follow_through: list[str] = field(default_factory=list)
    """For a cancel that counts: each person it counts against in a
    follow-through part, with the part's trait -- "‹person› (Reliable)".
    Empty if no one's follow-through tracks it."""


@dataclass(kw_only=True)
class ProposalNote:
    id: str
    timestamp: datetime
    description: str | None = None
    use: NoteUse = "unused"
    """What the revision does with it: sets an `edge` of an event,
    `annotates` one (its text added to the description), is `ignored`,
    or is `unused` (no text, or no event to add it to)."""

    event_id: str | None = None
    """The event (id, or key) it's added to -- or, for an `edge`, whose
    edge it sets."""

    edge_of: str | None = None
    """The event (id, or key) whose start or end it sets, if it sets one
    -- whether or not it's also added to an event."""

    decided_by: Literal["claude", "user"] | None = None
    """Who said what it's for -- `None` for a note just added where it
    falls."""


@dataclass(kw_only=True)
class Proposal:
    """A proposal's current revision -- see the module docstring."""

    id: str
    revision: int
    state: ProposalState
    window_start: datetime
    through: datetime
    by: Literal["claude", "user", "server"]
    reason: str
    created: datetime
    claude_through: datetime | None = None
    """Where Claude's own revision of it runs to: from there to `through`,
    the user extended it, its notes added where they fall unless the user
    said otherwise."""

    events: list[ProposalEvent] | None = None
    """Every event of its days, as it leaves them. `None` when it no
    longer plans against the calendar (see `problem`)."""

    problem: str | None = None
    notes: list[ProposalNote] = field(default_factory=list)
    changes: list[CompactionChange] = field(default_factory=list)
    """The calendar writes it makes."""

    warnings: list[str] = field(default_factory=list)
    timeline: Timeline | None = None
    additions: dict[str, list[dict]] | None = None
    """Actions, people and locations Claude's decisions add, created when
    it's applied -- those the user hasn't settled yet."""

    settled_additions: list[AdditionSettled] = field(default_factory=list)
    """Those the user settled: created, found, or dropped."""

    user_edits: list[UserEdit] = field(default_factory=list)
    feedback: list[Feedback] = field(default_factory=list)
    changed_since: list[str] | None = None
    """With `since_revision`: the events (ids and keys) whose outcome
    changed in a later revision."""

    replaced: list[str] | None = None
    """For `amend_proposal`: the events whose newer change by Claude the
    edits overrode."""


@dataclass(kw_only=True)
class ProposalSummary:
    """The open proposal, for `get_compaction_status`."""

    id: str
    revision: int
    state: ProposalState
    window_start: datetime
    through: datetime
    open_feedback: int


@dataclass(kw_only=True)
class ProposalResult:
    """What confirming or finishing a proposal came to."""

    status: Literal["applied", "rechecked", "needs_claude", "rebuilt", "abandoned"]
    message: str
    proposal: Proposal | None = None


@dataclass(kw_only=True)
class ProposalContext:
    """The open proposal, as `prepare_compaction` hands it to Claude to
    revise or extend: its own decisions, to send again, and what the
    user said."""

    id: str
    revision: int
    state: ProposalState
    user_seq: int
    """The last user edit its current revision includes."""

    updates: list[dict] = field(default_factory=list)
    creates: list[dict] = field(default_factory=list)
    """Each with its `key`: send it back to keep it."""

    cancels: list[dict] = field(default_factory=list)
    ignore_notes: list[str] = field(default_factory=list)
    annotate_notes: list[dict] = field(default_factory=list)
    """Your notes added to a particular event: {note_id, event_id}."""

    new_actions: list[dict] = field(default_factory=list)
    new_people: list[dict] = field(default_factory=list)
    new_locations: list[dict] = field(default_factory=list)
    """What your decisions add, by ref: send them again with them."""

    settled_additions: list[AdditionSettled] = field(default_factory=list)
    """Those of them the user settled -- created, found, or dropped: from
    now on, name each by its `id`, or leave it out."""

    user_edits: list[UserEdit] = field(default_factory=list)
    feedback: list[Feedback] = field(default_factory=list)


@dataclass
class Merged:
    decisions: list[EventDecision]
    decided_by: dict[str, Literal["claude", "user"]]
    unknown: list[UserEdit]
    """Edits naming an event (or key, or note) that isn't there."""

    ignore_notes: list[str] = field(default_factory=list)
    note_targets: dict[str, str | None] = field(default_factory=dict)
    """Note id -> the event (id, or key) it's added to; `None` for where
    it falls, or the event whose edge it sets."""

    notes_decided_by: dict[str, Literal["claude", "user"]] = field(default_factory=dict)
    settled: dict[str, AdditionSettled] = field(default_factory=dict)
    """Additions the user settled, by ref."""


def edit_decision(edit: UserEdit) -> EventDecision:
    """`edit` as a decision (its `as_planned` as a bare keep)."""
    data = dict(edit.edit)
    action = data.pop("action")
    decision = EventDecision.from_json_dict({**data, "action": "keep" if action == "as_planned" else action})
    if action == "create":
        decision = replace(decision, key=edit.id)
    return decision


def merge(
    proposal: str,
    claude: list[EventDecision],
    edits: list[UserEdit],
    *,
    known_ids: set[str] | None = None,
    aliases: dict[str, str] | None = None,
    settled: set[str] | frozenset[str] = frozenset(),
    claude_ignore: list[str] | tuple = (),
    claude_targets: dict[str, str] | None = None,
    known_notes: set[str] | None = None,
) -> Merged:
    """The decisions a revision is planned with -- `claude`'s, with every
    active edit in `edits` on top, in order -- and what it does with the
    notes: `claude_ignore` and `claude_targets` (note -> event), with the
    user's note edits on top (see the module docstring). `known_ids`, if
    given, are the event ids an edit may name (a key is known if a
    decision creates it), and `known_notes` the notes. `aliases` map the
    keys of events already created to their ids; edits of `settled`
    events (cancelled already) are skipped."""
    aliases = aliases or {}
    claude_targets = {n: aliases.get(e, e) for n, e in (claude_targets or {}).items()}
    ignore = set(claude_ignore)
    targets = dict(claude_targets)
    notes_by: dict[str, Literal["claude", "user"]] = {n: "claude" for n in ignore | set(targets)}
    keyed: dict[str, EventDecision] = {}
    decided_by: dict[str, Literal["claude", "user"]] = {}
    for decision in claude:
        name = (decision.key if decision.action == "create" else decision.event_id) or f"#{len(keyed)}"
        if name in keyed:
            # A second decision on one event: kept, for the planner to refuse.
            name = f"{name}#{len(keyed)}"
        keyed[name] = decision
        decided_by[name] = "claude"
    unknown: list[UserEdit] = []
    note_edits: list[UserEdit] = []
    settled_refs: dict[str, AdditionSettled] = {}
    described: dict[str, int] = {}
    """Event -> the user's last edit giving its whole description."""
    for edit in sorted((e for e in edits if e.status == "active"), key=lambda e: e.seq):
        action = edit.edit.get("action")
        if action == "note" or (action == "as_planned" and is_note_id(edit.event_id)):
            note_edits.append(edit)
            continue
        if action == "addition":
            settled_refs[edit.event_id] = AdditionSettled(
                ref=edit.event_id, use=edit.edit["use"], id=edit.edit.get("id")
            )
            continue
        if action == "as_planned" and is_ref(edit.event_id):
            settled_refs.pop(edit.event_id, None)
            continue
        if action == "create":
            keyed[edit.id] = edit_decision(edit)
            decided_by[edit.id] = "user"
            continue
        target = aliases.get(edit.event_id, edit.event_id)
        if target in settled:
            continue
        existing = keyed.get(target)
        creating = existing is not None and existing.action == "create"
        if not creating and (
            is_key(proposal, target) or (known_ids is not None and target not in known_ids)
        ):
            unknown.append(edit)
            continue
        if action == "as_planned":
            keyed.pop(target, None)
            decided_by.pop(target, None)
            continue
        if action == "cancel":
            if creating:
                keyed.pop(target)
                decided_by.pop(target, None)
            else:
                keyed[target] = EventDecision(
                    action="cancel",
                    event_id=target,
                    counts_against_follow_through=edit.edit.get("counts_against_follow_through"),
                    allow_history=edit.edit.get("allow_history"),
                )
                decided_by[target] = "user"
            continue
        given = edit_decision(edit)
        if given.description is not None:
            described[target] = edit.seq
        if existing is not None and existing.action in ("keep", "create"):
            keyed[target] = _overlay(existing, given)
        else:
            keyed[target] = replace(given, event_id=target)
        decided_by[target] = "user"

    for edit in note_edits:
        note = edit.event_id
        if known_notes is not None and note not in known_notes:
            unknown.append(edit)
            continue
        if edit.edit["action"] == "as_planned":
            ignore.discard(note)
            targets.pop(note, None)
            notes_by.pop(note, None)
            if note in claude_ignore:
                ignore.add(note)
            if note in claude_targets:
                targets[note] = claude_targets[note]
            if note in ignore or note in targets:
                notes_by[note] = "claude"
            continue
        if edit.edit["use"] == "ignore":
            ignore.add(note)
            targets.pop(note, None)
        else:
            ignore.discard(note)
            # Annotated after a description left it out: it's back.
            for name, decision in list(keyed.items()):
                if note in (decision.dropped_notes or ()) and described.get(name, edit.seq) < edit.seq:
                    keyed[name] = replace(decision, dropped_notes=[n for n in decision.dropped_notes if n != note])
            target = edit.edit.get("event_id")
            if target is None:
                # Where it falls -- or, for a note that sets an edge, with
                # that event, which it otherwise isn't.
                targets[note] = None
            else:
                target = aliases.get(target, target)
                creating = target in keyed and keyed[target].action == "create"
                if not creating and (
                    is_key(proposal, target) or (known_ids is not None and target not in known_ids)
                ):
                    unknown.append(edit)
                    continue
                targets[note] = target
        notes_by[note] = "user"
    # A note left with an event a later edit took away (a create the
    # user cancelled) goes back to where it falls.
    creates = {k for k, d in keyed.items() if d.action == "create"}
    for note, target in list(targets.items()):
        if is_key(proposal, target) and target not in creates:
            del targets[note]
    return Merged(
        decisions=list(keyed.values()),
        decided_by=decided_by,
        unknown=unknown,
        ignore_notes=sorted(ignore),
        note_targets=targets,
        notes_decided_by=notes_by,
        settled=settled_refs,
    )


def _overlay(under: EventDecision, over: EventDecision) -> EventDecision:
    """`under` with every field `over` sets set to its value instead -- an
    edge set by time letting go of the note `under` set it by, and the
    other way round."""
    changes: dict[str, Any] = {}
    for name in _EDIT_FIELDS:
        value = getattr(over, name)
        if value is not None:
            changes[name] = value
    for time, note in (("start", "start_note"), ("end", "end_note")):
        if getattr(over, time) is not None and getattr(over, note) is None:
            changes[note] = None
        if getattr(over, note) is not None and getattr(over, time) is None:
            changes[time] = None
    if over.allow_history:
        # The user approved it changing history: so, then, may the rest.
        changes["allow_history"] = True
    if over.description is not None:
        # The user's own description: no annotate text, and what it
        # leaves out is its own.
        changes["annotate"] = None
        changes["dropped_notes"] = over.dropped_notes
    return replace(under, **changes)


def edit_json(decision: EventDecision) -> dict[str, Any]:
    """A user's decision as an edit's JSON (`UserEdit.edit`)."""
    return decision.to_json_dict()


def decision_shape(decision: EventDecision) -> tuple[str, dict]:
    """Which of compact_notes' lists `decision` goes in, and as what --
    for handing Claude's decisions back to it."""
    data = decision.to_json_dict()
    action = data.pop("action")
    # The user's alone: never Claude's to send back.
    data.pop("allow_history", None)
    if action == "create":
        return "creates", data
    if action == "cancel":
        return "cancels", {
            "event_id": decision.event_id,
            "counts_against_follow_through": decision.counts_against_follow_through is not False,
        }
    data.pop("into", None)
    return "updates", data


def settle_refs(decision: EventDecision, settled: dict[str, AdditionSettled]) -> EventDecision:
    """`decision` with every ref the user settled replaced by its id -- or,
    dropped, left out -- in its actions and facts."""
    ids = {ref: choice.id for ref, choice in settled.items()}
    if not ids:
        return decision

    def named(values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        # One that's someone already named there is named once.
        return list(dict.fromkeys(ids.get(v, v) for v in values if ids.get(v, v) is not None))

    def noted(notes: dict[str, str]) -> dict[str, str]:
        # A note on someone who already has one keeps theirs.
        kept: dict[str, str] = {}
        for person, note in notes.items():
            target = ids.get(person, person)
            if target is not None and target not in kept:
                kept[target] = notes.get(target, note)
        return kept

    changes: dict[str, Any] = {}
    if decision.action_ids is not None:
        changes["action_ids"] = named(decision.action_ids)
    facts = decision.facts
    if facts is not None:
        changes["facts"] = Facts(
            location_id=ids.get(facts.location_id, facts.location_id) if facts.location_id else facts.location_id,
            with_ids=named(facts.with_ids),
            for_ids=named(facts.for_ids),
            notes=noted(facts.notes) if facts.notes is not None else None,
        )
    return replace(decision, **changes)
