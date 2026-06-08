#!/usr/bin/env bash
# Launch Starlogger for a play session.
#
# Two ways in:
#   - via lib/sc-run.sh (the .desktop entry point), which passes --launch so the tracker
#     spawns the game (sc-launch.sh) as its own child and ties lifetime to it;
#   - standalone (a terminal, a manual shortcut), watching an already-running game's log.
#
# Lifetime is the tracker's own concern now: under --launch the game is its child, so the
# game's exit flags launcher-death directly (no parent-death signal). Started standalone with
# no game to watch, it behaves like Windows -- stays up while a dashboard tab is open and
# idle-exits otherwise (STARLOGGER_IDLE_TIMEOUT seconds, default 30).
# Data dir: $STARLOGGER_DATA_DIR (default XDG).
#
# The "already serving?" decision lives in tracker.py (_wait_to_bind): on a relaunch
# the previous session's server is about to be torn down, so a one-shot port check here would
# wrongly bail before that teardown frees :8765. We hand off to Python, which waits out the
# handoff and takes over (or leaves a genuinely healthy instance alone).
set -euo pipefail

repo="$(dirname "$(readlink -f "$0")")"
py="$repo/.venv/bin/python"
: "${STARLOGGER_DATA_DIR:=${XDG_DATA_HOME:-$HOME/.local/share}/starlogger}"
export STARLOGGER_DATA_DIR

[ -x "$py" ] || { echo "run-tracker: venv missing ($py)" >&2; exit 0; }

mkdir -p "$STARLOGGER_DATA_DIR"

# exec (no extra shell layer) so the tracker is a direct child of the caller -- and, under
# --launch, the game it spawns is in turn our child, which is how launcher-death is observed.
exec "$py" "$repo/tracker.py" "$@" >> "$STARLOGGER_DATA_DIR/tracker.log" 2>&1
