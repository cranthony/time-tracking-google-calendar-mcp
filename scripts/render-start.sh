#!/usr/bin/env bash
# Render's start command (set in the dashboard as `bash scripts/render-start.sh`),
# kept here for the same reason as render-build.sh. `exec` so the server
# itself gets Render's shutdown signal. The Rust server, as render-build.sh
# builds it; to go back to the Python one, `exec python server.py`.
set -euo pipefail

exec bin/ctt-server
