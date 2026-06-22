"""Shipbuilder matcher: outfit a ship's component slots with Grade-A blueprints, falling back to
the closest component class when the chosen class doesn't make a part that size. Pins the picker
invariants so it stays deterministic (and Grade-A-only) across catalog rebuilds.

The matcher joins a ships db (slot sizes/counts) with the blueprints catalog (craftable parts);
both are injected here (a hand-built ships db + a seeded temp blueprints.json) so the test owns
its data and never touches the real caches."""

from __future__ import annotations

import starlogger.blueprints as bp
from starlogger.shipbuild import CLASSES, _FALLBACK, ship_build_plan


def _bp(name, crafts, size, cls, grade="A"):
    return {"name": name, "crafts": crafts, "size": size, "cls": cls, "grade": grade,
            "requirements": [], "minerals": [], "category": f"Vehicle Component S{size}"}


# Two Military + one Civilian S1 power plant; the only S4 shield is Industrial; an S2 Military
# radar; plus a Grade-B power plant that must never be picked.
RECORDS = [
    _bp("PWR-Mil", "powr_x_s01_a", 1, "Military"),
    _bp("PWR-Mil2", "powr_x_s01_b", 1, "Military"),
    _bp("PWR-Civ", "powr_x_s01_c", 1, "Civilian"),
    _bp("PWR-MilB", "powr_x_s01_d", 1, "Military", grade="B"),
    _bp("SHL-Ind", "shld_x_s04_a", 4, "Industrial"),
    _bp("RDR-Mil", "radr_x_s02_a", 2, "Military"),
]


def _seed(tmp_path, records):
    path = tmp_path / "blueprints.json"
    bp.save_blueprints(records, game_version="t", path=str(path))
    bp.load_blueprints(str(path))      # prime the by-name cache for this path
    return str(path)


def _db():
    """A two-slot ship + radar: an S1 power plant (×2) and an S4 shield, plus an S2 radar."""
    return {"ships": {"Testbird": {
        "name": "Testbird", "class": "TST_Testbird",
        "components": {
            "power_plant": [{"name": "stock", "size": 1, "count": 2, "grade": "C"}],
            "shield": [{"name": "stock", "size": 4, "count": 1, "grade": "C"}],
        },
        "radar": {"size": 2, "stock": "stockradar"},
    }}}


def _plan(tmp_path, records=RECORDS, cls="Military"):
    return ship_build_plan("Testbird", cls, _db(), bp_path=_seed(tmp_path, records))


def test_exact_match_qty_and_alternatives(tmp_path):
    pp = next(b for b in _plan(tmp_path)["builds"] if b["slot"] == "Power Plant")
    assert pp["name"] == "PWR-Mil"          # first Military A by name
    assert pp["qty"] == 2                    # = the slot's stock count
    assert pp["cls"] == "Military" and pp["substituted"] is False
    assert "PWR-Mil2" in pp["alternatives"]   # the other Military A is an alternative...
    assert "PWR-Civ" not in pp["alternatives"]  # ...but a different class is not


def test_grade_b_is_never_picked(tmp_path):
    assert "PWR-MilB" not in {b["name"] for b in _plan(tmp_path)["builds"]}


def test_closest_class_substitution(tmp_path):
    shield = next(b for b in _plan(tmp_path)["builds"] if b["slot"] == "Shield")
    assert shield["name"] == "SHL-Ind"        # Military has no S4 shield -> closest class wins
    assert shield["cls"] == "Industrial" and shield["substituted"] is True


def test_radar_slot_filled_and_nothing_unmatched(tmp_path):
    plan = _plan(tmp_path)
    radar = next(b for b in plan["builds"] if b["slot"] == "Radar")
    assert radar["name"] == "RDR-Mil" and radar["qty"] == 1
    assert plan["unmatched"] == []
    assert plan["buildable"] is True


def test_unmatched_when_no_class_makes_the_part(tmp_path):
    # Drop the only S4 shield -> no class can craft that slot -> it's reported, not silently lost.
    plan = _plan(tmp_path, [r for r in RECORDS if r["name"] != "SHL-Ind"])
    assert {"slot": "Shield", "size": 4} in plan["unmatched"]
    assert all(b["slot"] != "Shield" for b in plan["builds"])


def test_fallback_orders_are_total_permutations(tmp_path):
    # Each class's fallback starts with itself and lists every class once -> a slot fills whenever
    # ANY class makes the part (the "no hard gaps beyond missing supply" guarantee).
    for c in CLASSES:
        order = _FALLBACK[c]
        assert order[0] == c
        assert sorted(order) == sorted(CLASSES)


def test_fallback_user_rules(tmp_path):
    # User rules: Stealth substitutes Military first; Civilian is the last resort everywhere it
    # isn't the chosen class (it's cheap to just buy, not worth crafting as a substitute).
    assert _FALLBACK["Stealth"][1] == "Military"
    for c in CLASSES:
        if c != "Civilian":
            assert _FALLBACK[c][-1] == "Civilian", c
