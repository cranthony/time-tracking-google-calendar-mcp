"""The write-ahead journal of note compactions, one tab of the calendar
metadata spreadsheet (see utilities/calendar_metadata_sheet.py).

Compacting notes is a series of calendar writes that can die halfway, and
the model's interpretation of the notes (the dispositions) exists only in
a conversation. So before anything is applied, the whole approved plan --
the dispositions, and every step with its before/after state -- is written
here, and each step is checked off as it's applied. That makes it possible
to finish exactly the plan that was approved after a failure, and leaves a
before-state record of every change.

Layout: one row per fact, all in the same eight columns --

    compaction_id | step | kind | event_id | before | after | status | detail

- `kind == "compaction"` (step 0): the compaction itself. `status` is
  where it is in its life (see below); `detail` is JSON with `now`, the
  ids of the notes it consumes, and any planner warnings.
- `kind == "disposition"`: one per note. `event_id` holds the *note* id
  and `before` holds that note's effects as JSON.
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
    EventState,
    NoteDisposition,
    NoteEffect,
    Reschedule,
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
    dispositions: list[NoteDisposition]
    reschedules: list[Reschedule] = field(default_factory=list)
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
    def __init__(self, sheets_client: SheetsClient, spreadsheet_id: str, sheet_id: int) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id

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
        dispositions: list[NoteDisposition],
        plan: CompactionPlan,
        reschedules: list[Reschedule] | None = None,
    ) -> None:
        """Record a new `planned` compaction: the compaction row, one row
        per disposition, and one `pending` row per step -- all in one
        write, so a crash can't leave half a plan recorded."""
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
                        "reschedules": [r.to_json_dict() for r in reschedules or []],
                    }
                ),
            ]
        ]
        for disposition in dispositions:
            rows.append(
                [
                    compaction_id,
                    "0",
                    "disposition",
                    disposition.note_id,
                    json.dumps(
                        [
                            {k: v for k, v in effect.__dict__.items() if v is not None}
                            for effect in disposition.effects
                        ]
                    ),
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
        existing = self._read_rows()
        first_row = _FIRST_DATA_ROW + len(existing)
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, f"A{first_row}:H", rows
        )

    def load(self, compaction_id: str) -> JournalCompaction:
        """The compaction `compaction_id`, with everything needed to
        resume it. Raises `CompactionError` if there isn't one."""
        compaction: JournalCompaction | None = None
        dispositions: list[NoteDisposition] = []
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
                    dispositions=[],
                    reschedules=[Reschedule.from_json_dict(r) for r in detail.get("reschedules", [])],
                    row=sheet_row,
                )
            elif kind == "disposition":
                dispositions.append(
                    NoteDisposition(
                        note_id=row[3], effects=[NoteEffect(**e) for e in json.loads(row[4])]
                    )
                )
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
        compaction.dispositions = dispositions
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
        none has ever been stamped. The next day's window starts here, so
        it never re-fetches a calendar range that's already been finalized
        or misses one that ends right at the boundary."""
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
