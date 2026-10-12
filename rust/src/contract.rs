//! The tools' contract -- each tool's name, description, and input and
//! output schemas, as tools/list sends them -- derived from the code:
//! `#[derive(Schema)]` on argument and result types, `#[tool]` on each
//! tool function (see the ctt-derive crate).
//!
//! The schemas are in the shape pydantic gave them, so clients see what
//! they always have:
//!
//! - A struct is a model, in `$defs` under its title and referred to by
//!   `$ref`: `additionalProperties` (false, if it refuses unknown
//!   fields), its doc comment as `description`, `properties` in field
//!   order, `required`, `title`, `type: object`.
//! - A field has a `title` (its name in title case, "created_id" ->
//!   "Created Id") unless it's a `$ref`, a `description` from its doc
//!   comment, and a `default`: null for an `Option`, unless it's
//!   `#[schema(required)]`, or what `#[schema(default)]` says. A field
//!   without one is `required`.
//! - `Option<T>` is `anyOf: [T, null]`; `Vec<T>` an array of T; a map of
//!   text to T an object with T as `additionalProperties`; a free-form
//!   object `additionalProperties: true`.
//! - A tool's arguments are a model of its parameters, inlined, titled
//!   "<tool>Arguments". Its result is inlined the same way if it's a
//!   model (without its description or `additionalProperties`), or else
//!   wrapped as `{"result": ...}`, titled "<tool>Output".
//! - Every object's keys are sorted, except `properties` (field order)
//!   and each schema's top level: `$defs` and `title` go last.

use crate::app::App;
use crate::tools::ToolError;
use serde::de::DeserializeOwned;
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

pub use ctt_derive::{tool, Schema};

/// The models a schema refers to, by title.
#[derive(Default)]
pub struct Defs {
    models: BTreeMap<String, Value>,
}

impl Defs {
    /// `$ref` to the model `title` if it's there or being built (a model
    /// that refers to itself), else None -- and from then on, it's being
    /// built.
    pub fn reference(&mut self, title: &str) -> Option<Value> {
        let reference = json!({"$ref": format!("#/$defs/{title}")});
        if self.models.contains_key(title) {
            return Some(reference);
        }
        self.models.insert(title.to_string(), Value::Null);
        None
    }
}

/// A type's JSON schema.
pub trait Schema {
    fn schema(defs: &mut Defs) -> Value;

    /// Whether a field of this type defaults to null (an `Option`).
    const OPTIONAL: bool = false;

    /// The value of an argument that wasn't given: only an `Option` can
    /// be missing once the arguments are checked (defaults are filled in).
    fn absent() -> Self
    where
        Self: Sized,
    {
        panic!("a required argument is missing after the arguments were checked")
    }
}

macro_rules! simple {
    ($($ty:ty => $schema:expr),* $(,)?) => {
        $(impl Schema for $ty {
            fn schema(_: &mut Defs) -> Value {
                $schema
            }
        })*
    };
}

simple! {
    String => json!({"type": "string"}),
    bool => json!({"type": "boolean"}),
    i64 => json!({"type": "integer"}),
    f64 => json!({"type": "number"}),
    Map<String, Value> => json!({"additionalProperties": true, "type": "object"}),
}

impl<T: Schema> Schema for Option<T> {
    fn schema(defs: &mut Defs) -> Value {
        json!({"anyOf": [T::schema(defs), {"type": "null"}]})
    }

    const OPTIONAL: bool = true;

    fn absent() -> Self {
        None
    }
}

impl<T: Schema> Schema for Vec<T> {
    fn schema(defs: &mut Defs) -> Value {
        json!({"items": T::schema(defs), "type": "array"})
    }
}

impl<T: Schema> Schema for indexmap::IndexMap<String, T> {
    fn schema(defs: &mut Defs) -> Value {
        json!({"additionalProperties": T::schema(defs), "type": "object"})
    }
}

/// An instant, as text: what a client sends (with or without an offset;
/// the arguments' check makes sure it's one) and what a result shows.
#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(transparent)]
pub struct DateTimeText(pub String);

impl Schema for DateTimeText {
    fn schema(_: &mut Defs) -> Value {
        json!({"format": "date-time", "type": "string"})
    }
}

/// A string literal: its one value, or any of several.
pub fn literal(values: &[&str]) -> Value {
    match values {
        [one] => json!({"const": one, "type": "string"}),
        many => json!({"enum": many, "type": "string"}),
    }
}

/// One of a model's fields, or a tool's parameters.
pub struct Field {
    name: &'static str,
    schema: Value,
    default: Option<Value>,
    description: Option<&'static str>,
}

impl Field {
    pub fn new<T: Schema>(
        defs: &mut Defs,
        name: &'static str,
        default: Option<&'static str>,
        required: bool,
        description: Option<&'static str>,
    ) -> Field {
        let default = match default {
            Some(json) => Some(serde_json::from_str(json).unwrap_or_else(|e| panic!("{name}'s default {json:?}: {e}"))),
            None if T::OPTIONAL && !required => Some(Value::Null),
            None => None,
        };
        Field { name, schema: T::schema(defs), default, description }
    }
}

/// "created_id" -> "Created Id", as pydantic titles a field.
fn title_case(name: &str) -> String {
    name.split('_')
        .map(|word| {
            let mut chars = word.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().chain(chars.flat_map(char::to_lowercase)).collect(),
                None => String::new(),
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn is_reference(schema: &Value) -> bool {
    schema.get("$ref").is_some() || schema.pointer("/anyOf/0/$ref").is_some()
}

/// A model's `properties` and `required`.
fn object(fields: Vec<Field>) -> Map<String, Value> {
    let mut properties = Map::new();
    let mut required = Vec::new();
    for field in fields {
        let mut schema = field.schema;
        let reference = is_reference(&schema);
        let map = schema.as_object_mut().expect("a schema is an object");
        if !reference {
            map.insert("title".into(), json!(title_case(field.name)));
        }
        if let Some(description) = field.description {
            map.insert("description".into(), json!(description));
        }
        match field.default {
            Some(default) => {
                map.insert("default".into(), default);
            }
            None => required.push(json!(field.name)),
        }
        properties.insert(field.name.to_string(), schema);
    }
    let mut out = Map::new();
    out.insert("properties".into(), Value::Object(properties));
    if !required.is_empty() {
        out.insert("required".into(), Value::Array(required));
    }
    out.insert("type".into(), json!("object"));
    out
}

/// Add the model `title` to `defs`; its `$ref`.
pub fn model(defs: &mut Defs, title: &str, description: Option<&str>, deny_unknown: bool, fields: Vec<Field>) -> Value {
    let mut schema = object(fields);
    if deny_unknown {
        schema.insert("additionalProperties".into(), json!(false));
    }
    if let Some(description) = description {
        schema.insert("description".into(), json!(description));
    }
    schema.insert("title".into(), json!(title));
    defs.models.insert(title.to_string(), Value::Object(schema));
    json!({"$ref": format!("#/$defs/{title}")})
}

/// A tool's arguments' schema, before `top`.
pub fn arguments(_defs: &mut Defs, title: &str, fields: Vec<Field>) -> Value {
    let mut schema = object(fields);
    schema.insert("title".into(), json!(title));
    Value::Object(schema)
}

/// A schema as it's sent: `$defs` (if any), every object's keys sorted
/// but `properties`', and the top level's `$defs` and `title` last.
fn top(mut schema: Map<String, Value>, defs: Defs) -> Value {
    let models: Map<String, Value> = defs.models.into_iter().filter(|(_, m)| !m.is_null()).collect();
    if !models.is_empty() {
        schema.insert("$defs".into(), Value::Object(models));
    }
    let Value::Object(sorted) = sort_keys(Value::Object(schema)) else { unreachable!() };
    let mut out = Map::new();
    for (key, value) in &sorted {
        if key != "$defs" && key != "title" {
            out.insert(key.clone(), value.clone());
        }
    }
    for key in ["$defs", "title"] {
        if let Some(value) = sorted.get(key) {
            out.insert(key.into(), value.clone());
        }
    }
    Value::Object(out)
}

fn sort_keys(value: Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut entries: Vec<(String, Value)> = map.into_iter().collect();
            entries.sort_by(|a, b| a.0.cmp(&b.0));
            entries
                .into_iter()
                .map(|(key, value)| {
                    let value = match (key.as_str(), value) {
                        ("default" | "const" | "enum", value) => value,
                        // Field order, each field's schema sorted.
                        ("properties", Value::Object(fields)) => {
                            Value::Object(fields.into_iter().map(|(name, schema)| (name, sort_keys(schema))).collect())
                        }
                        (_, value) => sort_keys(value),
                    };
                    (key, value)
                })
                .collect::<Map<String, Value>>()
                .into()
        }
        Value::Array(items) => Value::Array(items.into_iter().map(sort_keys).collect()),
        other => other,
    }
}

/// A tool, as the registry lists it and tools/list describes it.
pub struct ToolDef {
    pub name: &'static str,
    pub description: &'static str,
    pub writes: bool,
    pub input: fn(&mut Defs) -> Value,
    pub output: fn(&mut Defs) -> Value,
    pub run: fn(&App, Value) -> Result<Value, ToolError>,
}

impl ToolDef {
    /// The tool's entry in tools/list.
    pub fn entry(&self) -> Value {
        let mut defs = Defs::default();
        let Value::Object(input) = (self.input)(&mut defs) else { unreachable!("arguments are an object") };
        let input = top(input, defs);

        let mut defs = Defs::default();
        let result = (self.output)(&mut defs);
        let output = match result.get("$ref").and_then(Value::as_str) {
            Some(reference) => {
                let title = reference.trim_start_matches("#/$defs/");
                let Some(Value::Object(mut inlined)) = defs.models.remove(title) else { unreachable!("a $ref is in $defs") };
                inlined.remove("description");
                inlined.remove("additionalProperties");
                inlined
            }
            None => {
                let mut result = result;
                if !is_reference(&result) {
                    result.as_object_mut().expect("a schema is an object").insert("title".into(), json!("Result"));
                }
                let mut wrapper = Map::new();
                wrapper.insert("properties".into(), json!({"result": result}));
                wrapper.insert("required".into(), json!(["result"]));
                wrapper.insert("title".into(), json!(format!("{}Output", self.name)));
                wrapper.insert("type".into(), json!("object"));
                wrapper
            }
        };
        let output = top(output, defs);
        json!({"description": self.description, "inputSchema": input, "name": self.name, "outputSchema": output})
    }
}

/// The checked arguments, as the tool's parameters.
pub fn from_arguments<T: DeserializeOwned>(arguments: Value) -> Result<T, ToolError> {
    serde_json::from_value(arguments).map_err(|e| ToolError::Unexpected(anyhow::anyhow!("the checked arguments don't fit the tool: {e}")))
}

/// A tool's result as JSON.
pub fn to_result<T: Serialize>(result: &T) -> Result<Value, ToolError> {
    serde_json::to_value(result).map_err(|e| ToolError::Unexpected(e.into()))
}

/// What `#[tool]` passes for a parameter that wasn't given: its default
/// (the arguments' check fills it in, but a call from inside the server
/// needn't), or None.
pub fn absent<T: Schema + DeserializeOwned>(default: Option<&str>) -> T {
    match default {
        Some(json) => serde_json::from_str(json).unwrap_or_else(|e| panic!("the default {json:?}: {e}")),
        None => T::absent(),
    }
}

/// A literal's text, as serde writes it ("hint" for ClearableField::Hint).
pub fn wire_text<T: Serialize>(value: &T) -> String {
    match serde_json::to_value(value) {
        Ok(Value::String(text)) => text,
        other => panic!("not a string literal: {other:?}"),
    }
}

#[cfg(test)]
#[path = "contract_tests.rs"]
mod tests;
