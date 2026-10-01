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
  and any planner warnings.
- `kind == "decision"`: one per `EventDecision`, as JSON in `before`
  (`event_id` repeats its event id, if it has one, for reading the tab by
  hand). Rows of the retired `disposition` kind, from before decisions
  replaced them, are skipped.
- `kind` in `update`/`create`/`cancel`: one calendar change (`step` is
  1-based, in the order they're applied). `status` is `pending` or `done`;
  `detail` is the human-readable reason.

One row per step (rather than one JSON cell for the whole plan) keeps every
cell far below Sheets' 50,000-character limit even for a busy day.

A compaction's status moves `planned` -> `applying` -> `applied` ->
`stamped` (its notes marked compacted -- the terminal state), or to
`abandoned`. `applying` and `applied` are the "open" states: a new
compaction is refused while one exists, so it has to be resumed or
abandoned first.

This tab only ever grows, so `start` -- appending a new compaction's rows
-- uses `utilities/row_hints.py`'s `RowHints` to seek straight to the
next free row instead of reading the whole tab just to count it, falling
back to that full read if the hint doesn't check out. `load` (so
`commit`/`describe`/`abandon` too) does the same for the opposite
direction: it seeks to where the most recently started compaction's own
rows begin instead of scanning from the top, since that's virtually
always the one being asked for -- at most one compaction is ever
non-terminal at a time (a new one is refused while another is open, and
an unapplied one is abandoned before a new dry run replaces it).

`garbage_collect` keeps this from growing forever by physically deleting
old, fully-finished compaction blocks from the top once the tab passes
`_MAX_ROWS`. It never deletes a compaction that's still open or merely
planned, and never deletes the most recently *stamped* one, since
`last_stamped_now` depends on it. Deleting shifts every row below up, so
row numbers -- including a compaction's own, and every note id it
recorded -- stop being permanent once this runs; anything holding an
older row number finds out the same way it already would if the sheet
had simply changed underneath it (`load`'s hint check, or
`NoteCompactor`'s "changed since preview" check), never silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    EventState,
)
from utilities.row_hints import RowHints

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

_NEXT_ROW_HINT = "journal_next_row"
_LATEST_COMPACTION_ROW_HINT = "journal_latest_compaction_row"
_CONFIRM_ROWS = 5
"""See utilities/row_hints.py -- how many rows a hint's confirmation
check reads before trusting it."""

_MAX_ROWS = 500
"""garbage_collect kicks in once this tab has more data rows than this."""

_TRIM_TO_ROWS = _MAX_ROWS - 100
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

    def changes(self) -> list[CompactionChange]:
        return [
            CompactionChange(
                action=s.action, reason=s.reason, event_id=s.event_id, before=s.before, after=s.after
            )
            for s in self.steps
        ]


class CompactionJournal:
    def __init__(
        self, sheets_client: SheetsClient, spreadsheet_id: str, sheet_id: int, hints: RowHints
    ) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id
        self._hints = hints

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
        hints = RowHints.ensure(sheets_client, spreadsheet_id)
        if created:
            sheets_client.write_rows_in_sheet(spreadsheet_id, sheet_id, _HEADER_RANGE, [_HEADER])
        return CompactionJournal(sheets_client, spreadsheet_id, sheet_id, hints)

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
        """Record a new `planned` compaction: the compaction row, one row
        per decision, and one `pending` row per step -- all in one write,
        so a crash can't leave half a plan recorded."""
        rows: list[list[str]] = [
            [
                compaction_id,
                "0",
                "compaction",
                "",
                "",
                "",
                PLANNED,
                json.dumps(
                    {
                        "now": now.isoformat(),
                        "note_ids": note_ids,
                        "warnings": plan.warnings,
                        "ignore_notes": ignore_notes or [],
                    }
                ),
            ]
        ]
        for decision in decisions:
            rows.append(
                [
                    compaction_id,
                    "0",
                    "decision",
                    decision.event_id or "",
                    json.dumps(decision.to_json_dict()),
                    "",
                    "",
                    "",
                ]
            )
        for number, change in enumerate(plan.changes, start=1):
            rows.append(
                [
                    compaction_id,
                    str(number),
                    change.action,
                    change.event_id or "",
                    json.dumps(change.before.to_json_dict()) if change.before else "",
                    json.dumps(change.after.to_json_dict()) if change.after else "",
                    "pending",
                    change.reason,
                ]
            )
        first_row = self._next_row()
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, f"A{first_row}:H", rows
        )
        self._hints.set(_NEXT_ROW_HINT, first_row + len(rows))
        self._hints.set(_LATEST_COMPACTION_ROW_HINT, first_row)

    def _next_row(self) -> int:
        """The row this journal's next compaction should start at: a
        hinted row, if confirmed still correct, or a full count of the
        existing rows otherwise. Confirming it checks two things: a few
        rows at and after it are blank (a crash between a previous
        `start`'s write and its hint update would otherwise leave the
        hint one short), and -- unless it's the very first data row,
        which by definition has nothing before it to check -- the row
        right before it is non-blank, so real data genuinely ends there
        instead of the hint floating somewhere past a gap a stale hint
        would otherwise hide. (Reading row 1 for that check would hit the
        header row, not data, for any tab whose first data row is 2 --
        true today, but this avoids depending on that.)"""
        hinted = self._hints.get(_NEXT_ROW_HINT)
        if hinted is not None and hinted >= _FIRST_DATA_ROW:
            if hinted == _FIRST_DATA_ROW:
                before_confirmed = True
                after = self._sheets_client.read_rows_in_sheet(
                    self._spreadsheet_id, self._sheet_id, f"A{hinted}:H{hinted + _CONFIRM_ROWS - 1}"
                )
            else:
                check = self._sheets_client.read_rows_in_sheet(
                    self._spreadsheet_id,
                    self._sheet_id,
                    f"A{hinted - 1}:H{hinted + _CONFIRM_ROWS - 1}",
                )
                before, after = (check[0], check[1:]) if check else ([], [])
                before_confirmed = any(cell.strip() for cell in before)
            if before_confirmed and not any(any(cell.strip() for cell in row) for row in after):
                return hinted
        return _FIRST_DATA_ROW + len(self._read_rows())

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
        next_row = self._next_row()
        data_rows = next_row - _FIRST_DATA_ROW
        if data_rows <= _MAX_ROWS:
            return
        excess = data_rows - _TRIM_TO_ROWS
        blocks: list[list] = []  # [compaction_id, status_of_its_compaction_row, row_count]
        for row in self._read_rows():
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
        self._hints.set(_NEXT_ROW_HINT, next_row - deletable_rows)
        latest = self._hints.get(_LATEST_COMPACTION_ROW_HINT)
        if latest is not None:
            self._hints.set(_LATEST_COMPACTION_ROW_HINT, max(_FIRST_DATA_ROW, latest - deletable_rows))

    def _latest_compaction_start_row(self, compaction_id: str) -> int:
        """Where `load` can start reading for `compaction_id`: the hinted
        start of the most recently started compaction, if confirmed to
        genuinely be its own first row, or the top of the tab otherwise.
        Confirming it checks two things: the hinted row itself belongs to
        `compaction_id` (not some other, older one -- e.g. `compaction_id`
        wasn't the last one started), and -- unless it's the first data
        row, which by definition has nothing before it -- the row right
        before it belongs to a *different* compaction (or none), so the
        hint is genuinely sitting at a compaction boundary and not
        partway through `compaction_id`'s own earlier rows (which would
        silently miss its `compaction`-kind row and any decisions before
        the hint)."""
        hinted = self._hints.get(_LATEST_COMPACTION_ROW_HINT)
        if hinted is None or hinted < _FIRST_DATA_ROW:
            return _FIRST_DATA_ROW
        if self._compaction_id_at(hinted) != compaction_id:
            return _FIRST_DATA_ROW
        if hinted > _FIRST_DATA_ROW and self._compaction_id_at(hinted - 1) == compaction_id:
            return _FIRST_DATA_ROW
        return hinted

    def _compaction_id_at(self, row: int) -> str | None:
        check = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, f"A{row}:A{row}"
        )
        return check[0][0] if check and check[0] else None

    def load(self, compaction_id: str) -> JournalCompaction:
        """The compaction `compaction_id`, with everything needed to
        resume it. Raises `CompactionError` if there isn't one.

        Starts from `_latest_compaction_start_row` instead of the top of
        the tab when that hint is confirmed to actually be
        `compaction_id`'s own first row -- true for virtually every real
        call (see the module docstring) -- so this reads only that one
        compaction's rows instead of the journal's entire history."""
        start_row = self._latest_compaction_start_row(compaction_id)
        compaction: JournalCompaction | None = None
        decisions: list[EventDecision] = []
        steps: list[JournalStep] = []
        rows = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, f"A{start_row}:H"
        )
        for offset, row in enumerate(rows):
            if row[0] != compaction_id:
                continue
            sheet_row = start_row + offset
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
        rows = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE
        )
        return [list(row) + [""] * (len(_HEADER) - len(row)) for row in rows]
