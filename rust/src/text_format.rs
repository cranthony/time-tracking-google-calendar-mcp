//! How a piece of free text -- a goal's description, an event's -- is
//! written, so the app can show it as it was meant.

use crate::contract::Schema;
use serde::{Deserialize, Serialize};

/// How a text is written: plain text, HTML or Markdown. None, it's as
/// it's always been read: HTML if it has tags, else plain text.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, Schema)]
pub enum TextFormat {
    #[serde(rename = "plain")]
    Plain,
    #[serde(rename = "html")]
    Html,
    #[serde(rename = "markdown")]
    Markdown,
}

impl TextFormat {
    pub fn text(self) -> &'static str {
        match self {
            TextFormat::Plain => "plain",
            TextFormat::Html => "html",
            TextFormat::Markdown => "markdown",
        }
    }

    /// The format `text` names, if it names one.
    pub fn parse(text: &str) -> Option<TextFormat> {
        match text.trim() {
            "plain" => Some(TextFormat::Plain),
            "html" => Some(TextFormat::Html),
            "markdown" => Some(TextFormat::Markdown),
            _ => None,
        }
    }
}
