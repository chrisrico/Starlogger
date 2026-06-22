"""Real Lagrange-point enrichment: starmap-API parsing + the merge onto catalogued fields.

The network is never hit -- ``_fetch`` is injected with a canned response map -- so these pin
the join logic (archetype -> field name -> (system, name) match) and the offline fallback.

Run: .venv/bin/python -m pytest tests/test_starmap.py
"""

from __future__ import annotations

from starlogger import starmap


def _detail(archetype: str) -> dict:
    """A location-detail response shaped like the live API: a Salvage group (ignored) plus the
    Ship Mining group whose provider_names carries the spawn archetype."""
    return {"data": {"resources": [
        {"mining_type": "Salvage",
         "resources": [{"label": "Hull", "provider_names": ["HPP_Salvage_Derelicts"]}]},
        {"mining_type": "Ship Mining",
         "resources": [{"label": "Aluminum", "tier": "common", "provider_names": [archetype]}]},
    ]}}


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


# --- (system, field name) -> real points ------------------------------------ #
def test_field_points_maps_archetype_to_points():
    pts = starmap.field_points(fetch=_fake_fetch)
    # archetype token renders through the SAME _field_name the catalog uses
    assert pts[("Stanton", "Lagrange F")] == ["ARC-L1", "ARC-L4"]   # shared + sorted
    assert pts[("Stanton", "Lagrange E")] == ["CRU-L1"]
    assert pts[("Stanton", "Lagrange A")] == ["HUR-L1"]
    # a point with no ship-mining archetype contributes nothing
    assert not any(name == "Lagrange C" for _, name in pts)


def test_field_points_empty_when_all_fetches_fail():
    assert starmap.field_points(fetch=lambda slug: None) == {}


# --- merge onto catalogued fields ------------------------------------------- #
def test_add_field_points_enriches_matching_fields():
    fields = [
        {"name": "Lagrange F", "system": "Stanton", "ship_mineables": []},
        {"name": "Lagrange E", "system": "Stanton", "ship_mineables": []},
        {"name": "Aaron Halo", "system": "Stanton", "ship_mineables": []},  # no real points
        {"name": "Lagrange F", "system": "Pyro", "ship_mineables": []},     # wrong system
    ]
    n = starmap.add_field_points(fields, fetch=_fake_fetch)
    assert n == 2
    assert fields[0]["points"] == ["ARC-L1", "ARC-L4"]
    assert fields[1]["points"] == ["CRU-L1"]
    assert "points" not in fields[2]   # field the starmap doesn't know stays bare
    assert "points" not in fields[3]   # match is (system, name), not name alone


def test_add_field_points_offline_is_a_noop():
    fields = [{"name": "Lagrange F", "system": "Stanton", "ship_mineables": []}]
    assert starmap.add_field_points(fields, fetch=lambda slug: None) == 0
    assert "points" not in fields[0]
