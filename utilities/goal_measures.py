"""The kinds of measure a goal can have, and what each one's spec holds.
Every active goal is rated once a day, in the daily reflection (see
utilities/reflection.py); its measure says how. utilities/goal_health.py
computes the kinds the calendar can answer; `measure_problems` checks a
spec before it's saved, so a typo (say "target_mins") is refused rather
than leaving the goal silently unmeasured.

| kind         | fields                                                    |
| ------------ | --------------------------------------------------------- |
| `duration`   | `target_min` (> 0): minutes per interval; optional        |
|              | `interval_days`, `zero_at_days`, `goal_ids` and           |
|              | `include_sub_goals`                                       |
| `count`      | `target` (> 0): events per interval; optional `noun`,     |
|              | `interval_days`, `zero_at_days`, `goal_ids` and           |
|              | `include_sub_goals`                                       |
| `wake_time`  | `target` ("HH:MM"); optional `grace_min` (>= 0, default   |
|              | 0) and `zero_at_min` (> grace, default 60)                |
| `subjective` | `prompt`: the question asked in a reflection; optional    |
|              | `interval_days`: how often it's asked (default every day) |
| `llm`        | `rubric`: what the model rates the day against, which may |
|              | refer to the immediate sub-goals' ratings                 |
| `rollup`     | optional `agg`: "mean" (the default), "weighted" (with    |
|              | `weights`) or "percentile" (with `percentile`) of the     |
|              | immediate sub-goals' ratings                              |

**Intervals.** A duration or count measure looks back over the
`interval_days` (default 1) days of wall-clock time before the end of the
day being rated -- e.g. 60 for "visit my parents every 2 months". Its
rating is 100 while the target is met over that window. Without
`zero_at_days` a shortfall is rated in proportion (3 of 4 hours → 75);
with it, a shortfall is rated by how long ago the target was last met,
falling linearly from 100 when it lapsed to 0 once `zero_at_days` (>
`interval_days`) have passed since -- measured from the same start as the
interval, so a 60-day visit goal with `zero_at_days` 90 is at 50 after 75
days without a visit.

A duration or count measure looks at the events serving its own goal or
any of its sub-goals -- or, given `goal_ids`, those serving any of these
goals or their sub-goals instead (utilities/goals.py checks they're
goals). With `include_sub_goals` false, only those goals' own events
count, not their sub-goals'.

**Subjective.** Its `prompt` is asked in the first daily reflection after
`interval_days` have passed since it was last answered -- in a reflection,
or given in passing with `record_assessments`, which resets the clock --
and on the days between, the previous day's rating carries over.

**Rollups** read the ratings their immediate sub-goals were given that
same day, which is why a reflection rates sub-goals before their parents.
`weights` maps sub-goal ids to weights (>= 0); a sub-goal added since, or
missing from it, weighs 0. `percentile` is 0-100: 0 is the lowest
sub-goal's rating, 100 the highest, 50 the median. A goal with no measure
but sub-goals to rate is rated as a "mean" rollup.
"""

from __future__ import annotations

import re
from typing import Any

MEASURE_KINDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "duration": (
        frozenset({"target_min"}),
        frozenset({"interval_days", "zero_at_days", "goal_ids", "include_sub_goals"}),
    ),
    "count": (
        frozenset({"target"}),
        frozenset({"noun", "interval_days", "zero_at_days", "goal_ids", "include_sub_goals"}),
    ),
    "wake_time": (frozenset({"target"}), frozenset({"grace_min", "zero_at_min"})),
    "subjective": (frozenset({"prompt"}), frozenset({"interval_days"})),
    "llm": (frozenset({"rubric"}), frozenset()),
    "rollup": (frozenset(), frozenset({"agg", "weights", "percentile"})),
}
"""Each kind's (required, optional) fields, besides `kind` itself."""

ROLLUP_AGGREGATES = ("mean", "weighted", "percentile")

DEFAULT_MEASURE: dict[str, Any] = {"kind": "rollup", "agg": "mean"}
"""How a goal with no measure, but sub-goals to rate, is rated."""

MEASURE_SHAPE_PROBLEM = 'must be an object with a "kind"'
"""The one problem `measure_problems` reports for a spec that isn't a
measure at all."""

_HH_MM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def measure_problems(measure: Any, *, sub_goal_ids: set[str] | None = None) -> list[str]:
    """Everything wrong with a measure spec, as phrases to follow "its
    measure" (e.g. 'needs "target_min"'); empty if it's fine. A weighted
    rollup's weights must name the goal's immediate sub-goals,
    `sub_goal_ids`, if they're given."""
    if not isinstance(measure, dict) or not isinstance(measure.get("kind"), str):
        return [MEASURE_SHAPE_PROBLEM]
    kind = measure["kind"]
    if kind not in MEASURE_KINDS:
        return [f"kind must be one of {', '.join(MEASURE_KINDS)}, not {kind!r}"]
    required, optional = MEASURE_KINDS[kind]
    problems = [f'needs "{name}"' for name in sorted(required - measure.keys())]
    for name in sorted(measure.keys() - required - optional - {"kind"}):
        takes = ", ".join(f'"{n}"' for n in sorted(required | optional)) or "nothing else"
        problems.append(f'has no field "{name}"; a {kind} measure takes {takes}')

    def positive(name: str) -> None:
        if name in measure and not (_is_number(measure[name]) and measure[name] > 0):
            problems.append(f'"{name}" must be a number above 0')

    def text(name: str) -> None:
        if name in measure and not (isinstance(measure[name], str) and measure[name].strip()):
            problems.append(f'"{name}" must be non-empty text')

    if "goal_ids" in measure and not (
        isinstance(measure["goal_ids"], list)
        and measure["goal_ids"]
        and all(isinstance(goal_id, str) and goal_id for goal_id in measure["goal_ids"])
    ):
        problems.append('"goal_ids" must be a list of goal ids, not empty')
    if "include_sub_goals" in measure and not isinstance(measure["include_sub_goals"], bool):
        problems.append('"include_sub_goals" must be true or false')
    if kind in ("duration", "count", "subjective"):
        positive("interval_days")
    if kind in ("duration", "count"):
        positive("target_min" if kind == "duration" else "target")
        interval = measure.get("interval_days", 1)
        if "zero_at_days" in measure and _is_number(interval) and not (
            _is_number(measure["zero_at_days"]) and measure["zero_at_days"] > interval
        ):
            problems.append(f'"zero_at_days" must be a number above "interval_days" ({interval:g})')
        if kind == "count":
            text("noun")
    elif kind == "wake_time":
        if "target" in measure and not (isinstance(measure["target"], str) and _HH_MM.fullmatch(measure["target"])):
            problems.append('"target" must be a time like "07:00"')
        grace = measure.get("grace_min", 0)
        if not (_is_number(grace) and grace >= 0):
            problems.append('"grace_min" must be a number, 0 or more')
        elif "zero_at_min" in measure and not (_is_number(measure["zero_at_min"]) and measure["zero_at_min"] > grace):
            problems.append(f'"zero_at_min" must be a number above "grace_min" ({grace:g})')
    elif kind == "subjective":
        text("prompt")
    elif kind == "llm":
        text("rubric")
    elif kind == "rollup":
        problems += _rollup_problems(measure, sub_goal_ids)
    return problems


def _rollup_problems(measure: dict[str, Any], sub_goal_ids: set[str] | None) -> list[str]:
    agg = measure.get("agg", "mean")
    if agg not in ROLLUP_AGGREGATES:
        return [f'"agg" must be one of {", ".join(ROLLUP_AGGREGATES)}']
    problems = []
    if agg == "weighted":
        weights = measure.get("weights")
        if not (
            isinstance(weights, dict)
            and weights
            and all(isinstance(goal_id, str) and _is_number(w) and w >= 0 for goal_id, w in weights.items())
        ):
            problems.append('a weighted rollup needs "weights": {sub-goal id: a number, 0 or more}')
        elif sub_goal_ids is not None and set(weights) - sub_goal_ids:
            stray = sorted(set(weights) - sub_goal_ids)[0]
            problems.append(f'"weights" names {stray!r}, which isn\'t one of its sub-goals')
    elif "weights" in measure:
        problems.append('"weights" is only for a weighted rollup')
    if agg == "percentile":
        percentile = measure.get("percentile")
        if not (_is_number(percentile) and 0 <= percentile <= 100):
            problems.append('a percentile rollup needs "percentile": a number from 0 (lowest) to 100 (highest)')
    elif "percentile" in measure:
        problems.append('"percentile" is only for a percentile rollup')
    return problems


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
