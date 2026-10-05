"""The timeline of a compaction: a day's notes and its events in time
order, so it's easy to see which note moved which event edge and which
notes just describe what was going on.

`Timeline` is plain data -- what a client (or, eventually, the Time
Tracker) needs to draw it itself -- plus `text`, a fixed-width rendering
of the same thing for clients that can only show text. Both are
built here from what the planner (utilities/note_compaction.py) or
`NoteCompactor.prepare` already worked out; nothing here decides anything.

The text rendering is one narrow column (`_WIDTH`, for a phone) -- each
moment's notes, then the event edges at it:

    17:00 ┌ Work
    18:15 ● Leaving for salsa early to prep
         →├ Salsa prep · new
    18:30 ├ Google Salsa class
    19:00 ● Learned the cross-body lead
            ↳ Google Salsa class
    19:30 ├ Dinner
    20:10 ● Done with dinner
         →└ Dinner ends · +10m (was 20:00)
         →├ Reading · +10m (was 20:00)
    20:30 ┄┄ now ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄
    21:00 └ Reading ends

`→` marks an edge the note just above it set (a note that set an edge at
another time -- "leaving in 15" -- says which, under it); `↳` names the
event a note's text was added to, and `○` marks a note that wasn't added
anywhere. A past event with no tag happened as planned; `+`/`−` is how
late or early an edge was, `⇢`/`⇠` how far a whole event moved. An
event's goals go on the line after its start: `◆` for one it already
serves, `◇` for one it's being given (or, before anything is decided,
one suggested for it), and each goal's total time follows the events.
Its facets (see utilities/facets.py) go on the line after that: `▸` for
those it has, `▹` for those it's being given.
`✓` marks the latest note an earlier compaction already used, shown as
context, and a `┄┄ last compaction` line marks when that compaction ran
-- what came before it is already on the calendar. Long lines wrap,
indented under their text.

Several days compacted together (see utilities/note_compactor.py) are
shown as one timeline, each day under a heading with its date --
`━━ Sat 03 Oct ━━━…` -- and its own goal time, with the legend once at
the end (`join_days`). Only the last day, the one that runs up to now,
draws a `now` line.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Literal

TimelineStatus = Literal["planned", "on_schedule", "adjusted", "reflowed", "new", "cancelled", "merged"]
"""What compaction did to an event:

- `planned`: not touched (an offered future event, or
  `prepare_compaction`'s view before anything was decided). Future
  events a dry run leaves untouched aren't shown at all.
- `on_schedule`: in the past, and recorded exactly as planned.
- `adjusted`: its times were changed to match the notes (or a direct
  request to move it).
- `reflowed`: moved to make room for an adjusted one.
- `new`: created -- something unplanned, or the remainder of an event an
  adjusted one split in two.
- `cancelled` / `merged`: removed (`merged` into `merged_into`)."""

_WIDTH = 40
"""How wide `text` aims to be -- narrow enough for a phone. Lines wrap to
it, with the continuation indented under the line's text; only a single
word longer than that runs past it."""

_GUTTER = 6
"""The time column: `HH:MM` and the column a `→` goes in."""

_LEGEND = [
    "→ note above set this edge",
    "↳ note added to that event",
    "○ note not added   ✓ compacted",
    "◆ goal   ◇ goal being added",
    "▸ facets   ▹ facets being set",
    "+/− late/early   ⇢/⇠ moved",
]


@dataclass(kw_only=True)
class TimelineNote:
    id: str
    time: datetime
    text: str | None = None
    anchors: list[str] = field(default_factory=list)
    """Which event edges this note sets, e.g. "end of Dinner"."""

    annotates: str | None = None
    """The title of the event this note's text was added to, if any."""

    ignored: bool = False
    compacted: bool = False
    """An earlier compaction's note, shown only as context (`✓`)."""


@dataclass(kw_only=True)
class TimelineEvent:
    summary: str
    status: TimelineStatus
    event_id: str | None = None
    """`None` for an event this compaction would create."""

    start: datetime | None = None
    end: datetime | None = None
    """Where it ends up; `None` if it's cancelled or merged."""

    planned_start: datetime | None = None
    planned_end: datetime | None = None
    """Where it was before compaction; `None` if it's new."""

    start_note: str | None = None
    end_note: str | None = None
    """Ids of the notes that set its start/end, if any."""

    merged_into: str | None = None
    """For `merged`: the title of the event it was merged into."""

    goals: list[str] = field(default_factory=list)
    """Names of the goals it serves (once this compaction is applied)."""

    new_goals: list[str] = field(default_factory=list)
    """Which of `goals` it's being given by this compaction -- or, before
    anything is decided, names of goals suggested for it."""

    facets: str | None = None
    """Its facets (see utilities/facets.py), once this compaction is
    applied, in a line."""

    new_facets: bool = False
    """Whether this compaction sets `facets`."""


@dataclass(kw_only=True)
class Timeline:
    notes: list[TimelineNote]
    events: list[TimelineEvent]
    now: datetime | None = None
    last_compaction: datetime | None = None
    """When the latest compaction ran (its `now`), if there's been one."""

    decided: bool = True
    """False for `prepare_compaction`'s view, before anything's been
    decided -- no note has been placed anywhere yet."""

    text: str = ""
    """`notes` and `events` as one narrow fixed-width column -- see the
    module docstring. Show it in a monospace block."""


def build_timeline(
    notes: list[TimelineNote],
    events: list[TimelineEvent],
    now: datetime | None,
    *,
    decided: bool = True,
    last_compaction: datetime | None = None,
) -> Timeline:
    timeline = Timeline(
        notes=sorted(notes, key=lambda n: n.time),
        events=sorted(events, key=lambda e: e.start or e.planned_start),
        now=now,
        last_compaction=last_compaction,
        decided=decided,
    )
    timeline.text = render(timeline)
    return timeline


def join_days(days: list[tuple[datetime, Timeline]]) -> Timeline:
    """Several days' timelines -- each with when that day starts -- as
    one, each day's under a heading with its date (see the module
    docstring). One day's is returned as it is."""
    if len(days) == 1:
        return days[0][1]
    timelines = [t for _, t in days]
    joined = Timeline(
        notes=[n for t in timelines for n in t.notes],
        events=[e for t in timelines for e in t.events],
        now=timelines[-1].now,
        last_compaction=timelines[0].last_compaction,
        decided=timelines[0].decided,
    )
    lines: list[str] = []
    for number, (start, timeline) in enumerate(days):
        if number < len(days) - 1:
            # An earlier day's `now` is just where it ends: it's past.
            timeline = replace(timeline, now=None)
        heading = start.astimezone(_display_tz(timeline)).strftime("━━ %a %d %b ")
        if lines:
            lines.append("")
        lines.append(heading.ljust(_WIDTH, "━"))
        lines.append(render(timeline, legend=False))
    if joined.decided:
        lines.append("")
        lines.extend(_legend(joined.events))
    joined.text = "\n".join(lines)
    return joined


def render(timeline: Timeline, *, legend: bool = True) -> str:
    tz = _display_tz(timeline)

    def hm(moment: datetime) -> str:
        return moment.astimezone(tz).strftime("%H:%M")

    live = [e for e in timeline.events if e.start is not None and e.end is not None]
    removed = [e for e in timeline.events if e.start is None and e.planned_start is not None]
    moments = sorted(
        {n.time for n in timeline.notes}
        | {e.start for e in live}
        | {e.end for e in live}
        | {e.planned_start for e in removed}
    )

    lines: list[str] = []
    # Marker lines, each drawn just before the first moment after it --
    # except that the last compaction goes before a moment it shares,
    # since everything at that moment came after it. `now` before the
    # first moment isn't drawn: nothing it would separate.
    markers: list[tuple[datetime, int, str]] = []
    if timeline.last_compaction is not None:
        label = "last compaction"
        if timeline.now is not None and timeline.last_compaction.astimezone(tz).date() != timeline.now.astimezone(tz).date():
            label += timeline.last_compaction.astimezone(tz).strftime(" (%a %d %b)")
        markers.append((timeline.last_compaction, 0, label))
    if timeline.now is not None and moments and timeline.now > moments[0]:
        markers.append((timeline.now, 1, "now"))
    markers.sort()

    def marker_line(at: datetime, label: str) -> str:
        return f"{hm(at)} ┄┄ {label} ".ljust(_WIDTH, "┄")

    for moment in moments:
        while markers and (markers[0][0] < moment or (markers[0][0] == moment and markers[0][1] == 0)):
            at, _, label = markers.pop(0)
            lines.append(marker_line(at, label))
        notes_here = [n for n in timeline.notes if n.time == moment]
        here = {n.id for n in notes_here}
        rows: list[tuple[bool, str, int]] = []  # (set by a note above, text, continuation indent)
        for note in notes_here:
            rows.append((False, _note_text(note, timeline.decided), 2))
            if note.annotates:
                rows.append((False, f"  ↳ {note.annotates}", 4))
            for edge in _edges_set_elsewhere(note, live, hm):
                rows.append((False, f"  → {edge}", 4))
        for anchored, edge in _edge_lines(moment, live, removed, here, hm):
            rows.append((anchored, edge, 2))
        for i, (anchored, text, indent) in enumerate(rows):
            time = hm(moment) if i == 0 else ""
            lines.extend(_wrap(f"{time:>5}{'→' if anchored else ' '}", text, indent))
    for at, _, label in markers:
        lines.append(marker_line(at, label))
    if goal_time := _goal_time(live):
        lines.append("")
        lines.append("Goal time:")
        lines.extend(f"{duration:>7}  {goal}" for goal, duration in goal_time)
    if timeline.decided and legend:
        lines.append("")
        lines.extend(_legend(timeline.events))
    return "\n".join(lines)


def _wrap(prefix: str, text: str, indent: int) -> list[str]:
    """`prefix` (the gutter) then `text`, wrapped to `_WIDTH`, its
    continuation lines indented `indent` past the gutter."""
    wrapped = textwrap.wrap(
        text,
        width=_WIDTH - _GUTTER,
        subsequent_indent=" " * indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]
    return [f"{prefix if i == 0 else ' ' * _GUTTER}{line}".rstrip() for i, line in enumerate(wrapped)]


def _edges_set_elsewhere(note: TimelineNote, live: list[TimelineEvent], hm) -> list[str]:
    """The edges `note` set at some other time -- "leaving 15 minutes
    early" -- which aren't drawn just below it, so it says where."""
    edges = []
    for event in live:
        if event.start_note == note.id and event.start != note.time:
            edges.append(f"start of {event.summary} ({hm(event.start)})")
        if event.end_note == note.id and event.end != note.time:
            edges.append(f"end of {event.summary} ({hm(event.end)})")
    return edges


def _edge_lines(moment, live, removed, here: set[str], hm) -> list[tuple[bool, str]]:
    """The event edges at `moment`, each with whether a note at `moment`
    (drawn just above) set it. An event's goals follow its start, on
    their own line."""
    ending = [e for e in live if e.end == moment]
    starting = [e for e in live if e.start == moment]
    lines: list[tuple[bool, str]] = []
    for event in ending:
        tag = _end_tag(event, hm)
        anchored = event.end_note in here
        if tag or anchored or not starting:
            lines.append((anchored, f"└ {event.summary} ends{tag}"))
    for event in starting:
        joint = "├" if ending else "┌"
        lines.append((event.start_note in here, f"{joint} {event.summary}{_start_tag(event, hm)}"))
        if goals := _goal_tag(event):
            lines.append((False, f"    {goals}"))
        if event.facets:
            lines.append((False, f"    {'▹' if event.new_facets else '▸'} {event.facets}"))
    for event in removed:
        if event.planned_start != moment:
            continue
        what = f"merged into {event.merged_into}" if event.status == "merged" else "cancelled"
        lines.append(
            (False, f"✕ {event.summary} · {what} (was {hm(event.planned_start)}–{hm(event.planned_end)})")
        )
    return lines


def _start_tag(event: TimelineEvent, hm) -> str:
    if event.status == "new":
        return " · new"
    if event.status not in ("adjusted", "reflowed") or event.planned_start is None:
        return ""
    start_shift = event.start - event.planned_start
    end_shift = event.end - event.planned_end
    if start_shift and start_shift == end_shift:
        arrow = "⇢" if start_shift > timedelta(0) else "⇠"
        return f" · {arrow}{_duration(start_shift)} (was {hm(event.planned_start)}–{hm(event.planned_end)})"
    if start_shift:
        return f" · {_shift(start_shift)} (was {hm(event.planned_start)})"
    return ""


def _end_tag(event: TimelineEvent, hm) -> str:
    if event.status not in ("adjusted", "reflowed") or event.planned_end is None:
        return ""
    start_shift = event.start - event.planned_start
    end_shift = event.end - event.planned_end
    if not end_shift or start_shift == end_shift:
        return ""
    return f" · {_shift(end_shift)} (was {hm(event.planned_end)})"


def _shift(delta: timedelta) -> str:
    return f"{'+' if delta > timedelta(0) else '−'}{_duration(delta)}"


def _goal_tag(event: TimelineEvent) -> str:
    marks = [f"◆ {g}" for g in event.goals if g not in event.new_goals]
    marks += [f"◇ {g}" for g in event.new_goals]
    return " ".join(marks)


def _goal_time(live: list[TimelineEvent]) -> list[tuple[str, str]]:
    """Each goal's total time across `live` events, longest first."""
    totals: dict[str, timedelta] = {}
    for event in live:
        for goal in dict.fromkeys(event.goals + event.new_goals):
            totals[goal] = totals.get(goal, timedelta()) + (event.end - event.start)
    return [
        (goal, _duration(total)) for goal, total in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _note_text(note: TimelineNote, decided: bool) -> str:
    placed = bool(note.anchors or note.annotates) or not decided
    marker = "✓" if note.compacted else "●" if placed and not note.ignored else "○"
    text = (note.text or "").strip() or "(no description)"
    suffix = " (compacted)" if note.compacted else ""
    return f"{marker} {text}{suffix}"


def _duration(delta: timedelta) -> str:
    hours, minutes = divmod(int(abs(delta).total_seconds() // 60), 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _display_tz(timeline: Timeline) -> tzinfo:
    """The notes' own time zone -- they're written by the user, so it's
    theirs -- or the calendar's, failing that."""
    for note in timeline.notes:
        if note.time.tzinfo is not None:
            return note.time.tzinfo
    for event in timeline.events:
        moment = event.start or event.planned_start
        if moment is not None and moment.tzinfo is not None:
            return moment.tzinfo
    return timezone.utc


def _legend(events: list[TimelineEvent]) -> list[str]:
    """The legend, its facets line only when an event has facets."""
    return [line for line in _LEGEND if not line.startswith("▸") or any(e.facets for e in events)]
