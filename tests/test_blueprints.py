"""Crafting-blueprint extraction + lookup, and mineral-name reconciliation.

Run: python3 -m pytest tests/test_blueprints.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import blueprints, scdata
from starlogger.mineables import _mineral_matches


def _write(path: str, record_name: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"_RecordName_": record_name, "_RecordValue_": value}, f)


def _resource_cost(resource: str, scu: float, min_q: int, slot: str) -> dict:
    return {"_Type_": "CraftingCost_Select", "nameInfo": {"debugName": slot}, "count": 1,
            "options": [{"_Type_": "CraftingCost_Resource",
                         "resource": {"_RecordName_": "ResourceType." + resource},
                         "quantity": {"standardCargoUnits": scu}, "minQuality": min_q}]}


def _fixture(root: str) -> None:
    # the crafted item's entity, carrying the localised name
    _write(os.path.join(root, "libs/foundry/records/entities/scitem/foo_scitem.json"),
           "EntityClassDefinition.Foo_SCItem",
           {"Components": [{"AttachDef": {"Localization": {"Name": "@item_Name_Foo"}}}]})
    # the blueprint that crafts it
    _write(os.path.join(root, "libs/foundry/records/crafting/blueprints/crafting/weapons/bp_foo.json"),
           "CraftingBlueprintRecord.BP_Foo",
           {"blueprint": {
               "_Type_": "CraftingBlueprint",
               "category": {"_RecordName_": "BlueprintCategoryRecord.FPSWeapons"},
               "blueprintName": "@LOC_PLACEHOLDER",
               "processSpecificData": {"_Type_": "CraftingProcess_Creation",
                                       "entityClass": "file://../../../entities/scitem/foo_scitem.json"},
               "tiers": [{"_Type_": "CraftingBlueprintTier", "recipe": {"_Type_": "CraftingRecipe", "costs": {
                   "_Type_": "CraftingRecipeCosts",
                   "craftTime": {"_Type_": "TimeValue_Partitioned", "days": 0, "hours": 0,
                                 "minutes": 11, "seconds": 30.0},
                   "mandatoryCost": {"_Type_": "CraftingCost_Select", "count": 2, "options": [
                       _resource_cost("Borase", 0.42, 1, "SHELL"),
                       _resource_cost("Gold", 0.68, 0, "CORE")]}}}}]}})
    # a placeholder-named blueprint that must be dropped
    _write(os.path.join(root, "libs/foundry/records/crafting/blueprints/crafting/weapons/bp_ph.json"),
           "CraftingBlueprintRecord.BP_PH",
           {"blueprint": {"_Type_": "CraftingBlueprint",
                          "category": {"_RecordName_": "BlueprintCategoryRecord.MissionItem"},
                          "processSpecificData": {"entityClass": "file://nope.json"},
                          "tiers": [{"recipe": {"costs": {"mandatoryCost":
                              _resource_cost("Iron", 0.2, 0, "FRAME")}}}]}})


def test_build_blueprints_extracts_recipe(tmp_path):
    root = str(tmp_path)
    _fixture(root)
    bps = scdata.build_blueprints(root, {"item_name_foo": "Foo Widget"})
    assert len(bps) == 1                       # the unnamed placeholder is dropped
    b = bps[0]
    assert b["name"] == "Foo Widget"
    assert b["category"] == "FPS Weapons"       # acronym-preserving camel split
    assert b["craft_seconds"] == 11 * 60 + 30
    assert b["minerals"] == ["Borase", "Gold"]
    assert b["requirements"] == [
        {"slot": "Shell", "resource": "Borase", "scu": 0.42, "min_quality": 1},
        {"slot": "Core", "resource": "Gold", "scu": 0.68, "min_quality": 0}]


def test_lookup_blueprint_by_name(tmp_path):
    root = str(tmp_path)
    _fixture(root)
    bps = scdata.build_blueprints(root, {"item_name_foo": "Foo Widget"})
    path = str(tmp_path / "blueprints.json")
    blueprints.save_blueprints(bps, game_version="4.8", path=path)
    blueprints._cache["mtime"] = None

    assert blueprints.blueprint_names(path) == ["Foo Widget"]
    assert blueprints.lookup_blueprint("foo widget", path=path)["minerals"] == ["Borase", "Gold"]
    assert blueprints.lookup_blueprint("widget", path=path)["name"] == "Foo Widget"   # substring
    assert blueprints.lookup_blueprint("nothing", path=path) is None


def test_blueprint_section_derivation():
    # picker grouping helpers: size, weapon kind, model-line label, armour set, grade gate
    assert blueprints._size_num("Vehicle Component S3") == 3
    assert blueprints._size_num("FPS Weapons") is None
    assert blueprints._vc_subtype("shld_behr_s03_5ca_scitem") == "Shield"
    assert blueprints._vc_subtype("wep_tractorbeam_s1_utility_1") == "Tractor Beam"  # shared kind
    assert blueprints._vweapon_kind("kbar_ballisticcannon_s2") == "Cannon"
    assert blueprints._vweapon_kind("hrst_laserrepeater_s3") == "Repeater"
    assert blueprints._line_label(["Omnisky III Cannon", "Omnisky VI Cannon"], "Cannon") == "Omnisky"
    assert blueprints._line_label(["Deadbolt I Cannon", "Deadbolt II Cannon"], "Cannon") == "Deadbolt"
    # coded line with no shared word falls back to the common char-prefix
    assert blueprints._line_label(["CF-117 Bulldog Repeater", "CF-227 Badger Repeater"], "Repeater") == "CF"
    assert blueprints._armor_set("ADP Arms Black") == "ADP"
    assert blueprints._armor_set("A23 Flight Helmet Woodland") == "A23 Flight"
    # Grade A only, but keep components until a grade is present (pending p4k change)
    assert blueprints._keep_component({}) is True
    assert blueprints._keep_component({"grade": "A"}) is True
    assert blueprints._keep_component({"grade": "B"}) is False


def test_mineral_name_reconciliation():
    # spelling variants + Ore/Raw suffixes resolve to the same mineral
    assert _mineral_matches("Aluminum", "Aluminium Ore")
    assert _mineral_matches("Quantanium", "Quantainium Raw")
    assert _mineral_matches("Gold", "Gold Ore")
    assert _mineral_matches("gold", "Gold Ore")          # partial / case-insensitive
    assert not _mineral_matches("Tin", "Titanium Ore")   # not a false positive
    assert not _mineral_matches("", "Gold Ore")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
