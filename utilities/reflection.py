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
that don't have a line of their own, naming the lowest of them (see
utilities/health_summary.py).

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
from utilities.goal_details import WHAT_MATTERS, GoalDetails, WhatMatters, add_to_section, checked_what_matters, section
from utilities.goal_measures import expired_weights
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
from utilities.health_summary import Rated, SummaryLine, grouped, summary_lines
from utilities.noted_time_sheet import NotedTimeSheet
from utilities.sleep_days import MissingSleep, NotOver, listing_range, period_window
from utilities.trait_scores import TraitScore

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
    """An immediate sub-goal's rating of the day."""

    goal_id: str
    path: str
    rating: int | Literal["skip"] | None
    """`None` while it waits on an answer."""

    explanation: str | None = None
    provisional: bool = False
    """Not final yet: it waits on answers further down."""


@dataclass(kw_only=True)
class JudgmentDue:
    """A traits goal's judgment part, to score 0-100 against its rubric."""

    trait_id: str
    trait: str
    """The trait's name."""

    part: str
    """The part's key within the trait (usually "judgment")."""

    rubric: str | None = None


@dataclass(kw_only=True)
class TraitJudgment:
    """A score given a traits goal's judgment part, in a reflection."""

    goal_id: str
    trait_id: str
    part: str = "judgment"
    score: int
    """0-100."""


@dataclass(kw_only=True)
class Question:
    """A goal that needs judgement: an llm goal to rate, a subjective goal
    whose prompt is due, or a goal rated by traits, whose proposed rating
    is to be confirmed (after making its judgments)."""

    goal_id: str
    path: str
    kind: Literal["llm", "subjective", "traits"]
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

    proposed_rating: int | Literal["skip"] | None = None
    """A traits goal's rating from its traits, with the judgments given so
    far (those still to make left out)."""

    explanation: str | None = None
    """How `proposed_rating` was reached, trait by trait."""

    traits: list[TraitScore] = field(default_factory=list)
    """A traits goal's traits: each one's score and parts, how each part
    was reached, and the events behind it."""

    judgments: list[JudgmentDue] = field(default_factory=list)
    """A traits goal's judgment parts still to score."""

    what_matters: str | None = None
    """A traits goal's "What matters to them" section, for its judgments."""


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
class ExpiredWeight:
    """A temporary weight in a weighted rollup whose `until` has come: the
    sub-goal now weighs `then` in `goal_id`'s rollup -- see
    utilities/goal_measures.py."""

    goal_id: str
    goal_path: str
    sub_goal_id: str
    sub_goal_path: str
    weight: float
    until: str
    then: float


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

    expired_weights: list[ExpiredWeight] = field(default_factory=list)
    """Temporary rollup weights whose date has come by the day: each is
    listed until it's extended or replaced by a plain number."""

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
class _Evaluation:
    results: dict[str, Rated]
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
    def __init__(
        self, health: GoalHealth, goals: Goals, notes: NotedTimeSheet | None = None,
        details: GoalDetails | None = None,
    ) -> None:
        """`details` holds goals' descriptions, whose "What matters to
        them" sections traits goals' judgments read and a reflection can
        add to."""
        self._health = health
        self._goals = goals
        self._notes = notes
        self._details = details

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
            expired_weights=_expired_weights(tree, day),
            instructions=_instructions(day),
        )

    def _evaluate(
        self,
        day: date,
        tree: GoalTree,
        given: dict[str, Assessment],
        proposed: set[str],
        judgments: dict[str, dict[str, dict[str, int]]] | None = None,
        *,
        confirm_traits: bool = False,
    ) -> _Evaluation:
        """Every rated goal's rating of `day`: `given` ones (final, unless
        in `proposed`), judged ones confirmed earlier, and the rest worked
        out -- see the module docstring. A traits goal's rating takes its
        judgment parts from `judgments` (goal id -> trait id -> part key ->
        score), and is final only if `confirm_traits` and none is still to
        make."""
        days = self._health.read_days(day - timedelta(days=7), day + timedelta(days=1))
        week = sorted((a for d in days.values() for a in d.assessments.values()), key=lambda a: (a.goal_id, a.day))
        existing = {a.goal_id: a for a in week if a.day == day and a.status == "confirmed"}
        previous = {
            a.goal_id: a for a in week if a.day == day - timedelta(days=1) and a.status == "confirmed"
        }
        rated = [g for g in tree.ordered() if tree.rated(g.id)]
        kept = {goal_id: a for goal_id, a in existing.items() if _judged(a) and goal_id in tree.by_id}
        to_measure = [g for g in rated if g.id not in given and g.id not in kept]
        breakdowns: dict[str, Any] = {}
        measured = dict(
            zip(
                (g.id for g in to_measure),
                self._health.propose(day, to_measure, tree, {}, judgments=judgments, breakdowns=breakdowns),
            )
        )
        _check_judgments(judgments or {}, breakdowns, given, kept)

        results: dict[str, Rated] = {}
        # Sub-goals before their parents: `ordered` lists parents first.
        for goal in reversed(rated):
            measure = tree.measure(goal.id) or {}
            kind = measure.get("kind")
            if goal.id in given:
                results[goal.id] = Rated(given[goal.id], final=goal.id not in proposed)
            elif goal.id in kept:
                results[goal.id] = Rated(kept[goal.id], final=True)
            elif kind == "traits" and goal.id in breakdowns and not measured[goal.id].unmet:
                due = breakdowns[goal.id].judgments_due
                results[goal.id] = Rated(measured[goal.id], final=confirm_traits and not due)
            elif (found := measured[goal.id]) is not None and (found.unmet or kind != "rollup"):
                results[goal.id] = Rated(found, final=True)
            elif kind == "rollup":
                children = [results[c.id] for c in tree.rated_children(goal.id)]
                ratings = {r.assessment.goal_id: r.assessment for r in children if r.assessment is not None}
                rolled = roll_up(goal, tree, ratings, day)
                results[goal.id] = Rated(rolled, final=rolled is not None and all(r.final for r in children))
            elif kind == "subjective":
                carried = self._carried_over(goal.id, measure, day, week)
                results[goal.id] = Rated(carried, final=carried is not None)
            elif kind == "llm":
                results[goal.id] = Rated(None, final=False)
            else:  # A measure the calendar couldn't answer (a bad spec): left out.
                results[goal.id] = Rated(None, final=True)

        questions = []
        for goal in rated:
            measure = tree.measure(goal.id) or {}
            kind = measure.get("kind")
            if kind == "traits" and goal.id in breakdowns and not results[goal.id].final:
                questions.append(self._traits_question(goal, tree, breakdowns[goal.id], week, day))
                continue
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

    def _traits_question(self, goal: Goal, tree: GoalTree, rated, week: list[Assessment], day: date) -> Question:
        """A traits goal's question: its proposed rating, part by part, and
        the judgments still to make."""
        description = self._details.get(goal.id) if self._details is not None else None
        return Question(
            goal_id=goal.id,
            path=tree.path(goal.id),
            kind="traits",
            note=goal.note,
            recent=[
                PastRating(day=a.day, rating=a.rating)
                for a in week if a.goal_id == goal.id and a.day < day and a.status == "confirmed"
            ][-_RECENT_RATINGS:],
            proposed_rating=rated.rating,
            explanation=rated.explanation,
            traits=rated.traits,
            judgments=[
                JudgmentDue(
                    trait_id=trait_id,
                    trait=next(t.name for t in rated.traits if t.trait_id == trait_id),
                    part=part.key,
                    rubric=part.rubric,
                )
                for trait_id, part in rated.judgments_due
            ],
            what_matters=section(description, WHAT_MATTERS),
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
        judgments: list[TraitJudgment] | None = None,
        what_matters: list[WhatMatters] | None = None,
        dry_run: bool = True,
    ) -> ReflectionResult:
        """See the module docstring: `assessments` are the questions'
        answers (and any rating being changed); with `dry_run`, those in
        `proposed` count as provisional, as do traits goals' ratings.
        `judgments` score traits goals' judgment parts; `what_matters`
        adds lines to goals' "What matters to them" sections, once it's
        recorded."""
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
        judged: dict[str, dict[str, dict[str, int]]] = {}
        for j in judgments or ():
            if not (isinstance(j.score, int) and 0 <= j.score <= 100):
                problems.append(f"{j.goal_id}: a judgment's score is a whole number from 0 to 100")
            judged.setdefault(j.goal_id, {}).setdefault(j.trait_id, {})[j.part] = j.score
        if problems:
            raise ValueError("; ".join(problems))
        self._health.check(assessments)
        additions = checked_what_matters(what_matters or [], set(tree.by_id))
        if additions and self._details is None:
            raise ValueError("this calendar has no goal descriptions to add what matters to")

        evaluation = self._evaluate(
            day, tree, given, set(proposed or ()) if dry_run else set(), judged, confirm_traits=not dry_run
        )
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
        for goal_id, items in additions.items():
            current = self._details.get(goal_id)
            updated = add_to_section(current, items, now.date())
            if updated != (current or ""):
                self._details.set(goal_id, updated)
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


def _sub_goal_rating(goal: Goal, tree: GoalTree, rated: Rated) -> SubGoalRating:
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
    nor skipped for want of an event), a traits rating (its judgments were
    made and it was confirmed), or one changed by hand."""
    if assessment.carried or assessment.unmet:
        return False
    return (
        assessment.method in ("subjective", "llm")
        or isinstance((assessment.metrics or {}).get("traits"), dict)
        or bool(assessment.rationale)
        or (assessment.explanation or "").startswith("Changed from")
    )


def _check_judgments(
    judgments: dict[str, dict[str, dict[str, int]]],
    breakdowns: dict[str, Any],
    given: dict[str, Assessment],
    kept: dict[str, Assessment],
) -> None:
    """Raise ValueError for a judgment that isn't one of a traits goal's
    judgment parts -- unless its goal is rated another way in this call."""
    problems = []
    for goal_id, traits in judgments.items():
        if goal_id in given or goal_id in kept:
            continue
        rated = breakdowns.get(goal_id)
        if rated is None:
            problems.append(f"judgments: {goal_id!r} isn't a goal rated by traits today")
            continue
        parts = {(t.trait_id, p.key) for t in rated.traits for p in t.parts if p.kind == "judgment"}
        for trait_id, keyed in traits.items():
            for key in keyed:
                if (trait_id, key) not in parts:
                    listed = ", ".join(f"{t}/{k}" for t, k in sorted(parts)) or "none"
                    problems.append(
                        f"judgments: {goal_id} has no judgment part {trait_id}/{key} (its judgment parts: {listed})"
                    )
    if problems:
        raise ValueError("; ".join(problems))


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
    lines = summary_lines(tree, results, evaluation.previous, lined)
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
    return lines, "\n".join([head] + grouped(lines))


def _expired_weights(tree: GoalTree, day: date) -> list[ExpiredWeight]:
    return [
        ExpiredWeight(
            goal_id=goal.id,
            goal_path=tree.path(goal.id),
            sub_goal_id=sub_goal_id,
            sub_goal_path=tree.path(sub_goal_id) if sub_goal_id in tree.by_id else sub_goal_id,
            weight=entry["weight"],
            until=entry["until"],
            then=entry["then"],
        )
        for goal in tree.ordered()
        for sub_goal_id, entry in expired_weights(tree.measure(goal.id), day).items()
    ]


def _instructions(day: date) -> str:
    return "\n".join(
        [
            f"This is the daily reflection for {day}. Ratings are 0-100 (0-39 red, 40-69 yellow, 70-100 "
            "green), or \"skip\". Everything the calendar can rate is filled in automatically; `questions` "
            "holds only the goals that need judgement. Keep it brief.",
            "If uncompacted_notes > 0, say the calendar may not reflect them yet, and offer to compact "
            "notes first.",
            "If expired_weights isn't empty, then after the reflection is recorded say, one line each, "
            "that the sub-goal's temporary weight in its goal's rollup ran out on `until`, so it now "
            "weighs `then`; ask whether to keep that (update_goal, replacing the entry with the plain "
            "number) or set it aside again with a new `until`.",
            "1. For each llm question, rate it yourself against its rubric, from its sub_goals, "
            "events_digest, notes_digest and goal_time, with a one-sentence rationale. Only when they don't "
            "tell you what the rubric needs, ask the user too: list its goal_id in `proposed`.",
            "Traits questions (kind traits) are goals rated by traits -- usually people. Their parts are "
            "computed already (each with how it was reached); make each of their `judgments` yourself, "
            "0-100 against its rubric, from the parts, what_matters, events_digest and notes_digest -- "
            "don't ask the user about each trait or person. Pass them as record_reflection's `judgments` "
            "{goal_id, trait_id, part, score}. A traits rating is only ever proposed until the user "
            "confirms it, so it stays provisional (~) in a dry run.",
            "2. Call record_reflection with your llm ratings (method llm), `proposed`, your traits "
            "`judgments` and dry_run=True. Show its `summary` exactly as given. If there's nothing to ask "
            "(no subjective or traits questions, nothing proposed), call it with dry_run=False instead, and "
            "show that summary.",
            "3. Below the summary, ask every question at once, as one numbered list, for the user to "
            "answer together: each proposed llm rating (your rating and why: OK, or theirs?), each "
            "subjective question's prompt, and the traits ratings in one item -- a line per goal: its "
            "proposed rating, its traits' scores and, for each judgment, your score and why -- to confirm "
            "all at once or correct any. Accept \"skip\"; turn words into a 0-100 number.",
            "4. Call record_reflection with all the answers -- the llm ratings, the subjective ones "
            "(method subjective, anything they said as the rationale), and the same traits `judgments` "
            "(with any the user corrected) -- and dry_run=False: that confirms the traits ratings, "
            "keeping the judgments beside the computed parts. A traits rating the user overrides is an "
            "assessment like any change (with their reason as the rationale). Then show only the summary "
            "lines whose rating changed from the first summary, as \"Name old → new\", and the final "
            "Overall.",
            "If the day's notes reveal something new worth remembering about a person (a goal rated by "
            "traits), pass it in record_reflection's `what_matters` {goal_id, items} with dry_run=False: "
            "it's added, dated, to their \"What matters to them\" section. Don't repeat what's there.",
            "5. If the user wants a rating changed, record it the same way with their reason as the "
            "rationale: its parents roll up again.",
        ]
    )
