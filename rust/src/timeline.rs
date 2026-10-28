//! The timeline of a compaction, or of a batch of event changes notes and
//! event edges in time order, as data and as one narrow fixed-width column
//! of text.

use crate::py;
use chrono::{DateTime, Duration, FixedOffset, Offset};
use serde_json::{json, Value};

pub type Moment = DateTime<FixedOffset>;

const WIDTH: usize = 40;
const GUTTER: usize = 6;
const LEGEND: [&str; 9] = [
    "→ note above set this edge",
    "↳ note added to that event",
    "○ note not added   ✓ compacted",
    "◆ action   ◇ action being added",
    "▸ facts   ▹ facts being set",
    "⚑ goal   ⚐ goal being tagged",
    "⚠ missing action or location",
    "✗ counts against follow-through",
    "+/− late/early   ⇢/⇠ moved",
];

#[derive(Debug, Clone, PartialEq, Default)]
pub struct TimelineNote {
    pub id: String,
    pub time: Option<Moment>,
    pub text: Option<String>,
    pub anchors: Vec<String>,
    pub annotates: Option<String>,
    pub ignored: bool,
    pub compacted: bool,
}

impl TimelineNote {
    fn time(&self) -> Moment {
        self.time.expect("a timeline note has a time")
    }

    pub fn to_json(&self) -> Value {
        json!({
            "id": self.id,
            "time": self.time.map(|t| py::pydantic_datetime(&t)),
            "text": self.text,
            "anchors": self.anchors,
            "annotates": self.annotates,
            "ignored": self.ignored,
            "compacted": self.compacted,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct TimelineEvent {
    pub summary: String,
    pub status: String,
    pub event_id: Option<String>,
    pub start: Option<Moment>,
    pub end: Option<Moment>,
    pub planned_start: Option<Moment>,
    pub planned_end: Option<Moment>,
    pub start_note: Option<String>,
    pub end_note: Option<String>,
    pub merged_into: Option<String>,
    pub actions: Vec<String>,
    pub new_actions: Vec<String>,
    pub facts: Vec<String>,
    pub new_facts: bool,
    /// The goals it's tagged with, each named with its share ("Fix the
    /// fence (30m)"), and whether the decisions tag it (`new_goals`).
    pub goals: Vec<String>,
    pub new_goals: bool,
    /// Their ids, in the same order: what `Timeline::goals` finds the
    /// events tagged with each goal by.
    pub goal_ids: Vec<String>,
    pub missing: Vec<String>,
    pub follow_through: Vec<String>,
}

impl TimelineEvent {
    /// Where it sorts: its start, or where it was to start.
    fn at(&self) -> Moment {
        self.start.or(self.planned_start).expect("a timeline event has a start or a planned start")
    }

    pub fn to_json(&self) -> Value {
        let when = |m: &Option<Moment>| m.map(|t| py::pydantic_datetime(&t));
        json!({
            "summary": self.summary,
            "status": self.status,
            "event_id": self.event_id,
            "start": when(&self.start),
            "end": when(&self.end),
            "planned_start": when(&self.planned_start),
            "planned_end": when(&self.planned_end),
            "start_note": self.start_note,
            "end_note": self.end_note,
            "merged_into": self.merged_into,
            "actions": self.actions,
            "new_actions": self.new_actions,
            "facts": self.facts,
            "new_facts": self.new_facts,
            "goals": self.goals,
            "new_goals": self.new_goals,
            "missing": self.missing,
            "follow_through": self.follow_through,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Timeline {
    pub notes: Vec<TimelineNote>,
    pub events: Vec<TimelineEvent>,
    pub now: Option<Moment>,
    pub last_compaction: Option<Moment>,
    pub decided: bool,
    /// The goals closed or due in the window, with the events tagged
    /// with each (see `with_goals`); listed at the end of `text`.
    pub goals: Vec<TimelineGoal>,
    pub text: String,
}

/// A goal closed or due in the window, as the timeline's `goals` lists it:
/// its `title`, `why` it's listed ("done 09:30", "due today"), and the
/// summaries of the events `tagged` with it -- none when nothing is, which
/// is what to look at.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct TimelineGoal {
    pub id: String,
    pub title: String,
    pub why: String,
    pub tagged: Vec<String>,
}

impl TimelineGoal {
    pub fn to_json(&self) -> Value {
        json!({"id": self.id, "title": self.title, "why": self.why, "tagged": self.tagged})
    }
}

impl Timeline {
    pub fn to_json(&self) -> Value {
        json!({
            "notes": self.notes.iter().map(TimelineNote::to_json).collect::<Vec<_>>(),
            "events": self.events.iter().map(TimelineEvent::to_json).collect::<Vec<_>>(),
            "now": self.now.map(|t| py::pydantic_datetime(&t)),
            "last_compaction": self.last_compaction.map(|t| py::pydantic_datetime(&t)),
            "decided": self.decided,
            "goals": self.goals.iter().map(TimelineGoal::to_json).collect::<Vec<_>>(),
            "text": self.text,
        })
    }
}

pub fn build_timeline(
    notes: Vec<TimelineNote>,
    events: Vec<TimelineEvent>,
    now: Option<Moment>,
    decided: bool,
    last_compaction: Option<Moment>,
) -> Timeline {
    let mut notes = notes;
    notes.sort_by_key(|n| n.time());
    let mut events = events;
    events.sort_by_key(|e| e.at());
    let mut timeline = Timeline { notes, events, now, last_compaction, decided, goals: Vec::new(), text: String::new() };
    timeline.text = render(&timeline, true);
    timeline
}

/// `timeline` with `goals` -- the goals closed or due in the window, and
/// the events tagged with each -- listed under "Goals closed or due:" at
/// the end of its text, before the legend. `goals` is what Claude checks
/// its tagging against: a goal there with nothing tagged is one to look
/// for the work on, or to say in the recap that none matched.
pub fn with_goals(timeline: Timeline, goals: Vec<TimelineGoal>) -> Timeline {
    let mut timeline = timeline;
    timeline.goals = goals;
    if timeline.goals.is_empty() {
        return timeline;
    }
    let mut section = vec![String::new(), "Goals closed or due:".to_string()];
    for goal in &timeline.goals {
        let tagged = if goal.tagged.is_empty() { "no event tagged".to_string() } else { goal.tagged.join(", ") };
        section.extend(textwrap(&format!("⚑ {} · {}: {tagged}", goal.title, goal.why), WIDTH, "  ", "    "));
    }
    let mut lines: Vec<String> = timeline.text.lines().map(str::to_string).collect();
    // The legend, if there is one, is the last block: insert before it.
    let legend_at = (0..lines.len())
        .rev()
        .take_while(|i| LEGEND.contains(&lines[*i].as_str()))
        .last()
        .filter(|i| *i > 0 && lines[*i - 1].is_empty())
        .map(|i| i - 1);
    match legend_at {
        Some(at) => {
            let rest = lines.split_off(at);
            lines.extend(section);
            lines.extend(rest);
        }
        None => lines.extend(section),
    }
    timeline.text = lines.join("\n");
    timeline
}

/// What heads a day revisited for historical notes, after its date.
pub const REVISITED_LABEL: &str = "revisited";

/// Several days' timelines as one, each under a heading with its date.
pub fn join_days(days: Vec<(Moment, Timeline)>) -> Timeline {
    join_labelled(days.into_iter().map(|(start, t)| (start, "", t)).collect())
}

/// `join_days`, each heading with its label (if any) after the date.
pub fn join_labelled(days: Vec<(Moment, &str, Timeline)>) -> Timeline {
    if days.len() == 1 {
        return days.into_iter().next().unwrap().2;
    }
    let count = days.len();
    let mut joined = Timeline {
        notes: days.iter().flat_map(|(_, _, t)| t.notes.clone()).collect(),
        events: days.iter().flat_map(|(_, _, t)| t.events.clone()).collect(),
        now: days.last().unwrap().2.now,
        last_compaction: days.iter().find_map(|(_, _, t)| t.last_compaction),
        decided: days[0].2.decided,
        goals: Vec::new(),
        text: String::new(),
    };
    let mut lines: Vec<String> = Vec::new();
    for (number, (start, label, timeline)) in days.into_iter().enumerate() {
        let mut timeline = timeline;
        if number < count - 1 {
            timeline.now = None;
        }
        let tz = display_tz(&timeline);
        let mut heading = start.with_timezone(&tz).format("━━ %a %d %b ").to_string();
        if !label.is_empty() {
            heading.push_str(&format!("· {label} "));
        }
        if !lines.is_empty() {
            lines.push(String::new());
        }
        lines.push(ljust(&heading, WIDTH, '━'));
        lines.push(render(&timeline, false));
    }
    if joined.decided {
        lines.push(String::new());
        lines.extend(legend(&joined.events));
    }
    joined.text = lines.join("\n");
    joined
}

/// A span's timeline cut into its days, each starting at one of `starts`.
pub fn split_days(timeline: &Timeline, starts: &[Moment]) -> Vec<(Moment, Timeline)> {
    if starts.len() < 2 {
        return match starts.first() {
            Some(start) => vec![(*start, timeline.clone())],
            None => vec![(timeline.now.expect("a span has a now"), timeline.clone())],
        };
    }
    let day_of = |moment: Moment| -> usize { (0..starts.len()).filter(|i| moment >= starts[*i]).max().unwrap_or(0) };
    let mut notes: Vec<Vec<TimelineNote>> = vec![Vec::new(); starts.len()];
    let mut events: Vec<Vec<TimelineEvent>> = vec![Vec::new(); starts.len()];
    for note in &timeline.notes {
        notes[day_of(note.time())].push(note.clone());
    }
    for event in &timeline.events {
        events[day_of(event.at())].push(event.clone());
    }
    starts
        .iter()
        .enumerate()
        .filter(|(i, _)| *i == 0 || !notes[*i].is_empty() || !events[*i].is_empty())
        .map(|(i, start)| {
            (
                *start,
                build_timeline(
                    notes[i].clone(),
                    events[i].clone(),
                    timeline.now,
                    timeline.decided,
                    if i == 0 { timeline.last_compaction } else { None },
                ),
            )
        })
        .collect()
}

pub fn render(timeline: &Timeline, with_legend: bool) -> String {
    let tz = display_tz(timeline);
    let hm = |moment: &Moment| moment.with_timezone(&tz).format("%H:%M").to_string();
    let live: Vec<&TimelineEvent> = timeline.events.iter().filter(|e| e.start.is_some() && e.end.is_some()).collect();
    let removed: Vec<&TimelineEvent> = timeline.events.iter().filter(|e| e.start.is_none() && e.planned_start.is_some()).collect();
    let mut moments: Vec<Moment> = Vec::new();
    for m in timeline
        .notes
        .iter()
        .map(|n| n.time())
        .chain(live.iter().map(|e| e.start.unwrap()))
        .chain(live.iter().map(|e| e.end.unwrap()))
        .chain(removed.iter().map(|e| e.planned_start.unwrap()))
    {
        if !moments.contains(&m) {
            moments.push(m);
        }
    }
    moments.sort();

    let mut lines: Vec<String> = Vec::new();
    let mut markers: Vec<(Moment, i32, String)> = Vec::new();
    if let Some(last) = timeline.last_compaction {
        let mut label = "last compaction".to_string();
        if let Some(now) = timeline.now {
            if last.with_timezone(&tz).date_naive() != now.with_timezone(&tz).date_naive() {
                label.push_str(&last.with_timezone(&tz).format(" (%a %d %b)").to_string());
            }
        }
        markers.push((last, 0, label));
    }
    if let Some(now) = timeline.now {
        if moments.first().is_some_and(|first| now > *first) {
            markers.push((now, 1, "now".into()));
        }
    }
    markers.sort();
    let marker_line = |at: &Moment, label: &str| ljust(&format!("{} ┄┄ {label} ", hm(at)), WIDTH, '┄');

    for moment in &moments {
        while markers.first().is_some_and(|(at, kind, _)| at < moment || (at == moment && *kind == 0)) {
            let (at, _, label) = markers.remove(0);
            lines.push(marker_line(&at, &label));
        }
        let notes_here: Vec<&TimelineNote> = timeline.notes.iter().filter(|n| n.time() == *moment).collect();
        let here: Vec<&str> = notes_here.iter().map(|n| n.id.as_str()).collect();
        let mut rows: Vec<(bool, String, usize)> = Vec::new();
        for note in &notes_here {
            rows.push((false, note_text(note, timeline.decided), 2));
            if let Some(annotates) = &note.annotates {
                rows.push((false, format!("  ↳ {annotates}"), 4));
            }
            for edge in edges_set_elsewhere(note, &live, &hm) {
                rows.push((false, format!("  → {edge}"), 4));
            }
        }
        for (anchored, edge) in edge_lines(moment, &live, &removed, &here, &hm) {
            let indent = if edge.starts_with("    ") { 6 } else { 2 };
            rows.push((anchored, edge, indent));
        }
        for (i, (anchored, text, indent)) in rows.iter().enumerate() {
            let time = if i == 0 { hm(moment) } else { String::new() };
            let prefix = format!("{time:>5}{}", if *anchored { '→' } else { ' ' });
            lines.extend(wrap(&prefix, text, *indent));
        }
    }
    for (at, _, label) in &markers {
        lines.push(marker_line(at, label));
    }
    let action_time = action_time(&live);
    if !action_time.is_empty() {
        lines.push(String::new());
        lines.push("Action time:".into());
        for (action, duration) in action_time {
            lines.push(format!("{duration:>7}  {action}"));
        }
    }
    let gaps: Vec<&&TimelineEvent> = live.iter().filter(|e| !e.missing.is_empty()).collect();
    if !gaps.is_empty() {
        lines.push(String::new());
        lines.push("Missing:".into());
        for event in gaps {
            lines.extend(textwrap(&format!("⚠ {}: {}", event.summary, event.missing.join(", ")), WIDTH, "  ", "    "));
        }
    }
    let dropped: Vec<&TimelineEvent> = timeline.events.iter().filter(|e| !e.follow_through.is_empty()).collect();
    if !dropped.is_empty() {
        lines.push(String::new());
        lines.push("Follow-through:".into());
        for event in dropped {
            lines.extend(textwrap(&format!("✗ {}: {}", event.summary, event.follow_through.join(", ")), WIDTH, "  ", "    "));
        }
    }
    if timeline.decided && with_legend {
        lines.push(String::new());
        lines.extend(legend(&timeline.events));
    }
    lines.join("\n")
}

fn wrap(prefix: &str, text: &str, indent: usize) -> Vec<String> {
    let mut wrapped = textwrap(text, WIDTH - GUTTER, "", &" ".repeat(indent));
    if wrapped.is_empty() {
        wrapped.push(String::new());
    }
    wrapped
        .iter()
        .enumerate()
        .map(|(i, line)| {
            let lead = if i == 0 { prefix.to_string() } else { " ".repeat(GUTTER) };
            format!("{lead}{line}").trim_end().to_string()
        })
        .collect()
}

fn edges_set_elsewhere(note: &TimelineNote, live: &[&TimelineEvent], hm: &dyn Fn(&Moment) -> String) -> Vec<String> {
    let mut edges = Vec::new();
    for event in live {
        if event.start_note.as_deref() == Some(note.id.as_str()) && event.start != note.time {
            edges.push(format!("start of {} ({})", event.summary, hm(&event.start.unwrap())));
        }
        if event.end_note.as_deref() == Some(note.id.as_str()) && event.end != note.time {
            edges.push(format!("end of {} ({})", event.summary, hm(&event.end.unwrap())));
        }
    }
    edges
}

fn edge_lines(
    moment: &Moment,
    live: &[&TimelineEvent],
    removed: &[&TimelineEvent],
    here: &[&str],
    hm: &dyn Fn(&Moment) -> String,
) -> Vec<(bool, String)> {
    let ending: Vec<&&TimelineEvent> = live.iter().filter(|e| e.end.as_ref() == Some(moment)).collect();
    let starting: Vec<&&TimelineEvent> = live.iter().filter(|e| e.start.as_ref() == Some(moment)).collect();
    let mut lines = Vec::new();
    for event in &ending {
        let tag = end_tag(event, hm);
        let anchored = event.end_note.as_deref().is_some_and(|n| here.contains(&n));
        if !tag.is_empty() || anchored || starting.is_empty() {
            lines.push((anchored, format!("└ {} ends{tag}", event.summary)));
        }
    }
    for event in &starting {
        let joint = if ending.is_empty() { "┌" } else { "├" };
        let anchored = event.start_note.as_deref().is_some_and(|n| here.contains(&n));
        lines.push((anchored, format!("{joint} {}{}", event.summary, start_tag(event, hm))));
        let actions = action_tag(event);
        if !actions.is_empty() {
            lines.push((false, format!("    {actions}")));
        }
        for fact in &event.facts {
            lines.push((false, format!("    {} {fact}", if event.new_facts { '▹' } else { '▸' })));
        }
        for goal in &event.goals {
            lines.push((false, format!("    {} {goal}", if event.new_goals { '⚐' } else { '⚑' })));
        }
        if !event.missing.is_empty() {
            lines.push((false, format!("    ⚠ no {}", event.missing.join(", no "))));
        }
    }
    for event in removed {
        if event.planned_start.as_ref() != Some(moment) {
            continue;
        }
        let what = if event.status == "merged" {
            format!("merged into {}", event.merged_into.clone().unwrap_or_else(|| "None".into()))
        } else {
            "cancelled".into()
        };
        lines.push((
            false,
            format!(
                "✕ {} · {what} (was {}–{})",
                event.summary,
                hm(&event.planned_start.unwrap()),
                hm(&event.planned_end.expect("a removed event has a planned end"))
            ),
        ));
    }
    lines
}

fn start_tag(event: &TimelineEvent, hm: &dyn Fn(&Moment) -> String) -> String {
    if event.status == "new" {
        return " · new".into();
    }
    let (Some(planned_start), true) = (event.planned_start, event.status == "adjusted") else {
        return String::new();
    };
    let start_shift = event.start.unwrap() - planned_start;
    let end_shift = event.end.unwrap() - event.planned_end.unwrap();
    if !start_shift.is_zero() && start_shift == end_shift {
        let arrow = if start_shift > Duration::zero() { "⇢" } else { "⇠" };
        return format!(" · {arrow}{} (was {}–{})", duration(start_shift), hm(&planned_start), hm(&event.planned_end.unwrap()));
    }
    if !start_shift.is_zero() {
        return format!(" · {} (was {})", shift(start_shift), hm(&planned_start));
    }
    String::new()
}

fn end_tag(event: &TimelineEvent, hm: &dyn Fn(&Moment) -> String) -> String {
    let (Some(planned_end), true) = (event.planned_end, event.status == "adjusted") else {
        return String::new();
    };
    let start_shift = event.start.unwrap() - event.planned_start.unwrap();
    let end_shift = event.end.unwrap() - planned_end;
    if end_shift.is_zero() || start_shift == end_shift {
        return String::new();
    }
    format!(" · {} (was {})", shift(end_shift), hm(&planned_end))
}

fn shift(delta: Duration) -> String {
    format!("{}{}", if delta > Duration::zero() { '+' } else { '−' }, duration(delta))
}

fn action_tag(event: &TimelineEvent) -> String {
    let mut marks: Vec<String> =
        event.actions.iter().filter(|g| !event.new_actions.contains(g)).map(|g| format!("◆ {g}")).collect();
    marks.extend(event.new_actions.iter().map(|g| format!("◇ {g}")));
    marks.join(" ")
}

fn action_time(live: &[&TimelineEvent]) -> Vec<(String, String)> {
    let mut totals: Vec<(String, Duration)> = Vec::new();
    for event in live {
        let mut seen: Vec<&String> = Vec::new();
        for action in event.actions.iter().chain(event.new_actions.iter()) {
            if seen.contains(&action) {
                continue;
            }
            seen.push(action);
            let length = event.end.unwrap() - event.start.unwrap();
            match totals.iter_mut().find(|(a, _)| a == action) {
                Some(slot) => slot.1 += length,
                None => totals.push((action.clone(), length)),
            }
        }
    }
    totals.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    totals.into_iter().map(|(action, total)| (action, duration(total))).collect()
}

fn note_text(note: &TimelineNote, decided: bool) -> String {
    let placed = !note.anchors.is_empty() || note.annotates.is_some() || !decided;
    let marker = if note.compacted {
        '✓'
    } else if placed && !note.ignored {
        '●'
    } else {
        '○'
    };
    let text = note.text.as_deref().map(str::trim).filter(|t| !t.is_empty()).unwrap_or("(no description)");
    let suffix = if note.compacted { " (compacted)" } else { "" };
    format!("{marker} {text}{suffix}")
}

pub fn duration(delta: Duration) -> String {
    let minutes_total = delta.num_seconds().abs() / 60;
    let (hours, minutes) = (minutes_total / 60, minutes_total % 60);
    if hours > 0 {
        format!("{hours}h{minutes:02}m")
    } else {
        format!("{minutes}m")
    }
}

/// The notes' own zone -- written by the user -- or the events', else UTC.
fn display_tz(timeline: &Timeline) -> FixedOffset {
    if let Some(note) = timeline.notes.first() {
        return note.time().offset().fix();
    }
    if let Some(event) = timeline.events.first() {
        return event.at().offset().fix();
    }
    FixedOffset::east_opt(0).unwrap()
}

fn legend(events: &[TimelineEvent]) -> Vec<String> {
    LEGEND
        .iter()
        .filter(|line| match line.chars().next() {
            Some('▸') => events.iter().any(|e| !e.facts.is_empty()),
            Some('⚑') => events.iter().any(|e| !e.goals.is_empty()),
            Some('⚠') => events.iter().any(|e| !e.missing.is_empty()),
            Some('✗') => events.iter().any(|e| !e.follow_through.is_empty()),
            _ => true,
        })
        .map(|l| l.to_string())
        .collect()
}

/// `str.ljust(width, fill)`, counting characters.
pub fn ljust(text: &str, width: usize, fill: char) -> String {
    let length = text.chars().count();
    if length >= width {
        return text.to_string();
    }
    let mut out = text.to_string();
    out.extend(std::iter::repeat(fill).take(width - length));
    out
}

const WHITESPACE: [char; 6] = ['\t', '\n', '\u{0b}', '\u{0c}', '\r', ' '];

/// `textwrap.wrap(text, width, initial_indent=, subsequent_indent=,
/// break_long_words=False, break_on_hyphens=False)`, exactly.
pub fn textwrap(text: &str, width: usize, initial_indent: &str, subsequent_indent: &str) -> Vec<String> {
    // expand_tabs, then each whitespace character as a space.
    let mut expanded = String::new();
    let mut column = 0;
    for c in text.chars() {
        match c {
            '\t' => {
                let pad = 8 - column % 8;
                expanded.extend(std::iter::repeat(' ').take(pad));
                column += pad;
            }
            '\n' | '\r' => {
                expanded.push(' ');
                column = 0;
            }
            c if WHITESPACE.contains(&c) => {
                expanded.push(' ');
                column += 1;
            }
            c => {
                expanded.push(c);
                column += 1;
            }
        }
    }
    // Chunks: runs of whitespace, and runs of anything else.
    let mut chunks: Vec<String> = Vec::new();
    for c in expanded.chars() {
        let space = c == ' ';
        match chunks.last_mut() {
            Some(last) if last.starts_with(' ') == space => last.push(c),
            _ => chunks.push(c.to_string()),
        }
    }
    let python_blank = |s: &str| s.trim().is_empty();
    chunks.reverse();
    let mut lines: Vec<String> = Vec::new();
    while !chunks.is_empty() {
        let mut current: Vec<String> = Vec::new();
        let mut length = 0usize;
        let indent = if lines.is_empty() { initial_indent } else { subsequent_indent };
        let room = width.saturating_sub(indent.chars().count());
        if python_blank(chunks.last().unwrap()) && !lines.is_empty() {
            chunks.pop();
        }
        while let Some(chunk) = chunks.last() {
            let l = chunk.chars().count();
            if length + l <= room {
                length += l;
                current.push(chunks.pop().unwrap());
            } else {
                break;
            }
        }
        if let Some(chunk) = chunks.last() {
            if chunk.chars().count() > room && current.is_empty() {
                current.push(chunks.pop().unwrap());
            }
        }
        if current.last().is_some_and(|c| python_blank(c)) {
            current.pop();
        }
        if !current.is_empty() {
            lines.push(format!("{indent}{}", current.concat()));
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wraps_like_python() {
        // textwrap.wrap("the quick brown fox jumps over the lazy dog", width=10,
        //               subsequent_indent="  ", break_long_words=False, break_on_hyphens=False)
        assert_eq!(
            textwrap("the quick brown fox jumps over the lazy dog", 10, "", "  "),
            vec!["the quick", "  brown", "  fox", "  jumps", "  over the", "  lazy dog"]
        );
        assert_eq!(textwrap("averyveryverylongword x", 5, "", ""), vec!["averyveryverylongword", "x"]);
        assert!(textwrap("   ", 5, "", "").is_empty());
        assert_eq!(textwrap("  lead", 10, "", ""), vec!["  lead"]);
    }
}
