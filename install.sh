#!/usr/bin/env bash
# Starlogger one-shot installer (Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/chrisrico/starlogger/main/install.sh | bash
#
# Idempotent: clones (or updates) the repo into the data dir and builds its venv, then sets up
# ONE of two run modes (asks which when run on a terminal; switch any time by re-running):
#
#   shim     (default) — repoints your Star Citizen .desktop at the clone's lib/sc-run.sh, so
#                        the tracker rides along when you launch the game and exits with it.
#   service           — installs + enables a systemd USER service (tracker.py --service) that
#                        runs persistently (always-on dashboard); the game launches normally.
#                        The .desktop is reverted to the stock launcher so it can't fight the
#                        service for :8765. Runs while you're logged in (no linger).
#
# Flags: --service, --shim, --uninstall-service (alias of --shim). After the first run the
# tracker self-updates; this script is the bootstrap / mode switch.
#
# Honored env: STARLOGGER_DATA_DIR (install location), WINEPREFIX (.desktop revert + cleanup),
#              XDG_CONFIG_HOME (systemd user-unit location).
set -uo pipefail

repo_url="https://github.com/chrisrico/starlogger.git"

usage() {
    cat >&2 <<'EOF'
usage: install.sh [--service | --shim | --uninstall-service]
  (no flag)            ask on a terminal, else keep the current mode (shim on a fresh box)
  --service            run as an always-on systemd user service
  --shim               run as the game-launch shim (.desktop rides the tracker along)
  --uninstall-service  alias of --shim (tears the service down, restores the shim)
EOF
}

mode_flag=""
while [ $# -gt 0 ]; do
    case "$1" in
        --service)                 mode_flag=service ;;
        --shim|--uninstall-service) mode_flag=shim ;;
        -h|--help)                 usage; exit 0 ;;
        *)                         echo "install: unknown option: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

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
py="$dir/.venv/bin/python"

# Star Citizen .desktop lives in the XDG applications dir (absolute XDG_DATA_HOME only).
apps_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
case "${XDG_DATA_HOME:-}" in /*) ;; *) apps_dir="$HOME/.local/share/applications" ;; esac

# systemd user unit lives in the XDG config dir (absolute XDG_CONFIG_HOME only).
config_home="${XDG_CONFIG_HOME:-}"
case "$config_home" in /*) ;; *) config_home="$HOME/.config" ;; esac
unit_dir="$config_home/systemd/user"
unit_path="$unit_dir/starlogger.service"

die() { echo "install: $*" >&2; exit 1; }

# --- .desktop helpers ------------------------------------------------------
find_desktop() {
    local f="$apps_dir/star-citizen-lug.desktop"
    [ -f "$f" ] && { printf '%s\n' "$f"; return; }
    grep -lE '^Exec=.*(sc-launch\.sh|sc-run\.sh)' "$apps_dir"/*.desktop 2>/dev/null | head -n1
}

# set_desktop_exec DESIRED [guard-ours]
#   Rewrite the Star Citizen entry's Exec= to DESIRED. With "guard-ours", only do so when it
#   currently points at our sc-run.sh (so a custom Exec is never clobbered on a mode switch).
set_desktop_exec() {
    local desired="$1" guard="${2:-}" desktop current tmp
    desktop="$(find_desktop)"
    if [ -z "$desktop" ]; then
        echo "install: no Star Citizen .desktop found in $apps_dir; point its Exec at: $desired"
        return 0
    fi
    current="$(sed -n 's/^Exec=//p' "$desktop" | head -n1)"
    if [ "$current" = "$desired" ]; then
        echo "install: .desktop already Exec=$desired"
        return 0
    fi
    if [ "$guard" = "guard-ours" ] && [ "$current" != "$launcher" ]; then
        echo "install: leaving .desktop Exec as-is ($current) -- not our launcher"
        return 0
    fi
    tmp="$(mktemp)" || die "mktemp failed"
    # Escape the sed replacement: a `&`, `|` (our delimiter), or `\` in the path would otherwise
    # corrupt the substitution. (Paths rarely contain these, but $WINEPREFIX is user-set.)
    local esc; esc="$(printf '%s' "$desired" | sed 's/[&|\\]/\\&/g')"
    if sed "s|^Exec=.*|Exec=$esc|" "$desktop" > "$tmp" && cat "$tmp" > "$desktop"; then
        rm -f "$tmp"; echo "install: patched $desktop -> Exec=$desired"
    else
        rm -f "$tmp"; die "could not patch $desktop"
    fi
}

# --- mode helpers ----------------------------------------------------------
# Current mode is read straight from systemd (no marker file) so a plain re-run -- e.g. a
# `curl | bash` update -- PRESERVES the active mode instead of silently re-patching the
# .desktop back to the shim and re-introducing the :8765 fight with a running service.
detect_mode() {
    if command -v systemctl >/dev/null 2>&1 \
       && systemctl --user is-enabled --quiet starlogger.service 2>/dev/null; then
        echo service
    else
        echo shim
    fi
}

resolve_target_mode() {
    if [ -n "$mode_flag" ]; then echo "$mode_flag"; return; fi
    local current; current="$(detect_mode)"
    # Prompt only with a usable controlling terminal (works under curl|bash); else keep current.
    if { : >/dev/tty; } 2>/dev/null; then
        local def ans
        [ "$current" = service ] && def=2 || def=1
        {
            printf 'How should Starlogger run?\n'
            printf '  [1] game-launch shim  -- tracker rides along when you launch Star Citizen\n'
            printf '  [2] systemd service   -- always-on dashboard; launch the game normally\n'
            printf 'Choice [%s]: ' "$def"
        } >/dev/tty
        IFS= read -r ans </dev/tty || ans=""
        case "${ans:-$def}" in
            1) echo shim ;;
            2) echo service ;;
            *) echo "$current" ;;
        esac
        return
    fi
    echo "$current"
}

remove_service() {
    command -v systemctl >/dev/null 2>&1 || return 0   # no systemd -> nothing to tear down
    # Tear down only if there's actually something to remove: the unit is enabled (covers a
    # unit file someone deleted by hand) or the unit file is present. `disable --now` both
    # stops the running service and drops the WantedBy symlink.
    local had=""
    systemctl --user is-enabled --quiet starlogger.service 2>/dev/null && had=1
    [ -f "$unit_path" ] && had=1
    [ -n "$had" ] || return 0
    systemctl --user disable --now starlogger.service >/dev/null 2>&1
    rm -f "$unit_path"
    systemctl --user daemon-reload >/dev/null 2>&1
    echo "install: removed the starlogger systemd service"
}

configure_shim() {
    remove_service                       # idempotent switch away from service mode
    set_desktop_exec "$launcher"         # the .desktop drives the tracker (--launch)
}

configure_service() {
    command -v systemctl >/dev/null 2>&1 || die "systemctl not found -- systemd is required for --service"
    [ -x "$py" ] || die "venv python missing: $py"
    # Revert the .desktop to the stock launcher so it can't fight the always-on service for
    # :8765 (a `tracker --launch` would POST /api/quit and stop the service). Only touch it if
    # it currently points at our sc-run.sh; a custom Exec is left alone.
    set_desktop_exec "${WINEPREFIX:-$HOME/Games/star-citizen}/sc-launch.sh" guard-ours
    mkdir -p "$unit_dir" || die "could not create $unit_dir"
    # Render to a temp file in the same dir, then mv atomically -- a redirect into $unit_path
    # truncates the live unit BEFORE Python runs, so a failed render would leave it empty/corrupt.
    local tmp; tmp="$(mktemp "$unit_dir/.starlogger.XXXXXX")" || die "mktemp failed"
    if "$py" "$dir/tracker.py" --print-systemd-unit > "$tmp"; then
        mv "$tmp" "$unit_path"
    else
        rm -f "$tmp"; die "could not render the systemd unit"
    fi
    systemctl --user daemon-reload || die "systemctl --user daemon-reload failed"
    systemctl --user enable --now starlogger.service || die "could not enable starlogger.service"
    echo "install: enabled starlogger.service (systemctl --user)"
}

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
[ -x "$py" ] || python3 -m venv "$dir/.venv" \
    || die "could not create venv"
"$dir/.venv/bin/pip" install -q --disable-pip-version-check --upgrade -r "$dir/requirements.txt" \
    || die "dependency install failed"

# --- apply the run mode ----------------------------------------------------
mode="$(resolve_target_mode)"
case "$mode" in
    service) configure_service ;;
    shim)    configure_shim ;;
    *)       die "internal: bad mode '$mode'" ;;
esac

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
if [ "$mode" = service ]; then
    echo "install: done -- running as a systemd user service (while you're logged in)."
    echo "         Dashboard: http://127.0.0.1:8765"
    echo "         Logs:      journalctl --user -u starlogger -f"
    echo "         Switch back to the launch shim any time:  $dir/install.sh --shim"
else
    echo "install: done. Launch Star Citizen as usual -- the tracker rides along and"
    echo "         updates itself on every launch. Dashboard: http://127.0.0.1:8765"
    echo "         Run it always-on instead (systemd service):  $dir/install.sh --service"
    echo "         Re-run this script if the LUG Helper ever reverts the .desktop."
fi
