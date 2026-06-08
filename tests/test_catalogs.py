"""The catalog-refresh decision logic in catalogs.py: when each cached catalog (ship
cargo / reference / mineables / blueprints) is considered stale, and that one pass rebuilds
exactly the stale ones — isolating each from the others' failures. No StarBreaker/p4k runs;
the build/save closures are fakes that just record calls.

Run: python3 -m pytest tests/test_catalogs.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import catalogs


def _cat(label, has_cache, cached_ver, rebuild=None, extract_ver=0, cached_extract_ver=0):
    return catalogs._Catalog(label, lambda: has_cache, lambda: cached_ver,
                              rebuild or (lambda p4k, ver, reason: None),
                              extract_ver, lambda: cached_extract_ver)


def test_reason_no_cache():
    assert catalogs._reason(_cat("x", False, None), "4.8") == "no cache"


def test_reason_version_moved():
    assert catalogs._reason(_cat("x", True, "4.7"), "4.8") == "version 4.7 -> 4.8"
    assert catalogs._reason(_cat("x", True, None), "4.8") == "version ? -> 4.8"


def test_reason_fresh_is_none():
    assert catalogs._reason(_cat("x", True, "4.8.0"), "4.8.3") is None  # same major.minor
    assert catalogs._reason(_cat("x", True, "4.7"), None) is None       # no live version yet


def test_reason_extract_schema_bumped():
    # The code emits v1 but the on-disk cache predates the stamp (v0): rebuild even though
    # the game version is unchanged -- this is what propagates a new field to existing installs.
    cat = _cat("x", True, "4.8", extract_ver=1, cached_extract_ver=0)
    assert catalogs._reason(cat, "4.8") == "extract schema v0 -> v1"


def test_reason_extract_schema_current_is_none():
    # Matching schema -> no rebuild; and v0-vs-absent (both 0) must NOT churn every launch.
    assert catalogs._reason(_cat("x", True, "4.8", extract_ver=1, cached_extract_ver=1), "4.8") is None
    assert catalogs._reason(_cat("x", True, "4.8", extract_ver=0, cached_extract_ver=0), "4.8") is None


def test_refresh_once_rebuilds_only_stale(monkeypatch):
    monkeypatch.setattr(catalogs.scdata, "find_p4k", lambda lp: "/fake/Data.p4k")
    calls = []
    mk = lambda label, has, ver: _cat(label, has, ver,
                                      lambda p4k, v, reason: calls.append(label))
    cats = [mk("ship cargo", True, "4.8"),   # current -> skip
            mk("reference", False, None),     # no cache -> rebuild
            mk("mineables", True, "4.7")]     # stale major -> rebuild
    catalogs._refresh_once(cats, "4.8", None)
    assert calls == ["reference", "mineables"]


def test_refresh_once_no_p4k_skips_all(monkeypatch):
    monkeypatch.setattr(catalogs.scdata, "find_p4k", lambda lp: None)
    calls = []
    cats = [_cat("ship cargo", False, None, lambda *a: calls.append("ship cargo"))]
    catalogs._refresh_once(cats, "4.8", None)
    assert calls == []


def _music_catalog(path="/fake/ship_cargo.json"):
    return next(c for c in catalogs._build_catalogs(path) if c.label == "music")


def test_music_schema_bump_forces_rebuild_even_when_song_set_unchanged(monkeypatch):
    # The keep-set is identical but the manifest predates a schema bump (cached < code): the
    # restamp shortcut MUST NOT fire, else the new row fields never reach the install and the
    # version gate stays satisfied forever. Falls through to the full re-decode instead.
    from starlogger import music, scdata
    monkeypatch.setattr(scdata, "scan_songs", lambda p4k: {"a", "b"})
    monkeypatch.setattr(music, "track_ids", lambda: {"a", "b"})
    monkeypatch.setattr(music, "music_extract_version", lambda: music.EXTRACT_VERSION - 1)
    did = []
    monkeypatch.setattr(music, "restamp_version", lambda ver: did.append("restamp"))
    monkeypatch.setattr(scdata, "build_music_from_p4k",
                        lambda p4k, out, progress=None: did.append("decode") or [])
    _music_catalog().rebuild("/fake/Data.p4k", "4.8", "extract schema v1 -> v2")
    assert did == ["decode"]


def test_music_no_new_songs_restamps_when_schema_current(monkeypatch):
    # Schema already current + unchanged keep-set -> the cheap restamp, no decode.
    from starlogger import music, scdata
    monkeypatch.setattr(scdata, "scan_songs", lambda p4k: {"a", "b"})
    monkeypatch.setattr(music, "track_ids", lambda: {"a", "b"})
    monkeypatch.setattr(music, "music_extract_version", lambda: music.EXTRACT_VERSION)
    did = []
    monkeypatch.setattr(music, "restamp_version", lambda ver: did.append("restamp"))
    monkeypatch.setattr(scdata, "build_music_from_p4k",
                        lambda p4k, out, progress=None: did.append("decode") or [])
    _music_catalog().rebuild("/fake/Data.p4k", "4.8", "version 4.7 -> 4.8")
    assert did == ["restamp"]


def test_refresh_once_failure_isolated(monkeypatch):
    monkeypatch.setattr(catalogs.scdata, "find_p4k", lambda lp: "/fake/Data.p4k")
    calls = []

    def boom(p4k, ver, reason):
        raise RuntimeError("nope")

    cats = [_cat("a", False, None, boom),
            _cat("b", False, None, lambda p4k, v, reason: calls.append("b"))]
    catalogs._refresh_once(cats, "4.8", None)   # 'a' raises, must not stop 'b'
    assert calls == ["b"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
