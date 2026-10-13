//! The time-tracking Google Calendar MCP server (see ../README.md): what the server (src/main.rs) and the command-line tools
//! (src/bin/) share.

pub mod app;
pub mod auth_mode;
pub mod cli;
pub mod compaction;
pub mod contract;
pub mod determinism;
pub mod difflib;
pub mod dotenv;
pub mod event;
pub mod facts;
pub mod google;
pub mod health;
pub mod mcp;
pub mod py;
pub mod setup;
pub mod sheet;
pub mod stores;
pub mod timeline;
pub mod tools;
pub mod write_lock;
pub mod zoned;
#[cfg(test)]
pub mod testing;
