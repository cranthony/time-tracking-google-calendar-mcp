"""The kinds of measure a goal can have, and what each one's spec holds.
Every active goal is rated once a day, in the daily reflection (see
utilities/reflection.py); its measure says how. utilities/goal_health.py
computes the kinds the calendar can answer; `measure_problems` checks a
spec before it's saved, so a typo (say "target_mins") is refused rather
than leaving the goal silently unmeasured.

| kind         | fields                                                    |
| ------------ | --------------------------------------------------------- |
| `duration`   | `target_min` (> 0): minutes per interval; optional        |
|              | `interval_days` and `zero_at_days`                        |
| `count`      | `target` (> 0): events per interval; optional `noun`,     |
|              | `interval_days` and `zero_at_days`                        |
| `time_       | `edge` ("start" or "end") and `target` ("HH:MM"): when    |
| constraint`  | the day's first event starts, or its last ends; optional  |
|              | `when` ("by", the default, or "after"), `grace_min`       |
|              | (>= 0, default 0) and `zero_at_min` (> grace, default 60) |
| `time_       | `from` and `to` ("HH:MM", `from` before `to`): a window   |
| window`      | one of the day's events should fall in; optional          |
|              | `grace_min` and `zero_at_min`, as for a time constraint   |
| `follow_     | optional `penalty` (> 0, default 25): points lost per     |
| through`     | cancelled event; `recovery` (> 0, default 25): points     |
|              | regained per day with a kept one; `look_back_days` (a     |
|              | whole number >= 1, default 30)                            |
| (all five)   | optional `events_of` and `include_sub_goals`: whose       |
|              | events they look at                                       |
| `subjective` | `prompt`: the question asked in a reflection; optional    |
|              | `interval_days`: how often it's asked (default every day) |
| `llm`        | `rubric`: what the model rates the day against, which may |
|              | refer to the immediate sub-goals' ratings                 |
| `rollup`     | optional `agg`: "mean" (the default), "weighted" (with    |
|              | `weights`) or "percentile" (with `percentile`) of the     |
|              | immediate sub-goals' ratings                              |
| `traits`     | `traits`: trait ids, or "all" (every active one); optional|
|              | `weights` ({trait id: weight}) and `window_days` (> 0,    |
|              | default 30)                                               |
| (any kind)   | optional `only_if`: rated only on days with an event of   |
|              | a goal                                                    |

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

**Whose events.** A duration, count, time constraint, time window or
follow-through measure looks at the events given its own goal or any of its sub-goals -- or, with
`events_of`, another goal's (and its sub-goals') instead, as though it
were that goal: "work 40 hours a week" can be a sub-goal of "Fulfil my
work commitment" that measures its parent's events, without tagging any
event with it. (utilities/goals.py checks `events_of` names a goal.) With
`include_sub_goals` false, only that goal's own events count, not its
sub-goals'.

**Time constraints** rate when the day's events of a goal start or end:
with `edge` "start", the first one's start; with "end", the last one's
end. "Up by 07:00" is `{"edge": "start", "target": "07:00"}` measuring a
"get up" goal's events; "in by 09:30" and "out by 17:30" measure work's.
With `when` "by" (the default) it's 100 at or before the target, plus
`grace_min`, falling linearly to 0 at `zero_at_min` minutes late; with
"after", the same the other way round, for "not before". A day with no
such events is rated 0; give it an `only_if` (below) to skip such days
instead.

**Time windows** rate whether one of the day's events of a goal falls in
a window: "lunch between 11:30 and 13:30" is `{"from": "11:30", "to":
"13:30", "events_of": "<Eat well's id>"}`, so any meal tagged "Eat
well" counts as lunch if it's in the window. Each event is as far outside the
window as its closest edge: one that overlaps it at all is 0 minutes out,
and one from 14:00 to 14:30 is 30 minutes out. The day is rated by its
closest event: 100 up to `grace_min` minutes out, falling linearly to 0
at `zero_at_min`. A day with no such events is rated 0: it's the event
that's wanted.

**Follow through** rates keeping your word: a goal's events being
cancelled -- pushed off the calendar by reallocation, or cancelled by a
compaction (it didn't happen) or by hand -- rather than kept. Its rating
is a running score that carries over from day to day: it starts at 100
`look_back_days` days before the end of the day being rated, and each
day, oldest first, loses `penalty` for each of that day's events of the
goal that was cancelled and regains `recovery` if any was kept (not
cancelled), staying within 0-100. So one cancellation drops it (100 →
75), it stays down through days with none of the goal's events, and it
climbs back only on days that have one that happened. An event counts on
the day it was to start; the days before the one being rated are the 24
hours before it, and the 24 before those, and so on. A cancelled event
overlapped by a kept event of the same goal (one merged into another by
a compaction, say) isn't counted: it was replaced, not dropped. A
cancellation more than `look_back_days` ago is forgotten.

**Subjective.** Its `prompt` is asked in the first daily reflection after
`interval_days` have passed since it was last answered -- in a reflection,
or given in passing with `record_assessments`, which resets the clock --
and on the days between, the previous day's rating carries over.

**Rollups** read the ratings their immediate sub-goals were given that
same day, which is why a reflection rates sub-goals before their parents.
`weights` maps sub-goal ids to weights (>= 0); a sub-goal added since, or
missing from it, weighs 0. A weight can instead be temporary:
`{"weight": 0, "until": "2026-10-12", "then": 1}` weighs `weight` on days
before `until` and `then` from it on -- to set a sub-goal aside for a
while, with a date to look at it again (see `weight_on`). Once `until` has
come, the reflection mentions it (`expired_weights`) until it's extended or
replaced by a plain number. `percentile` is 0-100: 0 is the lowest
sub-goal's rating, 100 the highest, 50 the median. A goal with no measure
but sub-goals to rate is rated as a "mean" rollup.

**Traits** rate a goal by the traits it selects from the Traits tab (see
utilities/traits.py): the weighted mean of their scores (a trait missing
from `weights` weighs 1, so one added later counts), each the weighted
mean of its parts, computed over the goal's events (its own and
its sub-goals') and their facets in the `window_days` before the day's
end. People goals select "all"; a goal of looking after oneself might
select only "reliable". A trait that's off or archived, or that every
part of has nothing to rate it by, is left out; if they all are, the day
is skipped. utilities/trait_scores.py computes it. Its sub-goals (visits,
activities) are its inputs, through the parts, rather than averaged.

**Only if.** Any measure can take `only_if`: `{"events_of": "<goal id>",
"include_sub_goals": true}`, both optional and meaning what they do
above. Without `events_of`, it looks at the same events the measure does
-- the measure's own `events_of` and `include_sub_goals`, if it has them
-- so `{}` on a time constraint skips the days it has nothing to rate,
and on a subjective measure means the goal itself and its sub-goals. The measure
is rated only on days with at least one such event; on any other day the
goal is proposed as "skip" -- its prompt isn't asked, nor its rubric
judged -- with "only_if" in its metrics, so it can be told apart from a
skip someone chose. "How did practice go?" can be asked only on days of
piano practice. A subjective measure's interval passes over those days:
they don't count as answers, nor are they carried over.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_EVENT_SOURCE = frozenset({"events_of", "include_sub_goals"})
"""The fields saying whose events a measure looks at."""

_ANY_KIND = frozenset({"only_if"})
"""The fields every kind of measure takes."""

MEASURE_KINDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    kind: (required, optional | _ANY_KIND)
    for kind, (required, optional) in {
        "duration": (frozenset({"target_min"}), frozenset({"interval_days", "zero_at_days"}) | _EVENT_SOURCE),
        "count": (frozenset({"target"}), frozenset({"noun", "interval_days", "zero_at_days"}) | _EVENT_SOURCE),
        "time_constraint": (
            frozenset({"edge", "target"}), frozenset({"when", "grace_min", "zero_at_min"}) | _EVENT_SOURCE
        ),
        "time_window": (frozenset({"from", "to"}), frozenset({"grace_min", "zero_at_min"}) | _EVENT_SOURCE),
        "follow_through": (frozenset(), frozenset({"penalty", "recovery", "look_back_days"}) | _EVENT_SOURCE),
        "subjective": (frozenset({"prompt"}), frozenset({"interval_days"})),
        "llm": (frozenset({"rubric"}), frozenset()),
        "rollup": (frozenset(), frozenset({"agg", "weights", "percentile"})),
        "traits": (frozenset({"traits"}), frozenset({"weights", "window_days"})),
    }.items()
}
"""Each kind's (required, optional) fields, besides `kind` itself."""

ROLLUP_AGGREGATES = ("mean", "weighted", "percentile")

EDGES = ("start", "end")
WHENS = ("by", "after")

DEFAULT_MEASURE: dict[str, Any] = {"kind": "rollup", "agg": "mean"}
"""How a goal with no measure, but sub-goals to rate, is rated."""

MEASURE_SHAPE_PROBLEM = 'must be an object with a "kind"'
"""The one problem `measure_problems` reports for a spec that isn't a
measure at all."""

_HH_MM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


def measure_problems(
    measure: Any, *, sub_goal_ids: set[str] | None = None, trait_ids: set[str] | None = None
) -> list[str]:
    """Everything wrong with a measure spec, as phrases to follow "its
    measure" (e.g. 'needs "target_min"'); empty if it's fine. A weighted
    rollup's weights must name the goal's immediate sub-goals,
    `sub_goal_ids`, if they're given; a traits measure's traits must be in
    `trait_ids`, if they're given."""
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

    problems += _event_source_problems(measure)
    if "only_if" in measure:
        condition = measure["only_if"]
        if not isinstance(condition, dict):
            problems.append('"only_if" must be an object, with optional "events_of" and "include_sub_goals"')
        else:
            for name in sorted(condition.keys() - _EVENT_SOURCE):
                problems.append(f'"only_if" has no field "{name}"; it takes "events_of", "include_sub_goals"')
            problems += [f'"only_if" {p}' for p in _event_source_problems(condition)]
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
    elif kind in ("time_constraint", "time_window"):
        if kind == "time_constraint":
            if "edge" in measure and measure["edge"] not in EDGES:
                problems.append(f'"edge" must be one of {", ".join(EDGES)}')
            if "when" in measure and measure["when"] not in WHENS:
                problems.append(f'"when" must be one of {", ".join(WHENS)}')
        times = ("target",) if kind == "time_constraint" else ("from", "to")
        malformed = [name for name in times if name in measure and not _is_time(measure[name])]
        problems += [f'"{name}" must be a time like "07:00"' for name in malformed]
        if kind == "time_window" and not malformed and {"from", "to"} <= measure.keys() and measure["from"] >= measure["to"]:
            problems.append('"from" must be before "to"')
        grace = measure.get("grace_min", 0)
        if not (_is_number(grace) and grace >= 0):
            problems.append('"grace_min" must be a number, 0 or more')
        elif "zero_at_min" in measure and not (_is_number(measure["zero_at_min"]) and measure["zero_at_min"] > grace):
            problems.append(f'"zero_at_min" must be a number above "grace_min" ({grace:g})')
    elif kind == "follow_through":
        positive("penalty")
        positive("recovery")
        if "look_back_days" in measure and not (
            isinstance(measure["look_back_days"], int)
            and not isinstance(measure["look_back_days"], bool)
            and measure["look_back_days"] >= 1
        ):
            problems.append('"look_back_days" must be a whole number, 1 or more')
    elif kind == "subjective":
        text("prompt")
    elif kind == "llm":
        text("rubric")
    elif kind == "rollup":
        problems += _rollup_problems(measure, sub_goal_ids)
    elif kind == "traits":
        positive("window_days")
        problems += _traits_problems(measure, trait_ids)
    return problems


def _traits_problems(measure: dict[str, Any], trait_ids: set[str] | None) -> list[str]:
    selected = measure.get("traits")
    problems = []
    if selected != "all" and not (
        isinstance(selected, list) and selected and all(isinstance(t, str) and t for t in selected)
    ):
        return ['"traits" must be "all" or a list of trait ids (get_traits lists them)']
    named = [] if selected == "all" else list(selected)
    if len(set(named)) != len(named):
        problems.append('"traits" names a trait more than once')
    weights = measure.get("weights")
    if "weights" in measure:
        if not (
            isinstance(weights, dict)
            and all(isinstance(t, str) and _is_number(w) and w >= 0 for t, w in weights.items())
        ):
            return problems + ['"weights" must be {trait id: a number, 0 or more}']
        stray = sorted(set(weights) - set(named)) if named else []
        if stray:
            problems.append(f"\"weights\" names {stray[0]!r}, which \"traits\" doesn't select")
        named += [t for t in weights if t not in named]
    if trait_ids is not None:
        unknown = [t for t in named if t not in trait_ids]
        if unknown:
            problems.append(f"names {unknown[0]!r}, which isn't a trait (get_traits lists them)")
    return problems


def _event_source_problems(fields: dict[str, Any]) -> list[str]:
    """What's wrong with `events_of` and `include_sub_goals`, in a measure
    or its `only_if`."""
    problems = []
    if "events_of" in fields and not (isinstance(fields["events_of"], str) and fields["events_of"]):
        problems.append('"events_of" must be a goal id')
    if "include_sub_goals" in fields and not isinstance(fields["include_sub_goals"], bool):
        problems.append('"include_sub_goals" must be true or false')
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
            and all(isinstance(goal_id, str) and _is_weight(w) for goal_id, w in weights.items())
        ):
            problems.append(
                'a weighted rollup needs "weights": {sub-goal id: a number, 0 or more, or a temporary '
                'weight like {"weight": 0, "until": "2026-10-12", "then": 1}}'
            )
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


def _is_time(value: Any) -> bool:
    return isinstance(value, str) and _HH_MM.fullmatch(value) is not None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_date(value: Any) -> bool:
    """Whether `value` is an ISO date, "YYYY-MM-DD"."""
    if not (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


TEMPORARY_WEIGHT_FIELDS = frozenset({"weight", "until", "then"})


def _is_weight(entry: Any) -> bool:
    """Whether `entry` is a rollup weight: a number, 0 or more, or a
    temporary one (see `weight_on`)."""
    if isinstance(entry, dict):
        return (
            entry.keys() == TEMPORARY_WEIGHT_FIELDS
            and _is_number(entry["weight"])
            and entry["weight"] >= 0
            and _is_number(entry["then"])
            and entry["then"] >= 0
            and _is_date(entry["until"])
        )
    return _is_number(entry) and entry >= 0


def weight_on(entry: Any, day: date) -> float:
    """What a (valid) rollup weight weighs on `day`: a plain number always;
    a temporary one, `{"weight": w, "until": "YYYY-MM-DD", "then": t}`, w
    on days before `until` and t from it on."""
    if isinstance(entry, dict):
        return entry["then"] if day >= date.fromisoformat(entry["until"]) else entry["weight"]
    return entry


def expired_weights(measure: dict[str, Any] | None, day: date) -> dict[str, dict[str, Any]]:
    """The temporary weights in a weighted rollup whose `until` has come by
    `day`, by sub-goal id: they now weigh their `then`, and are still
    written as temporary until someone extends them or replaces them with a
    plain number."""
    weights = (measure or {}).get("weights") if (measure or {}).get("agg") == "weighted" else None
    if not isinstance(weights, dict):
        return {}
    return {
        goal_id: entry
        for goal_id, entry in weights.items()
        if _is_weight(entry) and isinstance(entry, dict) and day >= date.fromisoformat(entry["until"])
    }
