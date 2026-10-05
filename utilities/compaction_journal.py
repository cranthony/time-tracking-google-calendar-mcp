"""The write-ahead journal of note compactions, one tab of the calendar
metadata spreadsheet (see utilities/calendar_metadata_sheet.py).

Compacting notes is a series of calendar writes that can die halfway, and
the model's interpretation of the notes (its decisions) exists only in a
conversation. So before anything is applied, the whole approved plan --
the decisions, and every step with its before/after state -- is written
here, and each step is checked off as it's applied. That makes it possible
to finish exactly the plan that was approved after a failure, and leaves a
before-state record of every change.

Layout: one row per fact, all in the same eight columns --

    compaction_id | step | kind | event_id | before | after | status | detail

- `kind == "compaction"` (step 0): the compaction itself. `status` is
  where it is in its life (see below); `detail` is JSON with `now`, the
  ids of the notes it consumes, the ids of notes it was told to ignore,
  and any planner warnings -- and, for the second and later days of a
  batch, the `batch` (the first day's id) and which `day` of it it is.
  The batch's first day also keeps any `additions`: the actions, people
  and locations the plan adds, each with the `ref` its decisions name it
  by ({"actions": [...], "people": [...], "locations": [...]}), created
  before that day's steps.
- `kind == "decision"`: one per `EventDecision`, as JSON in `before`
  (`event_id` repeats its event id, if it has one, for reading the tab by
  hand). Rows of the retired `disposition` kind, from before decisions
  replaced them, are skipped.
- `kind` in `update`/`create`/`cancel`: one calendar change (`step` is
  1-based, in the order they're applied). `status` is `pending` or `done`;
  `detail` is the human-readable reason.

One row per step (rather than one JSON cell for the whole plan) keeps every
cell far below Sheets' 50,000-character limit even for a busy day.

A compaction covers one day. Several days compacted together are a
*batch*: one compaction per day, written in one block, the first day's
id doubling as the batch's (so a one-day batch is just a compaction), and
the rest `<batch>d<day>`. Each day's compaction moves through the
statuses below on its own, in order, so a batch applied partway has its
earlier days finished and stamped.

A compaction's status moves `planned` -> `applying` -> `applied` ->
`stamped` (its notes marked compacted -- the terminal state), or to
`abandoned`. `applying` and `applied` are the "open" states: a new
compaction is refused while one exists, so it has to be resumed or
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
old, fully-finished compaction blocks from the top once the tab passes
`_MAX_ROWS`, so the full read stays small. It never deletes a compaction that's still open or merely
planned, and never deletes the most recently *stamped* one, since
`last_stamped_now` depends on it. Deleting shifts every row below up, so
row numbers -- including a compaction's own, and every note id it
recorded -- stop being permanent once this runs; anything holding an
older row number finds out the same way it already would if the sheet
had simply changed underneath it (`NoteCompactor`'s "changed since
preview" check), never silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    EventState,
)

PLANNED = "planned"
APPLYING = "applying"
APPLIED = "applied"
STAMPED = "stamped"
ABANDONED = "abandoned"
OPEN_STATUSES = (APPLYING, APPLIED)

_HEADER = ["compaction_id", "step", "kind", "event_id", "before", "after", "status", "detail"]
_HEADER_RANGE = "A1:H1"
_DATA_RANGE = "A2:H"
_FIRST_DATA_ROW = 2
_STATUS_COLUMN = "G"

_MAX_ROWS = 100
"""garbage_collect kicks in once this tab has more data rows than this."""

_TRIM_TO_ROWS = 50
"""What garbage_collect trims this tab's data rows down to once it kicks
in -- well under `_MAX_ROWS`, not just back to it, so it doesn't kick in
again on the very next append."""


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
    row: int = 0
    batch: str | None = None
    """The id of the batch's first day, for the second and later days of a
    batch -- see the module docstring."""

    day: int = 1
    additions: dict[str, list[dict]] = field(default_factory=dict)
    """The actions, people and locations the plan adds -- see the module
    docstring."""

    @property
    def batch_id(self) -> str:
        """The id of the batch this day belongs to: what the MCP tools
        call the compaction."""
        return self.batch or self.id

    def changes(self) -> list[CompactionChange]:
        return [
            CompactionChange(
                action=s.action, reason=s.reason, event_id=s.event_id, before=s.before, after=s.after
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

    def start_batch(self, days: list[PlannedDay]) -> None:
        """Record a new `planned` batch, a compaction per day (the first
        day's id is the batch's): for each, the compaction row, one row
        per decision, and one `pending` row per step -- all in one write,
        so a crash can't leave half a plan recorded."""
        batch = days[0].compaction_id
        rows: list[list[str]] = []
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
            rows.append([day.compaction_id, "0", "compaction", "", "", "", PLANNED, json.dumps(detail)])
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
                        change.event_id or "",
                        json.dumps(change.before.to_json_dict()) if change.before else "",
                        json.dumps(change.after.to_json_dict()) if change.after else "",
                        "pending",
                        change.reason,
                    ]
                )
        first_row = _FIRST_DATA_ROW + len(self._read_rows())
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, f"A{first_row}:H", rows
        )

    def garbage_collect(self) -> None:
        """Delete old, fully-finished compaction blocks from the top of
        this tab once it's grown past `_MAX_ROWS` data rows -- see the
        module docstring. Groups rows into blocks by their
        `compaction_id` column (each `start` always writes one
        contiguous block, and blocks are never reordered afterward), then
        deletes a prefix of them: every block up to, but never including,
        the most recently *stamped* one or the first block that isn't
        `stamped`/`abandoned`, whichever comes first, stopping as soon as
        it's down to `_TRIM_TO_ROWS` data rows (or there's nothing left
        it can safely delete)."""
        all_rows = self._read_rows()
        data_rows = len(all_rows)
        if data_rows <= _MAX_ROWS:
            return
        excess = data_rows - _TRIM_TO_ROWS
        blocks: list[list] = []  # [compaction_id, status_of_its_compaction_row, row_count]
        for row in all_rows:
            compaction_id = row[0]
            if not compaction_id:
                continue
            if not blocks or blocks[-1][0] != compaction_id:
                blocks.append([compaction_id, None, 0])
            if row[2] == "compaction":
                blocks[-1][1] = row[6]
            blocks[-1][2] += 1

        last_stamped_index = None
        for i, (_, status, _) in enumerate(blocks):
            if status == STAMPED:
                last_stamped_index = i

        deletable_rows = 0
        deletable_blocks = 0
        for i, (_, status, count) in enumerate(blocks):
            if i == last_stamped_index or status not in (STAMPED, ABANDONED):
                break
            deletable_rows += count
            deletable_blocks += 1
            if deletable_rows >= excess:
                break

        if deletable_blocks == 0:
            return
        self._sheets_client.delete_rows(
            self._spreadsheet_id,
            self._sheet_id,
            start_row=_FIRST_DATA_ROW,
            end_row=_FIRST_DATA_ROW + deletable_rows - 1,
            keep_at_least=calendar_metadata_sheet.MIN_TAB_ROWS,
        )

    def load(self, compaction_id: str) -> JournalCompaction:
        """The compaction `compaction_id`, with everything needed to
        resume it. Raises `CompactionError` if there isn't one."""
        compaction: JournalCompaction | None = None
        decisions: list[EventDecision] = []
        steps: list[JournalStep] = []
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
                    row=sheet_row,
                    batch=detail.get("batch"),
                    day=detail.get("day", 1),
                    additions=detail.get("additions", {}),
                )
            elif kind == "decision":
                decisions.append(EventDecision.from_json_dict(json.loads(row[4])))
            elif kind == "disposition":
                continue
            else:
                steps.append(
                    JournalStep(
                        step=int(row[1]),
                        action=kind,
                        event_id=row[3] or None,
                        before=EventState.from_json_dict(json.loads(row[4])) if row[4] else None,
                        after=EventState.from_json_dict(json.loads(row[5])) if row[5] else None,
                        status=row[6],
                        reason=row[7],
                        row=sheet_row,
                    )
                )
        if compaction is None:
            raise CompactionError(f"there's no compaction with id {compaction_id!r}")
        compaction.decisions = decisions
        compaction.steps = sorted(steps, key=lambda s: s.step)
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
        """The `now` of the most recently stamped compaction, or `None` if
        none has ever been stamped. The next compaction window starts here
        (unless its day starts later), so it never re-offers a calendar
        range that's already been settled."""
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
