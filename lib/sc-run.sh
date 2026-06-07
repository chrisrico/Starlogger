#!/usr/bin/env bash
# Special-logic launcher layer for Star Citizen (LUG).
#
# A thin wrapper around the LUG-managed sc-launch.sh that adds:
#   - the StarStrings global.ini auto-update
#   - the starlogger mission tracker (auto-installed on demand)
#
# The renderer (DXVK vs SC's native Vulkan) is set by hand via the
# r.GraphicsRenderer cvar in the game's USER.cfg. Machine-specific env
# (GPU/driver/DXVK tunings) lives in sc-launch.sh's ENVIRONMENT VARIABLES
# section, so this script carries no per-machine settings and is portable
# as-is. It execs sc-launch.sh at the end.
#
# Usage:
#   sc-run.sh                          launch the game (tracker + sc-launch.sh)
#   sc-run.sh shell|config|controllers   passthrough to stock sc-launch.sh
#   WINEPREFIX=/path sc-run.sh          point at a non-default Star Citizen prefix
set -uo pipefail

: "${WINEPREFIX:=$HOME/Games/star-citizen}"
export WINEPREFIX
SC_LAUNCH="$WINEPREFIX/sc-launch.sh"
user_cfg_dir="$WINEPREFIX/drive_c/Program Files/Roberts Space Industries/StarCitizen/LIVE"
game_log="$user_cfg_dir/Game.log"

# Mission tracker (starlogger). Installed on demand into $tracker_dir; see
# ensure_tracker below.
tracker_repo="https://github.com/chrisrico/starlogger.git"
tracker_dir="$HOME/.local/share/starlogger"
tracker="$tracker_dir/run-tracker.sh"

# Updating is the TRACKER's job now (not this launcher). The running dashboard checks
# GitHub, prompts in the UI (Settings -> Updates: prompt / automatic / off), and self-applies
# + restarts -- so the remote/branch/mode knobs live in settings.json and are read by the
# Python tracker, not here. This launcher only installs the tracker on first use
# (ensure_tracker); it no longer fetches or prompts for updates.

############################################################################
# Shared helpers (notify).
############################################################################
# Desktop notification, guarded so a missing notify-send never breaks launch.
notify() {  # $1 = app   $2 = urgency (normal|critical)   $3 = summary
    command -v notify-send >/dev/null 2>&1 \
        && notify-send --app-name="$1" --urgency="$2" "$3"
}

############################################################################
# StarStrings: fetch the latest community global.ini if newer than local, and
# install it into the LIVE localization folder. The ETag is cached as an xattr.
# A desktop notification fires only on an actual update or a fetch failure (the
# up-to-date case stays quiet); on failure we keep whatever's already there.
############################################################################
update_starstrings() {
    local url='https://raw.githubusercontent.com/MrKraken/StarStrings/refs/heads/master/Data/Localization/english/global.ini'
    local dest_dir="$user_cfg_dir/Data/Localization/english"
    local dest="$dest_dir/global.ini"
    local xattr='user.starstrings.etag'
    local tmp compare save stored

    mkdir -p "$dest_dir" || return 0
    tmp=$(mktemp) && compare=$(mktemp) && save=$(mktemp) || {
        rm -f "$tmp" "$compare" "$save"
        return 0
    }

    # GitHub raw doesn't return Last-Modified; use an ETag conditional GET.
    local curl_args=(--silent --show-error --fail --location
        --connect-timeout 5 --max-time 30 -o "$tmp" --etag-save "$save")

    # Send If-None-Match only if both the file and its stored ETag are present.
    if [ -f "$dest" ]; then
        stored=$(getfattr --only-values -n "$xattr" "$dest" 2>/dev/null || true)
        if [ -n "$stored" ]; then
            # GitHub requires the ETag quoted per HTTP spec; getfattr strips the
            # surrounding quotes, so re-add them if missing.
            case "$stored" in
                \"*\") ;;
                *) stored="\"$stored\"" ;;
            esac
            printf '%s' "$stored" > "$compare"
            curl_args+=(--etag-compare "$compare")
        fi
    fi

    if curl "${curl_args[@]}" "$url"; then
        if [ -s "$tmp" ]; then
            mv "$tmp" "$dest"
            [ -s "$save" ] && setfattr -n "$xattr" -v "$(cat "$save")" "$dest" 2>/dev/null
            notify StarStrings normal "global.ini updated"
        fi
    else
        notify StarStrings critical "Update failed — using existing global.ini"
    fi
    rm -f "$tmp" "$compare" "$save"
}
update_starstrings &

############################################################################
# Maintenance subcommands -> straight through to stock sc-launch.sh
# (no tracker)
############################################################################
case "${1:-}" in
    shell|config|controllers) exec "$SC_LAUNCH" "$@" ;;
esac

############################################################################
# Mission tracker (starlogger), tied to THIS process's lifetime (run-tracker
# uses --pdeathsig). Installed on demand, then started for this session. The
# install + launch run backgrounded so the one-time clone/venv build never
# delays the game; on failure we just launch without a tracker.
############################################################################
# Install starlogger into $tracker_dir on first use (clone + build its venv).
# A fast no-op once present. Best-effort throughout.
ensure_tracker() {
    [ -n "$tracker_repo" ] || return 0
    # Already installed -> nothing to do (stay quiet).
    [ -x "$tracker" ] && [ -x "$tracker_dir/.venv/bin/python" ] && return 0
    # First-run install: anonymous clone (never block on a credential prompt) +
    # build its venv + deps. Notify on the outcome so the setup isn't silent.
    if command -v git >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 \
        && { [ -d "$tracker_dir/.git" ] || GIT_TERMINAL_PROMPT=0 git clone --depth 1 "$tracker_repo" "$tracker_dir"; } \
        && { [ -x "$tracker_dir/.venv/bin/python" ] || python3 -m venv "$tracker_dir/.venv"; } \
        && "$tracker_dir/.venv/bin/pip" install -q -r "$tracker_dir/requirements.txt" \
        && [ -x "$tracker" ]; then
        notify Starlogger normal "tracker installed"
    else
        notify Starlogger critical "tracker install failed"
        return 1
    fi
}
{ ensure_tracker && [ -x "$tracker" ] && STARLOGGER_LOG="$game_log" exec "$tracker"; } &

############################################################################
# Run the stock LUG launcher.
############################################################################
exec "$SC_LAUNCH"
