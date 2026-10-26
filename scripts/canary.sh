#!/bin/sh
# One-command canary run for a systemd timer / cron: the whole player path
# (page fetch + a few game turns) against a throwaway jhost on loopback.
# Prints one summary line; exit 0 only when the path works.
#   CANARY_KEEP=1 scripts/canary.sh   keeps the temp logs (path in the FAIL line)
set -eu
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" -m tests.network.canary "$@"
