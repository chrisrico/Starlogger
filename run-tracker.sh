#!/usr/bin/env bash
# Launch the SC mission tracker for a play session, tied to the caller's lifetime.
#
# Meant to be backgrounded from the LUG sc-launch.sh:
#   SCMT_LOG="$user_cfg_dir/Game.log" "$HOME/Code/sc-mission-tracker/run-tracker.sh" &
#
# Lifetime: `setpriv --pdeathsig` asks the kernel to send the tracker SIGTERM the
# moment the calling process (sc-launch) dies -- normal exit, closed terminal, or
# even SIGKILL -- so the caller needs no PID tracking, trap, or explicit kill.
# Skips if something already serves :8765. Data dir: $SCMT_DATA_DIR (default XDG).
set -euo pipefail

repo="$(dirname "$(readlink -f "$0")")"
py="$repo/.venv/bin/python"
: "${SCMT_DATA_DIR:=$HOME/.local/share/sc-mission-tracker}"
export SCMT_DATA_DIR

[ -x "$py" ] || { echo "run-tracker: venv missing ($py)" >&2; exit 0; }

# Already serving? Leave the running instance alone.
if (exec 3<>/dev/tcp/127.0.0.1/8765) 2>/dev/null; then
    echo "run-tracker: :8765 already in use -- not starting another" >&2
    exit 0
fi

mkdir -p "$SCMT_DATA_DIR"

# exec (no extra shell layer) so the tracker is a direct child of the caller, and
# setpriv sets the parent-death signal relative to it. Fall back to a plain exec
# where setpriv is unavailable -- there the caller must stop the tracker itself.
if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --pdeathsig TERM -- "$py" "$repo/tracker.py" "$@" \
        >> "$SCMT_DATA_DIR/tracker.log" 2>&1
fi
exec "$py" "$repo/tracker.py" "$@" >> "$SCMT_DATA_DIR/tracker.log" 2>&1
