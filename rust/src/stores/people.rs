//! People and circles: who the user spends time with -- the user themself
//! always first, as "self" -- in the People and Circles tabs.

use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, Kind, Row, RowSheet, RowType};
use crate::sheet::{CIRCLES, PEOPLE};
use crate::stores::groups::{python_repr, python_str, truthy};
use crate::stores::locations::dedup;
use crate::stores::traits::person_traits_problems;
use anyhow::bail;
use serde_json::{json, Map, Value};
use std::sync::Arc;

pub const SELF_ID: &str = "self";
pub const SELF_NAME: &str = "Me";
pub const PERSON_STATUSES: [&str; 3] = ["active", "archived", "deleted"];
pub const DEFAULT_STATUSES: [&str; 1] = ["active"];
pub const MAX_NAME_LENGTH: usize = 100;
pub const PERSON_CLEARABLE_FIELDS: [&str; 4] = ["context", "circles", "what_matters", "traits"];
pub const CIRCLE_CLEARABLE_FIELDS: [&str; 1] = ["note"];

pub const PERSON_ROW: RowType = RowType {
    name: "Person",
    fields: &[
        ("id", Kind::Str),
        ("name", Kind::Str),
        ("context", Kind::Str),
        ("status", Kind::Str),
        ("circles", Kind::Json),
        ("what_matters", Kind::Str),
        ("traits", Kind::Json),
    ],
};

pub const CIRCLE_ROW: RowType = RowType { name: "Circle", fields: &[("id", Kind::Str), ("name", Kind::Str), ("note", Kind::Str)] };

pub type TraitIds = Arc<dyn Fn() -> anyhow::Result<Vec<String>> + Send + Sync>;

pub fn self_person() -> Row {
    let mut row = PERSON_ROW.empty();
    row.insert("id".into(), json!(SELF_ID));
    row.insert("name".into(), json!(SELF_NAME));
    row.insert("status".into(), json!("active"));
    row
}

fn is_self(row: &Row) -> bool {
    rows::text(row, "id") == Some(SELF_ID)
}

/// Self, from their row (if any), with what it leaves out filled in.
fn as_self(row: Option<&Row>) -> Row {
    match row {
        None => self_person(),
        Some(row) => {
            let mut out = row.clone();
            if !truthy(row.get("name")) {
                out.insert("name".into(), json!(SELF_NAME));
            }
            out.insert("status".into(), json!("active"));
            out
        }
    }
}

#[derive(Clone)]
pub struct People {
    sheet: RowSheet,
    circle_sheet: RowSheet,
    trait_ids: Option<TraitIds>,
}

fn circles_of(person: &Row) -> Vec<Value> {
    match person.get("circles") {
        Some(Value::Array(items)) => items.clone(),
        _ => Vec::new(),
    }
}

impl People {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str, trait_ids: Option<TraitIds>) -> anyhow::Result<People> {
        let sheet = RowSheet::ensure(sheets, spreadsheet_id, PEOPLE.0, PEOPLE.1, PERSON_ROW, &["id", "name"])?;
        let circle_sheet = RowSheet::ensure(sheets, spreadsheet_id, CIRCLES.0, CIRCLES.1, CIRCLE_ROW, &["id", "name"])?;
        Ok(People { sheet, circle_sheet, trait_ids })
    }

    pub fn whole_tabs(&self) -> Vec<TabRange> {
        vec![self.sheet.whole_tab(), self.circle_sheet.whole_tab()]
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    /// Every person, self first, then in sheet order.
    pub fn all(&self) -> anyhow::Result<Vec<Row>> {
        self.prefetch(&self.whole_tabs())?;
        let people = self.sheet.read()?;
        let mine = people.iter().find(|p| is_self(p));
        let mut out = vec![as_self(mine)];
        out.extend(people.iter().filter(|p| !is_self(p)).cloned());
        Ok(out)
    }

    pub fn circles(&self) -> anyhow::Result<Vec<Row>> {
        self.prefetch(&self.whole_tabs())?;
        self.circle_sheet.read()
    }

    pub fn get_people(&self, statuses: Option<&[String]>) -> anyhow::Result<Vec<Map<String, Value>>> {
        let statuses = check_statuses(statuses)?;
        let circles = self.circles()?;
        Ok(self
            .all()?
            .iter()
            .filter(|p| statuses.iter().any(|s| rows::text(p, "status") == Some(s.as_str())))
            .map(|p| listed(p, &circles))
            .collect())
    }

    pub fn get_person(&self, id_or_name: &str) -> anyhow::Result<Map<String, Value>> {
        let person = find_person(&self.all()?, id_or_name)?;
        Ok(listed(&person, &self.circles()?))
    }

    pub fn get_circles(&self) -> anyhow::Result<Value> {
        let people = self.all()?;
        Ok(Value::Array(self.circles()?.iter().map(|c| listed_circle(c, &people)).collect()))
    }

    pub fn get_circle(&self, id_or_name: &str) -> anyhow::Result<Value> {
        let circle = find_circle(&self.circles()?, id_or_name)?;
        Ok(listed_circle(&circle, &self.all()?))
    }

    pub fn create_person(&self, person: &Row) -> anyhow::Result<Value> {
        let people = self.sheet.read()?;
        let mut new = person.clone();
        let id = rows::new_id(people.iter().map(|p| rows::text(p, "id")).collect::<Vec<_>>());
        new.insert("id".into(), json!(id));
        if !truthy(person.get("status")) {
            new.insert("status".into(), json!("active"));
        }
        let mut everyone = vec![self_person()];
        everyone.extend(people.iter().cloned());
        check_context(&new, &everyone)?;
        let circles = self.circles()?;
        let mut all = people.clone();
        all.push(new.clone());
        self.write_people(&all, &circles)?;
        Ok(json!({ "person": Value::Object(listed(&new, &circles)), "created_id": id }))
    }

    pub fn update_person(&self, person: &Row, clear_fields: &[String]) -> anyhow::Result<Map<String, Value>> {
        let Some(id) = rows::text(person, "id").filter(|i| !i.is_empty()).map(str::to_string) else {
            bail!("update_person needs the person's id");
        };
        rows::check_clear(person, clear_fields, &PERSON_CLEARABLE_FIELDS)?;
        let mut people = self.sheet.read()?;
        let mut index = people.iter().position(|p| rows::text(p, "id") == Some(id.as_str()));
        if index.is_none() && id == SELF_ID {
            people.push(self_person());
            index = Some(people.len() - 1);
        }
        let Some(index) = index else {
            find_person(&self.all()?, &id)?;
            unreachable!()
        };
        let changed = rows::updated(&people[index], person, clear_fields, &["id"]);
        if rows::is_set(person, "name") || clear_fields.iter().any(|c| c == "context") {
            let mut everyone = vec![self_person()];
            everyone.extend(people.iter().cloned());
            check_context(&changed, &everyone)?;
        }
        people[index] = changed.clone();
        let circles = self.circles()?;
        self.write_people(&people, &circles)?;
        let shown = if is_self(&changed) { as_self(Some(&changed)) } else { changed };
        Ok(listed(&shown, &circles))
    }

    fn write_people(&self, people: &[Row], circles: &[Row]) -> anyhow::Result<()> {
        let checked: Vec<Row> =
            if people.iter().any(is_self) { people.to_vec() } else { std::iter::once(self_person()).chain(people.iter().cloned()).collect() };
        let trait_ids = match &self.trait_ids {
            Some(lookup) if people.iter().any(|p| rows::is_set(p, "traits")) => Some(lookup()?),
            _ => None,
        };
        let problems = person_problems(&checked, circles, trait_ids.as_deref());
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.sheet.write(people)
    }

    pub fn create_circle(&self, circle: &Row) -> anyhow::Result<Value> {
        let circles = self.circles()?;
        let mut new = circle.clone();
        let id = rows::new_id(circles.iter().map(|c| rows::text(c, "id")).collect::<Vec<_>>());
        new.insert("id".into(), json!(id));
        let mut all = circles.clone();
        all.push(new.clone());
        self.write_circles(&all)?;
        Ok(json!({ "circle": listed_circle(&new, &[]), "created_id": id }))
    }

    pub fn update_circle(&self, circle: &Row, clear_fields: &[String]) -> anyhow::Result<Value> {
        let Some(id) = rows::text(circle, "id").filter(|i| !i.is_empty()).map(str::to_string) else {
            bail!("update_circle needs the circle's id");
        };
        rows::check_clear(circle, clear_fields, &CIRCLE_CLEARABLE_FIELDS)?;
        let mut circles = self.circles()?;
        let Some(index) = circles.iter().position(|c| rows::text(c, "id") == Some(id.as_str())) else {
            find_circle(&circles, &id)?;
            unreachable!()
        };
        circles[index] = rows::updated(&circles[index], circle, clear_fields, &["id"]);
        let people = self.all()?;
        self.write_circles(&circles)?;
        Ok(listed_circle(&circles[index], &people))
    }

    pub fn delete_circle(&self, circle_id: &str) -> anyhow::Result<Value> {
        let circles = self.circles()?;
        let deleted = match circles.iter().find(|c| rows::text(c, "id") == Some(circle_id)) {
            Some(found) => found.clone(),
            None => find_circle(&circles, circle_id)?,
        };
        if rows::text(&deleted, "id") != Some(circle_id) {
            bail!(
                "Delete a circle by its id: {} is {}",
                python_repr(deleted.get("name").unwrap_or(&Value::Null)),
                python_str(deleted.get("id"))
            );
        }
        let mut people = self.sheet.read()?;
        let left: Vec<Value> = people
            .iter()
            .filter(|p| circles_of(p).iter().any(|c| c.as_str() == Some(circle_id)))
            .map(|p| p.get("id").cloned().unwrap_or(Value::Null))
            .collect();
        if !left.is_empty() {
            for person in people.iter_mut() {
                if left.contains(person.get("id").unwrap_or(&Value::Null)) {
                    let kept: Vec<Value> = circles_of(person).into_iter().filter(|c| c.as_str() != Some(circle_id)).collect();
                    person.insert("circles".into(), if kept.is_empty() { Value::Null } else { Value::Array(kept) });
                }
            }
            self.sheet.write(&people)?;
        }
        let kept: Vec<Row> = circles.into_iter().filter(|c| rows::text(c, "id") != Some(circle_id)).collect();
        self.circle_sheet.write(&kept)?;
        Ok(json!({ "deleted": Value::Object(deleted), "left": left }))
    }

    fn write_circles(&self, circles: &[Row]) -> anyhow::Result<()> {
        let problems = circle_problems(circles);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.circle_sheet.write(circles)
    }
}

pub fn find_person(people: &[Row], id_or_name: &str) -> anyhow::Result<Row> {
    if let Some(person) = people.iter().find(|p| rows::text(p, "id") == Some(id_or_name)) {
        return Ok(person.clone());
    }
    let wanted = id_or_name.to_lowercase();
    let named: Vec<&Row> = people.iter().filter(|p| rows::text(p, "name").unwrap_or_default().to_lowercase() == wanted).collect();
    if named.len() == 1 {
        return Ok(named[0].clone());
    }
    if !named.is_empty() {
        let which: Vec<String> = named
            .iter()
            .map(|p| {
                let context = p.get("context").filter(|c| truthy(Some(c))).map(|c| python_str(Some(c)));
                format!("{} ({})", python_str(p.get("id")), context.unwrap_or_else(|| "no context".into()))
            })
            .collect();
        bail!("There's more than one person named {}: {}; give the id", py::repr(id_or_name), which.join(", "));
    }
    bail!("{}", no_match("person", "get_people", people, id_or_name))
}

pub fn find_circle(circles: &[Row], id_or_name: &str) -> anyhow::Result<Row> {
    if let Some(circle) = circles.iter().find(|c| rows::text(c, "id") == Some(id_or_name)) {
        return Ok(circle.clone());
    }
    let wanted = id_or_name.to_lowercase();
    if let Some(circle) = circles.iter().rev().find(|c| rows::text(c, "name").unwrap_or_default().to_lowercase() == wanted) {
        return Ok(circle.clone());
    }
    bail!("{}", no_match("circle", "get_circles", circles, id_or_name))
}

fn no_match(kind: &str, lister: &str, items: &[Row], id_or_name: &str) -> String {
    let key = |row: &Row| rows::text(row, "name").unwrap_or_default().to_lowercase();
    let ids = dedup(items.iter().filter(|i| truthy(i.get("id"))).filter_map(|i| rows::text(i, "id").map(str::to_string)));
    let names = dedup(items.iter().map(key));
    let mut close: Vec<&Row> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(item) = items.iter().rev().find(|i| rows::text(i, "id") == Some(id.as_str())) {
            close.push(item);
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        close.extend(items.iter().filter(|i| key(i) == name));
    }
    let hints = dedup(close.iter().map(|i| format!("{} ({})", python_str(i.get("id")), python_str(i.get("name")))));
    let hint = hints.join(", ");
    format!(
        "There's no {kind} with the id or name {}{} ({lister} lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

/// Refuse a person -- one being added or changed -- with no context when
/// someone else has their name.
fn check_context(person: &Row, people: &[Row]) -> anyhow::Result<()> {
    let has_context = person.get("context").and_then(Value::as_str).is_some_and(|c| !c.trim().is_empty());
    let Some(name) = person.get("name").and_then(Value::as_str) else { return Ok(()) };
    if has_context {
        return Ok(());
    }
    let key = name.trim().to_lowercase();
    let others: Vec<&Row> = people
        .iter()
        .filter(|p| p.get("id") != person.get("id") && p.get("name").and_then(Value::as_str).is_some_and(|n| n.trim().to_lowercase() == key))
        .collect();
    if let Some(other) = others.first() {
        let name = if is_self(other) {
            other.get("name").filter(|n| truthy(Some(n))).map(|n| python_str(Some(n))).unwrap_or_else(|| SELF_NAME.into())
        } else {
            python_str(other.get("name"))
        };
        let where_ = match other.get("context").filter(|c| truthy(Some(c))) {
            Some(context) => format!(" with the context {}", python_repr(context)),
            None => " with no context".into(),
        };
        bail!(
            "there's already a person named {}{where_} ({}); a name someone else has needs a context that tells them apart (e.g. \"met at salsa\")",
            py::repr(&name),
            python_str(other.get("id"))
        );
    }
    Ok(())
}

pub fn person_problems(people: &[Row], circles: &[Row], trait_ids: Option<&[String]>) -> Vec<String> {
    let mut problems = Vec::new();
    let ids: Vec<Option<&str>> = people.iter().map(|p| rows::text(p, "id")).collect();
    let mut repeated: Vec<&str> =
        ids.iter().flatten().copied().filter(|i| !i.is_empty() && ids.iter().filter(|x| **x == Some(*i)).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    for id in repeated {
        problems.push(format!("person id {} is used more than once", py::repr(id)));
    }
    let circle_ids: Vec<&Value> = circles.iter().map(|c| c.get("id").unwrap_or(&Value::Null)).collect();
    let mut seen: Vec<((String, String), Row)> = Vec::new();
    for person in people {
        let label = match person.get("id").filter(|i| truthy(Some(i))) {
            Some(id) => format!("person {}", python_str(Some(id))),
            None => format!("person {}", python_repr(person.get("name").unwrap_or(&Value::Null))),
        };
        if !truthy(person.get("id")) {
            problems.push(format!("a person named {} has no id", python_repr(person.get("name").unwrap_or(&Value::Null))));
        }
        let name: Option<Value> = if is_self(person) {
            Some(person.get("name").filter(|n| truthy(Some(n))).cloned().unwrap_or(json!(SELF_NAME)))
        } else {
            person.get("name").cloned()
        };
        match name.as_ref().and_then(Value::as_str).filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(n) if n.chars().count() > MAX_NAME_LENGTH => problems.push(format!("{label}'s name is longer than {MAX_NAME_LENGTH} characters")),
            Some(n) => {
                let context = person.get("context").and_then(Value::as_str).unwrap_or_default().trim().to_lowercase();
                let key = (n.trim().to_lowercase(), context);
                if let Some((_, other)) = seen.iter().find(|(k, _)| *k == key) {
                    let where_ = match other.get("context").filter(|c| truthy(Some(c))) {
                        Some(context) => format!(" with the context {}", python_repr(context)),
                        None => " with no context".into(),
                    };
                    problems.push(format!(
                        "there's already a person named {}{where_} ({}); a person's name and context together must be unique -- give a context that tells them apart",
                        python_repr(other.get("name").unwrap_or(&Value::Null)),
                        python_str(other.get("id"))
                    ));
                }
                seen.retain(|(k, _)| *k != key);
                seen.push((key, person.clone()));
            }
        }
        if is_self(person) {
            let status = person.get("status").unwrap_or(&Value::Null);
            if !(status.is_null() || status.as_str() == Some("active")) {
                problems.push("self is always active".into());
            }
        } else if !PERSON_STATUSES.iter().any(|s| rows::text(person, "status") == Some(*s)) {
            problems.push(format!("{label}'s status must be one of {}", PERSON_STATUSES.join(", ")));
        }
        if let Some(circles) = person.get("circles").filter(|c| !c.is_null()) {
            match circles {
                Value::Array(items) if items.iter().all(Value::is_string) => {
                    let unknown: Vec<String> =
                        items.iter().filter(|c| !circle_ids.contains(c)).map(|c| c.as_str().unwrap().to_string()).collect();
                    if !unknown.is_empty() {
                        problems.push(format!("{label}'s circles {} aren't circles (get_circles lists them)", py::repr_list(&unknown)));
                    }
                }
                _ => problems.push(format!("{label}'s circles must be a list of circle ids")),
            }
        }
        if let Some(traits) = person.get("traits").filter(|t| !t.is_null()) {
            for p in person_traits_problems(traits, trait_ids) {
                problems.push(format!("{label}'s traits {p}"));
            }
        }
    }
    problems
}

pub fn circle_problems(circles: &[Row]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut names: Vec<(String, Row)> = Vec::new();
    for circle in circles {
        let label = match circle.get("id").filter(|i| truthy(Some(i))) {
            Some(id) => format!("circle {}", python_str(Some(id))),
            None => format!("circle {}", python_repr(circle.get("name").unwrap_or(&Value::Null))),
        };
        let Some(name) = circle.get("name").and_then(Value::as_str).filter(|n| !n.trim().is_empty()) else {
            problems.push(format!("{label} needs a name"));
            continue;
        };
        let key = name.trim().to_lowercase();
        if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
            problems.push(format!(
                "there's already a circle named {} ({}); circle names must be unique (a person may share one)",
                python_repr(other.get("name").unwrap_or(&Value::Null)),
                python_str(other.get("id"))
            ));
        }
        names.retain(|(k, _)| *k != key);
        names.push((key, circle.clone()));
    }
    problems
}

/// A person as the people tools return them (cancelled_events filled in
/// by the caller).
pub fn listed(person: &Row, circles: &[Row]) -> Map<String, Value> {
    let mut out: Map<String, Value> = Map::new();
    for (name, _) in PERSON_ROW.fields {
        out.insert(name.to_string(), person.get(*name).cloned().unwrap_or(Value::Null));
    }
    let names: Vec<Value> = circles_of(person)
        .iter()
        .filter_map(|c| circles.iter().rev().find(|circle| circle.get("id") == Some(c)).map(|circle| circle.get("name").cloned().unwrap_or(Value::Null)))
        .collect();
    out.insert("circle_names".into(), if names.is_empty() { Value::Null } else { Value::Array(names) });
    out.insert("cancelled_events".into(), Value::Null);
    out
}

pub fn listed_circle(circle: &Row, people: &[Row]) -> Value {
    let mut out: Map<String, Value> = Map::new();
    for (name, _) in CIRCLE_ROW.fields {
        out.insert(name.to_string(), circle.get(*name).cloned().unwrap_or(Value::Null));
    }
    let id = circle.get("id").cloned().unwrap_or(Value::Null);
    let members: Vec<Value> =
        people.iter().filter(|p| circles_of(p).contains(&id)).map(|p| p.get("id").cloned().unwrap_or(Value::Null)).collect();
    out.insert("member_ids".into(), Value::Array(members));
    Value::Object(out)
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

#[cfg(test)]
#[path = "people_tests.rs"]
mod tests;
