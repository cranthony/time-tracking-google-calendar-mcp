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
    /// Theirs, all together: "goals'", "people's".
    const THEIRS: &'static str;
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
        problems.push(format!("its {} minutes add up to {minutes}, more than the event's {event_minutes}", S::THEIRS));
    }
    if percent > 100 {
        problems.push(format!("its {} percents add up to {percent}, more than 100", S::THEIRS));
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

/// A share of an event by an id -- an action's, a person's, a place's --
/// sent and shown as its bare id when it has neither minutes nor percent,
/// as those ids always were, and as an object (`$split`, a schema model)
/// when it has one. `$kind` names it in its problems; `$neither` says
/// what one with neither takes.
macro_rules! id_share {
    (
        $(#[$doc:meta])*
        $name:ident, $split:ident, $field:ident, $kind:literal, $theirs:literal, $neither:literal, $id_doc:literal, $split_doc:literal
    ) => {
        $(#[$doc])*
        #[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
        pub struct $name {
            pub $field: String,
            pub minutes: Option<i64>,
            pub percent: Option<i64>,
            pub remaining: bool,
        }

        impl $name {
            /// It with neither minutes nor percent.
            pub fn whole(id: impl Into<String>) -> $name {
                $name { $field: id.into(), ..Default::default() }
            }
        }

        impl From<&str> for $name {
            fn from(id: &str) -> $name {
                $name::whole(id)
            }
        }

        impl From<String> for $name {
            fn from(id: String) -> $name {
                $name::whole(id)
            }
        }

        impl $crate::stores::shares::Share for $name {
            const KIND: &'static str = $kind;
            const THEIRS: &'static str = $theirs;
            const NEITHER: &'static str = $neither;

            fn new(id: String, minutes: Option<i64>, percent: Option<i64>, remaining: bool) -> Self {
                $name { $field: id, minutes, percent, remaining: remaining && (minutes.is_some() || percent.is_some()) }
            }
            fn id(&self) -> &str {
                &self.$field
            }
            fn minutes(&self) -> Option<i64> {
                self.minutes
            }
            fn percent(&self) -> Option<i64> {
                self.percent
            }
            fn remaining(&self) -> bool {
                self.remaining
            }
        }

        #[doc = $split_doc]
        #[derive(serde::Serialize, serde::Deserialize, $crate::contract::Schema)]
        #[serde(deny_unknown_fields)]
        #[schema(deny_unknown)]
        pub struct $split {
            $field: String,
            #[serde(default, skip_serializing_if = "Option::is_none")]
            minutes: Option<i64>,
            #[serde(default, skip_serializing_if = "Option::is_none")]
            percent: Option<i64>,
            /// The remaining time too, what the minutes and percents
            /// leave, as one given as a bare id takes.
            #[schema(default = "false")]
            #[serde(default, skip_serializing_if = "crate::stores::shares::is_false")]
            remaining: bool,
        }

        impl serde::Serialize for $name {
            fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                if self.minutes.is_none() && self.percent.is_none() {
                    return serializer.serialize_str(&self.$field);
                }
                $split { $field: self.$field.clone(), minutes: self.minutes, percent: self.percent, remaining: self.remaining }
                    .serialize(serializer)
            }
        }

        impl<'de> serde::Deserialize<'de> for $name {
            fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                #[derive(serde::Deserialize)]
                #[serde(untagged)]
                enum Given {
                    Id(String),
                    Split($split),
                }
                Ok(match Given::deserialize(deserializer)? {
                    Given::Id(id) => $name::whole(id),
                    Given::Split(s) => <$name as $crate::stores::shares::Share>::new(s.$field, s.minutes, s.percent, s.remaining),
                })
            }
        }

        impl $crate::contract::Schema for $name {
            fn schema(defs: &mut $crate::contract::Defs) -> serde_json::Value {
                let split = <$split as $crate::contract::Schema>::schema(defs);
                serde_json::json!({"anyOf": [{"description": $id_doc, "type": "string"}, split]})
            }
        }
    };
}

pub(crate) use id_share;

#[cfg(test)]
#[path = "shares_tests.rs"]
mod tests;
