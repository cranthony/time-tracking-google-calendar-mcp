//! Shares: how much of an event each goal, or action, it's tagged with
//! takes -- minutes (a fixed share), percent (of what the fixed shares
//! leave), or neither: a part of the remaining time, what the minutes and
//! percents leave (all of it for a goal, an even part for an action). One
//! with minutes or percent can be given the remaining time too ("+r").
//! An event keeps them as text, "a b=45m c=30% d=10m+r", a bare id being
//! one with neither, so ids written before shares were are read as they
//! always were.

use crate::py;

/// One of an event's shares: a goal's (`GoalShare`) or an action's
/// (`ActionShare`).
pub trait Share: Clone {
    /// What it's a share of, as its problems name it: "goal" or "action".
    const KIND: &'static str;
    /// What one with neither minutes nor percent takes.
    const NEITHER: &'static str;

    fn new(id: String, minutes: Option<i64>, percent: Option<i64>, remaining: bool) -> Self;
    fn id(&self) -> &str;
    fn minutes(&self) -> Option<i64>;
    fn percent(&self) -> Option<i64>;
    /// Whether it's given the remaining time as well as its minutes or
    /// percent. One with neither always takes the remaining time.
    fn remaining(&self) -> bool;
}

/// For serde: a flag left out unless it's set.
pub fn is_false(flag: &bool) -> bool {
    !*flag
}

/// Shares as an event keeps them: "a b=45m c=30% d=10m+r".
pub fn shares_text<S: Share>(shares: &[S]) -> String {
    shares
        .iter()
        .map(|s| {
            let plus = if s.remaining() { "+r" } else { "" };
            match (s.minutes(), s.percent()) {
                (Some(m), _) => format!("{}={m}m{plus}", s.id()),
                (None, Some(p)) => format!("{}={p}%{plus}", s.id()),
                (None, None) => s.id().to_string(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Shares as `shares_text` wrote them; a token that isn't one is skipped.
pub fn parse_shares<S: Share>(text: &str) -> Vec<S> {
    text.split_whitespace()
        .filter_map(|token| match token.split_once('=') {
            None => Some(S::new(token.into(), None, None, false)),
            Some((id, amount)) if !id.is_empty() => {
                let (amount, remaining) = match amount.strip_suffix("+r") {
                    Some(amount) => (amount, true),
                    None => (amount, false),
                };
                if let Some(m) = amount.strip_suffix('m').and_then(|m| m.parse().ok()) {
                    Some(S::new(id.into(), Some(m), None, remaining))
                } else {
                    amount.strip_suffix('%').and_then(|p| p.parse().ok()).map(|p| S::new(id.into(), None, Some(p), remaining))
                }
            }
            Some(_) => None,
        })
        .collect()
}

/// What's wrong with `shares` on an event `event_minutes` long.
pub fn share_problems<S: Share>(shares: &[S], event_minutes: i64) -> Vec<String> {
    let kind = S::KIND;
    let mut problems = Vec::new();
    let mut seen: Vec<&str> = Vec::new();
    let (mut minutes, mut percent) = (0, 0);
    for share in shares {
        let id = share.id().trim();
        if id.is_empty() || id.contains(char::is_whitespace) || id.contains('=') {
            problems.push(format!("{kind} id {} isn't one", py::repr(share.id())));
            continue;
        }
        if seen.contains(&id) {
            problems.push(format!("{kind} {id} is given more than once"));
        }
        seen.push(id);
        match (share.minutes(), share.percent()) {
            (Some(_), Some(_)) => problems.push(format!("{kind} {id} is given both minutes and percent: give one, or neither for {}", S::NEITHER)),
            (Some(m), None) if m <= 0 => problems.push(format!("{kind} {id}'s minutes must be more than 0, not {m}")),
            (Some(m), None) => minutes += m,
            (None, Some(p)) if !(1..=100).contains(&p) => problems.push(format!("{kind} {id}'s percent must be 1 to 100, not {p}")),
            (None, Some(p)) => percent += p,
            (None, None) => {}
        }
    }
    if minutes > event_minutes {
        problems.push(format!("its {kind}s' minutes add up to {minutes}, more than the event's {event_minutes}"));
    }
    if percent > 100 {
        problems.push(format!("its {kind}s' percents add up to {percent}, more than 100"));
    }
    problems
}

/// `shares` as an event `event_minutes` long gives them: only those
/// `known` (still there), each once, minutes cut down to fit in the event
/// (an event shortened since they were given), and percents to 100.
pub fn settled_shares<S: Share>(shares: &[S], event_minutes: i64, known: &dyn Fn(&str) -> bool) -> Vec<S> {
    let (mut minutes_left, mut percent_left) = (event_minutes.max(0), 100);
    let mut out: Vec<S> = Vec::new();
    for share in shares.iter().filter(|s| known(s.id())) {
        if out.iter().any(|s| s.id() == share.id()) {
            continue;
        }
        out.push(if let Some(m) = share.minutes() {
            let m = m.clamp(0, minutes_left);
            minutes_left -= m;
            S::new(share.id().into(), Some(m), None, share.remaining())
        } else if let Some(p) = share.percent() {
            let p = p.clamp(0, percent_left);
            percent_left -= p;
            S::new(share.id().into(), None, Some(p), share.remaining())
        } else {
            share.clone()
        });
    }
    out
}

#[cfg(test)]
#[path = "shares_tests.rs"]
mod tests;
