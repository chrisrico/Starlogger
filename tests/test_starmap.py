"""Real Lagrange-point enrichment: the committed bundle (runtime) + the maintainer refresh.

The network is never hit -- ``_fetch`` is injected with a canned response map. These pin the
parsing, the bundle round-trip (refresh -> read -> merge), and the SHIPPED bundle's coverage.

Run: .venv/bin/python -m pytest tests/test_starmap.py
"""

from __future__ import annotations

import json
import os

import pytest

from starlogger import config, starmap


def _detail(archetype: str) -> dict:
    """A location-detail response shaped like the live API: a Salvage group (ignored), the Ship
    Mining group whose provider_names carries the spawn archetype, and the patch version in meta."""
    return {
        "data": {"resources": [
            {"mining_type": "Salvage",
             "resources": [{"label": "Hull", "provider_names": ["HPP_Salvage_Derelicts"]}]},
            {"mining_type": "Ship Mining",
             "resources": [{"label": "Aluminum", "tier": "common", "provider_names": [archetype]}]},
        ]},
        "meta": {"resource": {"version": "4.8.2-TEST"}},
    }


# Two ARC points share Lagrange F; CRU-L1 is E, HUR-L1 is A. mic-l5 has only a salvage group
# (no ship-mining archetype). Every other slug 404s -> fetch returns None.
_FAKE = {
    "arc-l1": _detail("HPP_Lagrange_F"),
    "arc-l4": _detail("HPP_Lagrange_F"),
    "cru-l1": _detail("HPP_Lagrange_E"),
    "hur-l1": _detail("HPP_Lagrange_A"),
    "mic-l5": {"data": {"resources": [{"mining_type": "Salvage", "resources": []}]}},
}


def _fake_fetch(slug: str):
    return _FAKE.get(slug)


# --- archetype extraction (pure) -------------------------------------------- #
def test_archetype_picks_ship_mining_group():
    assert starmap._archetype(_detail("HPP_Lagrange_F")) == "HPP_Lagrange_F"
    # no ship-mining group, or no provider at all -> None
    assert starmap._archetype({"data": {"resources": []}}) is None
    assert starmap._archetype(_FAKE["mic-l5"]) is None
    assert starmap._archetype({}) is None


# --- live fetch -> (point map, api version) --------------------------------- #
def test_field_points_maps_archetype_to_points():
    pts, version = starmap.field_points(fetch=_fake_fetch)
    # archetype token renders through the SAME _field_name the catalog uses
    assert pts[("Stanton", "Lagrange F")] == ["ARC-L1", "ARC-L4"]   # shared + sorted
    assert pts[("Stanton", "Lagrange E")] == ["CRU-L1"]
    assert pts[("Stanton", "Lagrange A")] == ["HUR-L1"]
    assert not any(name == "Lagrange C" for _, name in pts)   # mic-l5 had no ship-mining archetype
    assert version == "4.8.2-TEST"                            # captured from meta.resource.version


def test_field_points_empty_when_all_fetches_fail():
    pts, version = starmap.field_points(fetch=lambda slug: None)
    assert pts == {} and version is None


# --- maintainer refresh: write the bundle ----------------------------------- #
def test_refresh_bundle_writes_and_roundtrips(tmp_path):
    p = str(tmp_path / "bundle.json")
    n = starmap.refresh_bundle(path=p, fetch=_fake_fetch, stamp="2026-01-01T00:00:00Z")
    assert n == 3                                             # F, E, A (mic-l5 contributes nothing)

    data = json.loads(open(p, encoding="utf-8").read())
    assert data["game_version"] == "4.8.2-TEST"
    assert data["generated_at"] == "2026-01-01T00:00:00Z"
    assert {f["name"] for f in data["fields"]} == {"Lagrange A", "Lagrange E", "Lagrange F"}

    # the written bundle round-trips through the runtime reader + merge
    fields = [{"name": "Lagrange F", "system": "Stanton", "ship_mineables": []},
              {"name": "Lagrange E", "system": "Stanton", "ship_mineables": []}]
    assert starmap.add_field_points(fields, path=p) == 2
    assert fields[0]["points"] == ["ARC-L1", "ARC-L4"]


def test_refresh_bundle_raises_when_api_down(tmp_path):
    p = str(tmp_path / "nothing.json")
    with pytest.raises(RuntimeError):
        starmap.refresh_bundle(path=p, fetch=lambda slug: None)
    assert not os.path.exists(p)                             # a failed refresh leaves no file


# --- runtime: merge from the bundle (no network) ---------------------------- #
def test_add_field_points_enriches_matching_fields(tmp_path):
    p = str(tmp_path / "bundle.json")
    starmap.refresh_bundle(path=p, fetch=_fake_fetch, stamp="x")
    fields = [
        {"name": "Lagrange F", "system": "Stanton", "ship_mineables": []},
        {"name": "Aaron Halo", "system": "Stanton", "ship_mineables": []},  # not in the bundle
        {"name": "Lagrange F", "system": "Pyro", "ship_mineables": []},     # wrong system
    ]
    assert starmap.add_field_points(fields, path=p) == 1
    assert fields[0]["points"] == ["ARC-L1", "ARC-L4"]
    assert "points" not in fields[1]   # field the bundle doesn't know stays bare
    assert "points" not in fields[2]   # match is (system, name), not name alone


def test_add_field_points_missing_bundle_is_a_noop(tmp_path):
    fields = [{"name": "Lagrange F", "system": "Stanton", "ship_mineables": []}]
    assert starmap.add_field_points(fields, path=str(tmp_path / "absent.json")) == 0
    assert "points" not in fields[0]


# --- the SHIPPED bundle: guard against an empty/corrupt commit --------------- #
def test_shipped_bundle_covers_stanton_lagrange():
    pts = starmap._bundled(config.DEFAULT_LAGRANGE_POINTS_PATH)
    for letter in "ABCDEF":
        key = ("Stanton", f"Lagrange {letter}")
        assert pts.get(key), f"shipped bundle missing {key}"
    assert pts[("Stanton", "Lagrange E")] == ["CRU-L1", "CRU-L2", "HUR-L3"]
    # every value is a list of real point codes (e.g. ARC-L4, PYR1-L1)
    assert all("-L" in code for v in pts.values() for code in v)
