"""Screen-lock detection that drives the jukebox auto-pause (web/jukebox.js jukeOnScreenLocked)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import screenlock


def test_feed_parses_activechanged():
    # dbus-monitor emits a signal header line then its boolean value on the next line.
    out = []
    screenlock._feed([
        "signal time=1 sender=:1.20 -> destination=(null) serial=9 path=/org/freedesktop/ScreenSaver;"
        " interface=org.freedesktop.ScreenSaver; member=ActiveChanged",
        "   boolean true",
        "signal time=2 sender=:1.20 ... member=ActiveChanged",
        "   boolean false",
    ], out.append)
    assert out == [True, False]


def test_feed_ignores_unrelated_and_orphan_lines():
    out = []
    screenlock._feed([
        "Monitoring connection on the session bus.",
        "method call ... member=GetActive",
        "   boolean true",     # a boolean NOT preceded by an ActiveChanged header -> ignored
    ], out.append)
    assert out == []


def test_watch_is_noop_without_a_session_bus(monkeypatch):
    # No session bus (or Windows / no dbus-monitor) -> detection unavailable -> returns None and
    # never blocks startup. Dropping the env var forces the unavailable path here.
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    assert screenlock.watch_screen_lock(lambda locked: None) is None
