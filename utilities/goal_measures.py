"""The kinds of measure a goal can have, and what each one's spec holds --
see docs/goals-design.md section 7. utilities/goal_health.py computes the
kinds the calendar can answer; `measure_problems` checks a spec before
it's saved, so a typo (say "target_mins") is refused rather than leaving
the goal silently unmeasured.

| kind         | fields                                                  |
| ------------ | ------------------------------------------------------- |
| `duration`   | `target_min` (> 0): minutes per period                  |
| `count`      | `target` (> 0): events per period; optional `noun`      |
| `wake_time`  | `target` ("HH:MM"); optional `grace_min` (>= 0, default |
|              | 0) and `zero_at_min` (> grace, default 60)              |
| `subjective` | optional `prompt`: the question asked in a reflection   |
| `llm`        | `rubric`: what the model rates the period against       |
| `rollup`     | optional `agg`: "min" (default) or "mean" of sub-goals  |
"""

from __future__ import annotations

import re
from typing import Any

MEASURE_KINDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "duration": (frozenset({"target_min"}), frozenset()),
    "count": (frozenset({"target"}), frozenset({"noun"})),
    "wake_time": (frozenset({"target"}), frozenset({"grace_min", "zero_at_min"})),
    "subjective": (frozenset(), frozenset({"prompt"})),
    "llm": (frozenset({"rubric"}), frozenset()),
    "rollup": (frozenset(), frozenset({"agg"})),
}
"""Each kind's (required, optional) fields, besides `kind` itself."""

ROLLUP_AGGREGATES = ("min", "mean")

MEASURE_SHAPE_PROBLEM = 'must be an object with a "kind"'
"""The one problem `measure_problems` reports for a spec that isn't a
measure at all."""

_HH_MM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def measure_problems(measure: Any) -> list[str]:
    """Everything wrong with a measure spec, as phrases to follow "its
    measure" (e.g. 'needs "target_min"'); empty if it's fine."""
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

    if kind == "duration":
        positive("target_min")
    elif kind == "count":
        positive("target")
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
        if "agg" in measure and measure["agg"] not in ROLLUP_AGGREGATES:
            problems.append(f'"agg" must be one of {", ".join(ROLLUP_AGGREGATES)}')
    return problems


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
