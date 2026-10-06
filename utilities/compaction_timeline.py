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
event's actions go on the line after its start: `◆` for one it already
has, `◇` for one it's being given (or, before anything is decided, one
suggested for it), and each action's total time follows the events. Its
facts (see utilities/facts.py) go on the lines after that -- where, with
whom, for whom, then a line per person's note: `▸` for those it has,
`▹` for those it's being given. `⚠` marks a past event still missing
its action or its location -- what's easy to miss among the rest -- and
each day lists them again at its end, under `Missing:`. An event the
user is cancelling that counts against someone's follow-through (see
utilities/cancellations.py) is listed at the end of its day too, under
`Follow-through:`, with `✗` and who it counts against.
`✓` marks the latest note an earlier compaction already used, shown as
context, and a `┄┄ last compaction` line marks when that compaction ran
-- what came before it is already on the calendar. Long lines wrap,
indented under their text.

Several days compacted together (see utilities/note_compactor.py) are
shown as one timeline, each day under a heading with its date --
`━━ Sat 03 Oct ━━━…` -- and its own action time, with the legend once at
the end (`join_days`). Only the last day, the one that runs up to now,
draws a `now` line.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Literal

TimelineGap = Literal["action", "location"]
"""What a past event can be missing once this compaction is applied."""

TimelineStatus = Literal["planned", "on_schedule", "adjusted", "new", "cancelled", "merged"]
"""What compaction did to an event:

- `planned`: not touched (an offered future event, or
  `prepare_compaction`'s view before anything was decided). Future
  events a dry run leaves untouched aren't shown at all.
- `on_schedule`: in the past, and recorded exactly as planned.
- `adjusted`: its times were changed to match the notes (or a direct
  request to move it).
- `new`: created -- something unplanned that happened.
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
    "◆ action   ◇ action being added",
    "▸ facts   ▹ facts being set",
    "⚠ missing action or location",
    "✗ counts against follow-through",
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

    actions: list[str] = field(default_factory=list)
    """Names of its actions (once this compaction is applied)."""

    new_actions: list[str] = field(default_factory=list)
    """Which of `actions` it's being given by this compaction -- or, before
    anything is decided, names of actions suggested for it."""

    facts: list[str] = field(default_factory=list)
    """Its facts (see utilities/facts.py), once this compaction is
    applied, a line each: where and with whom, then each person's note."""

    new_facts: bool = False
    """Whether this compaction sets `facts`."""

    missing: list[TimelineGap] = field(default_factory=list)
    """What it's still missing once this compaction is applied: set only
    for past events the compaction records, since only those are judged
    from their actions and location."""

    follow_through: list[str] = field(default_factory=list)
    """For an event the user is cancelling: each person it counts against
    in a follow-through part, with the part's trait -- "Sam (Reliable)"."""


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
            # An event's tag lines (its actions, its facts) wrap under
            # their text, past the mark.
            rows.append((anchored, edge, 6 if edge.startswith("    ") else 2))
        for i, (anchored, text, indent) in enumerate(rows):
            time = hm(moment) if i == 0 else ""
            lines.extend(_wrap(f"{time:>5}{'→' if anchored else ' '}", text, indent))
    for at, _, label in markers:
        lines.append(marker_line(at, label))
    if action_time := _action_time(live):
        lines.append("")
        lines.append("Action time:")
        lines.extend(f"{duration:>7}  {action}" for action, duration in action_time)
    if gaps := [e for e in live if e.missing]:
        lines.append("")
        lines.append("Missing:")
        for event in gaps:
            lines.extend(_wrap_plain(f"⚠ {event.summary}: {', '.join(event.missing)}", indent=4))
    if dropped := [e for e in timeline.events if e.follow_through]:
        lines.append("")
        lines.append("Follow-through:")
        for event in dropped:
            lines.extend(_wrap_plain(f"✗ {event.summary}: {', '.join(event.follow_through)}", indent=4))
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


def _wrap_plain(text: str, *, indent: int) -> list[str]:
    """`text` wrapped to `_WIDTH` with no gutter, two spaces in and its
    continuation lines `indent` in."""
    return textwrap.wrap(
        text,
        width=_WIDTH,
        initial_indent="  ",
        subsequent_indent=" " * indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


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
    (drawn just above) set it. An event's actions follow its start, on
    their own line, then its facts."""
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
        if actions := _action_tag(event):
            lines.append((False, f"    {actions}"))
        lines += [(False, f"    {'▹' if event.new_facts else '▸'} {fact}") for fact in event.facts]
        if event.missing:
            lines.append((False, f"    ⚠ no {', no '.join(event.missing)}"))
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
    if event.status != "adjusted" or event.planned_start is None:
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
    if event.status != "adjusted" or event.planned_end is None:
        return ""
    start_shift = event.start - event.planned_start
    end_shift = event.end - event.planned_end
    if not end_shift or start_shift == end_shift:
        return ""
    return f" · {_shift(end_shift)} (was {hm(event.planned_end)})"


def _shift(delta: timedelta) -> str:
    return f"{'+' if delta > timedelta(0) else '−'}{_duration(delta)}"


def _action_tag(event: TimelineEvent) -> str:
    marks = [f"◆ {g}" for g in event.actions if g not in event.new_actions]
    marks += [f"◇ {g}" for g in event.new_actions]
    return " ".join(marks)


def _action_time(live: list[TimelineEvent]) -> list[tuple[str, str]]:
    """Each action's total time across `live` events, longest first."""
    totals: dict[str, timedelta] = {}
    for event in live:
        for action in dict.fromkeys(event.actions + event.new_actions):
            totals[action] = totals.get(action, timedelta()) + (event.end - event.start)
    return [
        (action, _duration(total)) for action, total in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
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
    """The legend, its facts line only when an event has facts, its
    missing line only when an event is missing something, and its
    follow-through line only when a cancellation counts against it."""
    shown = {
        "▸": any(e.facts for e in events),
        "⚠": any(e.missing for e in events),
        "✗": any(e.follow_through for e in events),
    }
    return [line for line in _LEGEND if shown.get(line[0], True)]
