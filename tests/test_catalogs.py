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


def _cat(label, has_cache, cached_ver, rebuild=None):
    return catalogs._Catalog(label, lambda: has_cache, lambda: cached_ver,
                              rebuild or (lambda p4k, ver, reason: None))


def test_reason_no_cache():
    assert catalogs._reason(_cat("x", False, None), "4.8") == "no cache"


def test_reason_version_moved():
    assert catalogs._reason(_cat("x", True, "4.7"), "4.8") == "version 4.7 -> 4.8"
    assert catalogs._reason(_cat("x", True, None), "4.8") == "version ? -> 4.8"


def test_reason_fresh_is_none():
    assert catalogs._reason(_cat("x", True, "4.8.0"), "4.8.3") is None  # same major.minor
    assert catalogs._reason(_cat("x", True, "4.7"), None) is None       # no live version yet


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
