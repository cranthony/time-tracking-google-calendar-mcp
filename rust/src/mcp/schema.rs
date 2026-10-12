//! Checking a tool's arguments against its input schema (see
//! crate::contract), the way pydantic's lax mode does: what it
//! accepts (a "5" for an integer, "yes" for a boolean), what it fills in
//! (defaults), what it drops (fields a dataclass doesn't have, unless the
//! schema forbids them) -- and, for what it refuses, which field and what
//! kind of error, by pydantic's names. The messages themselves are this
//! server's own (the user chose simpler wording than pydantic's): see
//! conformance/harness.py's argument_errors.

use chrono::{DateTime, NaiveDate, NaiveDateTime};
use serde_json::{Map, Value};

#[derive(Debug, Clone, PartialEq)]
pub struct ArgError {
    pub loc: String,
    pub kind: &'static str,
    pub message: String,
}

pub struct Validator<'a> {
    root: &'a Value,
}

fn join(loc: &str, part: &str) -> String {
    if loc.is_empty() {
        part.to_string()
    } else {
        format!("{loc}.{part}")
    }
}

fn describe(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "a boolean",
        Value::Number(_) => "a number",
        Value::String(_) => "text",
        Value::Array(_) => "a list",
        Value::Object(_) => "an object",
    }
}

impl<'a> Validator<'a> {
    pub fn new(root: &'a Value) -> Validator<'a> {
        Validator { root }
    }

    fn resolve(&self, schema: &'a Value) -> &'a Value {
        match schema.get("$ref").and_then(Value::as_str) {
            Some(reference) => {
                let name = reference.rsplit('/').next().unwrap_or_default();
                self.root.pointer(&format!("/$defs/{name}")).unwrap_or(schema)
            }
            None => schema,
        }
    }

    /// The arguments as the tool receives them, or every error found.
    pub fn validate(&self, arguments: &Value) -> Result<Value, Vec<ArgError>> {
        let mut errors = Vec::new();
        let value = self.check(self.root, arguments, "", &mut errors, true);
        if errors.is_empty() { Ok(value) } else { Err(errors) }
    }

    fn check(&self, schema: &'a Value, value: &Value, loc: &str, errors: &mut Vec<ArgError>, top: bool) -> Value {
        let is_ref = schema.get("$ref").is_some();
        let schema = self.resolve(schema);
        if let Some(options) = schema.get("anyOf").and_then(Value::as_array) {
            return self.check_any_of(options, value, loc, errors);
        }
        if let Some(constant) = schema.get("const") {
            if value == constant {
                return value.clone();
            }
            errors.push(ArgError { loc: loc.into(), kind: "literal_error", message: format!("must be {constant}") });
            return Value::Null;
        }
        if let Some(choices) = schema.get("enum").and_then(Value::as_array) {
            if choices.contains(value) {
                return value.clone();
            }
            let listed: Vec<String> = choices.iter().map(Value::to_string).collect();
            errors.push(ArgError { loc: loc.into(), kind: "literal_error", message: format!("must be one of {}", listed.join(", ")) });
            return Value::Null;
        }
        // A list of types: null, if it's one of them and that's what was
        // given; otherwise the other.
        let ty = match schema.get("type") {
            Some(Value::Array(types)) => {
                let allows_null = types.iter().any(|t| t == "null");
                if value.is_null() && allows_null {
                    return Value::Null;
                }
                types.iter().filter_map(Value::as_str).find(|t| *t != "null")
            }
            other => other.and_then(Value::as_str),
        };
        match ty {
            Some("null") => {
                if value.is_null() {
                    Value::Null
                } else {
                    errors.push(ArgError { loc: loc.into(), kind: "none_required", message: "must be null".into() });
                    Value::Null
                }
            }
            Some("string") => self.check_string(schema, value, loc, errors),
            Some("integer") => check_integer(value, loc, errors),
            Some("number") => check_number(value, loc, errors),
            Some("boolean") => check_boolean(value, loc, errors),
            Some("array") => {
                let Value::Array(items) = value else {
                    errors.push(ArgError { loc: loc.into(), kind: "list_type", message: format!("must be a list, not {}", describe(value)) });
                    return Value::Null;
                };
                let item_schema = schema.get("items");
                Value::Array(
                    items
                        .iter()
                        .enumerate()
                        .map(|(i, item)| match item_schema {
                            Some(s) => self.check(s, item, &join(loc, &i.to_string()), errors, false),
                            None => item.clone(),
                        })
                        .collect(),
                )
            }
            Some("object") => self.check_object(schema, value, loc, errors, top || is_ref),
            _ => value.clone(),
        }
    }

    fn check_any_of(&self, options: &'a [Value], value: &Value, loc: &str, errors: &mut Vec<ArgError>) -> Value {
        // Null first, as pydantic's Optional does.
        if value.is_null() && options.iter().any(|o| o.get("type").and_then(Value::as_str) == Some("null")) {
            return Value::Null;
        }
        // Repeat.until's BeforeValidator: a 10-character string is a date,
        // anything else a datetime -- decided before pydantic's union sees it.
        let formats: Vec<&str> = options.iter().filter_map(|o| o.get("format").and_then(Value::as_str)).collect();
        if formats.contains(&"date-time") && formats.contains(&"date") {
            if let Value::String(text) = value {
                let format = if text.chars().count() == 10 { "date" } else { "date-time" };
                if let Some(option) = options.iter().find(|o| o.get("format").and_then(Value::as_str) == Some(format)) {
                    return self.check(option, value, loc, errors, false);
                }
            }
        }
        let mut first_errors: Option<Vec<ArgError>> = None;
        for option in options {
            if option.get("type").and_then(Value::as_str) == Some("null") {
                continue;
            }
            let mut attempt = Vec::new();
            let checked = self.check(option, value, loc, &mut attempt, false);
            if attempt.is_empty() {
                return checked;
            }
            if first_errors.is_none() {
                first_errors = Some(attempt);
            }
        }
        errors.extend(first_errors.unwrap_or_default());
        Value::Null
    }

    fn check_string(&self, schema: &Value, value: &Value, loc: &str, errors: &mut Vec<ArgError>) -> Value {
        let format = schema.get("format").and_then(Value::as_str);
        match format {
            Some("date-time") => match value {
                Value::String(text) => match parse_datetime(text) {
                    Some(normalized) => Value::String(normalized),
                    None => {
                        let kind = if looks_like_date_only(text) || text.len() < 10 { "datetime_from_date_parsing" } else { "datetime_parsing" };
                        errors.push(ArgError { loc: loc.into(), kind, message: "must be a date and time, like 2026-10-07T09:00:00-04:00".into() });
                        Value::Null
                    }
                },
                Value::Number(n) => match n.as_f64().and_then(|f| DateTime::from_timestamp(f as i64, 0)) {
                    Some(moment) => Value::String(moment.to_rfc3339()),
                    None => {
                        errors.push(ArgError { loc: loc.into(), kind: "datetime_parsing", message: "must be a date and time".into() });
                        Value::Null
                    }
                },
                other => {
                    errors.push(ArgError { loc: loc.into(), kind: "datetime_type", message: format!("must be a date and time, not {}", describe(other)) });
                    Value::Null
                }
            },
            Some("date") => match value {
                Value::String(text) if NaiveDate::parse_from_str(text, "%Y-%m-%d").is_ok() => value.clone(),
                Value::String(_) => {
                    errors.push(ArgError { loc: loc.into(), kind: "date_from_datetime_parsing", message: "must be a date, like 2026-10-07".into() });
                    Value::Null
                }
                other => {
                    errors.push(ArgError { loc: loc.into(), kind: "date_type", message: format!("must be a date, not {}", describe(other)) });
                    Value::Null
                }
            },
            _ => match value {
                Value::String(_) => value.clone(),
                other => {
                    errors.push(ArgError { loc: loc.into(), kind: "string_type", message: format!("must be text, not {}", describe(other)) });
                    Value::Null
                }
            },
        }
    }

    fn check_object(&self, schema: &'a Value, value: &Value, loc: &str, errors: &mut Vec<ArgError>, dataclass: bool) -> Value {
        let Value::Object(given) = value else {
            let (kind, what) = if schema.get("properties").is_some() && dataclass {
                ("dataclass_type", "an object")
            } else {
                ("dict_type", "an object")
            };
            errors.push(ArgError { loc: loc.into(), kind, message: format!("must be {what}, not {}", describe(value)) });
            return Value::Null;
        };
        let Some(properties) = schema.get("properties").and_then(Value::as_object) else {
            // A dict: every value checked against additionalProperties.
            let mut out = Map::new();
            for (key, item) in given {
                let checked = match schema.get("additionalProperties") {
                    Some(Value::Object(_)) => self.check(&schema["additionalProperties"], item, &join(loc, key), errors, false),
                    _ => item.clone(),
                };
                out.insert(key.clone(), checked);
            }
            return Value::Object(out);
        };
        let required: Vec<&str> =
            schema.get("required").and_then(Value::as_array).map(|r| r.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
        let mut out = Map::new();
        for (name, property) in properties {
            match given.get(name) {
                Some(item) => {
                    out.insert(name.clone(), self.check(property, item, &join(loc, name), errors, false));
                }
                None if required.contains(&name.as_str()) => {
                    errors.push(ArgError { loc: join(loc, name), kind: "missing", message: "is required".into() });
                }
                None => {
                    if let Some(default) = property.get("default") {
                        out.insert(name.clone(), default.clone());
                    }
                }
            }
        }
        if schema.get("additionalProperties") == Some(&Value::Bool(false)) {
            for key in given.keys() {
                if !properties.contains_key(key) {
                    errors.push(ArgError { loc: join(loc, key), kind: "unexpected_keyword_argument", message: "isn't a field it has".into() });
                }
            }
        }
        Value::Object(out)
    }
}

fn check_integer(value: &Value, loc: &str, errors: &mut Vec<ArgError>) -> Value {
    match value {
        Value::Number(n) if n.is_i64() || n.is_u64() => value.clone(),
        Value::Number(n) => {
            let f = n.as_f64().unwrap_or(f64::NAN);
            if f.fract() == 0.0 && f.is_finite() {
                Value::from(f as i64)
            } else {
                errors.push(ArgError { loc: loc.into(), kind: "int_from_float", message: "must be a whole number".into() });
                Value::Null
            }
        }
        Value::String(text) => match crate::event::python_int(text) {
            Some(i) => Value::from(i),
            None => {
                errors.push(ArgError { loc: loc.into(), kind: "int_parsing", message: "must be a whole number".into() });
                Value::Null
            }
        },
        Value::Bool(b) => Value::from(*b as i64),
        other => {
            errors.push(ArgError { loc: loc.into(), kind: "int_type", message: format!("must be a whole number, not {}", describe(other)) });
            Value::Null
        }
    }
}

fn check_number(value: &Value, loc: &str, errors: &mut Vec<ArgError>) -> Value {
    match value {
        Value::Number(_) => value.clone(),
        Value::String(text) => match text.trim().parse::<f64>() {
            Ok(f) => serde_json::Number::from_f64(f).map(Value::Number).unwrap_or(Value::Null),
            Err(_) => {
                errors.push(ArgError { loc: loc.into(), kind: "float_parsing", message: "must be a number".into() });
                Value::Null
            }
        },
        other => {
            errors.push(ArgError { loc: loc.into(), kind: "float_type", message: format!("must be a number, not {}", describe(other)) });
            Value::Null
        }
    }
}

fn check_boolean(value: &Value, loc: &str, errors: &mut Vec<ArgError>) -> Value {
    match value {
        Value::Bool(_) => value.clone(),
        Value::Number(n) if n.as_i64() == Some(0) || n.as_i64() == Some(1) => Value::Bool(n.as_i64() == Some(1)),
        Value::String(text) => match text.to_lowercase().as_str() {
            "0" | "off" | "f" | "false" | "n" | "no" => Value::Bool(false),
            "1" | "on" | "t" | "true" | "y" | "yes" => Value::Bool(true),
            _ => {
                errors.push(ArgError { loc: loc.into(), kind: "bool_parsing", message: "must be true or false".into() });
                Value::Null
            }
        },
        other => {
            errors.push(ArgError { loc: loc.into(), kind: "bool_type", message: format!("must be true or false, not {}", describe(other)) });
            Value::Null
        }
    }
}

fn looks_like_date_only(text: &str) -> bool {
    text.len() <= 10
}

/// A datetime pydantic would accept, as RFC 3339 text with its offset --
/// or, for one given without an offset, without one (pydantic keeps it
/// naive too).
pub fn parse_datetime(text: &str) -> Option<String> {
    let trimmed = text.trim();
    let normalized = trimmed.replacen(' ', "T", 1);
    let with_z = match normalized.strip_suffix(['Z', 'z']) {
        Some(rest) => format!("{rest}+00:00"),
        None => normalized.clone(),
    };
    for pattern in ["%Y-%m-%dT%H:%M:%S%.f%:z", "%Y-%m-%dT%H:%M%:z", "%Y-%m-%dT%H:%M:%S%.f%z", "%Y-%m-%dT%H:%M%z"] {
        if let Ok(moment) = DateTime::parse_from_str(&with_z, pattern) {
            return Some(crate::py::isoformat(&moment));
        }
    }
    for pattern in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M"] {
        if let Ok(naive) = NaiveDateTime::parse_from_str(&normalized, pattern) {
            return Some(naive.format("%Y-%m-%dT%H:%M:%S%.f").to_string());
        }
    }
    // A bare date is midnight, naive.
    if let Ok(date) = NaiveDate::parse_from_str(&normalized, "%Y-%m-%d") {
        return Some(format!("{date}T00:00:00"));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn schema() -> Value {
        json!({
            "type": "object",
            "properties": {
                "when": {"type": "string", "format": "date-time"},
                "count": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null},
                "statuses": {"anyOf": [{"type": "array", "items": {"enum": ["a", "b"], "type": "string"}}, {"type": "null"}], "default": null},
                "place": {"$ref": "#/$defs/Place"}
            },
            "required": ["when"],
            "$defs": {"Place": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": false}}
        })
    }

    #[test]
    fn accepts_and_fills_in_like_pydantic() {
        let s = schema();
        let checked = Validator::new(&s).validate(&json!({"when": "2026-10-07T09:00:00Z", "count": "5", "extra": 1})).unwrap();
        assert_eq!(checked, json!({"when": "2026-10-07T09:00:00+00:00", "count": 5, "statuses": null}));
    }

    #[test]
    fn refuses_by_field_and_kind() {
        let s = schema();
        let errors = Validator::new(&s)
            .validate(&json!({"when": "yesterday", "count": "many", "statuses": ["c"], "place": {"name": "x", "nope": 1}}))
            .unwrap_err();
        let found: Vec<(String, &str)> = errors.iter().map(|e| (e.loc.clone(), e.kind)).collect();
        assert_eq!(
            found,
            vec![
                ("when".into(), "datetime_from_date_parsing"),
                ("count".into(), "int_parsing"),
                ("statuses.0".into(), "literal_error"),
                ("place.nope".into(), "unexpected_keyword_argument"),
            ]
        );
        let missing = Validator::new(&s).validate(&json!({})).unwrap_err();
        assert_eq!(missing[0].kind, "missing");
    }
}
