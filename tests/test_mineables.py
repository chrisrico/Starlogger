"""Mineable-rock RS + composition extraction and the RS reverse-lookup.

Run: .venv/bin/python -m pytest tests/test_mineables.py  (or plain `python tests/test_mineables.py`)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import mineables, scdata


# --- tiny fixture mirroring the real DataCore record layout ----------------- #
def _write(path: str, record_name: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"_RecordName_": record_name, "_RecordValue_": value}, f)


def _entity(rs: float, comp_ref: str) -> dict:
    """An EntityClassDefinition value with the two components build_mineables reads."""
    return {"Components": [
        {"_Type_": "MineableParams", "composition": comp_ref},
        {"_Type_": "SSCSignatureSystemParams",
         "radarProperties": {"baseSignatureParams": {
             "signatures": [0.0, 0.0, 0.0, 0.0, rs, 0.0, 0.0, 0.0]}}},
    ]}


def _preset(deposit_name: str, min_distinct: int, parts: list) -> dict:
    return {"_Type_": "MineableComposition", "depositName": deposit_name,
            "minimumDistinctElements": min_distinct,
            "compositionArray": [
                {"mineableElement": e, "minPercentage": lo, "maxPercentage": hi,
                 "probability": pr} for e, lo, hi, pr in parts]}


def _fixture_records(root: str) -> None:
    ents = os.path.join(root, "libs/foundry/records/entities/mineable")
    presets = os.path.join(root, "libs/foundry/records/mining/rockcompositionpresets")
    # nested subfolder, as the real data splits ship-mining presets
    presets_sub = os.path.join(presets, "asteroidshipmining")
    elems = os.path.join(root, "libs/foundry/records/mining/mineableelements")

    _write(os.path.join(ents, "felsicmineablerock_titanium.json"),
           "EntityClassDefinition.FelsicMineableRock_Titanium",
           _entity(4000.0, "file://../mining/rockcompositionpresets/felsicdeposit_titanium.json"))
    _write(os.path.join(ents, "asteroidctypemineablerock_iron.json"),
           "EntityClassDefinition.AsteroidCTypeMineableRock_Iron",
           _entity(4700.0, "file://../rockcompositionpresets/asteroidshipmining/ctype_iron.json"))
    # a placeholder rock with no RS -> must be skipped
    _write(os.path.join(ents, "testmineablerock.json"),
           "EntityClassDefinition.TestMineableRock",
           _entity(0.0, "file://nope.json"))

    _write(os.path.join(presets, "felsicdeposit_titanium.json"),
           "MineableComposition.FelsicDeposit_Titanium",
           _preset("@type_felsic", 2, [
               ("file://../mineableelements/titanium_ore.json", 30.0, 70.0, 1.0),
               ("file://../mineableelements/beryl_raw.json", 30.0, 60.0, 0.9)]))
    _write(os.path.join(presets_sub, "ctype_iron.json"),
           "MineableComposition.CType_Iron",
           _preset("@type_ctype", 2, [
               ("file://../../mineableelements/iron_ore.json", 30.0, 70.0, 1.0)]))

    for ore in ("titanium_ore", "beryl_raw", "iron_ore"):
        _write(os.path.join(elems, ore + ".json"),
               "MineableElement." + "".join(w.capitalize() for w in ore.split("_")), {})


def test_build_mineables_extracts_rs_and_composition(tmp_path):
    root = str(tmp_path)
    _fixture_records(root)
    # localisation resolves the deposit type names
    loc = {"type_felsic": "Felsic", "type_ctype": "C-Type"}
    rocks = scdata.build_mineables(root, loc)

    assert len(rocks) == 2  # the RS-less placeholder is dropped
    by_cls = {r["class"]: r for r in rocks}

    fel = by_cls["FelsicMineableRock_Titanium"]
    assert fel["rs"] == 4000
    assert fel["deposit_name"] == "Felsic"
    assert fel["min_distinct"] == 2
    assert [e["element"] for e in fel["composition"]] == ["Titanium Ore", "Beryl Raw"]
    assert fel["composition"][0] == {"element": "Titanium Ore", "min_pct": 30.0,
                                     "max_pct": 70.0, "probability": 1.0}

    ctype = by_cls["AsteroidCTypeMineableRock_Iron"]  # preset in a nested subfolder
    assert ctype["rs"] == 4700
    assert ctype["deposit_name"] == "C-Type"
    assert [e["element"] for e in ctype["composition"]] == ["Iron Ore"]


def _entity_mech(rs: float, comp_ref: str, gp_ref: str) -> dict:
    """An entity value with the M1 mechanics components: MineableParams.globalParams +
    filledFactor and the SMineableHealthComponentParams hardness map. `damageStrength` is
    a Vec4 in the real data (a falloff curve), so it must be dropped, not surfaced."""
    return {"Components": [
        {"_Type_": "MineableParams", "composition": comp_ref,
         "globalParams": gp_ref, "filledFactor": 0.85},
        {"_Type_": "SMineableHealthComponentParams",
         "damageMapParamsCenter": {
             "damageStrength": {"_Type_": "Vec4", "x": 0.0, "y": 0.01, "z": 1.0, "w": 0.01},
             "laserDamageFullValue": 150.0}},
        {"_Type_": "SSCSignatureSystemParams",
         "radarProperties": {"baseSignatureParams": {
             "signatures": [0.0, 0.0, 0.0, 0.0, rs, 0.0, 0.0, 0.0]}}},
    ]}


def test_build_mineables_extracts_mechanics(tmp_path):
    root = str(tmp_path)
    ents = os.path.join(root, "libs/foundry/records/entities/mineable")
    presets = os.path.join(root, "libs/foundry/records/mining/rockcompositionpresets")
    mining = os.path.join(root, "libs/foundry/records/mining")
    elems = os.path.join(root, "libs/foundry/records/mining/mineableelements")

    _write(os.path.join(ents, "granitemineablerock_titanium.json"),
           "EntityClassDefinition.GraniteMineableRock_Titanium",
           _entity_mech(2000.0, "file://../mining/rockcompositionpresets/granite_titanium.json",
                        "file://../mining/miningglobalparamsship.json"))
    # a rock with no mechanics components -> mechanics is None (rides alongside cleanly)
    _write(os.path.join(ents, "plainmineablerock.json"),
           "EntityClassDefinition.PlainMineableRock",
           _entity(2500.0, "file://../mining/rockcompositionpresets/granite_titanium.json"))
    _write(os.path.join(presets, "granite_titanium.json"),
           "MineableComposition.Granite_Titanium",
           _preset("@type_granite", 1,
                   [("file://../mineableelements/titanium_ore.json", 30.0, 70.0, 1.0)]))
    _write(os.path.join(elems, "titanium_ore.json"), "MineableElement.Titanium_Ore", {})
    _write(os.path.join(mining, "miningglobalparamsship.json"),
           "MiningGlobalParamsShip.MiningGlobalParamsShip",
           {"_Type_": "MiningGlobalParamsShip", "resistanceCurveFactor": 0.5,
            "optimalWindowSize": 2.5, "optimalWindowMaxSize": 4.0,
            # a struct in the real data; we surface its wave period as `instability`
            "mineableInstabilityParams": {"_Type_": "MineableInstabilityParams",
                                          "instabilityWavePeriod": 3.0,
                                          "instabilityWaveVariance": 1.0},
            "defaultMass": 100.0, "cSCUPerVolume": 0.08})

    by_cls = {r["class"]: r for r in scdata.build_mineables(root, {"type_granite": "Granite"})}

    m = by_cls["GraniteMineableRock_Titanium"]["mechanics"]
    assert m["laser_power"] == 150.0        # per-rock hardness (health component)
    assert "damage_strength" not in m       # Vec4 curve is dropped, not dumped raw
    assert m["resistance"] == 0.5           # shared balance (global params, via ref)
    assert m["window_size"] == 2.5
    assert m["window_max"] == 4.0
    assert m["instability"] == 3.0          # the instability struct's wave period
    assert m["mass"] == 100.0
    assert m["scu_per_volume"] == 0.08
    assert m["filled_factor"] == 0.85

    assert by_cls["PlainMineableRock"]["mechanics"] is None


def test_lookup_rs_carries_mechanics(tmp_path):
    path = str(tmp_path / "mineables.json")
    mineables.save_mineables([
        {"class": "GraniteMineableRock_Titanium", "name": "Granite — Titanium",
         "deposit_name": "Granite", "rs": 2000, "min_distinct": 1, "composition": [],
         "mechanics": {"laser_power": 150.0, "mass": 100.0}},
    ], game_version="4.8", path=path)
    mineables._cache["mtime"] = None
    hit = mineables.lookup_rs(2000, path=path)
    assert hit[0]["rocks"][0]["mechanics"] == {"laser_power": 150.0, "mass": 100.0}


def _save_catalog(path: str) -> None:
    mineables.save_mineables([
        {"class": "FelsicMineableRock_Titanium", "name": "Felsic (Titanium)",
         "deposit_name": "Felsic", "rs": 4000, "min_distinct": 2,
         "composition": [{"element": "Titanium Ore", "min_pct": 30.0, "max_pct": 70.0,
                          "probability": 1.0}]},
        {"class": "AsteroidITypeMineableRock", "name": "I-Type", "deposit_name": "I-Type",
         "rs": 4000, "min_distinct": 2, "composition": []},
        {"class": "AsteroidCTypeMineableRock_Iron", "name": "C-Type (Iron)",
         "deposit_name": "C-Type", "rs": 4700, "min_distinct": 2, "composition": []},
    ], game_version="4.8", path=path)
    mineables._cache["mtime"] = None  # force the mtime cache to re-read the new file


def test_lookup_rs_single_rock_and_cluster(tmp_path):
    path = str(tmp_path / "mineables.json")
    _save_catalog(path)

    # one 4700 rock -> the C-type class, count 1
    one = mineables.lookup_rs(4700, path=path)
    assert len(one) == 1 and one[0]["base_rs"] == 4700 and one[0]["count"] == 1
    assert one[0]["rocks"][0]["deposit_name"] == "C-Type"

    # 9400 -> two 4700 rocks (count 2)
    two = mineables.lookup_rs(9400, path=path)
    c4700 = [c for c in two if c["base_rs"] == 4700]
    assert c4700 and c4700[0]["count"] == 2

    # 4000 is ambiguous: both the felsic and I-type classes share that base RS
    amb = mineables.lookup_rs(4000, path=path)
    base4000 = [c for c in amb if c["base_rs"] == 4000]
    assert base4000 and {r["class"] for r in base4000[0]["rocks"]} == {
        "FelsicMineableRock_Titanium", "AsteroidITypeMineableRock"}


def test_lookup_rs_rejects_non_multiples_and_bad_input(tmp_path):
    path = str(tmp_path / "mineables.json")
    _save_catalog(path)
    assert mineables.lookup_rs(4699, path=path) == []   # not a clean multiple of any base
    assert mineables.lookup_rs(0, path=path) == []
    assert mineables.lookup_rs("abc", path=path) == []


def _save_rich_catalog(path: str) -> None:
    """A catalog with real composition, for the forward/index/plan/decompose features."""
    def comp(*parts):
        return [{"element": e, "min_pct": lo, "max_pct": hi, "probability": pr}
                for e, lo, hi, pr in parts]
    mineables.save_mineables([
        {"class": "AsteroidCTypeMineableRock_Iron", "name": "Asteroid (C-Type) — Iron",
         "deposit_name": "Asteroid (C-Type)", "rs": 4700, "min_distinct": 2,
         "composition": comp(("Iron Ore", 30, 70, 1.0), ("Gold Ore", 20, 50, 0.1),
                             ("Bexalite Raw", 20, 50, 0.5))},
        {"class": "AsteroidSTypeMineableRock_Gold", "name": "Asteroid (S-Type) — Gold",
         "deposit_name": "Asteroid (S-Type)", "rs": 4720, "min_distinct": 2,
         "composition": comp(("Gold Ore", 40, 80, 1.0), ("Bexalite Raw", 10, 30, 0.3))},
        {"class": "FelsicMineableRock_Iron", "name": "Felsic Deposit — Iron",
         "deposit_name": "Felsic Deposit", "rs": 4000, "min_distinct": 2,
         "composition": comp(("Iron Ore", 20, 60, 0.8))},
    ], game_version="4.8", path=path)
    mineables._cache["mtime"] = None


def test_lookup_mineral_ranks_sources_and_lists_signatures(tmp_path):
    path = str(tmp_path / "mineables.json")
    _save_rich_catalog(path)
    res = mineables.lookup_mineral("gold", path=path)
    # both gold-bearing rocks, richest first (S-Type gold: 1.0*60=60 > C-Type gold: 0.1*35=3.5)
    assert [r["name"] for r in res["rocks"]] == ["Asteroid (S-Type) — Gold",
                                                 "Asteroid (C-Type) — Iron"]
    assert res["signatures"] == [4700, 4720]   # the RS values to hunt for gold
    assert res["rocks"][0]["score"] == 60.0
    # source rows carry the rock's mechanics so the Find tab can rank by minability
    assert "mechanics" in res["rocks"][0]
    assert mineables.lookup_mineral("", path=path)["rocks"] == []


def test_mineral_index_maps_each_mineral_to_rocks(tmp_path):
    path = str(tmp_path / "mineables.json")
    _save_rich_catalog(path)
    idx = {e["mineral"]: e for e in mineables.mineral_index(path=path)}
    assert set(idx) == {"Iron Ore", "Gold Ore", "Bexalite Raw"}
    assert idx["Iron Ore"]["count"] == 2
    assert idx["Iron Ore"]["signatures"] == [4000, 4700]


def test_decompose_rs_homogeneous_and_mixed(tmp_path):
    path = str(tmp_path / "mineables.json")
    _save_rich_catalog(path)
    # 9400 -> two C-type (4700) rocks, exact
    homo = mineables.decompose_rs(9400, path=path)
    assert any(c["parts"] == [{"base_rs": 4700, "count": 2,
                               "names": ["Asteroid (C-Type)"]}] for c in homo)
    # 9420 -> one C-type + one S-type (4700+4720), exact mixed cluster
    mixed = mineables.decompose_rs(9420, path=path)
    two = [c for c in mixed if len(c["parts"]) == 2 and c["residual"] == 0]
    assert two and {p["base_rs"] for p in two[0]["parts"]} == {4700, 4720}
    assert mineables.decompose_rs(0, path=path) == []


def test_mining_plan_coverage_ranks_multi_ingredient_deposits(tmp_path):
    path = str(tmp_path / "mineables.json")
    _save_rich_catalog(path)
    plan = mineables.mining_plan(["Gold", "Bexalite", "Iron"], path=path)
    assert plan["targets"] == ["Gold", "Bexalite", "Iron"]
    top = plan["coverage"][0]
    # C-Type yields all three -> ranks first
    assert top["deposit"] == "Asteroid (C-Type)" and top["n_covers"] == 3
    assert top["covers"] == ["Bexalite", "Gold", "Iron"]
    # per-mineral sourcing is present for each requested ingredient
    assert {p["mineral"] for p in plan["per_mineral"]} == {"Gold", "Bexalite", "Iron"}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
