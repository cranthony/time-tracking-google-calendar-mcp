"""The write-ahead journal of note compactions, one tab of the calendar
metadata spreadsheet (see utilities/calendar_metadata_sheet.py).

Compacting notes is a series of calendar writes that can die halfway, and
the model's interpretation of the notes (its decisions) exists only in a
conversation. So before anything is applied, the whole plan -- the
decisions, and every step with its before/after state -- is written
here, and each step is checked off as it's applied. That makes it possible
to finish exactly the plan that was confirmed after a failure, and leaves
a before-state record of every change.

Layout: one row per fact, all in the same eight columns --

    compaction_id | step | kind | event_id | before | after | status | detail

- `kind == "compaction"` (step 0): the compaction itself. `status` is
  where it is in its life (see below); `detail` is JSON with `now`, the
  ids of the notes it consumes, the ids of notes it was told to ignore
  (and, in `note_targets`, the events notes go with instead of the ones
  they fall within), and any planner warnings -- and, for the second and later days of a
  batch, the `batch` (the first day's id) and which `day` of it it is.
  The batch's first day also keeps any `additions`: the actions, people
  and locations the plan adds, each with the `ref` its decisions name it
  by ({"actions": [...], "people": [...], "locations": [...]}), created
  before that day's steps. A proposal's revision adds its own fields
  (`RevisionMeta`): every day says which `proposal` and `revision`, and
  the first day the rest.
- `kind == "decision"`: one per `EventDecision` the day was planned with
  -- for a revision, Claude's and the user's merged (see utilities/
  compaction_proposals.py) -- as JSON in `before` (`event_id` repeats its
  event id, if it has one, for reading the tab by hand). Rows of the
  retired `disposition` kind, from before decisions replaced them, are
  skipped.
- `kind == "claude_decision"`: a revision's own decisions from Claude,
  on its first day: what the next revision starts from.
- `kind` in `update`/`create`/`cancel`: one calendar change (`step` is
  1-based, in the order they're applied). `status` is `pending` or `done`;
  `detail` is the human-readable reason. A create's `event_id` is its
  key, if it has one.
- `kind == "user_decision"` and `kind == "feedback"`: a proposal's user
  edits and feedback, under the proposal's own id (not a revision's),
  `step` numbering them: see `UserEdit` and `Feedback` in utilities/
  compaction_proposals.py. The edit or the note is JSON in `before`;
  feedback's answer in `after`; `status` is theirs.

One row per step (rather than one JSON cell for the whole plan) keeps every
cell far below Sheets' 50,000-character limit even for a busy day.

A compaction covers one span: every day since the last, planned as one
(see utilities/note_compactor.py). Before, several days compacted
together were a *batch*: one compaction per day, written in one block,
the first day's id doubling as the batch's (so a one-day batch is just a
compaction), and the rest `<batch>d<day>` -- still read, so a batch
journaled then can be finished. A span is a batch of one. Each day's compaction moves through the
statuses below on its own, in order, so a batch applied partway has its
earlier days finished and stamped. A proposal's revision is a batch,
its id `<proposal>r<revision>`.

A compaction's status moves `proposed` -> `applying` -> `applied` ->
`stamped` (its notes marked compacted -- the terminal state), or to
`superseded` (a newer revision replaced it), `failed` (a write that
can't succeed stopped it) or `abandoned`. `planned` is a dry run's from
before proposals. `applying` and `applied` are the "open" states: a new
compaction is refused while one exists, so it has to be finished or
abandoned first.

Everything here reads the whole tab, in one range (`_read_rows`): every
compaction step needs it whole anyway -- to find open compactions, and
the last stamped one's `now` -- and inside `cached_sheet_reads` (see
calendar_clients/google_sheets.py) every later read in the same tool call
until this tab is next written is served from memory. So `start` counts
the rows to find where to append, and `load` (so `commit`/`describe`/
`abandon` too) scans them for its compaction, from that same read: Google
Sheets throttles read requests to 60 a minute, and seeking with row
hints instead cost extra requests to confirm the hints, while saving only
rows -- which `garbage_collect` keeps few.

`garbage_collect` keeps this from growing forever by physically deleting
rows -- anywhere in the tab, bottom first -- that nothing needs any more
(see there). Deleting shifts every row below up, so row numbers --
including a compaction's own, and every note id it recorded -- stop
being permanent once this runs; anything holding an older row number
finds out the same way it already would if the sheet had simply changed
underneath it (`NoteCompactor`'s "changed since preview" check), never
silently. Every id is kept in a row's own cells, so none changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.compaction_proposals import Feedback, UserEdit
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    EventState,
)

PLANNED = "planned"
PROPOSED = "proposed"
SUPERSEDED = "superseded"
APPLYING = "applying"
APPLIED = "applied"
STAMPED = "stamped"
FAILED = "failed"
ABANDONED = "abandoned"
OPEN_STATUSES = (APPLYING, APPLIED)

USER_EDIT = "user_decision"
FEEDBACK = "feedback"
CLAUDE_DECISION = "claude_decision"

_HEADER = ["compaction_id", "step", "kind", "event_id", "before", "after", "status", "detail"]
_HEADER_RANGE = "A1:H1"
_DATA_RANGE = "A2:H"
_FIRST_DATA_ROW = 2
_STATUS_COLUMN = "G"

_MAX_ROWS = 100
"""garbage_collect deletes finished compactions once this tab has more
data rows than this."""

_TRIM_TO_ROWS = 50
"""What garbage_collect trims this tab's data rows down to once it kicks
in -- well under `_MAX_ROWS`, not just back to it, so it doesn't kick in
again on the very next append."""

_WHOLE_SUPERSEDED = 3
"""How many of an open proposal's latest superseded revisions are kept
whole, for debugging; of older ones, only their `compaction` rows."""


@dataclass
class JournalStep:
    step: int
    action: str
    event_id: str | None
    before: EventState | None
    after: EventState | None
    status: str
    reason: str
    row: int
    key: str | None = None
    """A create's key, if it has one."""


@dataclass(kw_only=True)
class RevisionMeta:
    """What a proposal's revision adds to its first day's `detail` (every
    day gets `proposal` and `revision`) -- see utilities/
    compaction_proposals.py."""

    proposal: str
    revision: int
    window_start: datetime
    created: datetime
    base: int | None
    """The revision whoever made it started from."""

    user_seq: int
    """The last user edit it includes."""

    by: str
    reason: str
    changed: list[str] = field(default_factory=list)
    """Events (ids and keys) whose outcome differs from the revision
    before it."""

    outcomes: dict[str, str] = field(default_factory=dict)
    """Each event it changes (id or key) -> a digest of the change, to
    tell what the next revision changed."""

    key_seq: int = 0
    """The highest number in a key Claude's creates were given."""

    claude_decisions: list[EventDecision] = field(default_factory=list)
    claude_ignore_notes: list[str] | None = None
    """The notes Claude ignored -- `None` for a revision from before
    these were kept apart from the user's (its days' `ignore_notes`)."""

    claude_note_targets: dict[str, str] = field(default_factory=dict)
    """The notes Claude added to a particular event: note -> event (id,
    or key)."""

    claude_additions: dict[str, list[dict]] | None = None
    """What Claude's decisions add, by ref, including those the user has
    settled since (the first day's `additions` are those left to create)
    -- `None` for a revision from before they were kept apart."""

    aliases: dict[str, str] = field(default_factory=dict)
    """Keys of events already created (by an apply that then failed) ->
    their ids."""

    settled: list[str] = field(default_factory=list)
    """Events already cancelled by an apply that then failed."""

    judge_also: list[list] = field(default_factory=list)
    """[event id, start, end] of events an apply that then failed already
    gave facts: judged with this revision's."""

    claude_through: datetime | None = None
    """Where Claude's last revision of it ran to: past it, up to the
    revision's own `now`, the user extended it (`amend_proposal`'s
    `through`). `None` for a revision from before it was kept -- one
    that wasn't extended."""

    def to_detail(self) -> dict:
        return {
            "proposal": self.proposal,
            "revision": self.revision,
            "from": self.window_start.isoformat(),
            "created": self.created.isoformat(),
            "base": self.base,
            "user_seq": self.user_seq,
            "by": self.by,
            "reason": self.reason,
            "changed": self.changed,
            "outcomes": self.outcomes,
            "key_seq": self.key_seq,
            **({"claude_ignore_notes": self.claude_ignore_notes} if self.claude_ignore_notes is not None else {}),
            **({"claude_note_targets": self.claude_note_targets} if self.claude_note_targets else {}),
            **({"claude_additions": self.claude_additions} if self.claude_additions is not None else {}),
            **({"aliases": self.aliases} if self.aliases else {}),
            **({"settled": self.settled} if self.settled else {}),
            **({"judge_also": self.judge_also} if self.judge_also else {}),
            **({"claude_through": self.claude_through.isoformat()} if self.claude_through else {}),
        }

    @classmethod
    def from_detail(cls, detail: dict, claude_decisions: list[EventDecision]) -> "RevisionMeta":
        return cls(
            proposal=detail["proposal"],
            revision=detail["revision"],
            window_start=datetime.fromisoformat(detail["from"]),
            created=datetime.fromisoformat(detail["created"]),
            base=detail.get("base"),
            user_seq=detail.get("user_seq", 0),
            by=detail.get("by", "claude"),
            reason=detail.get("reason", ""),
            changed=detail.get("changed", []),
            outcomes=detail.get("outcomes", {}),
            key_seq=detail.get("key_seq", 0),
            claude_decisions=claude_decisions,
            claude_ignore_notes=detail.get("claude_ignore_notes"),
            claude_note_targets=detail.get("claude_note_targets", {}),
            claude_additions=detail.get("claude_additions"),
            aliases=detail.get("aliases", {}),
            settled=detail.get("settled", []),
            judge_also=detail.get("judge_also", []),
            claude_through=(
                datetime.fromisoformat(detail["claude_through"]) if detail.get("claude_through") else None
            ),
        )


@dataclass
class JournalCompaction:
    id: str
    status: str
    now: datetime
    note_ids: list[str]
    warnings: list[str]
    decisions: list[EventDecision]
    ignore_notes: list[str] = field(default_factory=list)
    steps: list[JournalStep] = field(default_factory=list)
    note_targets: dict[str, str] = field(default_factory=dict)
    """Notes added to a particular event (id, or key) rather than the
    one they fall within."""

    row: int = 0
    batch: str | None = None
    """The id of the batch's first day, for the second and later days of a
    batch -- see the module docstring."""

    day: int = 1
    additions: dict[str, list[dict]] = field(default_factory=dict)
    """The actions, people and locations the plan adds -- see the module
    docstring."""

    proposal: str | None = None
    revision: int = 0
    meta: RevisionMeta | None = None
    """For a revision's first day: what the revision adds -- see
    `RevisionMeta`."""

    @property
    def batch_id(self) -> str:
        """The id of the batch this day belongs to: what the MCP tools
        call the compaction."""
        return self.batch or self.id

    def changes(self) -> list[CompactionChange]:
        return [
            CompactionChange(
                action=s.action, reason=s.reason, event_id=s.event_id, key=s.key, before=s.before, after=s.after
            )
            for s in self.steps
        ]


@dataclass(kw_only=True)
class PlannedDay:
    """One day of a batch, for `CompactionJournal.start_batch`."""

    compaction_id: str
    now: datetime
    note_ids: list[str]
    decisions: list[EventDecision]
    plan: CompactionPlan
    ignore_notes: list[str] = field(default_factory=list)
    additions: dict[str, list[dict]] = field(default_factory=dict)
    note_targets: dict[str, str] = field(default_factory=dict)


class CompactionJournal:
    def __init__(self, sheets_client: SheetsClient, spreadsheet_id: str, sheet_id: int) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id

    @property
    def whole_tab(self) -> TabRange:
        """This whole tab, header and all, for `SheetsClient.prefetch`:
        every read of it falls within this."""
        return TabRange(self._spreadsheet_id, self._sheet_id, "A1:H")

    def prefetch(self, ranges: list[TabRange]) -> None:
        """`SheetsClient.prefetch`, through this tab's client: for a
        caller reading several tabs of this spreadsheet in one step (see
        server.py's `_prefetch`)."""
        self._sheets_client.prefetch(ranges)

    @staticmethod
    def ensure(sheets_client: SheetsClient, spreadsheet_id: str) -> "CompactionJournal":
        """The calendar's compactions tab within `spreadsheet_id`,
        creating and tagging it (with a header row) the first time only --
        see calendar_metadata_sheet.ensure_tab."""
        sheet_id, created = calendar_metadata_sheet.ensure_tab(
            sheets_client,
            spreadsheet_id,
            role=calendar_metadata_sheet.COMPACTIONS_SHEET_ROLE,
            title=calendar_metadata_sheet.COMPACTIONS_SHEET_TITLE,
        )
        if created:
            sheets_client.write_rows_in_sheet(spreadsheet_id, sheet_id, _HEADER_RANGE, [_HEADER])
        return CompactionJournal(sheets_client, spreadsheet_id, sheet_id)

    def start(
        self,
        compaction_id: str,
        *,
        now: datetime,
        note_ids: list[str],
        decisions: list[EventDecision],
        plan: CompactionPlan,
        ignore_notes: list[str] | None = None,
    ) -> None:
        """Record a new `planned` one-day compaction -- see `start_batch`."""
        self.start_batch(
            [
                PlannedDay(
                    compaction_id=compaction_id,
                    now=now,
                    note_ids=note_ids,
                    decisions=decisions,
                    plan=plan,
                    ignore_notes=ignore_notes or [],
                )
            ]
        )

    def start_batch(
        self,
        days: list[PlannedDay],
        meta: RevisionMeta | None = None,
        *,
        edits: list[UserEdit] = (),
    ) -> None:
        """Record a new batch, a compaction per day (the first day's id is
        the batch's): for each, the compaction row, one row per decision,
        and one `pending` row per step -- all in one write, so a crash
        can't leave half a plan recorded. With `meta`, it's a proposal's
        revision (`proposed`), and the user `edits` it's the first to
        include are recorded with it; without, a `planned` dry run."""
        batch = days[0].compaction_id
        status = PROPOSED if meta is not None else PLANNED
        rows: list[list[str]] = [_edit_row(edit) for edit in edits]
        for number, day in enumerate(days, start=1):
            detail = {
                "now": day.now.isoformat(),
                "note_ids": day.note_ids,
                "warnings": day.plan.warnings,
                "ignore_notes": day.ignore_notes,
            }
            if number > 1:
                detail.update(batch=batch, day=number)
            if day.additions:
                detail["additions"] = day.additions
            if day.note_targets:
                detail["note_targets"] = day.note_targets
            if meta is not None:
                detail.update(meta.to_detail() if number == 1 else {"proposal": meta.proposal, "revision": meta.revision})
            rows.append([day.compaction_id, "0", "compaction", "", "", "", status, json.dumps(detail)])
            if meta is not None and number == 1:
                rows += [
                    [day.compaction_id, "0", CLAUDE_DECISION, d.event_id or d.key or "", json.dumps(d.to_json_dict()), "", "", ""]
                    for d in meta.claude_decisions
                ]
            for decision in day.decisions:
                rows.append(
                    [
                        day.compaction_id,
                        "0",
                        "decision",
                        decision.event_id or "",
                        json.dumps(decision.to_json_dict()),
                        "",
                        "",
                        "",
                    ]
                )
            for step, change in enumerate(day.plan.changes, start=1):
                rows.append(
                    [
                        day.compaction_id,
                        str(step),
                        change.action,
                        change.event_id or change.key or "",
                        json.dumps(change.before.to_json_dict()) if change.before else "",
                        json.dumps(change.after.to_json_dict()) if change.after else "",
                        "pending",
                        change.reason,
                    ]
                )
        self._append(rows)

    def _append(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        first_row = _FIRST_DATA_ROW + len(self._read_rows())
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, f"A{first_row}:H", rows
        )

    # -- proposals: user edits and feedback ------------------------------------

    def add_user_edits(self, edits: list[UserEdit]) -> None:
        self._append([_edit_row(edit) for edit in edits])

    def user_edits(self, proposal: str) -> list[tuple[int, UserEdit]]:
        """`proposal`'s user edits, in order, each with its sheet row."""
        found = []
        for offset, row in enumerate(self._read_rows()):
            if row[0] == proposal and row[2] == USER_EDIT:
                detail = json.loads(row[7] or "{}")
                found.append(
                    (
                        _FIRST_DATA_ROW + offset,
                        UserEdit(
                            id=f"{proposal}u{row[1]}",
                            seq=int(row[1]),
                            event_id=row[3] or None,
                            edit=json.loads(row[4]),
                            status=row[6] or "active",
                            created=datetime.fromisoformat(detail["created"]),
                            base_revision=detail.get("base", 0),
                        ),
                    )
                )
        return sorted(found, key=lambda pair: pair[1].seq)

    def set_user_edit_status(self, row: int, status: str) -> None:
        self._write_status(row, status)

    def add_feedback(self, feedback: Feedback, proposal: str) -> None:
        self._append([_feedback_row(feedback, proposal)])

    def feedback(self, proposal: str) -> list[tuple[int, Feedback]]:
        """`proposal`'s feedback, in order, each with its sheet row."""
        found = []
        for offset, row in enumerate(self._read_rows()):
            if row[0] == proposal and row[2] == FEEDBACK:
                note = json.loads(row[4])
                answer = json.loads(row[5]) if row[5] else {}
                found.append(
                    (
                        _FIRST_DATA_ROW + offset,
                        Feedback(
                            id=f"{proposal}f{row[1]}",
                            seq=int(row[1]),
                            text=note["text"],
                            event_id=row[3] or None,
                            note_id=note.get("note_id"),
                            at=datetime.fromisoformat(note["at"]) if note.get("at") else None,
                            by=note.get("by", "user"),
                            created=datetime.fromisoformat(note["created"]),
                            status=row[6] or "open",
                            reply=answer.get("reply"),
                            answered_in=answer.get("revision"),
                            superseded_by=answer.get("superseded_by"),
                        ),
                    )
                )
        return sorted(found, key=lambda pair: pair[1].seq)

    def update_feedback(self, row: int, feedback: Feedback) -> None:
        """Write `feedback`'s answer and status into its row."""
        answer = {"reply": feedback.reply, "revision": feedback.answered_in}
        if feedback.superseded_by:
            answer["superseded_by"] = feedback.superseded_by
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id,
            self._sheet_id,
            f"F{row}:G{row}",
            [[json.dumps(answer) if feedback.reply is not None else "", feedback.status]],
        )

    # -- proposals: revisions --------------------------------------------------

    def revisions(self, proposal: str) -> list[tuple[int, str, str]]:
        """(revision, its first day's id, that day's status) of each of
        `proposal`'s revisions still in the journal, oldest first."""
        found = []
        for row in self._read_rows():
            if row[2] != "compaction" or not row[7]:
                continue
            detail = json.loads(row[7])
            if detail.get("proposal") == proposal and not detail.get("batch"):
                found.append((detail["revision"], row[0], row[6]))
        return sorted(found)

    def revision_meta(self, proposal: str, revision: int) -> RevisionMeta | None:
        """`RevisionMeta` of one of `proposal`'s revisions -- kept as long
        as the proposal's open, even once the revision's other rows are
        gone -- or `None`."""
        for row in self._read_rows():
            if row[2] != "compaction" or not row[7]:
                continue
            detail = json.loads(row[7])
            if detail.get("proposal") == proposal and detail.get("revision") == revision and not detail.get("batch"):
                return RevisionMeta.from_detail(detail, [])
        return None

    def open_proposal(self) -> str | None:
        """The id of the proposal still open -- some day of its newest
        revision neither stamped nor abandoned -- if there is one."""
        statuses: dict[tuple[str, int], set[str]] = {}
        for row in self._read_rows():
            if row[2] != "compaction" or not row[7]:
                continue
            detail = json.loads(row[7])
            if detail.get("proposal") is not None:
                statuses.setdefault((detail["proposal"], detail["revision"]), set()).add(row[6])
        latest: dict[str, int] = {}
        for proposal, revision in statuses:
            latest[proposal] = max(latest.get(proposal, 0), revision)
        return next(
            (
                proposal for proposal, revision in latest.items()
                if statuses[(proposal, revision)] - {STAMPED, ABANDONED, SUPERSEDED}
            ),
            None,
        )

    def load(self, compaction_id: str) -> JournalCompaction:
        """The compaction `compaction_id`, with everything needed to
        resume it. Raises `CompactionError` if there isn't one."""
        compaction: JournalCompaction | None = None
        decisions: list[EventDecision] = []
        claude: list[EventDecision] = []
        steps: list[JournalStep] = []
        detail: dict = {}
        for offset, row in enumerate(self._read_rows()):
            if row[0] != compaction_id:
                continue
            sheet_row = _FIRST_DATA_ROW + offset
            kind = row[2]
            if kind == "compaction":
                detail = json.loads(row[7])
                compaction = JournalCompaction(
                    id=compaction_id,
                    status=row[6],
                    now=datetime.fromisoformat(detail["now"]),
                    note_ids=detail["note_ids"],
                    warnings=detail.get("warnings", []),
                    decisions=[],
                    ignore_notes=detail.get("ignore_notes", []),
                    note_targets=detail.get("note_targets", {}),
                    row=sheet_row,
                    batch=detail.get("batch"),
                    day=detail.get("day", 1),
                    additions=detail.get("additions", {}),
                    proposal=detail.get("proposal"),
                    revision=detail.get("revision", 0),
                )
            elif kind == "decision":
                decisions.append(EventDecision.from_json_dict(json.loads(row[4])))
            elif kind == CLAUDE_DECISION:
                claude.append(EventDecision.from_json_dict(json.loads(row[4])))
            elif kind in ("disposition", USER_EDIT, FEEDBACK):
                continue
            else:
                create = kind == "create"
                steps.append(
                    JournalStep(
                        step=int(row[1]),
                        action=kind,
                        event_id=None if create else row[3] or None,
                        before=EventState.from_json_dict(json.loads(row[4])) if row[4] else None,
                        after=EventState.from_json_dict(json.loads(row[5])) if row[5] else None,
                        status=row[6],
                        reason=row[7],
                        row=sheet_row,
                        key=(row[3] or None) if create else None,
                    )
                )
        if compaction is None:
            raise CompactionError(f"there's no compaction with id {compaction_id!r}", category="unknown_compaction")
        compaction.decisions = decisions
        compaction.steps = sorted(steps, key=lambda s: s.step)
        if compaction.proposal is not None and compaction.day == 1:
            compaction.meta = RevisionMeta.from_detail(detail, claude)
        return compaction

    def load_batch(self, batch_id: str) -> list[JournalCompaction]:
        """Every day of the batch `batch_id`, in order -- just the one, for
        a one-day batch. Raises `CompactionError` if there isn't one."""
        first = self.load(batch_id)
        later = [
            row[0]
            for row in self._read_rows()
            if row[2] == "compaction" and json.loads(row[7] or "{}").get("batch") == batch_id
        ]
        return [first] + sorted((self.load(i) for i in later), key=lambda c: c.day)

    def compactions_with_status(self, *statuses: str) -> list[tuple[str, str]]:
        """(id, status) of every compaction currently in one of `statuses`."""
        return [
            (row[0], row[6])
            for row in self._read_rows()
            if row[2] == "compaction" and row[6] in statuses
        ]

    def open_compactions(self) -> list[tuple[str, str]]:
        """(id, status) of every compaction that's `applying` or `applied`
        -- begun but not finished."""
        return self.compactions_with_status(*OPEN_STATUSES)

    def last_stamped_now(self) -> datetime | None:
        """The `now` of the most recently stamped compaction -- for a
        proposal, its `through` -- or `None` if none has ever been
        stamped. Everything before it is history: the next compaction
        window starts here."""
        latest: datetime | None = None
        for row in self._read_rows():
            if row[2] != "compaction" or row[6] != STAMPED:
                continue
            now = datetime.fromisoformat(json.loads(row[7])["now"])
            if latest is None or now > latest:
                latest = now
        return latest

    def last_stamped_batch(self) -> str | None:
        """The batch id of the most recently stamped compaction, or `None`
        if none has ever been stamped."""
        latest: tuple[datetime, str] | None = None
        for row in self._read_rows():
            if row[2] != "compaction" or row[6] != STAMPED:
                continue
            detail = json.loads(row[7])
            now = datetime.fromisoformat(detail["now"])
            if latest is None or now > latest[0]:
                latest = (now, detail.get("batch") or row[0])
        return latest[1] if latest is not None else None

    def set_status(self, compaction: JournalCompaction, status: str) -> None:
        self._write_status(compaction.row, status)
        compaction.status = status

    def mark_step_done(self, step: JournalStep) -> None:
        self._write_status(step.row, "done")
        step.status = "done"

    def garbage_collect(self) -> None:
        """Delete rows nothing needs any more, anywhere in the tab, bottom
        first:

        - Of an open proposal's superseded revisions, all but the newest
          `_WHOLE_SUPERSEDED` lose every row but their `compaction` ones
          (whose `changed` lists still say what each revision changed).
        - Once the tab has more than `_MAX_ROWS` data rows, whole
          finished blocks go, oldest first, until it's down to
          `_TRIM_TO_ROWS`: a stamped, abandoned or superseded compaction
          (or a failed one, its proposal finished), and a finished
          proposal's edits and feedback. Never the most recently stamped
          batch, which `last_stamped_now` depends on, nor anything of an
          open proposal or a compaction still planned or open.

        Superseded revisions were never applied, and nothing reads them
        once a newer one exists, so they're safe to delete."""
        rows = self._read_rows()
        details = [json.loads(row[7]) if row[2] == "compaction" and row[7] else {} for row in rows]
        batch_of = {row[0]: details[i].get("batch") or row[0] for i, row in enumerate(rows) if row[2] == "compaction"}
        status_of = {row[0]: row[6] for row in rows if row[2] == "compaction"}
        proposal_of = {row[0]: details[i].get("proposal") for i, row in enumerate(rows) if row[2] == "compaction"}
        open_proposal = self.open_proposal()
        # The latest stamped by position: the journal's written in order.
        last_stamped = next(
            (batch_of[row[0]] for row in reversed(rows) if row[2] == "compaction" and row[6] == STAMPED), None
        )

        doomed: set[int] = set()
        if open_proposal is not None:
            superseded = sorted(
                {
                    (details[i]["revision"], batch_of[row[0]])
                    for i, row in enumerate(rows)
                    if row[2] == "compaction" and proposal_of.get(row[0]) == open_proposal
                    and status_of[row[0]] == SUPERSEDED
                },
                reverse=True,
            )
            trimmed = {batch for _, batch in superseded[_WHOLE_SUPERSEDED:]}
            doomed |= {
                i for i, row in enumerate(rows)
                if row[2] != "compaction" and batch_of.get(row[0]) in trimmed
            }

        if len(rows) - len(doomed) > _MAX_ROWS:
            excess = len(rows) - len(doomed) - _TRIM_TO_ROWS

            def finished(compaction_id: str) -> bool:
                if batch_of[compaction_id] == last_stamped:
                    return False
                proposal = proposal_of.get(compaction_id)
                if proposal is not None and proposal == open_proposal:
                    return False
                status = status_of[compaction_id]
                return status in (STAMPED, ABANDONED, SUPERSEDED) or (status == FAILED and proposal is not None)

            # Units of rows that go together: each compaction's block, and
            # each proposal's edits and feedback.
            units: dict[str, list[int]] = {}
            for i, row in enumerate(rows):
                if not row[0]:
                    continue
                unit = f"ledger:{row[0]}" if row[2] in (USER_EDIT, FEEDBACK) else row[0]
                units.setdefault(unit, []).append(i)
            proposals_finished = {
                p for p in set(proposal_of.values()) if p is not None and p != open_proposal
            }
            freed = 0
            for unit, indices in sorted(units.items(), key=lambda item: item[1][0]):
                if freed >= excess:
                    break
                if unit.startswith("ledger:"):
                    if unit.removeprefix("ledger:") not in proposals_finished:
                        continue
                elif unit not in status_of or not finished(unit):
                    continue
                fresh = [i for i in indices if i not in doomed]
                doomed |= set(fresh)
                freed += len(fresh)

        if not doomed:
            return
        # Contiguous runs, deleted bottom first so the rows above don't move.
        runs: list[list[int]] = []
        for i in sorted(doomed):
            if runs and runs[-1][1] == i - 1:
                runs[-1][1] = i
            else:
                runs.append([i, i])
        for start, end in reversed(runs):
            self._sheets_client.delete_rows(
                self._spreadsheet_id,
                self._sheet_id,
                start_row=_FIRST_DATA_ROW + start,
                end_row=_FIRST_DATA_ROW + end,
                keep_at_least=calendar_metadata_sheet.MIN_TAB_ROWS,
            )

    def _write_status(self, row: int, status: str) -> None:
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id,
            self._sheet_id,
            f"{_STATUS_COLUMN}{row}:{_STATUS_COLUMN}{row}",
            [[status]],
        )

    def _read_rows(self) -> list[list[str]]:
        """Every data row, through the last non-blank one (as the API
        returns them), padded to the full width -- see the module
        docstring for why this tab is always read whole."""
        rows = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE
        )
        return [list(row) + [""] * (len(_HEADER) - len(row)) for row in rows]


def _edit_row(edit: UserEdit) -> list[str]:
    proposal = edit.id[: -len(f"u{edit.seq}")]
    return [
        proposal,
        str(edit.seq),
        USER_EDIT,
        edit.event_id or "",
        json.dumps(edit.edit),
        "",
        edit.status,
        json.dumps({"created": edit.created.isoformat(), "base": edit.base_revision}),
    ]


def _feedback_row(feedback: Feedback, proposal: str) -> list[str]:
    note = {"text": feedback.text, "by": feedback.by, "created": feedback.created.isoformat()}
    if feedback.at is not None:
        note["at"] = feedback.at.isoformat()
    if feedback.note_id is not None:
        note["note_id"] = feedback.note_id
    return [proposal, str(feedback.seq), FEEDBACK, feedback.event_id or "", json.dumps(note), "", feedback.status, ""]
