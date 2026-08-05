#!/usr/bin/env bash
# recall-refresh.sh -- rebuild the file-existence index, then run the health pass.
#
# The index is what lets the engine verify, cheaply and at match time, that a
# catalogued path still exists. It is an ACCELERATOR: with no index at all the
# engine falls back to the disk and answers the same thing, only slower.
#
# ONE definition of the updatedb command lives here, so the timer and a manual
# run can never drift apart.
#
# Environment:
#   HARNESS_STATE_DIR         state directory (default: ~/.harness)
#   HARNESS_RECALL_INDEX_DB   index database (default: $STATE/recall/fs-index.db)
#   HARNESS_RECALL_SCOPE      directory tree to index (default: $HOME)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${HARNESS_STATE_DIR:-$HOME/.harness}"
DB="${HARNESS_RECALL_INDEX_DB:-$STATE/recall/fs-index.db}"
SCOPE="${HARNESS_RECALL_SCOPE:-$HOME}"

mkdir -p "$(dirname "$DB")"

# Noisy trees that hold nothing anyone would ever ask "did we already build it?"
# about. Space-separated on ONE value: updatedb -n takes a single string.
PRUNE=".git node_modules __pycache__ .cache .venv venv site-packages"
PRUNE="$PRUNE .npm .cargo .rustup .mozilla .pytest_cache .mypy_cache"
PRUNE="$PRUNE .ruff_cache dist build .next"

if command -v updatedb >/dev/null 2>&1; then
  TMP="${DB}.tmp.$$"
  # --require-visibility 0 is required for a per-user index: without it the
  # chown toward the system index group fails and nothing is written.
  updatedb -U "$SCOPE" -o "$TMP" --require-visibility 0 \
    -n "$PRUNE" --prune-bind-mounts yes
  mv -f "$TMP" "$DB"
else
  echo "recall-refresh: updatedb not installed, index skipped (existence" \
    "checks fall back to the disk)" >&2
fi

# Deterministic health pass: writes the freshness report the passive surface
# reads at session start. Never blocking for the index above.
HARNESS_RECALL_TODAY="$(date +%F)" \
  python3 "$HERE/recall.py" check-all --report >/dev/null || true
