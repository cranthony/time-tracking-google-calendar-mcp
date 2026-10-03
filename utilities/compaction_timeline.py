"""The two-lane view of a compaction: a day's notes on one line, its events
on a parallel one, aligned by time, so it's easy to see which note moved
which event edge and which notes just describe what was going on.

`Timeline` is plain data -- what a client (or, eventually, the Time
Tracker) needs to draw the two lanes itself -- plus `text`, a fixed-width
rendering of the same thing for clients that can only show text. Both are
built here from what the planner (utilities/note_compaction.py) or
`NoteCompactor.prepare` already worked out; nothing here decides anything.

The text rendering, one row per moment something happens:

     TIME  NOTES                                EVENTS
    18:15  ● Leaving for salsa early ─────────── ┌ Salsa prep · new
    18:30                                        ├ Google Salsa class · on schedule
    19:30                                        ├ Dinner · on schedule
    20:10  ● Done with dinner ────────────────── └ Dinner ends · 10m late (planned 20:00)

A note joined to an event by `───` sets that event edge; a note with no
rule had its text added to the event it falls within (`↳`), and `○` marks
a note that wasn't added anywhere. An event's goals follow its title: `◆`
for one it already serves, `◇` for one it's being given (or, before
anything is decided, one suggested for it). A last line totals each
goal's time. `✓` marks the latest note an earlier compaction already
used, shown as context, and a `┄┄ last compaction` line marks when that
compaction ran -- what came before it is already on the calendar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

_NOTE_WIDTH = 36
_LEGEND = (
    "── note sets that event edge   ↳ note added to that event   "
    "○ note not added to any event   ✓ note already compacted"
)


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
    """`notes` and `events` as two fixed-width lanes -- see the module
    docstring. Show it in a monospace block if you can't draw the lanes."""


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


def render(timeline: Timeline) -> str:
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

    lines = [f"{'TIME':>5}  {'NOTES':<{_NOTE_WIDTH}}  EVENTS"]
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
        return f"{hm(at):>5}  {'┄' * _NOTE_WIDTH}  ┄┄ {label}"

    for moment in moments:
        while markers and (markers[0][0] < moment or (markers[0][0] == moment and markers[0][1] == 0)):
            at, _, label = markers.pop(0)
            lines.append(marker_line(at, label))
        edges = _edge_lines(moment, live, removed, hm)
        active = any(e.start < moment < e.end for e in live)
        notes_here = [n for n in timeline.notes if n.time == moment]
        rows: list[tuple[str, bool, str]] = []
        for i, note in enumerate(notes_here):
            if i < len(edges):
                right = edges[i]
            elif note.annotates:
                right = f"│   ↳ added to {note.annotates}"
            else:
                right = "│" if active else ""
            rows.append((_note_text(note, timeline.decided), bool(note.anchors) and i < len(edges), right))
        for edge in edges[len(notes_here) :]:
            rows.append(("", False, edge))
        for i, (left, anchored, right) in enumerate(rows):
            time = hm(moment) if i == 0 else ""
            if anchored:
                left = f"{left} ".ljust(_NOTE_WIDTH, "─") + "─"
            else:
                left = left.ljust(_NOTE_WIDTH) + " "
            lines.append(f"{time:>5}  {left} {right}".rstrip())
    for at, _, label in markers:
        lines.append(marker_line(at, label))
    if goal_time := _goal_time(live):
        lines.append("")
        lines.append(f"Goal time: {goal_time}")
    if timeline.decided:
        lines.append("")
        lines.append(_LEGEND)
    return "\n".join(lines)


def _edge_lines(moment, live, removed, hm) -> list[str]:
    ending = [e for e in live if e.end == moment]
    starting = [e for e in live if e.start == moment]
    lines: list[str] = []
    for event in ending:
        tag = _end_tag(event, hm)
        if tag or not starting:
            lines.append(f"└ {event.summary} ends{tag}")
    for event in starting:
        joint = "├" if ending else "┌"
        lines.append(f"{joint} {event.summary}{_start_tag(event, hm)}{_goal_tag(event)}")
    for event in removed:
        if event.planned_start != moment:
            continue
        what = f"merged into {event.merged_into}" if event.status == "merged" else "cancelled"
        lines.append(
            f"✕ {event.summary} · {what} (was {hm(event.planned_start)}–{hm(event.planned_end)})"
        )
    return lines


def _start_tag(event: TimelineEvent, hm) -> str:
    if event.status == "new":
        return " · new"
    if event.status == "on_schedule":
        return " · on schedule"
    if event.status not in ("adjusted", "reflowed") or event.planned_start is None:
        return ""
    start_shift = event.start - event.planned_start
    end_shift = event.end - event.planned_end
    if start_shift and start_shift == end_shift:
        return (
            f" · moved {_duration(start_shift)} {'later' if start_shift > timedelta(0) else 'earlier'}"
            f" (planned {hm(event.planned_start)}–{hm(event.planned_end)})"
        )
    if start_shift:
        return (
            f" · starts {_duration(start_shift)} {'late' if start_shift > timedelta(0) else 'early'}"
            f" (planned {hm(event.planned_start)})"
        )
    return ""


def _end_tag(event: TimelineEvent, hm) -> str:
    if event.status not in ("adjusted", "reflowed") or event.planned_end is None:
        return ""
    start_shift = event.start - event.planned_start
    end_shift = event.end - event.planned_end
    if not end_shift or start_shift == end_shift:
        return ""
    return (
        f" · {_duration(end_shift)} {'late' if end_shift > timedelta(0) else 'early'}"
        f" (planned {hm(event.planned_end)})"
    )


def _goal_tag(event: TimelineEvent) -> str:
    marks = [f"◆ {g}" for g in event.goals if g not in event.new_goals]
    marks += [f"◇ {g}" for g in event.new_goals]
    return f"  {' '.join(marks)}" if marks else ""


def _goal_time(live: list[TimelineEvent]) -> str:
    """Each goal's total time across `live` events, longest first."""
    totals: dict[str, timedelta] = {}
    for event in live:
        for goal in dict.fromkeys(event.goals + event.new_goals):
            totals[goal] = totals.get(goal, timedelta()) + (event.end - event.start)
    return " · ".join(
        f"{goal} {_duration(total)}" for goal, total in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def _note_text(note: TimelineNote, decided: bool) -> str:
    placed = bool(note.anchors or note.annotates) or not decided
    marker = "✓" if note.compacted else "●" if placed and not note.ignored else "○"
    text = (note.text or "").strip() or "(no description)"
    suffix = " (compacted)" if note.compacted else ""
    room = (_NOTE_WIDTH - 4 if note.anchors else _NOTE_WIDTH - 2) - len(suffix)
    if len(text) > room:
        text = text[: room - 1] + "…"
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
