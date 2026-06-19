"""Render the systemd *user* unit for running Starlogger as a persistent service.

Pure text generation only -- the file write and `systemctl --user` orchestration live in
install.sh (shell, where every other system mutation already lives). tracker.py
--print-systemd-unit calls systemd_unit_text() with resolved absolute paths, and install.sh
captures that into ~/.config/systemd/user/starlogger.service. Kept out of tracker.py so the
rendering is unit-tested without standing up the server -- see tests/test_service.py.
"""

from __future__ import annotations

import os

UNIT_NAME = "starlogger.service"


def unit_dest_path() -> str:
    """Where the user unit installs: $XDG_CONFIG_HOME/systemd/user (default ~/.config)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if not (xdg and os.path.isabs(xdg)):
        xdg = os.path.expanduser("~/.config")
    return os.path.join(xdg, "systemd", "user", UNIT_NAME)


def systemd_unit_text(python_path: str, script_path: str, data_dir: str,
                      log_path: str | None = None) -> str:
    """The .service text for a persistent Starlogger user service.

    Absolute paths are baked in (NOT %h) because the data dir is configurable -- %h would
    silently point at the wrong tree for a non-default STARLOGGER_DATA_DIR. Environment=
    values are double-quoted: the resolved Game.log path contains spaces ("Program Files",
    "Roberts Space Industries"), and an unquoted value would be split. STARLOGGER_LOG is
    emitted only when known, so a service whose log can't be resolved at install time still
    falls back to runtime auto-detection (config.find_log) rather than baking in a bad value
    -- and never trips the "Could not find Game.log" exit that Restart=on-failure would loop.

    DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus points the screen-lock D-Bus watcher
    (screenlock.watch_screen_lock) at the user session bus: the systemd user manager does not
    reliably inherit it from the graphical login, and %t (XDG_RUNTIME_DIR) is always set for
    user units, where dbus-user-session/KDE place the bus.
    """
    env = [f'Environment="STARLOGGER_DATA_DIR={data_dir}"']
    if log_path:
        env.append(f'Environment="STARLOGGER_LOG={log_path}"')
    env.append('Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus"')
    env_block = "\n".join(env)
    return f"""\
[Unit]
Description=Starlogger -- Star Citizen cargo/flight logger + dashboard
Documentation=https://github.com/chrisrico/starlogger
After=graphical-session.target
# Backstop a crash-loop (e.g. an unresolvable log path): cap restarts within a window.
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory={data_dir}
{env_block}
# --service runs persistently: no browser, and the idle-exit watchdog is skipped so the
# dashboard stays up whether or not the game (or any dashboard tab) is running.
ExecStart={python_path} {script_path} --service
Restart=on-failure
RestartSec=2
# Bound a stop: if a graceful shutdown ever wedges (e.g. a self-update re-exec racing the
# stop), SIGKILL after this instead of the long default, so the unit never hangs in stopping.
TimeoutStopSec=20

[Install]
WantedBy=default.target
"""
