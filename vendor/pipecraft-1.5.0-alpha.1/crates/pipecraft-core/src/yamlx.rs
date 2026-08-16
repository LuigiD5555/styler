//! Small coercion helpers over `serde_yaml::Value`.
//!
//! The prototype kept executor-specific config in a free-form `with:` mapping
//! and coerced values on demand (`_as_bool`, `_as_list`, `_as_mapping`). We keep
//! the same forgiving-but-explicit behaviour so existing pipeline YAML keeps
//! working: booleans accept `true/yes/1/on`, lists must be lists, etc.

use serde_yaml::Value;

use crate::error::{ConfigError, ConfigResult};

/// A `with:` style mapping: string keys to arbitrary YAML values.
pub type WithMap = std::collections::BTreeMap<String, Value>;

/// Read a string-ish value as `String`. Numbers/bools are stringified.
pub fn as_string(value: &Value) -> Option<String> {
    match value {
        Value::String(s) => Some(s.clone()),
        Value::Bool(b) => Some(b.to_string()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

/// Coerce a value to bool with the prototype's accepted spellings.
pub fn as_bool(value: &Value, field: &str) -> ConfigResult<bool> {
    match value {
        Value::Bool(b) => Ok(*b),
        Value::Null => Ok(false),
        Value::String(s) => {
            let clean = s.trim().to_ascii_lowercase();
            match clean.as_str() {
                "true" | "yes" | "1" | "on" => Ok(true),
                "false" | "no" | "0" | "off" => Ok(false),
                _ => Err(bool_err(field)),
            }
        }
        _ => Err(bool_err(field)),
    }
}

fn bool_err(field: &str) -> ConfigError {
    ConfigError::new(format!("Field '{field}' must be boolean"), "FIELD_TYPE_ERROR")
        .with_hint("Use true or false without quotes.")
}

/// Coerce a value to a list. `null` becomes an empty list.
pub fn as_list<'a>(value: &'a Value, field: &str) -> ConfigResult<Vec<&'a Value>> {
    match value {
        Value::Null => Ok(Vec::new()),
        Value::Sequence(items) => Ok(items.iter().collect()),
        _ => Err(
            ConfigError::new(format!("Field '{field}' must be a list"), "FIELD_TYPE_ERROR")
                .with_hint(format!("Use '- item' lines under '{field}'.")),
        ),
    }
}

/// Read a list of strings, lstripping a leading `#` (used for labels).
pub fn as_label_list(value: &Value, field: &str) -> ConfigResult<Vec<String>> {
    let mut out = Vec::new();
    for item in as_list(value, field)? {
        if let Some(s) = as_string(item) {
            out.push(s.trim_start_matches('#').to_string());
        }
    }
    Ok(out)
}

/// Read a list of plain strings (no `#` stripping).
pub fn as_string_list(value: &Value, field: &str) -> ConfigResult<Vec<String>> {
    let mut out = Vec::new();
    for item in as_list(value, field)? {
        if let Some(s) = as_string(item) {
            out.push(s);
        }
    }
    Ok(out)
}

/// Coerce a value into a `WithMap`. `null` becomes an empty map.
pub fn as_mapping(value: &Value, field: &str) -> ConfigResult<WithMap> {
    match value {
        Value::Null => Ok(WithMap::new()),
        Value::Mapping(m) => {
            let mut out = WithMap::new();
            for (k, v) in m {
                if let Some(key) = as_string(k) {
                    out.insert(key, v.clone());
                }
            }
            Ok(out)
        }
        _ => Err(
            ConfigError::new(format!("Field '{field}' must be a mapping"), "FIELD_TYPE_ERROR")
                .with_hint(format!("Use '{field}: {{}}' or nested YAML key/value pairs.")),
        ),
    }
}

/// Convenience: get a key from a YAML mapping value, returning `None` if absent
/// or if `value` is not a mapping.
pub fn get<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
    value.get(Value::String(key.to_string()))
}
