#!/usr/bin/env bash
# Star Citizen (LUG) launch shim -- the .desktop entry point.
#
# The starlogger dashboard now DRIVES the launch: this script just hands off to the tracker,
# which spawns the stock sc-launch.sh as its own child (so the game's lifetime is the
# dashboard's), refreshes the StarStrings global.ini, and self-updates -- all in Python now
# (see starlogger/ignition.py + tracker.py). No StarStrings fetch, tracker bootstrap, or
# parent-death signal lives here anymore; install.sh does first-time setup.
#
# Machine-specific env (GPU/driver/DXVK, the DXVK-vs-native-Vulkan choice via USER.cfg) lives
# in sc-launch.sh, so this script carries no per-machine settings and is portable as-is.
#
# Usage:
#   sc-run.sh                          launch the game (via the tracker)
#   sc-run.sh shell|config|controllers   passthrough to stock sc-launch.sh (no tracker)
#   WINEPREFIX=/path sc-run.sh          point at a non-default Star Citizen prefix
set -uo pipefail

: "${WINEPREFIX:=$HOME/Games/star-citizen}"
export WINEPREFIX
SC_LAUNCH="$WINEPREFIX/sc-launch.sh"

# Maintenance subcommands -> straight through to the stock launcher (no tracker).
case "${1:-}" in
    shell|config|controllers) exec "$SC_LAUNCH" "$@" ;;
esac

# Otherwise hand off to the tracker (--launch => it spawns sc-launch.sh as its child). sc-run.sh
# lives in <repo>/lib, so the repo root -- and its venv + run-tracker.sh -- is one level up.
repo="$(dirname "$(dirname "$(readlink -f "$0")")")"
if [ -x "$repo/.venv/bin/python" ]; then
    exec "$repo/run-tracker.sh" --launch
fi

# Tracker unavailable (venv not built -- run install.sh): still launch the game so the .desktop
# is never a dead end, just without logging this session.
command -v notify-send >/dev/null 2>&1 \
    && notify-send --app-name=Starlogger --urgency=critical \
        "tracker venv missing — launching without it (run install.sh)"
exec "$SC_LAUNCH"
