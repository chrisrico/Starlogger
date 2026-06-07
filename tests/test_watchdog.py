"""Lifecycle watchdog decision logic (tracker.should_shutdown / _idle_timeout).

The tracker now lives while a dashboard SSE stream is open OR (on Linux) the launcher
runs, and exits after an idle grace once neither holds. should_shutdown() is the pure
core of that rule; these pin every branch without threads, signals, or a real clock.

Run: python -m pytest tests/test_watchdog.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracker

NOW = 1000.0
T = 30.0  # timeout


def _call(**over):
    kw = dict(streams=0, launcher_dead=False, has_launcher_detection=True,
              last_empty=None, launcher_dead_at=None, start_ts=0.0, now=NOW, timeout=T)
    kw.update(over)
    return tracker.should_shutdown(**kw)


# --- a dashboard is open -> never shut down ------------------------------- #

def test_open_stream_keeps_alive_linux():
    assert _call(streams=1, launcher_dead=True, last_empty=0.0) is False


def test_open_stream_keeps_alive_windows():
    assert _call(streams=1, has_launcher_detection=False, last_empty=0.0) is False


# --- Linux: launcher alive keeps it up regardless of clients -------------- #

def test_launcher_alive_keeps_alive():
    # no stream, last tab closed ages ago, but the launcher is still running
    assert _call(streams=0, launcher_dead=False, last_empty=0.0) is False


# --- Linux: launcher dead + idle since last tab closed -------------------- #

def test_launcher_dead_recent_close_stays():
    assert _call(launcher_dead=True, last_empty=NOW - 5) is False


def test_launcher_dead_stale_close_shuts_down():
    assert _call(launcher_dead=True, last_empty=NOW - 31) is True


def test_boundary_is_strict():
    # exactly at the timeout is not yet "> timeout"
    assert _call(launcher_dead=True, last_empty=NOW - T) is False


# --- Linux: never connected, launcher dies -> count from death ----------- #

def test_never_connected_launcher_death_stale():
    assert _call(launcher_dead=True, last_empty=None, launcher_dead_at=NOW - 31) is True


def test_never_connected_launcher_death_recent():
    assert _call(launcher_dead=True, last_empty=None, launcher_dead_at=NOW - 5) is False


def test_never_connected_missing_death_ts_falls_back_to_start():
    # defensive: launcher_dead True but no timestamp -> use start_ts
    assert _call(launcher_dead=True, last_empty=None, launcher_dead_at=None,
                 start_ts=NOW - 31) is True


# --- Windows: no launcher signal -> client-liveness only ----------------- #

def test_windows_idle_stale_shuts_down():
    assert _call(has_launcher_detection=False, launcher_dead=False,
                 last_empty=NOW - 31) is True


def test_windows_idle_recent_stays():
    assert _call(has_launcher_detection=False, last_empty=NOW - 5) is False


def test_windows_never_connected_counts_from_start():
    assert _call(has_launcher_detection=False, last_empty=None, start_ts=NOW - 31) is True


def test_windows_never_connected_recent_start_stays():
    assert _call(has_launcher_detection=False, last_empty=None, start_ts=NOW - 5) is False


# --- _idle_timeout env parsing ------------------------------------------- #

def test_idle_timeout_default(monkeypatch):
    monkeypatch.delenv("STARLOGGER_IDLE_TIMEOUT", raising=False)
    assert tracker._idle_timeout() == tracker.IDLE_TIMEOUT_DEFAULT


def test_idle_timeout_valid(monkeypatch):
    monkeypatch.setenv("STARLOGGER_IDLE_TIMEOUT", "10")
    assert tracker._idle_timeout() == 10.0


def test_idle_timeout_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("STARLOGGER_IDLE_TIMEOUT", "soon")
    assert tracker._idle_timeout() == tracker.IDLE_TIMEOUT_DEFAULT


@pytest.mark.parametrize("val", ["0", "-5", "0.5"])
def test_idle_timeout_clamped_to_one(monkeypatch, val):
    monkeypatch.setenv("STARLOGGER_IDLE_TIMEOUT", val)
    assert tracker._idle_timeout() == 1.0


# --- Presence counter ----------------------------------------------------- #

def test_presence_tracks_open_streams():
    p = tracker.Presence()
    assert p.snapshot()[0] == 0
    p.stream_connect()
    p.stream_connect()
    streams, last_empty, _, _ = p.snapshot()
    assert streams == 2 and last_empty is None  # still attached
    p.stream_disconnect()
    assert p.snapshot()[0] == 1 and p.snapshot()[1] is None  # one tab left, still attached
    p.stream_disconnect()
    streams, last_empty, _, _ = p.snapshot()
    assert streams == 0 and last_empty is not None  # last tab closed -> grace clock starts


def test_presence_disconnect_floors_at_zero():
    p = tracker.Presence()
    p.stream_disconnect()  # spurious; must not go negative
    assert p.snapshot()[0] == 0
