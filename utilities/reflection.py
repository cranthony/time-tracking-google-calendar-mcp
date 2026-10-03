"""Reflections: a conversation, run by the MCP client, that confirms each
goal's rating of one day -- from waking on it to waking the next (see
utilities/sleep_days.py) -- and may record a short journal and a few
intentions. Every rated goal (see utilities/goals.py's GoalTree.rated) is
reflected on daily; there are no other cadences.

**Bottom up.** A rating only ever flows up the tree, from sub-goals to
their parents: a rollup is computed from its immediate sub-goals'
ratings, and an llm rubric may refer to them. So a day is rated a level
at a time: `prepare` offers only the goals whose rated sub-goals all have
a confirmed rating that day -- first the goals without any -- and `record`
confirms them. Calling `prepare` again then offers their parents, until
every rated goal has one. Each `record` confirms its ratings on the Goal
Health calendar straight away, so a reflection cut short (a crash, a
closed conversation) picks up where it left off: nothing confirmed is
asked again.

`record` also writes a reflection event beside the assessments: an
all-day event on the day, its description the journal, its private
extended properties the day, the intentions and whether every rated goal
has been rated ("complete"). Its id encodes the day, so recording again
replaces it, keeping the journal and intentions unless new ones are
given. Committing is the only way an assessment becomes `confirmed`
(utilities/goal_health.py's `confirm_assessments`).

**Subjective goals** are asked their prompt only once their
`interval_days` have passed since it was last answered (in a reflection,
or given in passing with `record_assessments`); on the days between, the
previous day's rating is proposed again, marked as carried over.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from calendar_clients.google_calendar import Event
from utilities.goal_calendar import fill_in_from_goals
from utilities.goal_health import (
    CADENCE,
    MAX_RATIONALE_BYTES,
    Assessment,
    GoalHealth,
    band,
    day_period,
)
from utilities.goal_time import goal_minutes
from utilities.goals import Goals, GoalTree
from utilities.noted_time_sheet import NotedTimeSheet
from utilities.sleep_days import MissingSleep, NotOver, listing_range, period_window

_PREFIX = "cascading-time-tracker-"

MAX_INTENTIONS = 3

_LOOKBACK_DAYS = 12
"""How far back `prepare_reflection` looks for a day still to reflect on."""

_CHOICES = 3
"""How many completed, unreflected days a reflection offers, newest first,
when none is named."""

_RECENT_RATINGS = 6


@dataclass(kw_only=True)
class PastRating:
    day: date
    rating: int | Literal["skip"]


@dataclass(kw_only=True)
class SubGoalRating:
    """An immediate sub-goal's confirmed rating of the day."""

    goal_id: str
    path: str
    rating: int | Literal["skip"]
    explanation: str | None = None


@dataclass(kw_only=True)
class DueGoal:
    """A goal ready to rate: its rated sub-goals, if any, all have been."""

    goal_id: str
    path: str
    measure: dict[str, Any]
    """How it's rated: its own measure, or, without one, the mean of its
    sub-goals'."""

    target: str | None = None
    deadline: date | None = None
    note: str | None = None
    ask: str | None = None
    """For a subjective goal due to be asked: its prompt. Ask it."""

    recent: list[PastRating] = field(default_factory=list)
    """Its last confirmed ratings before this day."""

    sub_goals: list[SubGoalRating] = field(default_factory=list)
    """Its rated sub-goals' ratings of this day (for a rollup, or an llm
    rubric that refers to them)."""

    proposed: Assessment | None = None
    """What to start from: a rating recorded earlier for this day, one
    measured from the calendar or rolled up from its sub-goals (with its
    explanation), or a subjective one carried over from the day before;
    `None` for a goal to ask about (`ask`) or judge (llm)."""


@dataclass(kw_only=True)
class DayChoice:
    """A day a reflection could be for, offered when none was named."""

    day: date
    starts: datetime | None
    """When it started: when you woke on it (see utilities/sleep_days.py)
    -- `None` if that sleep isn't in the calendar."""

    ends: datetime | None
    """When it ended: when you woke the next day -- `None` if that sleep
    isn't in the calendar."""

    missing_sleep: list[date] = field(default_factory=list)
    """The days whose end-of-day sleep it needs but isn't in the calendar:
    until they are, it can't be reflected on."""

    already_reflected: bool
    started: bool = False
    """Some of its goals are rated already, but not all."""


@dataclass(kw_only=True)
class GoalTimeSpent:
    goal_id: str
    path: str
    minutes: int
    """Minutes of events serving the goal or any of its sub-goals."""


@dataclass(kw_only=True)
class ReflectionContext:
    day: date | None
    """`None` when no day was named: see `choices`."""

    starts: datetime | None = None
    """When the day started: when you woke on it."""

    ends: datetime | None = None
    """When it ended: when you woke the next day. Its events, notes and
    minutes per goal are those between the two."""

    already_reflected: bool = False
    """Every rated goal has been rated for this day already; rating one
    again replaces its rating."""

    choices: list[DayChoice] = field(default_factory=list)
    """When no day was named: the days to ask about -- the most recent
    completed ones not fully reflected on, or, if every one has been, the
    last completed one -- and nothing else is filled in."""

    older_unreflected: int = 0
    """Days not reflected on before `choices`, within the last 12 (and
    since the first goal was created), not offered."""

    journal: str | None = None
    """The journal recorded with it, if so."""

    due: list[DueGoal] = field(default_factory=list)
    """The goals to rate now: not rated yet, and with every rated sub-goal
    rated already. Goals without rated sub-goals come first."""

    rated: list[Assessment] = field(default_factory=list)
    """The goals already confirmed for this day."""

    waiting: list[str] = field(default_factory=list)
    """Paths of goals to rate once their sub-goals in `due` are rated:
    call prepare_reflection again then."""

    unmeasured: list[str] = field(default_factory=list)
    """Paths of active goals that aren't rated: they have no measure and
    no rated sub-goals."""

    goal_time: list[GoalTimeSpent] = field(default_factory=list)
    events_digest: str | None = None
    """The day's events, with their goals."""

    notes_digest: str | None = None
    """The day's compacted notes."""

    previous_intentions: list[str] = field(default_factory=list)
    """From the previous day's reflection."""

    uncompacted_notes: int = 0
    """Notes up to the day's end that haven't been compacted: until they
    are, the calendar (and so the measured ratings) may be off."""

    instructions: str = ""


@dataclass(kw_only=True)
class ReflectionResult:
    status: Literal["preview", "recorded"]
    day: date
    preview: str
    """One line per rating, and the journal and intentions."""

    assessments: list[Assessment] = field(default_factory=list)
    not_rated: list[str] = field(default_factory=list)
    """Paths of goals ready to rate but not rated in this call."""

    complete: bool = False
    """Every rated goal has a confirmed rating of the day (once recorded)."""

    message: str = ""


def reflection_event_id(day: date) -> str:
    encoded = base64.b32hexencode(f"reflection|{CADENCE}|{day.isoformat()}".encode()).decode()
    return encoded.rstrip("=").lower()


class Reflections:
    def __init__(self, health: GoalHealth, goals: Goals, notes: NotedTimeSheet | None = None) -> None:
        self._health = health
        self._goals = goals
        self._notes = notes

    # -- preparing --------------------------------------------------------------

    def prepare(self, day: date | None = None) -> ReflectionContext:
        tree = self._goals.tree()
        if day is None:
            return self._choices(tree)
        if day > self._health.today():
            raise ValueError(f"{day} hasn't started yet")
        events = self._events(day, tree)
        tz = self._health.now().tzinfo
        start, end = period_window(day_period(day), events, tz, self._health.now())
        reflected = self._reflection(day)
        previous = self._reflection(day - timedelta(days=1))

        rated_goals = [g for g in tree.ordered() if tree.rated(g.id)]
        week = self._health.read(day - timedelta(days=7), day + timedelta(days=1))
        that_day = {a.goal_id: a for a in week if a.day == day}
        confirmed = {goal_id: a for goal_id, a in that_day.items() if a.status == "confirmed"}
        ready = [
            g for g in rated_goals
            if g.id not in confirmed and all(c.id in confirmed for c in tree.rated_children(g.id))
        ]
        # Goals without rated sub-goals first, as the conversation goes.
        ready.sort(key=lambda g: bool(tree.rated_children(g.id)))
        measured = dict(zip((g.id for g in ready), self._health.propose(day, ready, tree, confirmed)))

        due = []
        for goal in ready:
            measure = tree.measure(goal.id)
            recorded = that_day.get(goal.id)
            proposed, ask = recorded, None
            if proposed is None and measure["kind"] == "subjective":
                proposed = self._carried_over(goal.id, measure, day, week)
                if proposed is None:
                    ask = measure.get("prompt") or f"How did {goal.name!r} go?"
            due.append(
                DueGoal(
                    goal_id=goal.id,
                    path=tree.path(goal.id),
                    measure=measure,
                    target=goal.target,
                    deadline=goal.deadline,
                    note=goal.note,
                    ask=ask,
                    recent=[
                        PastRating(day=a.day, rating=a.rating)
                        for a in week if a.goal_id == goal.id and a.day < day and a.status == "confirmed"
                    ][-_RECENT_RATINGS:],
                    sub_goals=[
                        SubGoalRating(
                            goal_id=c.id,
                            path=tree.path(c.id),
                            rating=confirmed[c.id].rating,
                            explanation=confirmed[c.id].explanation,
                        )
                        for c in tree.rated_children(goal.id)
                    ],
                    proposed=proposed or measured.get(goal.id),
                )
            )

        notes = self._notes.read(include_compacted=True) if self._notes else []
        minutes = goal_minutes(events, tree, start, end)
        return ReflectionContext(
            day=day,
            starts=start,
            ends=end,
            already_reflected=not ready,
            journal=(reflected or {}).get("description") or None,
            due=due,
            rated=[a for a in confirmed.values() if a.goal_id in tree.by_id],
            waiting=[tree.path(g.id) for g in rated_goals if g.id not in confirmed and g not in ready],
            unmeasured=[tree.path(g.id) for g in tree.ordered() if g.active and not tree.rated(g.id)],
            goal_time=sorted(
                (GoalTimeSpent(goal_id=g, path=tree.path(g), minutes=m) for g, m in minutes.items() if g in tree.by_id),
                key=lambda t: (-t.minutes, t.path),
            ),
            events_digest=_events_digest(events, tree, start, end, tz),
            notes_digest=_notes_digest(notes, start, end, tz) if self._notes else None,
            previous_intentions=_intentions(previous),
            uncompacted_notes=sum(1 for n in notes if n.compaction_id is None and n.timestamp < end),
            instructions=_instructions(day),
        )

    def _carried_over(
        self, goal_id: str, measure: dict[str, Any], day: date, week: list[Assessment]
    ) -> Assessment | None:
        """A subjective goal's rating carried over to `day` from the day
        before, if it was answered within its interval -- else `None`: ask
        it."""
        interval = measure.get("interval_days", 1)
        first = day - timedelta(days=interval) + timedelta(days=1)
        if first > day:
            return None
        history = (
            [a for a in week if a.goal_id == goal_id]
            if first >= day - timedelta(days=7)
            else self._health.read(first - timedelta(days=1), day + timedelta(days=1), goal_id=goal_id)
        )
        answered = [a for a in history if first <= a.day <= day and a.method == "subjective" and not a.carried]
        if not answered:
            return None
        before = [a for a in history if a.day < day and a.status == "confirmed" and a.day >= first - timedelta(days=1)]
        source = before[-1] if before else answered[-1]
        asked = answered[-1].day
        next_asked = asked + timedelta(days=interval)
        return Assessment(
            goal_id=goal_id,
            day=day,
            rating=source.rating,
            method="subjective",
            explanation=f"Carried over from {source.day} (last asked {asked}; asked again {next_asked})",
            metrics={"carried_from": source.day.isoformat(), "asked": asked.isoformat()},
        )

    def _choices(self, tree: GoalTree) -> ReflectionContext:
        """The days a reflection could be for, when none was named: the
        most recent completed ones not fully reflected on (see `_CHOICES`),
        or the last completed one, if they all have been. The day you're in
        isn't offered: it isn't over."""
        now = self._health.now()
        tz = now.tzinfo
        completed = self._health.today() - timedelta(days=1)
        first = completed - timedelta(days=_LOOKBACK_DAYS - 1)
        created = [g.created for g in tree.goals if g.created and g.status != "deleted"]
        first = max(first, min(created)) if created else completed
        reflections = {r["day"]: r for r in self._reflections(first, completed + timedelta(days=1))}
        unreflected = []
        day = completed
        while day >= first:
            if not _complete(reflections.get(day)):
                unreflected.append(day)
            day -= timedelta(days=1)
        offered = unreflected[:_CHOICES] or [completed]

        def choice(day: date) -> DayChoice:
            listed = self._health.calendar_client.list_events(*listing_range(day_period(day), tz))
            try:
                start, end = period_window(day_period(day), [e for e in listed if e.status != "cancelled"], tz, now)
                missing = []
            except MissingSleep as exc:
                start = end = None
                missing = exc.days
            except NotOver:  # Not offered: only days before the current one are.
                raise AssertionError(f"{day} was offered before it ended") from None
            return DayChoice(
                day=day,
                starts=start,
                ends=end,
                missing_sleep=missing,
                already_reflected=_complete(reflections.get(day)),
                started=day in reflections and not _complete(reflections[day]),
            )

        return ReflectionContext(
            day=None,
            choices=[choice(day) for day in offered],
            older_unreflected=max(0, len(unreflected) - _CHOICES),
            instructions=(
                "No day was named. Ask which day to reflect on, offering these choices: completed days not "
                "fully reflected on, newest first (one that's `started` picks up where it left off) -- or, if "
                "every recent one has been, the last completed one, which rating again replaces. Mention "
                "older_unreflected if it isn't 0: any day can be named. A choice with missing_sleep can't be "
                "reflected on until the calendar has those days' end-of-day sleep: say so. Then call "
                "prepare_reflection again with the day picked."
            ),
        )

    # -- recording --------------------------------------------------------------

    def record(
        self,
        day: date,
        assessments: list[Assessment],
        journal: str | None = None,
        intentions: list[str] | None = None,
        *,
        dry_run: bool = True,
    ) -> ReflectionResult:
        if day > self._health.now().date():
            raise ValueError(f"{day} hasn't started yet")
        # Only a day that's over, whose bounding sleeps are in the calendar.
        tz = self._health.now().tzinfo
        listed = self._health.calendar_client.list_events(*listing_range(day_period(day), tz))
        period_window(day_period(day), [e for e in listed if e.status != "cancelled"], tz, self._health.now())
        tree = self._goals.tree()
        confirmed = {a.goal_id for a in self._health.read(day, day + timedelta(days=1)) if a.status == "confirmed"}
        problems = []
        seen = set()
        for a in assessments:
            if a.day != day:
                problems.append(f"{a.goal_id}: this reflection rates {day}, not {a.day}")
            if a.goal_id in seen:
                problems.append(f"{a.goal_id} is rated more than once")
            seen.add(a.goal_id)
            unrated = [c for c in tree.rated_children(a.goal_id) if c.id not in confirmed]
            if unrated:
                names = ", ".join(tree.path(c.id) for c in unrated)
                problems.append(
                    f"{tree.path(a.goal_id)} can't be rated until its sub-goals are ({names}): ratings flow up "
                    "from sub-goals, so record theirs first, then call prepare_reflection again"
                )
        if journal and len(journal.encode()) > MAX_RATIONALE_BYTES:
            problems.append(f"the journal is longer than {MAX_RATIONALE_BYTES} bytes (Calendar would cut it short)")
        intentions = [i.strip() for i in intentions or [] if i.strip()] if intentions is not None else None
        if intentions and len(intentions) > MAX_INTENTIONS:
            problems.append(f"at most {MAX_INTENTIONS} intentions")
        elif intentions and len(json.dumps(intentions)) > 1024:
            problems.append("the intentions are too long together (1024 characters as JSON)")
        if problems:
            raise ValueError("; ".join(problems))
        self._health.check(assessments)

        rated_goals = [g for g in tree.ordered() if tree.rated(g.id)]
        ready = [
            g for g in rated_goals
            if g.id not in confirmed and all(c.id in confirmed for c in tree.rated_children(g.id))
        ]
        not_rated = [tree.path(g.id) for g in ready if g.id not in seen]
        complete = all(g.id in confirmed or g.id in seen for g in rated_goals)
        preview = _preview(assessments, tree, journal, intentions or [], not_rated)
        if dry_run:
            return ReflectionResult(
                status="preview",
                day=day,
                preview=preview,
                assessments=assessments,
                not_rated=not_rated,
                complete=complete,
                message=(
                    "Nothing has been recorded yet. Show the user the preview; once they agree, call "
                    "record_reflection again with the same arguments and dry_run=False."
                ),
            )
        written = self._health.confirm_assessments(assessments)
        self._write_reflection(day, journal, intentions, complete)
        now_confirmed = confirmed | seen
        next_ready = [
            tree.path(g.id) for g in rated_goals
            if g.id not in now_confirmed and all(c.id in now_confirmed for c in tree.rated_children(g.id))
        ]
        if complete:
            message = f"Recorded {len(written)} rating(s): every goal is rated for {day}."
        elif next_ready:
            message = (
                f"Recorded {len(written)} rating(s). Ready to rate now: {', '.join(next_ready)}. Call "
                "prepare_reflection again for them."
            )
        else:
            message = f"Recorded {len(written)} rating(s). Still to rate: {', '.join(not_rated)}."
        return ReflectionResult(
            status="recorded",
            day=day,
            preview=preview,
            assessments=written,
            not_rated=not_rated,
            complete=complete,
            message=message,
        )

    def _write_reflection(self, day: date, journal: str | None, intentions: list[str] | None, complete: bool) -> None:
        """Upsert the day's reflection event, keeping its journal and
        intentions unless new ones are given."""
        existing = self._reflection(day) or {}
        calendar = self._health.health_calendar()
        properties = {
            "kind": "reflection",
            "cadence": CADENCE,
            "period": day.isoformat(),
            "intentions": json.dumps(intentions) if intentions is not None else existing.get("intentions", "[]"),
            "complete": "true" if complete else "false",
            "reflected": self._health.now().isoformat(),
            "schema": "2",
        }
        description = journal if journal is not None else existing.get("description") or ""
        body = {
            "summary": f"📝 Reflection · {day.isoformat()}" + ("" if complete else " (in progress)"),
            "description": description,
            "start": {"date": day.isoformat()},
            "end": {"date": (day + timedelta(days=1)).isoformat()},
            "transparency": "transparent",
            "extendedProperties": {"private": {f"{_PREFIX}{k}": v for k, v in properties.items()}},
        }
        response = calendar.upsert_event_resource(reflection_event_id(day), body)
        if len(response.get("description", "")) < len(body["description"]):
            raise ValueError(f"Calendar shortened the journal; keep it under {MAX_RATIONALE_BYTES} bytes")

    # -- reading ------------------------------------------------------------------

    def _reflections(self, first: date, end: date) -> list[dict]:
        """The daily reflection events from `first` up to `end`
        (exclusive), as dicts with their properties unprefixed, plus the
        description and their `day`."""
        calendar = self._health.health_calendar(create=False)
        if calendar is None:
            return []
        tz = self._health.now().tzinfo
        items = calendar.list_event_resources(
            datetime.combine(first, time(), tz),
            datetime.combine(end, time(), tz),
            private_property=f"{_PREFIX}kind=reflection",
        )
        found = []
        for item in items:
            if item.get("status") == "cancelled":
                continue
            properties = {
                key.removeprefix(_PREFIX): value
                for key, value in item.get("extendedProperties", {}).get("private", {}).items()
            }
            if properties.get("cadence") != CADENCE:
                continue
            try:
                day = date.fromisoformat(properties.get("period", ""))
            except ValueError:
                continue
            found.append({**properties, "description": item.get("description"), "day": day})
        return found

    def _reflection(self, day: date) -> dict | None:
        return next((r for r in self._reflections(day, day + timedelta(days=1)) if r["day"] == day), None)

    def _events(self, day: date, tree: GoalTree) -> list[Event]:
        """The events around `day`, with the sleeps that bound it -- see
        utilities/sleep_days.py."""
        listed = self._health.calendar_client.list_events(*listing_range(day_period(day), self._health.now().tzinfo))
        return [e for e in fill_in_from_goals(listed, tree) if e.status != "cancelled"]


# -- helpers --------------------------------------------------------------------


def _complete(reflection: dict | None) -> bool:
    """A reflection event recorded with every rated goal rated (or from
    before reflections went a level at a time, when one always was)."""
    return reflection is not None and reflection.get("complete") != "false"


def _intentions(reflection: dict | None) -> list[str]:
    if not reflection or not reflection.get("intentions"):
        return []
    try:
        return [str(i) for i in json.loads(reflection["intentions"])]
    except ValueError:
        return []


def _events_digest(events: list[Event], tree: GoalTree, start: datetime, end: datetime, tz) -> str:
    lines = []
    for event in sorted(events, key=lambda e: e.start):
        if event.end <= start or event.start >= end:
            continue
        local = event.start.astimezone(tz)
        goals = ", ".join(tree.by_id[g].name for g in event.goal_ids or () if g in tree.by_id)
        lines.append(
            f"{local:%a %H:%M}-{event.end.astimezone(tz):%H:%M} {event.summary or '(no title)'}"
            + (f" [{goals}]" if goals else "")
        )
    return "\n".join(lines) or "(no events)"


def _notes_digest(notes, start: datetime, end: datetime, tz) -> str:
    lines = [
        f"{n.timestamp.astimezone(tz):%a %H:%M} {n.description}"
        for n in sorted(notes, key=lambda n: n.timestamp)
        if start <= n.timestamp < end and n.description
    ]
    return "\n".join(lines) or "(no notes)"


def _preview(
    assessments: list[Assessment], tree: GoalTree, journal: str | None, intentions: list[str], not_rated: list[str]
) -> str:
    lines = []
    for a in assessments:
        name = tree.path(a.goal_id) if a.goal_id in tree.by_id else a.goal_id
        rating = "skipped" if a.rating == "skip" else str(a.rating)
        why = " — ".join(part for part in (a.explanation, a.rationale) if part)
        lines.append(f"{band(a.rating)} {name}: {rating}" + (f" — {why}" if why else ""))
    for path in not_rated:
        lines.append(f"· {path}: not rated yet")
    if journal:
        lines.append(f"Journal: {journal}")
    for intention in intentions:
        lines.append(f"Intention: {intention}")
    return "\n".join(lines) or "(nothing to record)"


def _instructions(day: date) -> str:
    return "\n".join(
        [
            f"This is the daily reflection for {day}. Ratings are 0-100 (0-39 red, 40-69 yellow, 70-100 "
            "green), or \"skip\".",
            "If uncompacted_notes > 0, say the calendar may not reflect them yet, and offer to compact "
            "notes first.",
            "Ratings flow up from sub-goals to their parents, so the day is rated a level at a time: `due` "
            "holds only the goals ready now, and `waiting` the ones to rate once those are.",
            "1. Open with the due goals that have a `proposed` rating (measured, rolled up from sub-goals, "
            "or carried over): one line each with its band, rating and explanation. Ask for agreement in "
            "bulk; change only what the user objects to, recording their reason as the rationale.",
            "2. For each due goal with `ask`, ask it, one goal at a time, for a 0-100 number. Accept "
            "\"skip\"; turn words like \"pretty good\" into a number and confirm it.",
            "3. For each due goal with an llm measure, propose a rating with a one-sentence rationale "
            "grounded in its rubric, its sub_goals' ratings, events_digest, notes_digest and goal_time, and "
            "ask for confirmation.",
            "4. Call record_reflection with this level's ratings (method: metric, rollup, subjective or "
            "llm; keep a proposed rating's explanation and metrics). Once the user has agreed to the "
            "ratings shown, call it with dry_run=False: it confirms them straight away, so an interrupted "
            "reflection resumes where it stopped.",
            "5. If `waiting` isn't empty, call prepare_reflection again for this day and repeat from 1 "
            "for the next level.",
            "6. Once every goal is rated: if there are previous_intentions, ask whether they happened. "
            f"Ask, optionally, for a short journal entry and up to {MAX_INTENTIONS} intentions for "
            "tomorrow, and record them with record_reflection (assessments may be empty). Keep it brief: "
            "a daily reflection should take a couple of minutes.",
        ]
    )
