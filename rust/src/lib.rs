//! The time-tracking Google Calendar MCP server, ported from Python (see
//! ../README.md): what the server (src/main.rs) and the command-line tools
//! (src/bin/) share.

pub mod app;
pub mod compaction;
pub mod determinism;
pub mod difflib;
pub mod dotenv;
pub mod event;
pub mod facts;
pub mod google;
pub mod health;
pub mod mcp;
pub mod py;
pub mod sheet;
pub mod stores;
pub mod timeline;
pub mod tools;
pub mod write_lock;
pub mod zoned;
#[cfg(test)]
pub mod testing;
