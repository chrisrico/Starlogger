#!/usr/bin/env bash
# Starlogger one-shot installer (Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/chrisrico/starlogger/main/install.sh | bash
#
# Idempotent: clones (or updates) the repo into the data dir, builds its venv, and
# repoints your Star Citizen .desktop launcher at the clone's lib/sc-run.sh -- which hands off
# to the tracker, which then drives the game launch. Re-run it any time to update or to
# re-assert the .desktop if the LUG Helper has reverted it. After the first run the tracker
# self-updates on every launch (sc-run.sh is just the launch shim) -- this script is the bootstrap.
#
# Honored env: STARLOGGER_DATA_DIR (install location), WINEPREFIX (stale-copy cleanup).
set -uo pipefail

repo_url="https://github.com/chrisrico/starlogger.git"

# Install location == data dir (code and per-user data share one tree), resolved the
# same way as starlogger/config.py and run-tracker.sh: explicit override, else XDG
# (only if absolute), else ~/.local/share.
resolve_dir() {
    if [ -n "${STARLOGGER_DATA_DIR:-}" ]; then
        printf '%s\n' "$STARLOGGER_DATA_DIR"; return
    fi
    local xdg="${XDG_DATA_HOME:-}"
    case "$xdg" in
        /*) : ;;            # absolute -> use it
        *)  xdg="$HOME/.local/share" ;;
    esac
    printf '%s\n' "$xdg/starlogger"
}
dir="$(resolve_dir)"
launcher="$dir/lib/sc-run.sh"

die() { echo "install: $*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
command -v git >/dev/null 2>&1     || die "git is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

# --- clone or update -------------------------------------------------------
if [ -d "$dir/.git" ]; then
    echo "install: updating existing clone at $dir"
    git -C "$dir" fetch --quiet --depth 1 origin main \
        && git -C "$dir" reset --hard --quiet origin/main \
        || die "could not update $dir"
else
    echo "install: cloning into $dir"
    mkdir -p "$(dirname "$dir")" || die "could not create $(dirname "$dir")"
    GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 "$repo_url" "$dir" \
        || die "clone failed"
fi
[ -x "$launcher" ] || die "launcher missing after install: $launcher"

# --- venv + deps -----------------------------------------------------------
[ -x "$dir/.venv/bin/python" ] || python3 -m venv "$dir/.venv" \
    || die "could not create venv"
"$dir/.venv/bin/pip" install -q --disable-pip-version-check --upgrade -r "$dir/requirements.txt" \
    || die "dependency install failed"

# --- patch the .desktop launcher ------------------------------------------
# Repoint the existing Star Citizen entry at the clone's sc-run.sh. Prefer the LUG
# Helper's entry; otherwise pick the entry whose Exec mentions sc-launch.sh/sc-run.sh.
apps_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
case "${XDG_DATA_HOME:-}" in /*) ;; *) apps_dir="$HOME/.local/share/applications" ;; esac

find_desktop() {
    local f="$apps_dir/star-citizen-lug.desktop"
    [ -f "$f" ] && { printf '%s\n' "$f"; return; }
    grep -lE '^Exec=.*(sc-launch\.sh|sc-run\.sh)' "$apps_dir"/*.desktop 2>/dev/null | head -n1
}
desktop="$(find_desktop)"
if [ -n "$desktop" ]; then
    current="$(sed -n 's/^Exec=//p' "$desktop" | head -n1)"
    if [ "$current" = "$launcher" ]; then
        echo "install: .desktop already points at the tracker ($desktop)"
    else
        # Replace the whole Exec= value; preserve everything else in the file.
        tmp="$(mktemp)" || die "mktemp failed"
        if sed "s|^Exec=.*|Exec=$launcher|" "$desktop" > "$tmp" && cat "$tmp" > "$desktop"; then
            rm -f "$tmp"
            echo "install: patched $desktop -> Exec=$launcher"
        else
            rm -f "$tmp"
            die "could not patch $desktop"
        fi
    fi
else
    echo "install: no Star Citizen .desktop found in $apps_dir;"
    echo "         point your launcher's Exec at: $launcher"
fi

# --- remove now-redundant standalone sc-run.sh copies ----------------------
# These drift from the clone (the old install put a copy outside it). The launcher
# now lives only inside the clone, so any external copy is stale -- clear it out.
prefix="${WINEPREFIX:-$HOME/Games/star-citizen}"
for stale in "$HOME/Games/sc-run.sh" "$prefix/sc-run.sh"; do
    if [ -f "$stale" ] && [ "$stale" != "$launcher" ] \
        && grep -q 'starlogger' "$stale" 2>/dev/null; then
        rm -f "$stale" && echo "install: removed stale launcher copy $stale"
    fi
done

echo
echo "install: done. Launch Star Citizen as usual -- the tracker rides along and"
echo "         updates itself on every launch. Dashboard: http://127.0.0.1:8765"
echo "         Re-run this script if the LUG Helper ever reverts the .desktop."
