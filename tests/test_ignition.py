"""Launch orchestration (starlogger.ignition): StarStrings conditional GET, sc-launch.sh
resolution, and the re-exec game-PID adoption path.

These pin the file-touching + process-spawning behavior without real network or a real
game: urlopen is faked, notify is silenced, and the watcher is captured instead of threaded.

Run: python -m pytest tests/test_ignition.py
"""

from __future__ import annotations

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import ignition


@pytest.fixture(autouse=True)
def _silence_notify(monkeypatch):
    """Don't fire real desktop notifications during the suite."""
    monkeypatch.setattr(ignition, "notify", lambda *a, **k: None)


class _FakeResp:
    def __init__(self, data: bytes, etag: str | None = None):
        self._data = data
        self.headers = {"ETag": etag} if etag else {}

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _live(tmp_path):
    """A fake Game.log path inside a .../LIVE folder; returns (log_path, global.ini path)."""
    live = tmp_path / "drive_c" / "StarCitizen" / "LIVE"
    live.mkdir(parents=True)
    dest = live / "Data" / "Localization" / "english" / "global.ini"
    return str(live / "Game.log"), dest


# --- StarStrings: 200 writes atomically + stores the ETag ----------------- #

def test_starstrings_200_writes_and_stores_etag(tmp_path, monkeypatch):
    log_path, dest = _live(tmp_path)
    etag_file = tmp_path / "starstrings.etag"
    monkeypatch.setattr(ignition, "_ETAG_PATH", str(etag_file))

    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return _FakeResp(b"[strings]\nfoo=bar\n", etag='"newtag"')
    monkeypatch.setattr(ignition.urllib.request, "urlopen", fake_urlopen)

    ignition.update_starstrings(log_path)

    assert dest.read_bytes() == b"[strings]\nfoo=bar\n"
    assert etag_file.read_text() == '"newtag"'
    # no stored etag + no existing file on the first run -> no If-None-Match sent
    assert captured["req"].headers.get("If-none-match") is None


# --- StarStrings: 304 leaves the existing file (and etag) untouched -------- #

def test_starstrings_304_leaves_file_untouched(tmp_path, monkeypatch):
    log_path, dest = _live(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"OLD CONTENT")
    etag_file = tmp_path / "starstrings.etag"
    etag_file.write_text('"oldtag"')
    monkeypatch.setattr(ignition, "_ETAG_PATH", str(etag_file))

    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        raise urllib.error.HTTPError(ignition.STARSTRINGS_URL, 304, "Not Modified", None, None)
    monkeypatch.setattr(ignition.urllib.request, "urlopen", fake_urlopen)

    ignition.update_starstrings(log_path)

    assert dest.read_bytes() == b"OLD CONTENT"          # untouched
    assert etag_file.read_text() == '"oldtag"'          # untouched
    # both file + stored etag present -> conditional GET was sent
    assert captured["req"].headers.get("If-none-match") == '"oldtag"'


def test_starstrings_network_error_keeps_existing(tmp_path, monkeypatch):
    log_path, dest = _live(tmp_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"OLD CONTENT")
    monkeypatch.setattr(ignition, "_ETAG_PATH", str(tmp_path / "etag"))
    def boom(req, timeout=None):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(ignition.urllib.request, "urlopen", boom)

    ignition.update_starstrings(log_path)               # must not raise
    assert dest.read_bytes() == b"OLD CONTENT"


# --- sc-launch.sh resolution ---------------------------------------------- #

def test_resolve_sc_launch_from_wineprefix(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    launch = prefix / "sc-launch.sh"
    launch.write_text("#!/bin/sh\n")
    monkeypatch.setenv("WINEPREFIX", str(prefix))
    assert ignition.resolve_sc_launch("/anything/Game.log") == str(launch)


def test_resolve_sc_launch_derived_from_log_path(tmp_path, monkeypatch):
    monkeypatch.delenv("WINEPREFIX", raising=False)
    prefix = tmp_path / "star-citizen"
    launch = prefix / "sc-launch.sh"
    launch.parent.mkdir(parents=True)
    launch.write_text("#!/bin/sh\n")
    log_path = str(prefix / "drive_c" / "Program Files" / "RSI" / "LIVE" / "Game.log")
    assert ignition.resolve_sc_launch(log_path) == str(launch)


def test_resolve_sc_launch_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("WINEPREFIX", raising=False)
    assert ignition.resolve_sc_launch(str(tmp_path / "nope" / "Game.log")) is None


# --- launch_game: re-exec adoption never spawns a second game ------------- #

def test_launch_game_adopts_existing_pid(monkeypatch):
    monkeypatch.setenv("STARLOGGER_GAME_PID", "12345")
    def no_popen(*a, **k):
        raise AssertionError("must not spawn a second game when adopting")
    monkeypatch.setattr(ignition.subprocess, "Popen", no_popen)
    captured = {}
    monkeypatch.setattr(ignition, "_watch_until_exit",
                        lambda waiter, on_exit: captured.update(on_exit=on_exit))

    sentinel = object()
    assert ignition.launch_game("/x/Game.log", on_exit=sentinel) == 12345
    assert captured["on_exit"] is sentinel


def test_launch_game_no_launcher_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("STARLOGGER_GAME_PID", raising=False)
    monkeypatch.delenv("WINEPREFIX", raising=False)
    # resolve_sc_launch finds nothing -> serve without a game, don't crash
    assert ignition.launch_game(str(tmp_path / "Game.log"), on_exit=lambda: None) is None
