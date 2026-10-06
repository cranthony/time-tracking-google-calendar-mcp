"""Rolling traits up into daily scores, per person (utilities/trait_scores.py
says how each is scored), kept in the **Trait Scores** tab of the
calendar's metadata spreadsheet: one row per person per day, with each
trait's score and its parts'.

It's a cache, rebuilt whenever it's wanted: a compaction rolls up the days
it settled once it's complete (its facts written and its judgments made --
see utilities/note_compactor.py), and `rebuild_trait_scores` rolls up any
span of days again, to backfill or after a judgment is redone. A day is a
calendar date in the calendar's own time zone, midnight to midnight, and
is only rolled up once it's over. Only the last `KEEP_DAYS` days are kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.action_calendar import fill_in_from_actions
from utilities.actions import Actions
from utilities.cancellations import Cancellations
from utilities.people import People
from utilities.row_sheet import RowSheet
from utilities.trait_scores import reach, score_person, traits_for
from utilities.traits import Traits

KEEP_DAYS = 400
"""How many days of scores the tab keeps; older rows are dropped."""


@dataclass(kw_only=True)
class TraitScoreRow:
    """One person's trait scores for one day."""

    id: str | None = None
    """"<day>/<person id>"."""

    day: str | None = None
    """The day, e.g. "2026-10-05"."""

    person_id: str | None = None
    scores: dict[str, Any] | None = None
    """{trait id: 0-100, or null with nothing to score it by}."""

    parts: dict[str, Any] | None = None
    """{trait id: {part key: {"score", "said"}}}: how each was reached."""


class TraitRollup:
    """A calendar's daily trait scores -- see the module docstring."""

    def __init__(
        self,
        client,
        actions: Actions,
        people: People,
        traits: Traits,
        sheet: RowSheet[TraitScoreRow],
        cancellations: Cancellations | None = None,
    ) -> None:
        """`client` lists events (a CalendarClient) and gives the calendar's
        time zone. `cancellations` are what follow-through counts (see
        utilities/cancellations.py); without them, nothing's cancelled."""
        self._client = client
        self._actions = actions
        self._people = people
        self._traits = traits
        self._sheet = sheet
        self._cancellations = cancellations

    @staticmethod
    def ensure(
        client,
        actions: Actions,
        people: People,
        traits: Traits,
        sheets_client: SheetsClient,
        spreadsheet_id: str,
        cancellations: Cancellations | None = None,
    ) -> "TraitRollup":
        """The calendar's trait scores, adding the Trait Scores tab the first
        time."""
        sheet = RowSheet.ensure(
            sheets_client,
            spreadsheet_id,
            role=calendar_metadata_sheet.TRAIT_SCORES_SHEET_ROLE,
            title=calendar_metadata_sheet.TRAIT_SCORES_SHEET_TITLE,
            row_type=TraitScoreRow,
            required=("id", "day", "person_id"),
        )
        return TraitRollup(client, actions, people, traits, sheet, cancellations)

    @property
    def whole_tabs(self) -> list[TabRange]:
        cancellations = [self._cancellations.whole_tabs[0]] if self._cancellations is not None else []
        return [self._sheet.whole_tab, self._traits.whole_tab, *cancellations]

    def days_between(self, start: datetime, end: datetime) -> list[date]:
        """The days from the one `start` falls in to the last that's over by
        `end`, in the calendar's time zone."""
        tz = self._client.get_time_zone()
        first = start.astimezone(tz).date()
        last = end.astimezone(tz).date() - timedelta(days=1)
        return [first + timedelta(days=n) for n in range((last - first).days + 1)]

    def get(self, person_id: str | None = None, start: date | None = None, end: date | None = None) -> list[TraitScoreRow]:
        """The rows for `person_id` (by default everyone's) from `start` to
        `end` (inclusive, either open), by day then person."""
        return [
            r for r in self._sheet.read()
            if (person_id is None or r.person_id == person_id)
            and (start is None or r.day >= start.isoformat())
            and (end is None or r.day <= end.isoformat())
        ]

    def roll_up(self, days: list[date]) -> list[TraitScoreRow]:
        """Score every active person's traits for each of `days` that's over,
        replacing any rows for them. Returns the rows written."""
        tz = self._client.get_time_zone()
        today = datetime.now(tz).date()
        days = sorted(d for d in set(days) if d < today)
        if not days:
            return []
        # Every tab it reads, in one request (Sheets allows 60 a minute).
        self._sheet.prefetch(self.whole_tabs + self._people.whole_tabs + self._actions.whole_tabs)
        people = self._people.get_people()
        traits = self._traits.all()
        parts = [p for person in people for _t, ps in traits_for(person, traits) for p in ps]
        back, ahead = reach(parts)
        first = datetime.combine(days[0], time(), tz)
        last = datetime.combine(days[-1], time(), tz) + timedelta(days=1)
        tree = self._actions.tree()
        kept = [
            e for e in fill_in_from_actions(self._client.list_events(first - back, last + ahead), tree)
            if e.status != "cancelled"
        ]
        cancelled = [c.to_event() for c in self._cancellations.all()] if self._cancellations is not None else []
        rows = []
        for day in days:
            start = datetime.combine(day, time(), tz)
            window = (start, start + timedelta(days=1))
            for person in people:
                scored = score_person(person, traits, window, kept, cancelled, tree)
                rows.append(TraitScoreRow(
                    id=f"{day.isoformat()}/{person.id}",
                    day=day.isoformat(),
                    person_id=person.id,
                    scores={t.trait_id: t.score for t in scored},
                    parts={t.trait_id: {p.key: {"score": p.score, "said": p.said} for p in t.parts} for t in scored},
                ))
        written = {r.id for r in rows}
        oldest = (today - timedelta(days=KEEP_DAYS)).isoformat()
        kept_rows = [r for r in self._sheet.read() if r.id not in written and (r.day or "") >= oldest]
        self._sheet.write(sorted(kept_rows + rows, key=lambda r: (r.day or "", r.person_id or "")))
        return rows
