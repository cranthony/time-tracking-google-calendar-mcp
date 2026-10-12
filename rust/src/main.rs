//! The time-tracking Google Calendar MCP server: the Rust port of
//! server.py. See ../README.md.

pub mod app;
pub mod determinism;
pub mod difflib;
pub mod event;
pub mod facts;
pub mod google;
pub mod health;
pub mod mcp;
pub mod py;
pub mod sheet;
pub mod stores;
pub mod tools;
pub mod write_lock;

use std::sync::Arc;

fn main() -> anyhow::Result<()> {
    // Logs go to stderr: over stdio, stdout carries the protocol.
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    let app = Arc::new(app::App::new(app::Config::from_env()));
    app.set_self();
    let transport = std::env::var("MCP_TRANSPORT").unwrap_or_else(|_| "stdio".into());
    let runtime = tokio::runtime::Builder::new_multi_thread().enable_all().build()?;
    match transport.as_str() {
        "streamable-http" => anyhow::bail!("streamable-http isn't ported yet"),
        _ => runtime.block_on(mcp::stdio::serve(app)),
    }
}
