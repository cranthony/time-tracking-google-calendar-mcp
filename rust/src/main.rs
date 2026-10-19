//! The time-tracking Google Calendar MCP server. See ../README.md.

use time_tracker::{app, dotenv, mcp};
use std::sync::Arc;

/// The most threads tool calls (and the rest of spawn_blocking) run on at
/// once, rather than tokio's 512: more wait their turn. The app's refresh
/// sends about a dozen at once.
const MAX_BLOCKING_THREADS: usize = 16;

fn main() -> anyhow::Result<()> {
    // Before anything reads the environment.
    dotenv::load(std::path::Path::new(".env"));
    // Logs go to stderr: over stdio, stdout carries the protocol.
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    // Before any thread starts: see allocator.rs.
    time_tracker::allocator::cap_arenas();
    let app = Arc::new(app::App::new(app::Config::from_env()));
    app.set_self();
    let transport = std::env::var("MCP_TRANSPORT").unwrap_or_else(|_| "stdio".into());
    let runtime = tokio::runtime::Builder::new_multi_thread().max_blocking_threads(MAX_BLOCKING_THREADS).enable_all().build()?;
    // Before the background threads start: their trims wait on it.
    time_tracker::allocator::set_runtime(runtime.handle().clone());
    app.start_health_writer();
    app.start_series_renewal();
    app.start_sheets_refresher();
    app.start_calendar_sync();
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
