#!/usr/bin/env bash
# Copies a release's files to a Google Drive folder and removes the older
# releases of the same package there, so the folder holds only the latest
# one of each. See "Linux release packages" in README.md.
#
#     scripts/publish_to_drive.sh time-tracker-server-AUTH_DISABLED- dist/*
#
# The first argument is the package's file-name prefix: only files with it
# are removed, so the web app's packages, which the client repository copies
# to the same folder, are left alone. Needs rclone set up, and DRIVE_FOLDER
# (an rclone path, e.g. gdrive:TimeTracker) in the environment. rclone
# moves what it removes to Drive's trash, where it stays for 30 days.
set -euo pipefail

package=$1
shift

keep=()
for file in "$@"; do
  rclone copyto "$file" "$DRIVE_FOLDER/$(basename "$file")"
  keep+=(--filter "- $(basename "$file")")
done
# The first filter a file matches decides: these files stay, the rest of
# this package's go, and everything else stays.
rclone delete "$DRIVE_FOLDER" --max-depth 1 \
  "${keep[@]}" --filter "+ ${package}*" --filter "- *"
rclone lsl "$DRIVE_FOLDER" --max-depth 1
