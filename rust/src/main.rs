//! The time-tracking Google Calendar MCP server. See ../README.md.

use time_tracker::{app, dotenv, mcp};
use std::sync::Arc;

fn main() -> anyhow::Result<()> {
    // Before anything reads the environment.
    dotenv::load(std::path::Path::new(".env"));
    // Logs go to stderr: over stdio, stdout carries the protocol.
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    let app = Arc::new(app::App::new(app::Config::from_env()));
    app.set_self();
    let transport = std::env::var("MCP_TRANSPORT").unwrap_or_else(|_| "stdio".into());
    let runtime = tokio::runtime::Builder::new_multi_thread().enable_all().build()?;
    app.start_health_writer();
    app.start_series_renewal();
    let served = match transport.as_str() {
        "streamable-http" => runtime.block_on(mcp::http::serve(app.clone())),
        _ => runtime.block_on(mcp::stdio::serve(app.clone())),
    };
    // As Python's atexit does: write what the health writer hasn't yet.
    if let Some(writer) = app.health_writer() {
        writer.stop();
    }
    served
}
