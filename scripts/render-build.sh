#!/usr/bin/env bash
# Render's build command (set in the dashboard as `bash scripts/render-build.sh`),
# kept here so changing how the server builds is a reviewed commit rather
# than a dashboard edit.
set -euo pipefail

pip install -r requirements.txt
