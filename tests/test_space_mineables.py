"""Space mining locations: HarvestableProviderPreset parsing + the mineral->field reverse map.

Run: .venv/bin/python -m pytest tests/test_space_mineables.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import scdata, space_mineables
from starlogger.scdata._space_mineables import _field_name
from scdata_helpers import write_record

_RECORDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "p4k", "records")


# --- field naming (pure) ---------------------------------------------------- #
def test_field_name_variants():
    loc = {"stanton2c": "Yela"}
    assert _field_name("HPP_AaronHalo", loc) == "Aaron Halo"
    assert _field_name("HPP_Lagrange_E", loc) == "Lagrange E"
    assert _field_name("HPP_Nyx_KeegerBelt", loc) == "Keeger Belt"
    assert _field_name("HPP_Pyro_DeepSpaceAsteroids", loc) == "Deep Space Asteroids"
    assert _field_name("HPP_Stanton2c_Belt", loc) == "Yela Belt"   # body name resolved


# --- build against synthetic provider presets ------------------------------- #
def _preset(root: str, system: str, token: str, refs: list) -> None:
    p = os.path.join(root, "libs/foundry/records/harvestable/providerpresets/system",
                     system, "asteroidfield", token + ".json")
    val = {"_Type_": "HarvestableProviderPreset",
           "harvestableGroups": [{"archetype": f"file://./../../{r}.json"} for r in refs]}
    write_record(p, f"HarvestableProviderPreset.{token}", val)


def test_build_space_mineables(tmp_path):
    root = str(tmp_path)
    _preset(root, "pyro", "HPP_Pyro_AkiroCluster",
            ["mining_asteroidcommon_copper", "mining_asteroidcommon_iron",
             "mining_asteroiduncommon_titanium", "mining_asteroidrare_bexalite"])
    _preset(root, "stanton", "HPP_Stanton2c_Belt", ["mining_asteroidcommon_ice"])
    # generic cluster + resource-rush event -> skipped (not navigable destinations)
    _preset(root, "stanton", "AsteroidCluster_Low_Yield", ["mining_asteroidcommon_copper"])
    _preset(root, "stanton", "HPP_ResourceRush_Gold", ["mining_asteroidrare_gold"])
    # a preset with no ship-mineable archetype -> skipped
    _preset(root, "stanton", "HPP_Stanton_SalvageOnly", ["salvage_cluster_normal_common"])

    fields = scdata.build_space_mineables(root, {"stanton2c": "Yela"})
    by = {f["name"]: f for f in fields}
    assert set(by) == {"Akiro Cluster", "Yela Belt"}

    ak = by["Akiro Cluster"]
    assert ak["system"] == "Pyro"                       # from the record path
    # ordered common -> rare, then by name; each {mineral, rarity}
    assert ak["ship_mineables"] == [
        {"mineral": "Copper", "rarity": "common"},
        {"mineral": "Iron", "rarity": "common"},
        {"mineral": "Titanium", "rarity": "uncommon"},
        {"mineral": "Bexalite", "rarity": "rare"},
    ]
    assert by["Yela Belt"]["ship_mineables"] == [{"mineral": "Ice", "rarity": "common"}]


# --- reverse map: mineral -> fields, spelling-tolerant ---------------------- #
def _save_fields(path: str) -> None:
    space_mineables.save_space_mineables([
        {"name": "Aaron Halo", "system": "Stanton",
         "ship_mineables": [{"mineral": "Copper", "rarity": "common"},
                            {"mineral": "Bexalite", "rarity": "rare"}]},
        {"name": "Lagrange E", "system": "Stanton",
         "ship_mineables": [{"mineral": "Bexalite", "rarity": "rare"}],
         # real Lagrange points grouped by planet (via starmap)
         "points": [{"planet": "Crusader", "lpoints": ["L1", "L2"]},
                    {"planet": "Hurston", "lpoints": ["L3"]}]},
    ], game_version="4.8", path=path)
    space_mineables._cache["mtime"] = None


def test_locations_for(tmp_path):
    path = str(tmp_path / "space_mineables.json")
    _save_fields(path)
    # a field with no real points carries no `points` key (kept lean for the common case)
    assert space_mineables.locations_for("Copper", path=path) == [
        {"field": "Aaron Halo", "system": "Stanton", "rarity": "common"}]
    bx = space_mineables.locations_for("Bexalite", path=path)
    assert [(l["field"], l["rarity"]) for l in bx] == [("Aaron Halo", "rare"), ("Lagrange E", "rare")]
    # the archetype field surfaces its real points (grouped by planet); the plain field does not
    le = next(l for l in bx if l["field"] == "Lagrange E")
    assert le["points"] == [{"planet": "Crusader", "lpoints": ["L1", "L2"]},
                            {"planet": "Hurston", "lpoints": ["L3"]}]
    assert "points" not in next(l for l in bx if l["field"] == "Aaron Halo")
    assert space_mineables.locations_for("Unobtainium", path=path) == []


# --- integration: pin real coverage from the persistent extract ------------- #
@pytest.mark.skipif(not os.path.isdir(_RECORDS), reason="no ./p4k/records extract")
def test_space_mineables_from_extract():
    from starlogger.scdata._p4k import load_localization
    fields = scdata.build_space_mineables(_RECORDS, load_localization(_RECORDS))
    by = {f["name"]: f for f in fields}

    assert "Aaron Halo" in by and "Yela Belt" in by
    halo = {s["mineral"] for s in by["Aaron Halo"]["ship_mineables"]}
    assert {"Copper", "Beryl"} <= halo
    # space mining spans three systems
    assert sorted({f["system"] for f in fields}) == ["Nyx", "Pyro", "Stanton"]
    # copper in space is everywhere -- definitely not "only Euterpe"
    copper = {f["name"] for f in fields
              if any(s["mineral"] == "Copper" for s in f["ship_mineables"])}
    assert "Aaron Halo" in copper and len(copper) >= 5
    # generic clusters / resource-rush events are not catalogued
    assert not any("Cluster" == f["name"].split()[0] and "Yield" in f["name"] for f in fields)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
