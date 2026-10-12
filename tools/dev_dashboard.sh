#!/usr/bin/env bash
# Development only: run Ambient Mood from source with the mood trace on.
# The app serves the mood path dashboard and shows a "Mood path dashboard" link
# in the sidebar and the menu bar menu (http://127.0.0.1:8765).
#
#   bash tools/dev_dashboard.sh
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
export AMBIENT_MOOD_TRACE="${AMBIENT_MOOD_HOME:-$root}/data/mood_trace.jsonl"
exec "$root/.venv/bin/python" -m frontend.app "$@"
