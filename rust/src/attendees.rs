//! Who an event or a goal names, and who that is. Its with_ids and
//! for_ids name people by id -- and circles: a plain circle (kind
//! "circle") stands for its members, every one there unless an `except`
//! on its entry says otherwise ({"circle_id": ..., "except": [...]}); a
//! community stands for itself, as a person does, no member assumed (see
//! stores::people's `CircleKind`). A bare id names a person or a circle
//! alike.
//!
//! Every question of who was at an event, or is part of a goal, goes
//! through `Circles::attendees`, by who's in each circle now: there's no
//! history of who was in it when.

use crate::contract::{Defs, Schema};
use crate::facts::SELF_ID;
use crate::py;
use crate::stores::people::{Circle, CircleKind, Person};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

/// One entry of a with_ids or for_ids list: a person's or circle's id,
/// and those of a plain circle's members it leaves out.
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct Entry {
    pub id: String,
    pub except: Vec<String>,
}

impl Entry {
    pub fn new(id: impl Into<String>) -> Entry {
        Entry { id: id.into(), except: Vec::new() }
    }
}

/// The entries of a with_ids or for_ids list as stored: each an id, or an
/// object with a person_id or circle_id (and a circle's except); any other
/// item left out.
pub fn entries(value: Option<&Value>) -> Vec<Entry> {
    let Some(Value::Array(items)) = value else { return Vec::new() };
    items.iter().filter_map(entry).collect()
}

/// One item's entry, if it's one.
pub fn entry(item: &Value) -> Option<Entry> {
    match item {
        Value::String(s) => Some(Entry::new(s.clone())),
        Value::Object(map) => {
            let id = ["person_id", "circle_id"].iter().find_map(|k| map.get(*k)).and_then(Value::as_str)?;
            let except = match map.get("except") {
                Some(Value::Array(ids)) => ids.iter().filter_map(Value::as_str).map(str::to_string).collect(),
                _ => Vec::new(),
            };
            Some(Entry { id: id.to_string(), except })
        }
        _ => None,
    }
}

/// One named in a goal's with_ids or for_ids: a person or a circle, by
/// id, or a plain circle and those of its members it leaves out
/// ({"circle_id", "except"}).
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct Named {
    pub id: String,
    pub except: Vec<String>,
}

impl Named {
    pub fn entry(&self) -> Entry {
        Entry { id: self.id.clone(), except: self.except.clone() }
    }
}

impl From<&str> for Named {
    fn from(id: &str) -> Named {
        Named { id: id.into(), except: Vec::new() }
    }
}

/// A plain circle named with those of its members who aren't part of it.
#[derive(Serialize, Deserialize, crate::contract::Schema)]
#[serde(deny_unknown_fields)]
#[schema(deny_unknown)]
pub struct CircleExcept {
    /// The circle's id.
    circle_id: String,
    /// Those of its members who aren't, by person id.
    #[schema(optional)]
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    except: Vec<String>,
}

impl Serialize for Named {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        if self.except.is_empty() {
            return serializer.serialize_str(&self.id);
        }
        CircleExcept { circle_id: self.id.clone(), except: self.except.clone() }.serialize(serializer)
    }
}

impl<'de> Deserialize<'de> for Named {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum Given {
            Id(String),
            Circle(CircleExcept),
        }
        Ok(match Given::deserialize(deserializer)? {
            Given::Id(id) => Named { id, except: Vec::new() },
            Given::Circle(c) => Named { id: c.circle_id, except: c.except },
        })
    }
}

impl Schema for Named {
    fn schema(defs: &mut Defs) -> Value {
        let circle = <CircleExcept as Schema>::schema(defs);
        json!({"anyOf": [{"description": "A person's or circle's id: a plain circle, all its members.", "type": "string"}, circle]})
    }
}

/// The entries of goal list `named`.
pub fn named_entries(named: Option<&[Named]>) -> Vec<Entry> {
    named.unwrap_or_default().iter().map(Named::entry).collect()
}

/// A circle, its kind and who's in it now.
#[derive(Clone, Debug, PartialEq)]
pub struct Members {
    pub id: String,
    pub kind: CircleKind,
    pub members: Vec<String>,
}

/// Every circle, with who's in it: to tell who entries name.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Circles(pub Vec<Members>);

/// What's wrong with naming some people and circles with and for.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Naming {
    /// Ids that are neither a person nor a circle ("self" aside).
    pub unknown: Vec<String>,
    /// Those both with and for only through a plain circle -- a member
    /// in one and their circle in the other -- as named in neither
    /// directly.
    pub both: Vec<String>,
    /// The rest, each a phrase: an except where there can't be one.
    pub other: Vec<String>,
}

impl Circles {
    pub fn of(circles: &[Circle], people: &[Person]) -> Circles {
        Circles(
            circles
                .iter()
                .filter_map(|c| {
                    let id = c.id.clone()?;
                    let members = people.iter().filter(|p| p.circles.iter().flatten().any(|i| *i == id)).filter_map(|p| p.id.clone()).collect();
                    Some(Members { kind: c.kind.unwrap_or_default(), id, members })
                })
                .collect(),
        )
    }

    pub fn get(&self, id: &str) -> Option<&Members> {
        self.0.iter().find(|c| c.id == id)
    }

    /// Whether `id` is a plain circle's: one standing for its members.
    pub fn is_plain(&self, id: &str) -> bool {
        self.get(id).is_some_and(|c| c.kind == CircleKind::Circle)
    }

    /// Who `entries` name, each once, in order: each person; each plain
    /// circle's members, less its except (and "self", who's at every
    /// event); each community, as itself.
    pub fn attendees(&self, entries: &[Entry]) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        let mut add = |id: &str| {
            if !out.iter().any(|o| o == id) {
                out.push(id.to_string());
            }
        };
        for entry in entries {
            match self.get(&entry.id) {
                Some(c) if c.kind == CircleKind::Circle => {
                    for member in c.members.iter().filter(|m| m.as_str() != SELF_ID && !entry.except.contains(m)) {
                        add(member);
                    }
                }
                _ => add(&entry.id),
            }
        }
        out
    }

    /// The plain circles `person_id` is in now.
    pub fn containing(&self, person_id: &str) -> Vec<&Members> {
        self.0.iter().filter(|c| c.kind == CircleKind::Circle && c.members.iter().any(|m| m == person_id)).collect()
    }

    /// What's wrong with `with` and `for_`, `is_person` telling a person's
    /// id (besides "self").
    pub fn naming(&self, with: &[Entry], for_: &[Entry], is_person: &dyn Fn(&str) -> bool) -> Naming {
        let mut naming = Naming::default();
        for entry in with.iter().chain(for_) {
            let id = entry.id.as_str();
            let circle = self.get(id);
            if circle.is_none() && id != SELF_ID && !is_person(id) && !naming.unknown.iter().any(|u| u == id) {
                naming.unknown.push(id.to_string());
            }
            if entry.except.is_empty() {
                continue;
            }
            match circle {
                Some(c) if c.kind == CircleKind::Circle => {
                    let outside: Vec<String> = entry.except.iter().filter(|e| !c.members.contains(e)).cloned().collect();
                    if !outside.is_empty() {
                        naming.other.push(format!("{}'s except names {}, who aren't in it", py::repr(id), py::repr_list(&outside)));
                    }
                }
                Some(_) => naming.other.push(format!("{} is a community: it stands for itself, not its members, so it takes no except", py::repr(id))),
                None => naming.other.push(format!("{} takes no except: only a circle does", py::repr(id))),
            }
        }
        let direct = |entries: &[Entry]| entries.iter().map(|e| e.id.clone()).collect::<Vec<_>>();
        let (with_ids, for_ids) = (direct(with), direct(for_));
        let there = self.attendees(with);
        let mut both: Vec<String> = self
            .attendees(for_)
            .into_iter()
            .filter(|p| there.contains(p) && !(with_ids.contains(p) && for_ids.contains(p)))
            .collect();
        both.sort();
        naming.both = both;
        naming
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn circles() -> Circles {
        Circles(vec![
            Members { id: "fam".into(), kind: CircleKind::Circle, members: vec!["roger".into(), "cecile".into(), "ulyssie".into()] },
            Members { id: "dance".into(), kind: CircleKind::Community, members: vec!["sam".into()] },
        ])
    }

    fn named(value: Value) -> Vec<Entry> {
        entries(Some(&value))
    }

    #[test]
    fn a_plain_circle_is_its_members_less_its_except_and_a_community_itself() {
        let found = circles().attendees(&named(json!(["sam", {"circle_id": "fam", "except": ["ulyssie"]}, "roger", "dance"])));
        assert_eq!(found, vec!["sam", "roger", "cecile", "dance"]);
    }

    #[test]
    fn its_members_are_who_is_in_it_now() {
        let circle = Circle { id: Some("fam".into()), name: Some("Family".into()), ..Default::default() };
        let people = vec![
            Person { id: Some("roger".into()), circles: Some(vec!["fam".into()]), ..Default::default() },
            Person { id: Some("bo".into()), ..Default::default() },
        ];
        let found = Circles::of(&[circle], &people);
        assert_eq!(found.0, vec![Members { id: "fam".into(), kind: CircleKind::Circle, members: vec!["roger".into()] }]);
        assert_eq!(found.containing("roger").len(), 1);
    }

    #[test]
    fn naming_finds_who_isnt_anyone_an_except_that_cant_be_and_who_would_be_with_and_for() {
        let is_person = |id: &str| ["sam", "roger", "cecile", "ulyssie"].contains(&id);
        let found = circles().naming(
            &named(json!([{"circle_id": "fam", "except": ["bo"]}, {"circle_id": "dance", "except": ["sam"]}, {"person_id": "sam", "except": ["x"]}])),
            &named(json!(["cecile", "ghost"])),
            &is_person,
        );
        assert_eq!(found.unknown, vec!["ghost"]);
        assert_eq!(found.both, vec!["cecile"]);
        assert_eq!(found.other.len(), 3, "{:?}", found.other);
        assert!(found.other[0].contains("'fam''s except names ['bo'], who aren't in it"), "{:?}", found.other);
        assert!(found.other[1].contains("is a community"));
        assert!(found.other[2].contains("only a circle does"));
    }

    #[test]
    fn a_goal_names_a_circle_bare_or_with_its_except() {
        let given: Vec<Named> = serde_json::from_value(json!(["sam", {"circle_id": "fam", "except": ["ulyssie"]}])).unwrap();
        assert_eq!(given[1], Named { id: "fam".into(), except: vec!["ulyssie".into()] });
        assert_eq!(serde_json::to_value(&given).unwrap(), json!(["sam", {"circle_id": "fam", "except": ["ulyssie"]}]));
        assert!(serde_json::from_value::<Named>(json!({"circle_id": "fam", "minutes": 3})).is_err());
    }
}
