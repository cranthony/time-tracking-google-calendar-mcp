"""A day's goal health in brief: a line for each of a chosen few goals,
grouped by priority (0 first) and best rated first within each. Each line
folds in the sub-goals that don't have a line of their own, naming the
lowest of them, and marks a rating that moved a lot since the day before.

A reflection (utilities/reflection.py) shows it for the top-level goals
and every goal given its own priority; the description of a day's Goal
Health event (utilities/health_days.py) for just those given their own
priority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from utilities.goals import OVERALL_ID, Goal, GoalTree
from utilities.health_days import Assessment, band, explanation_of

DEFAULT_PRIORITY = 2
"""A line's priority for a top-level goal without one -- as for an event
(calendar_clients/google_calendar.py's color_for_priority)."""

_DETAILS = 3
"""How many folded-in sub-goals a line names."""

_RATING = re.compile(r" → (\d+|skip)$")
"""The rating at the end of an explanation `measure` gave."""

_TREND = 10
"""How far (in points) a line's rating must move since the day before to
be marked with an arrow."""


@dataclass
class Rated:
    assessment: Assessment | None
    """`None` while it waits on an answer (or can't be rated)."""

    final: bool


@dataclass(kw_only=True)
class SummaryLine:
    """One line of a summary: a goal, folding in the sub-goals without a
    line of their own."""

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


def summary_lines(
    tree: GoalTree,
    results: dict[str, Rated],
    previous: dict[str, Assessment],
    lined: list[Goal],
    *,
    markdown: bool = True,
) -> list[SummaryLine]:
    """A line for each of the `lined` goals, from the day's `results` (a
    goal missing from them wasn't rated that day) and the day before's
    confirmed ratings, in order -- see the module docstring. With
    `markdown`, a line's goal and rating are in bold."""
    lined_ids = {g.id for g in lined}
    order = {g.id: i for i, g in enumerate(tree.ordered())}
    names: dict[str, int] = {}
    for g in tree.goals:
        names[g.name] = names.get(g.name, 0) + 1

    def bold(text: str) -> str:
        return f"**{text}**" if markdown else text

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
            if child.id in lined_ids or child.id not in results:
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
            # Without the rating it ends in: the line shows it already.
            return _RATING.sub("", explanation_of(assessment) or "") or assessment.rationale
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
        r = results.get(goal.id)
        if not tree.rated(goal.id) or r is None or (r.assessment is None and r.final):
            lines.append(
                SummaryLine(
                    goal_id=goal.id, name=goal.name, priority=priority, rating=None, state="unmeasured",
                    text=f"⚪ {bold(goal.name)}: not measured",
                )
            )
            continue
        rating = r.assessment.rating if r.assessment else None
        state = "waiting" if rating is None else "final" if r.final else "provisional"
        before = previous.get(goal.id)
        change = rating - before.rating if isinstance(rating, int) and before and isinstance(before.rating, int) else None
        why = detail(goal)
        if rating is None:
            text = f"⏳ {bold(goal.name)}: waiting on your answers"
        elif rating == "skip":
            text = f"⚪ {bold(goal.name)} skipped"
        else:
            arrow = (f" ↑{change}" if change > 0 else f" ↓{-change}") if change and abs(change) >= _TREND else ""
            mark = "~" if state == "provisional" else ""
            text = f"{band(rating)} {bold(f'{goal.name} {mark}{rating}')}{arrow}"
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

    return sorted(lines, key=rank)


def grouped(lines: list[SummaryLine], *, markdown: bool = True) -> list[str]:
    """`lines`' texts under a heading for each priority, each group after
    a blank line. With `markdown`, the headings are in bold."""
    text = []
    for priority in sorted({line.priority for line in lines}):
        heading = f"Priority {priority}"
        text += ["", f"**{heading}**" if markdown else heading]
        text += [line.text for line in lines if line.priority == priority]
    return text


def day_summary(tree: GoalTree, assessments: dict[str, Assessment], previous: dict[str, Assessment]) -> str:
    """A day's `assessments` in brief, for its Goal Health event: a line
    for each goal given its own priority, with a rating not confirmed yet
    marked "~" -- see the module docstring. `previous` is the day before's
    assessments."""
    results = {goal_id: Rated(a, final=a.status == "confirmed") for goal_id, a in assessments.items()}
    lined = [g for g in tree.ordered() if g.id != OVERALL_ID and g.priority is not None and g.id in results]
    confirmed = {goal_id: a for goal_id, a in previous.items() if a.status == "confirmed"}
    lines = summary_lines(tree, results, confirmed, lined, markdown=False)
    return "\n".join(grouped(lines, markdown=False)).strip()
