"""Screen-lock detection that drives the jukebox auto-pause (web/jukebox.js jukeOnScreenLocked)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import screenlock


def _hdr(iface):
    return ("signal time=1 sender=:1.20 -> destination=(null) serial=9 "
            f"path=/{iface.replace('.', '/')}; interface={iface}; member=ActiveChanged")


def test_feed_accepts_any_screensaver_interface():
    # KDE (freedesktop) + GNOME / Cinnamon / MATE (their own org.<de>.ScreenSaver) all parse.
    out = []
    screenlock._feed([
        _hdr("org.freedesktop.ScreenSaver"), "   boolean true",
        _hdr("org.gnome.ScreenSaver"), "   boolean false",
        _hdr("org.cinnamon.ScreenSaver"), "   boolean true",
        _hdr("org.mate.ScreenSaver"), "   boolean false",
    ], out.append)
    assert out == [True, False, True, False]


def test_feed_ignores_non_screensaver_activechanged():
    # A broad member='ActiveChanged' match can deliver other interfaces' signals -> keep only
    # the *.ScreenSaver ones.
    out = []
    screenlock._feed([
        "signal ... interface=org.example.Widget; member=ActiveChanged", "   boolean true",
    ], out.append)
    assert out == []


def test_feed_ignores_orphan_boolean():
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
