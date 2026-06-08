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


def test_no_update_when_upstream_is_behind(monkeypatch):
    """Running from a checkout that's AHEAD of upstream: have != want, but want is an ancestor of
    have (upstream is behind). Must read as 'current' -- never apply, or reset --hard would delete
    the local commits. This is the dev-source footgun."""
    _stub_source(monkeypatch)
    monkeypatch.setattr(tracker, "_fetch_target", lambda *a: ("a" * 40, "b" * 40))
    monkeypatch.setattr(tracker, "_is_ancestor", lambda repo, anc, desc: True)   # want ⊂ have
    applied = threading.Event()
    monkeypatch.setattr(tracker, "_apply", lambda us, tr: applied.set())
    r = tracker._manual_check(tracker.UpdateState(), _State(), lambda: None)
    assert r["status"] == "current"
    assert not applied.wait(0.3)                                # _apply must NOT have run


def test_update_when_upstream_is_ahead(monkeypatch):
    """Upstream genuinely ahead: want is NOT an ancestor of have -> apply."""
    _stub_source(monkeypatch)
    monkeypatch.setattr(tracker, "_fetch_target", lambda *a: ("a" * 40, "c" * 40))
    monkeypatch.setattr(tracker, "_is_ancestor", lambda repo, anc, desc: False)  # want ⊄ have
    applied = threading.Event()
    monkeypatch.setattr(tracker, "_apply", lambda us, tr: applied.set())
    r = tracker._manual_check(tracker.UpdateState(), _State(), lambda: None)
    assert r["status"] == "updating" and applied.wait(2)


def _record_git(monkeypatch, shallow: bool):
    """Stub tracker._git so _fetch_target runs without a real repo; capture the fetch argv.
    rev-parse --is-shallow-repository answers `shallow`; HEAD/FETCH_HEAD return distinct hashes."""
    calls = []

    def fake(repo, *args, check=True):
        calls.append(list(args))
        if args[:2] == ("rev-parse", "--is-shallow-repository"):
            return "true\n" if shallow else "false\n"
        if args == ("rev-parse", "HEAD"):
            return "a" * 40 + "\n"
        if args == ("rev-parse", "FETCH_HEAD"):
            return "b" * 40 + "\n"
        return ""            # the fetch itself: non-None == success
    monkeypatch.setattr(tracker, "_git", fake)
    return calls


def test_fetch_target_never_shallows_a_full_clone(monkeypatch):
    """The bug that shallowed the dev source: a --depth 1 fetch on a FULL clone. A full clone
    must get a normal fetch (no --depth), which can't write .git/shallow."""
    calls = _record_git(monkeypatch, shallow=False)
    assert tracker._fetch_target("/repo", "origin", "main") == ("a" * 40, "b" * 40)
    fetch = next(c for c in calls if c and c[0] == "fetch")
    assert "--depth" not in fetch                              # full clone -> no shallow fetch


def test_fetch_target_keeps_an_install_shallow(monkeypatch):
    """An already-shallow managed install still fetches --depth 1, so it stays small."""
    calls = _record_git(monkeypatch, shallow=True)
    tracker._fetch_target("/repo", "origin", "main")
    fetch = next(c for c in calls if c and c[0] == "fetch")
    assert fetch[:3] == ["fetch", "--depth", "1"]


def test_repo_ready_refuses_when_source_is_self(monkeypatch, tmp_path):
    """A tracker run from the dev tree whose update_remote points back at that same tree must
    NOT be update-ready: fetching itself + reset --hard would clobber the dev checkout. The
    managed install (a different dir pulling FROM the dev tree) stays ready."""
    repo = tmp_path / "dev"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(tracker, "BASE_DIR", str(repo))
    monkeypatch.setattr(tracker, "_git", lambda *a, **k: "")          # clean tree

    # update_remote IS this checkout (the footgun) -> refuse
    monkeypatch.setattr(tracker.settings, "resolve_str",
                        lambda k: str(repo) if k == "update_remote" else "main")
    assert tracker._repo_ready() is None

    # a different source dir (the normal managed-install case) -> ready
    other = tmp_path / "install"
    other.mkdir()
    monkeypatch.setattr(tracker.settings, "resolve_str",
                        lambda k: str(other) if k == "update_remote" else "main")
    assert tracker._repo_ready() == str(repo)


def test_repo_ready_ignores_untracked_files(monkeypatch, tmp_path):
    """A managed install accumulates untracked runtime files (*.bak, etc.). reset --hard never
    touches those, so they must NOT block updates — the dirty check is tracked-only (-uno)."""
    repo = tmp_path / "install"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(tracker, "BASE_DIR", str(repo))

    seen = {}

    def fake_git(r, *args, **k):
        if args[:1] == ("status",):
            seen["status_args"] = args
            return ""          # tracked tree is clean (untracked files exist but are ignored)
        return ""

    monkeypatch.setattr(tracker, "_git", fake_git)
    monkeypatch.setattr(tracker.settings, "resolve_str",
                        lambda key: "origin" if key == "update_remote" else "main")
    assert tracker._repo_ready() == str(repo)
    assert "--untracked-files=no" in seen["status_args"]   # tracked-only, untracked don't block


# --- update_loop interval responsiveness (_check_due decision) ------------- #

def test_check_due_initial_check_is_immediate():
    # last_check is None => the post-launch check fires on the first tick.
    assert tracker._check_due(None, 1000.0, 900) is True


def test_check_due_waits_for_interval():
    # 100s since the last check, interval 900s -> not yet.
    assert tracker._check_due(1000.0, 1100.0, 900) is False
    # ...and exactly at the interval -> due.
    assert tracker._check_due(1000.0, 1900.0, 900) is True


def test_check_due_shortened_interval_takes_effect_now():
    # The whole point: 100s elapsed under a 900s interval is NOT due, but if the user shortens
    # it to 60s, the same elapsed time IS due -- because _check_due reads the CURRENT interval.
    assert tracker._check_due(1000.0, 1100.0, 900) is False
    assert tracker._check_due(1000.0, 1100.0, 60) is True


def test_check_due_disabled_never_checks():
    # interval <= 0 genuinely disables checks (matches the "0 disables" help), even long after.
    assert tracker._check_due(None, 1e9, 0) is False
    assert tracker._check_due(1000.0, 1e9, 0) is False
    assert tracker._check_due(1000.0, 1e9, -5) is False
