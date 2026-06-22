"""Crafting-blueprint extraction + lookup, and mineral-name reconciliation.

Run: python3 -m pytest tests/test_blueprints.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import blueprints, scdata
from scdata_helpers import write_record
from starlogger.mineables import _mineral_matches


def _resource_cost(resource: str, scu: float, min_q: int, slot: str) -> dict:
    return {"_Type_": "CraftingCost_Select", "nameInfo": {"debugName": slot}, "count": 1,
            "options": [{"_Type_": "CraftingCost_Resource",
                         "resource": {"_RecordName_": "ResourceType." + resource},
                         "quantity": {"standardCargoUnits": scu}, "minQuality": min_q}]}


def _fixture(root: str) -> None:
    # the crafted item's entity, carrying the localised name
    write_record(os.path.join(root, "libs/foundry/records/entities/scitem/foo_scitem.json"),
           "EntityClassDefinition.Foo_SCItem",
           {"Components": [{"_Type_": "SAttachableComponentParams",
                           "AttachDef": {"Grade": 1, "Size": 2,
                                         "Localization": {"Name": "@item_Name_Foo",
                                                          "Description": "@item_Desc_Foo"}}}]})
    # the blueprint that crafts it
    write_record(os.path.join(root, "libs/foundry/records/crafting/blueprints/crafting/weapons/bp_foo.json"),
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
    # two reward pools that grant bp_foo: one wired to a faction's missions via a contract
    # generator (org dir -> "Foxwell Enforcement"), one a standalone event pool (XenoThreat).
    def _pool(rec, bp_ref):
        return {"_Type_": "BlueprintPoolRecord", "blueprintRewards": [
            {"_Type_": "BlueprintReward", "weight": 1.0,
             "blueprintRecord": f"file://./../../{bp_ref}"}]}
    write_record(os.path.join(root, "libs/foundry/records/crafting/blueprintrewards/blueprintmissionpools/bp_missionreward_test.json"),
           "BlueprintPoolRecord.BP_MissionReward_Test", _pool("x", "weapons/bp_foo.json"))
    write_record(os.path.join(root, "libs/foundry/records/crafting/blueprintrewards/xenothreat2rewards/bp_rewards_xenothreat2_test.json"),
           "BlueprintPoolRecord.BP_Rewards_XenoThreat2_Test", _pool("x", "weapons/bp_foo.json"))
    # a generator under the foxwellenforcement org dir whose contract grants the first pool;
    # the contract carries a player-facing Title (a stringParamOverride) the planner resolves.
    write_record(os.path.join(root, "libs/foundry/records/contracts/contractgenerator/mercenary_guild/foxwellenforcement/foxwell_test.json"),
           "ContractGenerator.Foxwell_Test",
           {"_Type_": "ContractGenerator", "generators": [{"_Type_": "Generator", "contracts": [
               {"_Type_": "Contract",
                "paramOverrides": {"stringParamOverrides": [
                    {"param": "Title", "value": "@Foxwell_Test_Title"}]},
                "missionResults": [{"rewards": [{"_Type_": "BlueprintRewards", "blueprintPool":
                    "file://./../../../../crafting/blueprintrewards/blueprintmissionpools/bp_missionreward_test.json"}]}]}]}]})
    # a placeholder-named blueprint that must be dropped
    write_record(os.path.join(root, "libs/foundry/records/crafting/blueprints/crafting/weapons/bp_ph.json"),
           "CraftingBlueprintRecord.BP_PH",
           {"blueprint": {"_Type_": "CraftingBlueprint",
                          "category": {"_RecordName_": "BlueprintCategoryRecord.MissionItem"},
                          "processSpecificData": {"entityClass": "file://nope.json"},
                          "tiers": [{"recipe": {"costs": {"mandatoryCost":
                              _resource_cost("Iron", 0.2, 0, "FRAME")}}}]}})


def test_build_blueprints_extracts_recipe(tmp_path):
    root = str(tmp_path)
    _fixture(root)
    bps = scdata.build_blueprints(root, {"item_name_foo": "Foo Widget",
                                         "item_desc_foo": "Item Type: Cannon\\nClass: Military"})
    assert len(bps) == 1                       # the unnamed placeholder is dropped
    b = bps[0]
    assert b["name"] == "Foo Widget"
    assert b["category"] == "FPS Weapons"       # acronym-preserving camel split
    assert b["craft_seconds"] == 11 * 60 + 30
    assert b["minerals"] == ["Borase", "Gold"]
    assert b["requirements"] == [
        {"slot": "Shell", "resource": "Borase", "scu": 0.42, "min_quality": 1},
        {"slot": "Core", "resource": "Gold", "scu": 0.68, "min_quality": 0}]
    assert b["grade"] == "A" and b["grade_num"] == 1   # from AttachDef.Grade
    assert b["size"] == 2                               # from AttachDef.Size
    assert b["cls"] == "Military"                       # parsed from the description's Class: line


def test_blueprint_sources_from_reward_pools(tmp_path):
    # bp_foo is granted by a Foxwell contract (its Title resolved + cleaned of the [tag] and the
    # ~mission() fill-in) and a standalone XenoThreat event pool (faction only, no contract).
    root = str(tmp_path)
    _fixture(root)
    loc = {"foxwell_test_title": "[Yellow] Disrupt ~mission(Location)"}
    expected = [{"faction": "Foxwell Enforcement", "contracts": ["Disrupt a location"]},
                {"faction": "XenoThreat", "contracts": []}]
    src = scdata.build_blueprint_sources(root, loc)
    assert src["bp_foo"] == expected
    b = scdata.build_blueprints(root, {**loc, "item_name_foo": "Foo Widget"})[0]
    assert b["sources"] == expected


def test_clean_contract_title_strips_tags_and_placeholders():
    from starlogger.scdata._blueprints import _clean_contract_title
    assert _clean_contract_title("[Yellow Level] Deal with ~mission(TargetName)") == "Deal with the target"
    assert _clean_contract_title("  Wanted:   ~mission(Item) ") == "Wanted: an item"
    assert _clean_contract_title("~mission(UnknownTok)") == "…"   # unknown token -> ellipsis
    assert _clean_contract_title("") == ""


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


def test_aggregate_blueprints(tmp_path):
    # A build-list of {name, qty} merges by resource: scu scales with qty, the two blueprints'
    # shared Gold sums, the strictest min_quality wins, craft time is qty-weighted, and an
    # unknown name is echoed (found=False) without polluting the totals.
    path = str(tmp_path / "blueprints.json")
    bps = [
        {"name": "Widget A", "category": "FPS Weapons", "craft_seconds": 100,
         "minerals": ["Borase", "Gold"], "requirements": [
             {"slot": "Shell", "resource": "Borase", "scu": 0.42, "min_quality": 1},
             {"slot": "Core", "resource": "Gold", "scu": 0.5, "min_quality": 0}]},
        {"name": "Widget B", "category": "FPS Weapons", "craft_seconds": 60,
         "minerals": ["Gold"], "requirements": [
             {"slot": "Core", "resource": "Gold", "scu": 1.0, "min_quality": 2}]},
    ]
    blueprints.save_blueprints(bps, game_version="4.8", path=path)
    blueprints._cache["mtime"] = None

    agg = blueprints.aggregate_blueprints(
        [{"name": "Widget A", "qty": 2}, {"name": "Widget B", "qty": 1}, {"name": "ghost"}], path=path)

    gold = next(r for r in agg["requirements"] if r["resource"] == "Gold")
    assert gold["scu"] == 2.0 and gold["min_quality"] == 2          # 0.5×2 + 1.0×1, strictest Q
    assert gold["from"] == [{"name": "Widget A", "qty": 2}, {"name": "Widget B", "qty": 1}]
    borase = next(r for r in agg["requirements"] if r["resource"] == "Borase")
    assert borase["scu"] == 0.84 and borase["min_quality"] == 1     # 0.42×2
    assert [r["resource"] for r in agg["requirements"]] == ["Gold", "Borase"]   # heaviest first
    assert agg["minerals"] == ["Borase", "Gold"]
    assert agg["craft_seconds"] == 100 * 2 + 60                     # qty-weighted
    assert agg["total_scu"] == 2.84
    assert {"name": "ghost", "qty": 1, "found": False} in agg["items"]


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
    assert blueprints._armor_piece("ADP Arms Black") == "Arms"
    assert blueprints._armor_piece("A23 Flight Helmet Woodland") == "Helmet"
    assert blueprints._armor_piece("Untagged Thing") == ""
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



def test_blueprint_catalog_columns(tmp_path):
    # the catalog exposes one row per blueprint with the table's six columns; Class comes from
    # the description, Quality from the grade, Size from the AttachDef.
    root = str(tmp_path)
    _fixture(root)
    bps = scdata.build_blueprints(root, {"item_name_foo": "Foo Widget",
                                         "item_desc_foo": "Class: Military"})
    path = str(tmp_path / "blueprints.json")
    blueprints.save_blueprints(bps, path=path)
    blueprints._cache["mtime"] = None
    rows = blueprints.blueprint_catalog(path)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"name", "type", "subtype", "cls", "quality", "size"}
    assert row["name"] == "Foo Widget" and row["type"] == "FPS Weapons"
    assert row["cls"] == "Military" and row["quality"] == "A" and row["size"] == 2
    assert isinstance(row["subtype"], str)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
