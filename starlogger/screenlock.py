"""Detect the desktop screen lock (Linux) so the dashboard can auto-pause the jukebox while the
screen is locked -- the same way it pauses while the game runs (web/jukebox.js). This lets a locked
screen (e.g. left overnight) actually sleep instead of being held awake by playing audio.

Watches the freedesktop ScreenSaver ``ActiveChanged`` D-Bus signal via ``dbus-monitor`` with an
*interface* match rule -- matching by interface rather than bus name, so it catches the signal
whichever service emits it (KDE, GNOME, …) without an extra Python dependency. It's a no-op on
Windows, headless sessions, or when dbus-monitor / the session bus isn't available, so it never
blocks startup. (active == screensaver/lock engaged.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

from .config import IS_WINDOWS

# dbus-monitor prints each signal as a header line then its argument(s), e.g.:
#   signal ... interface=org.freedesktop.ScreenSaver; member=ActiveChanged
#      boolean true
_MATCH = "type='signal',interface='org.freedesktop.ScreenSaver',member='ActiveChanged'"


def _feed(lines, on_change):
    """Drive ``on_change(locked: bool)`` from dbus-monitor output: an ActiveChanged header line is
    followed by its ``boolean true|false`` value on the next line. Pure/streaming so it's unit-
    testable with a list and reused verbatim as the watcher loop with the live stdout iterator."""
    pending = False
    for line in lines:
        if "member=ActiveChanged" in line:
            pending = True
        elif pending:
            s = line.strip().lower()
            if s.startswith("boolean"):
                try:
                    on_change("true" in s)
                except Exception:
                    pass
                pending = False


def watch_screen_lock(on_change):
    """Spawn a background watcher that calls ``on_change(locked: bool)`` on each lock/unlock.
    Returns the dbus-monitor subprocess (terminate it on shutdown) or None when screen-lock
    detection isn't available in this environment."""
    if IS_WINDOWS or not os.environ.get("DBUS_SESSION_BUS_ADDRESS") or not shutil.which("dbus-monitor"):
        return None
    try:
        proc = subprocess.Popen(
            ["dbus-monitor", "--session", _MATCH],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except OSError:
        return None
    threading.Thread(target=lambda: _feed(proc.stdout, on_change),
                     daemon=True, name="screenlock-watch").start()
    return proc
