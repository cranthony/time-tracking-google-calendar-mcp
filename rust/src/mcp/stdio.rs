//! MCP over stdio: one JSON-RPC message per line in, one per line out --
//! for a client that runs this server as its own subprocess (and for
//! conformance/'s scenarios). Messages are handled as they arrive, each
//! tool call on a worker thread, so calls that arrive together run at once
//! (writes still take turns: see write_lock).

use crate::app::App;
use serde_json::Value;
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

pub async fn serve(app: Arc<App>) -> anyhow::Result<()> {
    let stdin = BufReader::new(tokio::io::stdin());
    let stdout = Arc::new(tokio::sync::Mutex::new(tokio::io::stdout()));
    let session = Arc::new(Mutex::new(super::Session::new()));
    let mut lines = stdin.lines();
    let mut running = tokio::task::JoinSet::new();
    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }
        let message: Value = match serde_json::from_str(&line) {
            Ok(message) => message,
            Err(_) => {
                write_line(&stdout, &super::parse_error_response()).await?;
                continue;
            }
        };
        let is_call = message.get("method").and_then(Value::as_str) == Some("tools/call");
        if is_call {
            let (app, session, stdout) = (app.clone(), session.clone(), stdout.clone());
            running.spawn(async move {
                let response = tokio::task::spawn_blocking(move || {
                    let mut session = session.lock().unwrap_or_else(|e| e.into_inner());
                    // Only the session's flags are shared; a call itself
                    // doesn't need the lock held while it runs.
                    let mut local = super::Session { initialized: session.initialized };
                    drop(session);
                    let mut timings = Vec::new();
                    let response = super::handle(&app, &mut local, &message, &mut timings);
                    for (name, work_ms, ok) in timings {
                        app.health.record_call(&name, work_ms, None, ok);
                    }
                    response
                })
                .await
                .ok()
                .flatten();
                if let Some(response) = response {
                    let _ = write_line(&stdout, &response).await;
                }
            });
        } else {
            let response = {
                let mut session = session.lock().unwrap_or_else(|e| e.into_inner());
                super::handle(&app, &mut session, &message, &mut Vec::new())
            };
            if let Some(response) = response {
                write_line(&stdout, &response).await?;
            }
        }
    }
    while running.join_next().await.is_some() {}
    Ok(())
}

async fn write_line(stdout: &tokio::sync::Mutex<tokio::io::Stdout>, message: &Value) -> anyhow::Result<()> {
    let mut text = serde_json::to_string(message)?;
    text.push('\n');
    let mut out = stdout.lock().await;
    out.write_all(text.as_bytes()).await?;
    out.flush().await?;
    Ok(())
}
