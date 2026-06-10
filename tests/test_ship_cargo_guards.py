"""Guards that stop a degraded extract from poisoning the ship catalog (the 2026-06-09
one-ship ``ships.json`` incident):

* ``save_ship_cargo`` refuses to overwrite a healthy catalog with a drastically smaller one.
* ``build_ships`` raises when too many per-ship ``entity loadout`` extractions fail, rather
  than silently dropping the unresolved ships and returning a decimated catalog.

No StarBreaker/p4k runs -- the heavy extract + per-ship subprocess calls are monkeypatched.

Run: python3 -m pytest tests/test_ship_cargo_guards.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import ships
from starlogger.scdata import _ships


# --- #1: save refuses to shrink a healthy catalog --------------------------- #
def _count(path):
    with open(path) as f:
        return len(json.load(f)["ships"])


def test_save_refuses_drastic_shrink(tmp_path):
    p = str(tmp_path / "ships.json")
    healthy = {f"Ship{i}": {"scu": 1} for i in range(100)}
    ships.save_ship_cargo(healthy, game_version="4.8", path=p)
    assert _count(p) == 100

    with pytest.raises(ships.PartialCatalogError):
        ships.save_ship_cargo({"Vulture": {"scu": 12}}, game_version="4.8", path=p)
    assert _count(p) == 100   # the good cache is left intact, not clobbered


def test_save_allows_first_build_and_minor_changes(tmp_path):
    p = str(tmp_path / "ships.json")
    ships.save_ship_cargo({"Vulture": {"scu": 12}}, path=p)   # no prior -> allowed
    assert _count(p) == 1
    big = {f"Ship{i}": {"scu": 1} for i in range(100)}
    ships.save_ship_cargo(big, path=p)                        # growth -> allowed
    assert _count(p) == 100
    trimmed = {f"Ship{i}": {"scu": 1} for i in range(60)}     # 60/100 > RETAIN_FRACTION
    ships.save_ship_cargo(trimmed, path=p)                    # mild shrink -> allowed
    assert _count(p) == 60


# --- #2: build raises when most per-ship loadout calls fail ------------------ #
def _stub_extract(monkeypatch, n_ships, run):
    """Make build_ships iterate ``n_ships`` fake vehicles with ``run`` as `entity loadout`,
    bypassing the real DataCore extract and record reads."""
    monkeypatch.setattr(_ships, "extract_records", lambda workdir, p4k, sb: "recs")
    monkeypatch.setattr(_ships, "load_localization", lambda recs: {})
    monkeypatch.setattr(_ships, "build_grid_index", lambda recs: {})
    monkeypatch.setattr(_ships, "build_component_index", lambda recs: {})
    monkeypatch.setattr(_ships, "base_ship_classes",
                        lambda recs: [(f"SHIP_{i}", f"/p/{i}.json") for i in range(n_ships)])
    monkeypatch.setattr(_ships, "_vehicle_classes", lambda recs, rel: [])
    monkeypatch.setattr(_ships, "_ship_meta", lambda path, loc: {})
    monkeypatch.setattr(_ships, "_run", run)


def test_build_raises_when_loadouts_mostly_fail(monkeypatch, tmp_path):
    def boom(sb, p4k, args, timeout=120):
        raise RuntimeError("starbreaker wedged")
    _stub_extract(monkeypatch, 20, boom)
    with pytest.raises(RuntimeError, match="loadout extractions failed"):
        _ships.build_ships("p4k", sb="sb", workdir=str(tmp_path))


def test_build_tolerates_a_few_failures(monkeypatch, tmp_path):
    # All loadout calls "succeed" (empty -> no cargo); no ship is kept, but no raise either.
    _stub_extract(monkeypatch, 20, lambda sb, p4k, args, timeout=120: "")
    assert _ships.build_ships("p4k", sb="sb", workdir=str(tmp_path)) == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
