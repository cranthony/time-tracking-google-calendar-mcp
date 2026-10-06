"""Priority labels: four of the calendar's event labels, reserved to color
events by their priority -- the priority palette.

An event takes its color from its label: an action's, if it does one
(see utilities/action_calendar.py), or else its priority's -- "Priority
0" to "Priority 3", its own priority's, or priority 2's (the default) if
it has none. The server never colors an event by `colorId`. Each
priority's color is its label's, so it's kept in the calendar itself,
and changed by recoloring the label (`Actions.set_priority_color`); an
action or group with no color of its own takes its priority's from here
too, so its label follows a priority's change.

The labels have fixed ids, so they're found again whatever they're named,
and are kept by `Actions` beside the actions' own (they're not any
action's): created with their default colors the first time any labels
are written, and counted against the calendar's label budget.
"""

from __future__ import annotations

import re
import uuid

from calendar_clients.google_calendar import EventLabel as RawEventLabel, color_for_priority

PRIORITIES = (0, 1, 2, 3)
DEFAULT_PRIORITY = 2
"""An event's priority when it has none: its label is this priority's."""

_NAMESPACE = uuid.UUID("3b8f6f1e-5d3a-4c8e-9a52-7e2f0c4d1b67")

_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def clamp(priority: int | None) -> int:
    """`priority` as one with a label: the default for none, and the
    nearest one for one past either end."""
    if priority is None:
        return DEFAULT_PRIORITY
    return min(PRIORITIES[-1], max(PRIORITIES[0], priority))


def label_id(priority: int | None) -> str:
    """The id of `priority`'s label (see `clamp`)."""
    return str(uuid.uuid5(_NAMESPACE, f"priority-{clamp(priority)}"))


LABEL_IDS = {label_id(p): p for p in PRIORITIES}
"""Each priority label's id, with its priority."""


def default_color(priority: int) -> str:
    return color_for_priority(priority)[1]


def palette(raw_labels: list[RawEventLabel]) -> dict[int, str]:
    """Each priority's color: its label's, or its default color if the
    calendar doesn't have the label yet."""
    found = {LABEL_IDS[label.id]: label.background_color for label in raw_labels if label.id in LABEL_IDS}
    return {p: found.get(p) or default_color(p) for p in PRIORITIES}


def with_priority_labels(raw_labels: list[RawEventLabel], colors: dict[int, str] | None = None) -> list[RawEventLabel]:
    """`raw_labels` with every priority label in them -- those missing
    added in their default colors, each named for its priority -- colored
    as `colors` says, where it says."""
    colors = {**palette(raw_labels), **(colors or {})}
    others = [label for label in raw_labels if label.id not in LABEL_IDS]
    return others + [
        RawEventLabel(id=label_id(p), name=f"Priority {p}", background_color=colors[p]) for p in PRIORITIES
    ]


def color_problem(color: str) -> str | None:
    """Why `color` can't be a label's, or `None`."""
    if not isinstance(color, str) or not _COLOR.fullmatch(color):
        return f"{color!r} isn't a color: give one as #rrggbb, e.g. \"#a4bdfc\""
    return None
