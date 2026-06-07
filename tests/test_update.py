"""Manual update-check logic (tracker._manual_check + UpdateState bookkeeping).

The "Check for updates" button checks synchronously and, when a new build exists, applies
it immediately (no prompt -- the click is the approval). These pin every branch by stubbing
the git helpers at the tracker boundary, so no network, clock, or real repo is touched.

Run: python -m pytest tests/test_update.py
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracker


class _State:
    """Stand-in for the live State: only bump_version() is exercised here."""
    def __init__(self) -> None:
        self.bumps = 0

    def bump_version(self) -> None:
        self.bumps += 1


def _stub_source(monkeypatch, repo="/repo", remote="origin", branch="main"):
    monkeypatch.setattr(tracker, "_repo_ready", lambda: repo)
    monkeypatch.setattr(tracker.settings, "resolve_str",
                        lambda k: remote if k == "update_remote" else branch)


def test_manual_check_blocked_when_not_a_clean_clone(monkeypatch):
    monkeypatch.setattr(tracker, "_repo_ready", lambda: None)   # dirty tree / not a clone
    r = tracker._manual_check(tracker.UpdateState(), _State(), lambda: None)
    assert r == {"ok": False, "status": "blocked"}


def test_manual_check_offline_when_fetch_fails(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(tracker, "_fetch_target", lambda *a: None)
    r = tracker._manual_check(tracker.UpdateState(), _State(), lambda: None)
    assert r == {"ok": False, "status": "offline"}


def test_manual_check_current_clears_a_stale_banner(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(tracker, "_fetch_target", lambda *a: ("a" * 40, "a" * 40))
    us = tracker.UpdateState()
    us.offer("aaaaaaaaa", "bbbbbbbbb", "b" * 40, None)          # a banner is showing
    st = _State()
    r = tracker._manual_check(us, st, lambda: None)
    assert r["ok"] is True and r["status"] == "current"
    assert us.available is False and st.bumps == 1             # cleared + pushed


def test_manual_check_current_no_bump_when_no_banner(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(tracker, "_fetch_target", lambda *a: ("a" * 40, "a" * 40))
    st = _State()
    r = tracker._manual_check(tracker.UpdateState(), st, lambda: None)
    assert r["status"] == "current" and st.bumps == 0          # nothing to clear


def test_manual_check_applies_immediately_on_new_build(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(tracker, "_fetch_target", lambda *a: ("a" * 40, "c" * 40))
    applied = threading.Event()
    monkeypatch.setattr(tracker, "_apply", lambda us, tr: applied.set())
    r = tracker._manual_check(tracker.UpdateState(), _State(), lambda: None)
    assert r["status"] == "updating"
    assert r["current"] == "aaaaaaaaa" and r["latest"] == "ccccccccc"
    assert applied.wait(2)                                     # _apply ran off-thread
