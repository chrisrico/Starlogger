#!/usr/bin/env bash
# Launch Starlogger for a play session, tied to the caller's lifetime.
#
# Meant to be backgrounded from the LUG sc-launch.sh:
#   STARLOGGER_LOG="$user_cfg_dir/Game.log" "$HOME/Code/starlogger/run-tracker.sh" &
#
# Lifetime: `setpriv --pdeathsig` asks the kernel to send the tracker SIGTERM the
# moment the calling process (sc-launch) dies -- normal exit, closed terminal, or
# even SIGKILL -- so the caller needs no PID tracking, trap, or explicit kill.
# Data dir: $STARLOGGER_DATA_DIR (default XDG).
#
# The "already serving?" decision lives in tracker.py (_wait_to_bind): on a relaunch
# the previous session's server is about to be torn down by sc-launch's `wineserver -k`,
# so a one-shot port check here would wrongly bail before that teardown frees :8765.
# We hand off to Python, which waits out the handoff and takes over (or leaves a
# genuinely healthy instance alone).
set -euo pipefail

repo="$(dirname "$(readlink -f "$0")")"
py="$repo/.venv/bin/python"
: "${STARLOGGER_DATA_DIR:=${XDG_DATA_HOME:-$HOME/.local/share}/starlogger}"
export STARLOGGER_DATA_DIR

[ -x "$py" ] || { echo "run-tracker: venv missing ($py)" >&2; exit 0; }

mkdir -p "$STARLOGGER_DATA_DIR"

# exec (no extra shell layer) so the tracker is a direct child of the caller, and
# setpriv sets the parent-death signal relative to it. Fall back to a plain exec
# where setpriv is unavailable -- there the caller must stop the tracker itself.
if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --pdeathsig TERM -- "$py" "$repo/tracker.py" "$@" \
        >> "$STARLOGGER_DATA_DIR/tracker.log" 2>&1
fi
exec "$py" "$repo/tracker.py" "$@" >> "$STARLOGGER_DATA_DIR/tracker.log" 2>&1
