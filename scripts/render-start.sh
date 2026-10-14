#!/usr/bin/env bash
# Render's start command (set in the dashboard as `bash scripts/render-start.sh`),
# kept here for the same reason as render-build.sh. `exec` so the server
# itself gets Render's shutdown signal.
set -euo pipefail

exec bin/time-tracker-server
