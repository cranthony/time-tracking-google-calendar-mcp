#!/bin/sh
# Runs pylint on the given files, failing only for error/warning-level
# findings (not convention/refactor -- see .pylintrc for why those are
# quieter here). Shared by .githooks/pre-commit (local commits to main)
# and .github/workflows/lint.yml (every PR into main), so both gates
# enforce exactly the same thing.
#
# Usage: pylint-check.sh <file> [<file> ...]

if [ "$#" -eq 0 ]; then
    exit 0
fi

if [ -x ".venv/Scripts/python.exe" ]; then
    python=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
    python=".venv/bin/python"
else
    python="python"
fi

if "$python" -m pylint "$@"; then
    status=0
else
    status=$?
fi

# pylint's exit code is a bitmask: 1=fatal, 2=error, 4=warning,
# 8=refactor, 16=convention, 32=usage error.
if [ $((status & 6)) -ne 0 ] || [ $((status & 33)) -ne 0 ]; then
    exit 1
fi
exit 0
