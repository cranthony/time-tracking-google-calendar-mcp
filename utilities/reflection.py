"""Reflections: a conversation, run by the MCP client, that rates every
goal for one day -- from waking on it to waking the next (see
utilities/sleep_days.py). Every rated goal (see utilities/goals.py's
GoalTree.rated) is reflected on daily; there are no other cadences.

**Filled in automatically.** Everything the calendar can answer is
rated without asking: measured goals (utilities/goal_health.py), skips
for want of an event an `only_if` needs, subjective ratings carried over
between askings, and rollups of sub-goals. Only two kinds of goal need
judgement -- the **questions**: an llm goal, which the model rates
against its rubric (asking the user only when the day's events and
notes don't say), and a subjective goal whose prompt is due. They're all
asked at once.

**Bottom up.** A rating only ever flows up the tree, from sub-goals to
their parents, so a goal above an unanswered question has no final
rating yet. Until the answers are in, its rating is **provisional**:
rolled up from the sub-goals that have one (an llm rating the model only
proposed counts as given), leaving out the unanswered ones. `record`
with `dry_run` shows that provisional summary; recording the answers
makes it final, and confirms every final rating on the Goal Health
calendar in one write. Provisional ratings are never written. A
reflection cut short picks up where it left off: nothing confirmed is
asked again.

Calendar-derived ratings are worked out afresh each time, so a
correction to a sub-goal rolls up into its parents; a rating someone
judged -- a subjective or llm one, or one changed by hand (with a
rationale, or "Changed from ...") -- is kept as recorded.

**The summary** lists the top-level goals and every goal given its own
priority, grouped by priority (0 first; a top-level goal without one is
2) and best rated first within each. Each line folds in the sub-goals
that don't have a line of their own, naming the lowest of them.

**Subjective goals** are asked their prompt only once their
`interval_days` have passed since it was last answered (in a reflection,
or given in passing with `record_assessments`); on the days between, the
previous day's rating carries over.

**Only if.** A goal whose measure's `only_if` isn't met that day (see
utilities/goal_measures.py) is skipped instead, whatever its kind: its
prompt isn't asked, nor its rubric judged. Those skips are neither
answers to a subjective prompt nor carried over, so the interval passes
over them.

`record` also notes, in the same Goal Health day event as the day's
assessments (see utilities/health_days.py), whether every rated goal has
a final rating ("complete"). Recording is the only way an assessment
becomes `confirmed` (utilities/goal_health.py's `confirm_assessments`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from calendar_clients.google_calendar import Event
from utilities.goal_calendar import fill_in_from_goals
from utilities.goal_health import (
    Assessment,
    DayReflection,
    GoalHealth,
    band,
    day_period,
    explanation_of,
    roll_up,
)
from utilities.goal_time import goal_minutes
from utilities.goals import OVERALL_ID, Goal, Goals, GoalTree
from utilities.noted_time_sheet import NotedTimeSheet
from utilities.sleep_days import MissingSleep, NotOver, listing_range, period_window

_LOOKBACK_DAYS = 12
"""How far back `prepare_reflection` looks for a day still to reflect on."""

_CHOICES = 3
"""How many completed, unreflected days a reflection offers, newest first,
when none is named."""

_RECENT_RATINGS = 6

DEFAULT_PRIORITY = 2
"""The summary's priority for a top-level goal without one -- as for an
event (calendar_clients/google_calendar.py's color_for_priority)."""

_DETAILS = 3
"""How many folded-in sub-goals a summary line names."""

_TREND = 10
"""How far (in points) a line's rating must move since the day before to
be marked with an arrow."""


@dataclass(kw_only=True)
class PastRating:
    day: date
    rating: int | Literal["skip"]


@dataclass(kw_only=True)
class SubGoalRating:
    """An immediate sub-goal's rating of the day."""

    goal_id: str
    path: str
    rating: int | Literal["skip"] | None
    """`None` while it waits on an answer."""

    explanation: str | None = None
    provisional: bool = False
    """Not final yet: it waits on answers further down."""


@dataclass(kw_only=True)
class Question:
    """A goal that needs judgement: an llm goal to rate, or a subjective
    goal whose prompt is due."""

    goal_id: str
    path: str
    kind: Literal["llm", "subjective"]
    prompt: str | None = None
    """A subjective goal's question for the user."""

    rubric: str | None = None
    """What an llm goal is rated against."""

    note: str | None = None
    recent: list[PastRating] = field(default_factory=list)
    """Its last confirmed ratings before this day."""

    sub_goals: list[SubGoalRating] = field(default_factory=list)
    """Its rated sub-goals' ratings of this day, for a rubric that refers
    to them."""


@dataclass(kw_only=True)
class SummaryLine:
    """One line of a reflection's summary: a top-level goal, or one with
    its own priority, folding in the sub-goals without a line of their
    own."""

    goal_id: str
    name: str
    priority: int
    rating: int | Literal["skip"] | None
    state: Literal["final", "provisional", "waiting", "unmeasured"]
    """`provisional`: rated, but it waits on answers further down, which
    are left out. `waiting`: nothing to rate it by until they're in.
    `unmeasured`: it has no measure, and no rated sub-goals."""

    change: int | None = None
    """Since its confirmed rating of the day before."""

    detail: str | None = None
    """How it was rated, or its lowest-rated folded-in sub-goals."""

    text: str = ""


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
    """Every rated goal has a final rating of this day already."""

    choices: list[DayChoice] = field(default_factory=list)
    """When no day was named: the days to ask about -- the most recent
    completed ones not fully reflected on, or, if every one has been, the
    last completed one -- and nothing else is filled in."""

    older_unreflected: int = 0
    """Days not reflected on before `choices`, within the last 12, not
    offered."""

    questions: list[Question] = field(default_factory=list)
    """The goals that need judgement; everything else is rated
    automatically."""

    unmeasured: list[str] = field(default_factory=list)
    """Paths of active goals that aren't rated: they have no measure and
    no rated sub-goals."""

    goal_time: list[GoalTimeSpent] = field(default_factory=list)
    events_digest: str | None = None
    """The day's events, with their goals."""

    notes_digest: str | None = None
    """The day's compacted notes."""

    uncompacted_notes: int = 0
    """Notes up to the day's end that haven't been compacted: until they
    are, the calendar (and so the measured ratings) may be off."""

    instructions: str = ""


@dataclass(kw_only=True)
class ReflectionResult:
    status: Literal["preview", "recorded"]
    day: date
    summary: str
    """The summary to show, as is: see `lines`."""

    overall: int | Literal["skip"] | None = None
    overall_provisional: bool = False
    lines: list[SummaryLine] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    """Those still to answer."""

    rated: list[Assessment] = field(default_factory=list)
    """Every final rating of the day, parents before their sub-goals --
    for showing them all, if asked."""

    recorded: list[Assessment] = field(default_factory=list)
    """The ratings written by this call (none in a preview)."""

    complete: bool = False
    """Every rated goal has a final rating of the day."""

    message: str = ""


@dataclass
class _Rated:
    assessment: Assessment | None
    """`None` while it waits on an answer (or can't be rated)."""

    final: bool


@dataclass
class _Evaluation:
    results: dict[str, _Rated]
    """Every rated goal's rating of the day, by id."""

    questions: list[Question]
    existing: dict[str, Assessment]
    """The day's confirmed ratings before this, by goal."""

    previous: dict[str, Assessment]
    """The day before's confirmed ratings, by goal."""

    reflection: DayReflection | None

    @property
    def complete(self) -> bool:
        return all(r.final for r in self.results.values())


class Reflections:
    def __init__(self, health: GoalHealth, goals: Goals, notes: NotedTimeSheet | None = None) -> None:
        self._health = health
        self._goals = goals
        self._notes = notes

    # -- preparing --------------------------------------------------------------

    def prepare(self, day: date | None = None) -> ReflectionContext:
        # The goals and the day's notes, in one read request (see
        # `SheetsClient.prefetch`).
        self._goals.prefetch([self._goals.whole_tab, self._notes.whole_tab])
        tree = self._goals.tree()
        if day is None:
            return self._choices(tree)
        if day > self._health.today():
            raise ValueError(f"{day} hasn't started yet")
        events = self._events(day, tree)
        tz = self._health.now().tzinfo
        start, end = period_window(day_period(day), events, tz, self._health.now())
        evaluation = self._evaluate(day, tree, {}, set())
        notes = self._notes.read(include_compacted=True) if self._notes else []
        minutes = goal_minutes(events, tree, start, end)
        return ReflectionContext(
            day=day,
            starts=start,
            ends=end,
            already_reflected=bool(evaluation.reflection and evaluation.reflection.complete),
            questions=evaluation.questions,
            unmeasured=[
                tree.path(g.id) for g in tree.ordered() if g.active and not tree.rated(g.id) and g.id != OVERALL_ID
            ],
            goal_time=sorted(
                (GoalTimeSpent(goal_id=g, path=tree.path(g), minutes=m) for g, m in minutes.items() if g in tree.by_id),
                key=lambda t: (-t.minutes, t.path),
            ),
            events_digest=_events_digest(events, tree, start, end, tz),
            notes_digest=_notes_digest(notes, start, end, tz) if self._notes else None,
            uncompacted_notes=sum(1 for n in notes if n.compaction_id is None and n.timestamp < end),
            instructions=_instructions(day),
        )

    def _evaluate(
        self, day: date, tree: GoalTree, given: dict[str, Assessment], proposed: set[str]
    ) -> _Evaluation:
        """Every rated goal's rating of `day`: `given` ones (final, unless
        in `proposed`), judged ones confirmed earlier, and the rest worked
        out -- see the module docstring."""
        days = self._health.read_days(day - timedelta(days=7), day + timedelta(days=1))
        week = sorted((a for d in days.values() for a in d.assessments.values()), key=lambda a: (a.goal_id, a.day))
        existing = {a.goal_id: a for a in week if a.day == day and a.status == "confirmed"}
        previous = {
            a.goal_id: a for a in week if a.day == day - timedelta(days=1) and a.status == "confirmed"
        }
        rated = [g for g in tree.ordered() if tree.rated(g.id)]
        kept = {goal_id: a for goal_id, a in existing.items() if _judged(a) and goal_id in tree.by_id}
        to_measure = [g for g in rated if g.id not in given and g.id not in kept]
        measured = dict(zip((g.id for g in to_measure), self._health.propose(day, to_measure, tree, {})))

        results: dict[str, _Rated] = {}
        # Sub-goals before their parents: `ordered` lists parents first.
        for goal in reversed(rated):
            measure = tree.measure(goal.id) or {}
            kind = measure.get("kind")
            if goal.id in given:
                results[goal.id] = _Rated(given[goal.id], final=goal.id not in proposed)
            elif goal.id in kept:
                results[goal.id] = _Rated(kept[goal.id], final=True)
            elif (found := measured[goal.id]) is not None and (found.unmet or kind != "rollup"):
                results[goal.id] = _Rated(found, final=True)
            elif kind == "rollup":
                children = [results[c.id] for c in tree.rated_children(goal.id)]
                ratings = {r.assessment.goal_id: r.assessment for r in children if r.assessment is not None}
                rolled = roll_up(goal, tree, ratings, day)
                results[goal.id] = _Rated(rolled, final=rolled is not None and all(r.final for r in children))
            elif kind == "subjective":
                carried = self._carried_over(goal.id, measure, day, week)
                results[goal.id] = _Rated(carried, final=carried is not None)
            elif kind == "llm":
                results[goal.id] = _Rated(None, final=False)
            else:  # A measure the calendar couldn't answer (a bad spec): left out.
                results[goal.id] = _Rated(None, final=True)

        questions = []
        for goal in rated:
            measure = tree.measure(goal.id) or {}
            kind = measure.get("kind")
            if results[goal.id].assessment is not None or kind not in ("llm", "subjective"):
                continue
            questions.append(
                Question(
                    goal_id=goal.id,
                    path=tree.path(goal.id),
                    kind=kind,
                    prompt=(measure.get("prompt") or f"How did {goal.name!r} go?") if kind == "subjective" else None,
                    rubric=measure.get("rubric") if kind == "llm" else None,
                    note=goal.note,
                    recent=[
                        PastRating(day=a.day, rating=a.rating)
                        for a in week if a.goal_id == goal.id and a.day < day and a.status == "confirmed"
                    ][-_RECENT_RATINGS:],
                    sub_goals=[_sub_goal_rating(c, tree, results[c.id]) for c in tree.rated_children(goal.id)],
                )
            )
        reflection = days[day].reflection if day in days else None
        return _Evaluation(results, questions, existing, previous, reflection)

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
        # Skips for want of an event its only_if needs don't count.
        history = [a for a in history if not a.unmet]
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
        reflections = {
            d: r for d, health_day in self._health.read_days(first, completed + timedelta(days=1)).items()
            if (r := health_day.reflection) is not None
        }
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
        proposed: list[str] | None = None,
        *,
        dry_run: bool = True,
    ) -> ReflectionResult:
        """See the module docstring: `assessments` are the questions'
        answers (and any rating being changed); with `dry_run`, those in
        `proposed` count as provisional."""
        if day > self._health.now().date():
            raise ValueError(f"{day} hasn't started yet")
        # Only a day that's over, whose bounding sleeps are in the calendar.
        tz = self._health.now().tzinfo
        listed = self._health.calendar_client.list_events(*listing_range(day_period(day), tz))
        period_window(day_period(day), [e for e in listed if e.status != "cancelled"], tz, self._health.now())
        tree = self._goals.tree()
        problems = []
        given: dict[str, Assessment] = {}
        for a in assessments:
            if a.day != day:
                problems.append(f"{a.goal_id}: this reflection rates {day}, not {a.day}")
            if a.goal_id in given:
                problems.append(f"{a.goal_id} is rated more than once")
            given[a.goal_id] = a
        unknown = sorted(set(proposed or ()) - set(given))
        if unknown:
            problems.append(f"proposed names goals not rated in this call: {', '.join(unknown)}")
        if problems:
            raise ValueError("; ".join(problems))
        self._health.check(assessments)

        evaluation = self._evaluate(day, tree, given, set(proposed or ()) if dry_run else set())
        lines, summary = _summary(day, tree, evaluation)
        overall = evaluation.results.get(OVERALL_ID)
        result = ReflectionResult(
            status="preview",
            day=day,
            summary=summary,
            overall=overall.assessment.rating if overall and overall.assessment else None,
            overall_provisional=bool(overall and not overall.final),
            lines=lines,
            questions=evaluation.questions,
            rated=[
                r.assessment for g in tree.ordered()
                if (r := evaluation.results.get(g.id)) is not None and r.final and r.assessment is not None
            ],
            complete=evaluation.complete,
        )
        if dry_run:
            result.message = (
                "Nothing has been recorded yet. Show the summary and ask the questions; record the answers "
                "with dry_run=False."
            )
            return result

        to_write = [
            r.assessment
            for goal_id, r in evaluation.results.items()
            if r.final and r.assessment is not None
            and (goal_id in given or _differs(r.assessment, evaluation.existing.get(goal_id)))
        ]
        now = self._health.now()
        complete = evaluation.complete

        def reflect(existing: DayReflection | None) -> DayReflection:
            """The day's reflection, keeping any journal and intentions
            recorded with it before."""
            return DayReflection(
                journal=existing.journal if existing else None,
                intentions=existing.intentions if existing else [],
                complete=complete,
                reflected=now,
            )

        result.recorded = self._health.write_day(day, to_write, status="confirmed", reflect=reflect)
        result.status = "recorded"
        waiting = len(evaluation.questions)
        result.message = (
            f"Recorded {len(result.recorded)} rating(s): every goal is rated for {day}."
            if complete
            else f"Recorded {len(result.recorded)} rating(s); {waiting} question(s) still to answer."
        )
        return result

    # -- reading ------------------------------------------------------------------

    def _events(self, day: date, tree: GoalTree) -> list[Event]:
        """The events around `day`, with the sleeps that bound it -- see
        utilities/sleep_days.py."""
        listed = self._health.calendar_client.list_events(*listing_range(day_period(day), self._health.now().tzinfo))
        return [e for e in fill_in_from_goals(listed, tree) if e.status != "cancelled"]


# -- helpers --------------------------------------------------------------------


def _complete(reflection: DayReflection | None) -> bool:
    """A reflection recorded with every rated goal rated."""
    return reflection is not None and reflection.complete


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


def _sub_goal_rating(goal: Goal, tree: GoalTree, rated: _Rated) -> SubGoalRating:
    a = rated.assessment
    return SubGoalRating(
        goal_id=goal.id,
        path=tree.path(goal.id),
        rating=a.rating if a else None,
        explanation=explanation_of(a) if a else None,
        provisional=a is not None and not rated.final,
    )


def _judged(assessment: Assessment) -> bool:
    """Whether someone judged the rating, so it's kept as recorded rather
    than worked out again: a subjective or llm rating (not carried over,
    nor skipped for want of an event), or one changed by hand."""
    if assessment.carried or assessment.unmet:
        return False
    return (
        assessment.method in ("subjective", "llm")
        or bool(assessment.rationale)
        or (assessment.explanation or "").startswith("Changed from")
    )


def _differs(assessment: Assessment, recorded: Assessment | None) -> bool:
    """Whether writing `assessment` would change what's `recorded`."""
    if recorded is None:
        return True
    return (
        assessment.rating, assessment.method, explanation_of(assessment), assessment.metrics, assessment.rationale
    ) != (recorded.rating, recorded.method, recorded.explanation, recorded.metrics, recorded.rationale)


def _summary(day: date, tree: GoalTree, evaluation: _Evaluation) -> tuple[list[SummaryLine], str]:
    """The summary's lines, and its text -- see the module docstring."""
    results = evaluation.results
    lined = [
        g for g in tree.ordered()
        if g.id != OVERALL_ID and g.active and (not g.parent_id or g.priority is not None)
    ]
    lined_ids = {g.id for g in lined}
    order = {g.id: i for i, g in enumerate(tree.ordered())}
    names: dict[str, int] = {}
    for g in tree.goals:
        names[g.name] = names.get(g.name, 0) + 1

    def name(goal: Goal) -> str:
        """Its name, after its parent's if another goal has it too (say,
        "Visit")."""
        parent = tree.by_id.get(goal.parent_id) if goal.parent_id else None
        return f"{parent.name} › {goal.name}" if names[goal.name] > 1 and parent else goal.name

    def folded(goal_id: str) -> list[Goal]:
        """The rated goals under `goal_id` without a line of their own,
        down to those rated by a measure of their own."""
        found = []
        for child in tree.rated_children(goal_id):
            if child.id in lined_ids:
                continue
            assessment = results[child.id].assessment
            if assessment is not None and assessment.method == "rollup":
                found += folded(child.id)
            else:
                found.append(child)
        return found

    def detail(goal: Goal) -> str | None:
        assessment = results[goal.id].assessment
        if assessment is not None and assessment.method != "rollup":
            return explanation_of(assessment) or assessment.rationale
        parts = []
        for child in folded(goal.id):
            r = results[child.id]
            a = r.assessment
            if a is None:
                parts.append((-1, order[child.id], f"{name(child)} ?"))
            elif isinstance(a.rating, int):
                parts.append((a.rating, order[child.id], f"{name(child)} {'' if r.final else '~'}{a.rating}"))
        return " · ".join(text for _, _, text in sorted(parts)[:_DETAILS]) or None

    lines = []
    for goal in lined:
        priority = goal.priority if goal.priority is not None else DEFAULT_PRIORITY
        if not tree.rated(goal.id) or (results[goal.id].assessment is None and results[goal.id].final):
            lines.append(
                SummaryLine(
                    goal_id=goal.id, name=goal.name, priority=priority, rating=None, state="unmeasured",
                    text=f"⚪ **{goal.name}**: not measured",
                )
            )
            continue
        r = results[goal.id]
        rating = r.assessment.rating if r.assessment else None
        state = "waiting" if rating is None else "final" if r.final else "provisional"
        before = evaluation.previous.get(goal.id)
        change = rating - before.rating if isinstance(rating, int) and before and isinstance(before.rating, int) else None
        why = detail(goal)
        if rating is None:
            text = f"⏳ **{goal.name}**: waiting on your answers"
        elif rating == "skip":
            text = f"⚪ **{goal.name}** skipped"
        else:
            arrow = (f" ↑{change}" if change > 0 else f" ↓{-change}") if change and abs(change) >= _TREND else ""
            text = f"{band(rating)} **{goal.name} {'~' if state == 'provisional' else ''}{rating}**{arrow}"
        if why and rating is not None:
            text += f": {why}"
        lines.append(
            SummaryLine(
                goal_id=goal.id, name=goal.name, priority=priority, rating=rating, state=state, change=change,
                detail=why, text=text,
            )
        )

    def rank(line: SummaryLine) -> tuple:
        group = 0 if isinstance(line.rating, int) else 1 if line.rating == "skip" else 2 if line.state != "unmeasured" else 3
        return (line.priority, group, -line.rating if isinstance(line.rating, int) else 0, order[line.goal_id])

    lines.sort(key=rank)
    overall = results.get(OVERALL_ID)
    rating = overall.assessment.rating if overall and overall.assessment else None
    # Unanswered questions, and llm ratings only proposed so far.
    waiting = len(evaluation.questions) + sum(
        1 for goal_id, r in results.items() if not r.final and r.assessment is not None and r.assessment.method == "llm"
    )
    head = f"**{day:%a %b} {day.day} · Overall"
    if rating is None:
        head += " — waiting on your answers**"
    else:
        head += f" {'' if overall.final else '~'}{rating} {band(rating)}**"
    if waiting:
        head += f" ({waiting} answer{'s' if waiting != 1 else ''} to go)"
    text = [head]
    for priority in sorted({line.priority for line in lines}):
        text += ["", f"**Priority {priority}**"] + [line.text for line in lines if line.priority == priority]
    return lines, "\n".join(text)


def _instructions(day: date) -> str:
    return "\n".join(
        [
            f"This is the daily reflection for {day}. Ratings are 0-100 (0-39 red, 40-69 yellow, 70-100 "
            "green), or \"skip\". Everything the calendar can rate is filled in automatically; `questions` "
            "holds only the goals that need judgement. Keep it brief.",
            "If uncompacted_notes > 0, say the calendar may not reflect them yet, and offer to compact "
            "notes first.",
            "1. For each llm question, rate it yourself against its rubric, from its sub_goals, "
            "events_digest, notes_digest and goal_time, with a one-sentence rationale. Only when they don't "
            "tell you what the rubric needs, ask the user too: list its goal_id in `proposed`.",
            "2. Call record_reflection with your llm ratings (method llm), `proposed` and dry_run=True. "
            "Show its `summary` exactly as given. If there's nothing to ask (no subjective questions, "
            "nothing proposed), call it with dry_run=False instead, and show that summary.",
            "3. Below the summary, ask every question at once, as one numbered list, for the user to "
            "answer together: each proposed llm rating (your rating and why: OK, or theirs?) and each "
            "subjective question's prompt. Accept \"skip\"; turn words into a 0-100 number.",
            "4. Call record_reflection with all the answers -- the llm ratings, and the subjective ones "
            "(method subjective, anything they said as the rationale) -- and dry_run=False. Then show "
            "only the summary lines whose rating changed from the first summary, as \"Name old → new\", "
            "and the final Overall.",
            "5. If the user wants a rating changed, record it the same way with their reason as the "
            "rationale: its parents roll up again.",
        ]
    )
