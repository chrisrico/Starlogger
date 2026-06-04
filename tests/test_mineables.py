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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
