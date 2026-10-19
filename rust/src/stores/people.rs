//! People and circles: who the user spends time with -- the user themself
//! always first, as "self" -- in the People and Circles tabs.

use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::contract::{wire_text, RowItem, Schema};
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::{CIRCLES, PEOPLE};
use crate::stores::cancellations::CancelledEvent;
use crate::stores::foundations::{self, foundation_problems, Node, NodesFn, OnGraph};
use crate::stores::groups::{repr_or_none, set, str_or_none};
use crate::stores::locations::dedup;
use crate::stores::scoring::aggregation_problems;
use crate::stores::traits::{fact_override_problems, person_traits_problems, settled_fact_overrides, settled_spec, spec_with_new_part_ids, SpecOf, Trait};
use anyhow::bail;
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::sync::Arc;

pub const SELF_ID: &str = "self";
pub const SELF_NAME: &str = "Me";
pub const PERSON_STATUSES: [&str; 3] = ["active", "archived", "deleted"];
pub const DEFAULT_STATUSES: [&str; 1] = ["active"];
pub const MAX_NAME_LENGTH: usize = 100;
pub const PERSON_CLEARABLE_FIELDS: [&str; 4] = ["context", "circles", "traits", "foundations"];
pub const CIRCLE_CLEARABLE_FIELDS: [&str; 2] = ["note", "scoring"];

// -- the types -------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum Status {
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "archived")]
    Archived,
    #[serde(rename = "deleted")]
    Deleted,
}

/// A person as the people tools return them: plus their circles'
/// names, their trait facts' summaries ({trait id: {fact id: summary}}),
/// and the events the user cancelled that count against their
/// follow-through.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedPerson {
    pub id: Option<String>,
    pub name: Option<String>,
    pub context: Option<String>,
    pub status: Option<Status>,
    pub circles: Option<Vec<String>>,
    pub traits: Option<Map<String, Value>>,
    pub foundations: Option<Vec<String>>,
    pub circle_names: Option<Vec<String>>,
    pub facts: Option<IndexMap<String, IndexMap<String, String>>>,
    pub cancelled_events: Option<Vec<CancelledEvent>>,
    pub version: Option<i64>,
}

/// One person the user spends time with (see get_people).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct Person {
    pub id: Option<String>,
    pub name: Option<String>,
    pub context: Option<String>,
    #[row(text)]
    pub status: Option<Status>,
    pub circles: Option<Vec<String>>,
    pub traits: Option<Map<String, Value>>,
    pub foundations: Option<Vec<String>>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct CreatedPerson {
    pub person: ListedPerson,
    pub created_id: String,
}

/// A circle as the circle tools return it: plus who's in it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedCircle {
    pub id: Option<String>,
    pub name: Option<String>,
    pub note: Option<String>,
    pub scoring: Option<Map<String, Value>>,
    pub member_ids: Option<Vec<String>>,
    pub version: Option<i64>,
}

/// One circle: a group of people, such as family (see get_circles).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct Circle {
    pub id: Option<String>,
    pub name: Option<String>,
    pub note: Option<String>,
    /// How its people's scores combine into its own: an aggregation (see
    /// get_scoring_defaults), its weights by person id. Without it, the
    /// default's (scoring defaults' circles).
    pub scoring: Option<Map<String, Value>>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct CreatedCircle {
    pub circle: ListedCircle,
    pub created_id: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct DeletedCircle {
    pub deleted: Circle,
    pub left: Vec<String>,
}

impl OnGraph for Person {
    fn foundations_mut(&mut self) -> &mut Option<Vec<String>> {
        &mut self.foundations
    }
}

pub const PERSON_ROW: RowType = <Person as rows::RowItem>::ROW_TYPE;

pub const CIRCLE_ROW: RowType = <Circle as rows::RowItem>::ROW_TYPE;

pub type TraitIds = Arc<dyn Fn() -> anyhow::Result<Vec<String>> + Send + Sync>;

pub fn self_person() -> Person {
    // Version 0 until it's first saved, as anything saved before versions.
    Person { id: Some(SELF_ID.into()), name: Some(SELF_NAME.into()), status: Some(Status::Active), version: Some(0), ..Default::default() }
}

fn is_self(person: &Person) -> bool {
    person.id.as_deref() == Some(SELF_ID)
}

/// Self, from their row (if any), with what it leaves out filled in.
fn as_self(person: Option<&Person>) -> Person {
    match person {
        None => self_person(),
        Some(person) => Person {
            name: Some(set(person.name.as_deref()).unwrap_or(SELF_NAME).to_string()),
            status: Some(Status::Active),
            ..person.clone()
        },
    }
}

/// A person's status as its text ("active"), if they have one.
fn status_of(person: &Person) -> Option<String> {
    person.status.as_ref().map(wire_text)
}

#[derive(Clone)]
pub struct People {
    sheet: RowSheet,
    circle_sheet: RowSheet,
    trait_ids: Option<TraitIds>,
    habits: Option<NodesFn>,
    traits: Option<TraitsFn>,
}

/// Every trait, to check a person's own fact prompts against.
pub type TraitsFn = Arc<dyn Fn() -> anyhow::Result<Vec<Trait>> + Send + Sync>;

impl People {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str, trait_ids: Option<TraitIds>) -> anyhow::Result<People> {
        let sheet = RowSheet::ensure(sheets, spreadsheet_id, PEOPLE.0, PEOPLE.1, PERSON_ROW, &["id", "name"])?;
        let circle_sheet = RowSheet::ensure(sheets, spreadsheet_id, CIRCLES.0, CIRCLES.1, CIRCLE_ROW, &["id", "name"])?;
        Ok(People { sheet, circle_sheet, trait_ids, habits: None, traits: None })
    }

    /// It, checking a person's own fact prompts against `traits`' facts.
    pub fn with_traits(mut self, traits: TraitsFn) -> People {
        self.traits = Some(traits);
        self
    }

    /// `person`'s own fact prompts checked against their traits' facts --
    /// each new or changed from `before`'s, for a fact its trait has -- and
    /// those of facts their trait no longer has let go of.
    fn settle_fact_overrides(&self, person: &mut Person, before: Option<&Person>) -> anyhow::Result<()> {
        let (Some(spec), Some(lookup)) = (&person.traits, &self.traits) else { return Ok(()) };
        if !spec.contains_key("facts") {
            return Ok(());
        }
        let traits = lookup()?;
        let problems = fact_override_problems(spec, before.and_then(|b| b.traits.as_ref()), &traits);
        if !problems.is_empty() {
            let label = set(person.id.as_deref()).map(|i| format!("person {i}")).unwrap_or_else(|| "the person".into());
            bail!("{}", problems.iter().map(|p| format!("{label}'s traits {p}")).collect::<Vec<_>>().join("; "));
        }
        person.traits = Some(settled_fact_overrides(spec, Some(&traits)));
        Ok(())
    }

    /// It, checking foundations against `habits`' nodes too.
    pub fn with_habits(mut self, habits: NodesFn) -> People {
        self.habits = Some(habits);
        self
    }

    /// Every person as a node of the habit graph: any may be a
    /// foundation, though only those with foundations (even none) are
    /// on it by themselves.
    pub fn nodes(&self) -> anyhow::Result<Vec<Node>> {
        Ok(person_nodes(&self.all()?))
    }

    /// Takes `subject` (a habit's or a person's, deleted) out of every
    /// person's foundations.
    pub fn drop_foundation(&self, subject: &str) -> anyhow::Result<()> {
        let mut people = self.read_people()?;
        if foundations::without(&mut people, subject) {
            self.sheet.write_items(&people)?;
        }
        Ok(())
    }

    /// The People tab's people, their traits settled (see
    /// traits::settled_spec).
    fn read_people(&self) -> anyhow::Result<Vec<Person>> {
        Ok(self.sheet.read_items::<Person>()?.into_iter().map(|p| Person { traits: p.traits.clone().map(settled_spec), ..p }).collect())
    }

    pub fn whole_tabs(&self) -> Vec<TabRange> {
        vec![self.sheet.whole_tab(), self.circle_sheet.whole_tab()]
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    /// Every person, self first, then in sheet order.
    pub fn all(&self) -> anyhow::Result<Vec<Person>> {
        self.prefetch(&self.whole_tabs())?;
        let people = self.read_people()?;
        let mine = people.iter().find(|p| is_self(p));
        let mut out = vec![as_self(mine)];
        out.extend(people.iter().filter(|p| !is_self(p)).cloned());
        Ok(out)
    }

    pub fn circles(&self) -> anyhow::Result<Vec<Circle>> {
        self.prefetch(&self.whole_tabs())?;
        self.circle_sheet.read_items()
    }

    pub fn get_people(&self, statuses: Option<&[String]>) -> anyhow::Result<Vec<ListedPerson>> {
        let statuses = check_statuses(statuses)?;
        let circles = self.circles()?;
        Ok(self.all()?.iter().filter(|p| statuses.iter().any(|s| Some(s) == status_of(p).as_ref())).map(|p| listed(p, &circles)).collect())
    }

    pub fn get_person(&self, id_or_name: &str) -> anyhow::Result<ListedPerson> {
        let person = find_person(&self.all()?, id_or_name)?;
        Ok(listed(&person, &self.circles()?))
    }

    pub fn get_circles(&self) -> anyhow::Result<Vec<ListedCircle>> {
        let people = self.all()?;
        Ok(self.circles()?.iter().map(|c| listed_circle(c, &people)).collect())
    }

    pub fn get_circle(&self, id_or_name: &str) -> anyhow::Result<ListedCircle> {
        let circle = find_circle(&self.circles()?, id_or_name)?;
        Ok(listed_circle(&circle, &self.all()?))
    }

    pub fn create_person(&self, person: &Person) -> anyhow::Result<CreatedPerson> {
        let people = self.read_people()?;
        let circles = self.circles()?;
        let id = match rows::created(person, &people, &[Some(SELF_ID)])? {
            rows::Created::New(id) => id,
            rows::Created::Again(made) => return Ok(CreatedPerson { person: listed(&made, &circles), created_id: made.id.clone().unwrap_or_default() }),
        };
        let traits = person.traits.as_ref().map(spec_with_new_part_ids);
        let mut new =
            Person { id: Some(id.clone()), status: Some(person.status.unwrap_or(Status::Active)), traits, version: None, ..person.clone() };
        self.settle_fact_overrides(&mut new, None)?;
        let mut everyone = vec![self_person()];
        everyone.extend(people.iter().cloned());
        check_context(&new, &everyone)?;
        let mut all = people.clone();
        all.push(new.clone());
        self.write_people(&all, &circles, &id)?;
        Ok(CreatedPerson { person: listed(&new, &circles), created_id: id })
    }

    pub fn update_person(&self, person: &Person, clear_fields: &[String]) -> anyhow::Result<ListedPerson> {
        let Some(id) = set(person.id.as_deref()).map(str::to_string) else {
            bail!("update_person needs the person's id");
        };
        rows::check_clear_item(person, clear_fields, &PERSON_CLEARABLE_FIELDS)?;
        let mut people = self.read_people()?;
        let mut index = people.iter().position(|p| p.id.as_deref() == Some(id.as_str()));
        if index.is_none() && id == SELF_ID {
            people.push(self_person());
            index = Some(people.len() - 1);
        }
        let Some(index) = index else {
            rows::expect_item_version::<Person>(&id, None, person.version)?;
            find_person(&self.all()?, &id)?;
            unreachable!()
        };
        let was_deleted = people[index].status == Some(Status::Deleted);
        let given = Person { traits: person.traits.as_ref().map(spec_with_new_part_ids), ..person.clone() };
        let mut changed = rows::updated_item(&people[index], &given, clear_fields, &["id"])?;
        self.settle_fact_overrides(&mut changed, Some(&people[index]))?;
        if person.name.is_some() || clear_fields.iter().any(|c| c == "context") {
            let mut everyone = vec![self_person()];
            everyone.extend(people.iter().cloned());
            check_context(&changed, &everyone)?;
        }
        people[index] = changed.clone();
        if !was_deleted && changed.status == Some(Status::Deleted) {
            foundations::without(&mut people, &id);
        }
        let circles = self.circles()?;
        self.write_people(&people, &circles, &id)?;
        let shown = if is_self(&changed) { as_self(Some(&changed)) } else { changed };
        Ok(listed(&shown, &circles))
    }

    fn write_people(&self, people: &[Person], circles: &[Circle], changed: &str) -> anyhow::Result<()> {
        let checked: Vec<Person> =
            if people.iter().any(is_self) { people.to_vec() } else { std::iter::once(self_person()).chain(people.iter().cloned()).collect() };
        let trait_ids = match &self.trait_ids {
            Some(lookup) if people.iter().any(|p| p.traits.is_some()) => Some(lookup()?),
            _ => None,
        };
        let mut problems = person_problems(&checked, circles, trait_ids.as_deref());
        if let Some(person) = checked.iter().find(|p| p.id.as_deref() == Some(changed)) {
            if let Some(given) = &person.foundations {
                let mut nodes = person_nodes(&checked);
                if let Some(habits) = &self.habits {
                    nodes.extend(habits()?);
                }
                for p in foundation_problems(changed, &json!(given), &nodes) {
                    problems.push(format!("person {changed}'s {p}"));
                }
            }
        }
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.sheet.write_items(people)
    }

    pub fn create_circle(&self, circle: &Circle) -> anyhow::Result<CreatedCircle> {
        let circles = self.circles()?;
        let id = match rows::created(circle, &circles, &[])? {
            rows::Created::New(id) => id,
            rows::Created::Again(made) => {
                return Ok(CreatedCircle { circle: listed_circle(&made, &self.all()?), created_id: made.id.clone().unwrap_or_default() })
            }
        };
        let new = Circle { id: Some(id.clone()), version: None, ..circle.clone() };
        let mut all = circles.clone();
        all.push(new.clone());
        self.write_circles(&all)?;
        Ok(CreatedCircle { circle: listed_circle(&new, &[]), created_id: id })
    }

    pub fn update_circle(&self, circle: &Circle, clear_fields: &[String]) -> anyhow::Result<ListedCircle> {
        let Some(id) = set(circle.id.as_deref()).map(str::to_string) else {
            bail!("update_circle needs the circle's id");
        };
        rows::check_clear_item(circle, clear_fields, &CIRCLE_CLEARABLE_FIELDS)?;
        let mut circles = self.circles()?;
        let Some(index) = circles.iter().position(|c| c.id.as_deref() == Some(id.as_str())) else {
            rows::expect_item_version::<Circle>(&id, None, circle.version)?;
            find_circle(&circles, &id)?;
            unreachable!()
        };
        circles[index] = rows::updated_item(&circles[index], circle, clear_fields, &["id"])?;
        let people = self.all()?;
        self.write_circles(&circles)?;
        Ok(listed_circle(&circles[index], &people))
    }

    /// Deletes a circle -- unless it's changed since version
    /// `if_version`, if given.
    pub fn delete_circle(&self, circle_id: &str, if_version: Option<i64>) -> anyhow::Result<DeletedCircle> {
        let circles = self.circles()?;
        let deleted = match circles.iter().find(|c| c.id.as_deref() == Some(circle_id)) {
            Some(found) => found.clone(),
            None => {
                if !rows::deleting::<Circle>(circle_id, None, if_version)? {
                    return Ok(DeletedCircle { deleted: rows::gone(circle_id), left: Vec::new() });
                }
                find_circle(&circles, circle_id)?
            }
        };
        rows::expect_item_version(circle_id, Some(&deleted), if_version)?;
        if deleted.id.as_deref() != Some(circle_id) {
            bail!("Delete a circle by its id: {} is {}", repr_or_none(deleted.name.as_deref()), str_or_none(deleted.id.as_deref()));
        }
        let mut people = self.read_people()?;
        let in_it = |p: &Person| p.circles.iter().flatten().any(|c| c == circle_id);
        let left: Vec<String> = people.iter().filter(|p| in_it(p)).filter_map(|p| p.id.clone()).collect();
        // Both tabs in one request.
        let mut writes = Vec::new();
        if !left.is_empty() {
            for person in people.iter_mut().filter(|p| in_it(p)) {
                let kept: Vec<String> = person.circles.iter().flatten().filter(|c| *c != circle_id).cloned().collect();
                person.circles = if kept.is_empty() { None } else { Some(kept) };
            }
            writes.push(self.sheet.prepared_items(&people)?);
        }
        let kept: Vec<Circle> = circles.into_iter().filter(|c| c.id.as_deref() != Some(circle_id)).collect();
        writes.push(self.circle_sheet.prepared_items(&kept)?);
        self.sheet.sheets().write_tabs(self.sheet.spreadsheet_id(), &writes)?;
        Ok(DeletedCircle { deleted, left })
    }

    fn write_circles(&self, circles: &[Circle]) -> anyhow::Result<()> {
        let problems = circle_problems(circles);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.circle_sheet.write_items(circles)
    }
}

fn person_nodes(people: &[Person]) -> Vec<Node> {
    people
        .iter()
        .filter_map(|p| {
            let id = set(p.id.as_deref())?;
            Some(Node { subject: id.to_string(), deleted: p.status == Some(Status::Deleted), foundations: p.foundations.clone().unwrap_or_default() })
        })
        .collect()
}


pub fn find_person(people: &[Person], id_or_name: &str) -> anyhow::Result<Person> {
    if let Some(person) = people.iter().find(|p| p.id.as_deref() == Some(id_or_name)) {
        return Ok(person.clone());
    }
    let wanted = id_or_name.to_lowercase();
    let named: Vec<&Person> = people.iter().filter(|p| p.name.as_deref().unwrap_or_default().to_lowercase() == wanted).collect();
    if named.len() == 1 {
        return Ok(named[0].clone());
    }
    if !named.is_empty() {
        let which: Vec<String> = named
            .iter()
            .map(|p| format!("{} ({})", str_or_none(p.id.as_deref()), set(p.context.as_deref()).unwrap_or("no context")))
            .collect();
        bail!("There's more than one person named {}: {}; give the id", py::repr(id_or_name), which.join(", "));
    }
    let named: Vec<(Option<&str>, Option<&str>)> = people.iter().map(|p| (p.id.as_deref(), p.name.as_deref())).collect();
    bail!("{}", no_match("person", "get_people", &named, id_or_name))
}

pub fn find_circle(circles: &[Circle], id_or_name: &str) -> anyhow::Result<Circle> {
    if let Some(circle) = circles.iter().find(|c| c.id.as_deref() == Some(id_or_name)) {
        return Ok(circle.clone());
    }
    let wanted = id_or_name.to_lowercase();
    if let Some(circle) = circles.iter().rev().find(|c| c.name.as_deref().unwrap_or_default().to_lowercase() == wanted) {
        return Ok(circle.clone());
    }
    let named: Vec<(Option<&str>, Option<&str>)> = circles.iter().map(|c| (c.id.as_deref(), c.name.as_deref())).collect();
    bail!("{}", no_match("circle", "get_circles", &named, id_or_name))
}

/// No `kind` with the id or name, suggesting close ones among `items`
/// (each an id and a name).
fn no_match(kind: &str, lister: &str, items: &[(Option<&str>, Option<&str>)], id_or_name: &str) -> String {
    let key = |item: &(Option<&str>, Option<&str>)| item.1.unwrap_or_default().to_lowercase();
    let ids = dedup(items.iter().filter_map(|i| set(i.0).map(str::to_string)));
    let names = dedup(items.iter().map(key));
    let mut close: Vec<&(Option<&str>, Option<&str>)> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(item) = items.iter().rev().find(|i| i.0 == Some(id.as_str())) {
            close.push(item);
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        close.extend(items.iter().filter(|i| key(i) == name));
    }
    let hints = dedup(close.iter().map(|i| format!("{} ({})", str_or_none(i.0), str_or_none(i.1))));
    let hint = hints.join(", ");
    format!(
        "There's no {kind} with the id or name {}{} ({lister} lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

/// Refuse a person -- one being added or changed -- with no context when
/// someone else has their name.
fn check_context(person: &Person, people: &[Person]) -> anyhow::Result<()> {
    let has_context = person.context.as_deref().is_some_and(|c| !c.trim().is_empty());
    let Some(name) = person.name.as_deref() else { return Ok(()) };
    if has_context {
        return Ok(());
    }
    let key = name.trim().to_lowercase();
    let others: Vec<&Person> =
        people.iter().filter(|p| p.id != person.id && p.name.as_deref().is_some_and(|n| n.trim().to_lowercase() == key)).collect();
    if let Some(other) = others.first() {
        let name = if is_self(other) { set(other.name.as_deref()).unwrap_or(SELF_NAME).to_string() } else { str_or_none(other.name.as_deref()) };
        let where_ = match set(other.context.as_deref()) {
            Some(context) => format!(" with the context {}", py::repr(context)),
            None => " with no context".into(),
        };
        bail!(
            "there's already a person named {}{where_} ({}); a name someone else has needs a context that tells them apart (e.g. \"met at salsa\")",
            py::repr(&name),
            str_or_none(other.id.as_deref())
        );
    }
    Ok(())
}

pub fn person_problems(people: &[Person], circles: &[Circle], trait_ids: Option<&[String]>) -> Vec<String> {
    let mut problems = Vec::new();
    let ids: Vec<Option<&str>> = people.iter().map(|p| p.id.as_deref()).collect();
    let mut repeated: Vec<&str> =
        ids.iter().flatten().copied().filter(|i| !i.is_empty() && ids.iter().filter(|x| **x == Some(*i)).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    for id in repeated {
        problems.push(format!("person id {} is used more than once", py::repr(id)));
    }
    let circle_ids: Vec<&str> = circles.iter().filter_map(|c| c.id.as_deref()).collect();
    let mut seen: Vec<((String, String), &Person)> = Vec::new();
    for person in people {
        let label = match set(person.id.as_deref()) {
            Some(id) => format!("person {id}"),
            None => format!("person {}", repr_or_none(person.name.as_deref())),
        };
        if set(person.id.as_deref()).is_none() {
            problems.push(format!("a person named {} has no id", repr_or_none(person.name.as_deref())));
        }
        let name: Option<&str> = if is_self(person) { Some(set(person.name.as_deref()).unwrap_or(SELF_NAME)) } else { person.name.as_deref() };
        match name.filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(n) if n.chars().count() > MAX_NAME_LENGTH => problems.push(format!("{label}'s name is longer than {MAX_NAME_LENGTH} characters")),
            Some(n) => {
                let context = person.context.as_deref().unwrap_or_default().trim().to_lowercase();
                let key = (n.trim().to_lowercase(), context);
                if let Some((_, other)) = seen.iter().find(|(k, _)| *k == key) {
                    let where_ = match set(other.context.as_deref()) {
                        Some(context) => format!(" with the context {}", py::repr(context)),
                        None => " with no context".into(),
                    };
                    problems.push(format!(
                        "there's already a person named {}{where_} ({}); a person's name and context together must be unique -- give a context that tells them apart",
                        repr_or_none(other.name.as_deref()),
                        str_or_none(other.id.as_deref())
                    ));
                }
                seen.retain(|(k, _)| *k != key);
                seen.push((key, person));
            }
        }
        if is_self(person) {
            if !(person.status.is_none() || person.status == Some(Status::Active)) {
                problems.push("self is always active".into());
            }
        } else if person.status.is_none() {
            problems.push(format!("{label}'s status must be one of {}", PERSON_STATUSES.join(", ")));
        }
        if let Some(circles) = &person.circles {
            let unknown: Vec<String> = circles.iter().filter(|c| !circle_ids.contains(&c.as_str())).cloned().collect();
            if !unknown.is_empty() {
                problems.push(format!("{label}'s circles {} aren't circles (get_circles lists them)", py::repr_list(&unknown)));
            }
        }
        if let Some(traits) = &person.traits {
            for p in person_traits_problems(&Value::Object(traits.clone()), trait_ids, SpecOf::Person) {
                problems.push(format!("{label}'s traits {p}"));
            }
        }
    }
    problems
}

pub fn circle_problems(circles: &[Circle]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut names: Vec<(String, &Circle)> = Vec::new();
    for circle in circles {
        let label = match set(circle.id.as_deref()) {
            Some(id) => format!("circle {id}"),
            None => format!("circle {}", repr_or_none(circle.name.as_deref())),
        };
        let Some(name) = circle.name.as_deref().filter(|n| !n.trim().is_empty()) else {
            problems.push(format!("{label} needs a name"));
            continue;
        };
        let key = name.trim().to_lowercase();
        if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
            problems.push(format!(
                "there's already a circle named {} ({}); circle names must be unique (a person may share one)",
                repr_or_none(other.name.as_deref()),
                str_or_none(other.id.as_deref())
            ));
        }
        names.retain(|(k, _)| *k != key);
        names.push((key, circle));
        if let Some(scoring) = &circle.scoring {
            for p in aggregation_problems(&Value::Object(scoring.clone()), None) {
                problems.push(format!("{label}'s scoring: {p}"));
            }
        }
    }
    problems
}

/// A person as the people tools return them (cancelled_events filled in
/// by the caller).
pub fn listed(person: &Person, circles: &[Circle]) -> ListedPerson {
    let names: Vec<String> = person
        .circles
        .iter()
        .flatten()
        .filter_map(|c| circles.iter().rev().find(|circle| circle.id.as_deref() == Some(c.as_str())).and_then(|circle| circle.name.clone()))
        .collect();
    ListedPerson {
        id: person.id.clone(),
        name: person.name.clone(),
        context: person.context.clone(),
        status: person.status,
        circles: person.circles.clone(),
        traits: person.traits.clone(),
        foundations: person.foundations.clone(),
        circle_names: if names.is_empty() { None } else { Some(names) },
        facts: None,
        cancelled_events: None,
        version: person.version,
    }
}

pub fn listed_circle(circle: &Circle, people: &[Person]) -> ListedCircle {
    let members: Vec<String> = match circle.id.as_deref() {
        Some(id) => people.iter().filter(|p| p.circles.iter().flatten().any(|c| c == id)).filter_map(|p| p.id.clone()).collect(),
        None => Vec::new(),
    };
    ListedCircle {
        id: circle.id.clone(),
        name: circle.name.clone(),
        note: circle.note.clone(),
        scoring: circle.scoring.clone(),
        member_ids: Some(members),
        version: circle.version,
    }
}

fn check_statuses(statuses: Option<&[String]>) -> anyhow::Result<Vec<String>> {
    match statuses {
        None => Ok(DEFAULT_STATUSES.iter().map(|s| s.to_string()).collect()),
        Some(given) => {
            let mut unknown: Vec<&String> = given.iter().filter(|s| !PERSON_STATUSES.contains(&s.as_str())).collect();
            unknown.sort();
            unknown.dedup();
            if !unknown.is_empty() {
                bail!("Unknown person status(es) {}; statuses are {}", py::repr_list(&unknown), PERSON_STATUSES.join(", "));
            }
            Ok(given.to_vec())
        }
    }
}

// Written by the app: made once by their ids and versions.
impl rows::Versioned for Person {}
impl rows::Versioned for Circle {}

#[cfg(test)]
#[path = "people_tests.rs"]
mod tests;
