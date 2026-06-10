"""Mining-equipment (head + module) extraction and the catalog cache.

Run: .venv/bin/python -m pytest tests/test_mining_gear.py

Fixtures are tiny synthetic DataCore records mirroring the real layout + field names
(verified against the live Data.p4k); see scdata._mining_gear.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import mining_gear, scdata
from scdata_helpers import write_record
from starlogger.scdata._p4k import load_localization


# --- fixture helpers -------------------------------------------------------- #
def _mod(value: float) -> dict:
    return {"_Type_": "FloatModifierMultiplicative", "showInUI": True, "value": value}


def _head_value(size: int, name_key: str, power, mods: dict, slots: int,
                throttle_min=0.1, secondary=None) -> dict:
    fire = [{"damagePerSecond": {"DamageEnergy": power},
             "fullDamageRange": 60.0, "zeroDamageRange": 180.0}]
    if secondary is not None:
        fire.append({"damagePerSecond": {"DamageEnergy": secondary}})
    ports = [{"_Type_": "SItemPortDef", "Name": "VEN", "RequiredPortTags": ""}]
    ports += [{"_Type_": "SItemPortDef", "Name": f"Mining_Modifier_{i}",
               "RequiredPortTags": "miningConsumable"} for i in range(slots)]
    return {"Components": [
        {"_Type_": "SAttachableComponentParams", "AttachDef": {
            "Size": size,
            "Localization": {"_Type_": "SCItemLocalization", "Name": name_key},
            "Manufacturer": "file://.../scitemmanufacturer.misc.json"}},
        {"_Type_": "SCItemWeaponComponentParams", "fireActions": fire},
        {"_Type_": "SItemPortContainerComponentParams", "Ports": ports},
        {"_Type_": "SEntityComponentMiningLaserParams",
         "throttleMinimum": throttle_min,
         "miningLaserModifiers": {"_Type_": "MiningLaserModifiers",
                                  **{k: _mod(v) for k, v in mods.items()}}},
    ]}


def _module_value(name_key: str, mfr: str, mods: dict, charges: int,
                  power_mult=None) -> dict:
    # Real modules carry several modifier entries: a weapon modifier whose damageMultiplier is
    # the beam-power delta (Rieger ×1.25), and a mining modifier holding the minigame deltas.
    # The rest are empty MiningLaserModifier structs the extractor must aggregate past.
    weapon = {"_Type_": "ItemWeaponModifiersParams",
              "MiningLaserModifier": {"_Type_": "MiningLaserModifiers"}}
    if power_mult is not None:
        weapon["weaponModifier"] = {"weaponStats": {"damageMultiplier": power_mult}}
    return {"Components": [
        {"_Type_": "SAttachableComponentParams", "AttachDef": {
            "Size": 1, "Manufacturer": f"file://./.../scitemmanufacturer/{mfr}.json",
            "Localization": {"_Type_": "SCItemLocalization", "Name": name_key}}},
        {"_Type_": "EntityComponentAttachableModifierParams", "charges": charges,
         "modifiers": [
             weapon,
             {"_Type_": "ItemMiningModifierParams", "MiningLaserModifier": {
                 "_Type_": "MiningLaserModifiers", **{k: _mod(v) for k, v in mods.items()}}}]},
    ]}


def _fixture_records(root: str) -> None:
    heads = os.path.join(root, "libs/foundry/records/entities/scitem/ships/weapons")
    modules = os.path.join(root, "libs/foundry/records/entities/scitem/ships/utility/mining/miningarm")

    # A real S1 ship head (Arbor: power 1890, +25% resistance, +40% window, -35% instab, 1 slot).
    write_record(os.path.join(heads, "mining_laser_grin_arbor_s1.json"),
           "EntityClassDefinition.Mining_Laser_GRIN_Arbor_S1",
           _head_value(1, "@item_arbor_s1", 1890.0,
                       {"resistanceModifier": 25.0, "optimalChargeWindowSizeModifier": 40.0,
                        "laserInstability": -35.0}, slots=1, secondary=1850.0))
    # A S2 head with 3 module slots.
    write_record(os.path.join(heads, "mining_laser_thcn_helix_s2.json"),
           "EntityClassDefinition.Mining_Laser_THCN_Helix_S2",
           _head_value(2, "@item_helix_s2", 4080.0,
                       {"resistanceModifier": -30.0, "optimalChargeWindowSizeModifier": -40.0},
                       slots=3))
    # Out of scope: size-0 handheld head (excluded).
    write_record(os.path.join(heads, "mining_laser_shin_klein_s0.json"),
           "EntityClassDefinition.Mining_Laser_SHIN_Klein_S0",
           _head_value(0, "@item_klein_s0", 0.8, {"laserInstability": 30.0}, slots=2))
    # Out of scope: the MPUV/ROC arm (size 1 but excluded by class name).
    write_record(os.path.join(heads, "mining_laser_mpuv_arm.json"),
           "EntityClassDefinition.Mining_Laser_MPUV_arm",
           _head_value(1, "@item_mpuv", 1850.0, {}, slots=1))
    # Out of scope: a test entity (excluded by the skip regex).
    write_record(os.path.join(heads, "mining_laser_grin_arbor_s1_test_active_1.json"),
           "EntityClassDefinition.Mining_Laser_GRIN_Arbor_S1_TEST_active_1",
           _head_value(1, "@item_test", 1890.0, {}, slots=1))

    # Modules: a tiered passive (Focus III), a yield-only passive (FLTR, no cracking mods),
    # an active consumable (Brandt), and a vehicle built-in that must be skipped.
    write_record(os.path.join(modules, "mining_modules_passive_focus_mk3.json"),
           "EntityClassDefinition.Mining_Modules_Passive_Focus_MK3", _module_value(
               "@item_focus_mk3", "thcn", {"optimalChargeWindowSizeModifier": 40.0}, charges=1))
    write_record(os.path.join(modules, "mining_modules_passive_fltr_mk1.json"),
           "EntityClassDefinition.Mining_Modules_Passive_FLTR_MK1",
           _module_value("@item_fltr_mk1", "grin", {}, charges=1))
    write_record(os.path.join(modules, "mining_modules_active_brandt.json"),
           "EntityClassDefinition.Mining_Modules_Active_Brandt", _module_value(
               "@item_brandt", "scitemmanufacturer.misc",
               {"resistanceModifier": 15.5, "shatterdamageModifier": -30.0}, charges=5,
               power_mult=1.35))
    # A power-booster passive (Rieger C3: ×1.25 beam power = +25%, -1% window).
    write_record(os.path.join(modules, "mining_modules_passive_rieger_mk3.json"),
           "EntityClassDefinition.Mining_Modules_Passive_Rieger_MK3", _module_value(
               "@item_rieger_mk3", "grin", {"optimalChargeWindowSizeModifier": -1.0},
               charges=1, power_mult=1.25))
    write_record(os.path.join(modules, "mining_modules_vehiclemod_rocds.json"),
           "EntityClassDefinition.Mining_Modules_VehicleMod_ROCDS",
           _module_value("@item_rocds", "thcn", {}, charges=0))

    # localisation: global.ini with the @keys above.
    loc_dir = os.path.join(root, "Data/Localization/english")
    os.makedirs(loc_dir, exist_ok=True)
    with open(os.path.join(loc_dir, "global.ini"), "w", encoding="utf-8") as f:
        f.write("item_arbor_s1=Arbor MH1 Mining Laser\n")
        f.write("item_helix_s2=Helix II Mining Laser\n")
        f.write("item_focus_mk3=Focus III Module\n")
        f.write("item_fltr_mk1=FLTR Module\n")
        f.write("item_brandt=Brandt Module\n")
        f.write("item_rieger_mk3=Rieger-C3 Module\n")
        f.write("item_rocds=ROC Module\n")


def _build(tmp_path):
    root = str(tmp_path / "records")
    _fixture_records(root)
    loc = load_localization(root)
    return scdata.build_mining_gear(root, loc)


# --- extraction tests ------------------------------------------------------- #
def test_only_ship_turret_heads_kept(tmp_path):
    gear = _build(tmp_path)
    classes = {h["class"] for h in gear["heads"]}
    assert classes == {"Mining_Laser_GRIN_Arbor_S1", "Mining_Laser_THCN_Helix_S2"}
    # handheld S0, MPUV arm, and the test entity are all excluded
    assert all("_S0" not in c and "MPUV" not in c and "TEST" not in c for c in classes)


def test_head_fields(tmp_path):
    gear = _build(tmp_path)
    arbor = next(h for h in gear["heads"] if h["class"] == "Mining_Laser_GRIN_Arbor_S1")
    assert arbor["name"] == "Arbor MH1 Mining Laser"
    assert arbor["manufacturer_code"] == "GRIN"
    assert arbor["manufacturer"] == "Greycat Industrial"
    assert arbor["size"] == 1
    assert arbor["power"] == 1890.0
    assert arbor["secondary_power"] == 1850.0
    assert arbor["optimal_range"] == [60.0, 180.0]
    assert arbor["module_slots"] == 1
    assert arbor["modifiers"] == {"resistance": 25.0, "window_size": 40.0, "instability": -35.0}
    helix = next(h for h in gear["heads"] if h["class"] == "Mining_Laser_THCN_Helix_S2")
    assert helix["module_slots"] == 3 and helix["power"] == 4080.0


def test_heads_sorted_by_size_then_power(tmp_path):
    gear = _build(tmp_path)
    keys = [(h["size"], h["power"]) for h in gear["heads"]]
    assert keys == sorted(keys)


def test_module_families_and_active_flag(tmp_path):
    gear = _build(tmp_path)
    by_name = {m["name"]: m for m in gear["modules"]}
    # the vehicle built-in (ROC) is skipped; the rest are kept (incl. the yield-only FLTR).
    assert set(by_name) == {"Focus III", "FLTR", "Brandt", "Rieger-C3"}
    # passive vs active comes from the class family, NOT charges (passive Focus has charges 1).
    assert by_name["Focus III"]["active"] is False and by_name["Focus III"]["charges"] == 1
    assert by_name["Focus III"]["tier"] == 3 and by_name["Focus III"]["manufacturer_code"] == "THCN"
    assert by_name["Focus III"]["modifiers"] == {"window_size": 40.0}
    # the beam-power delta (damageMultiplier) is captured as a `power` percent on both families.
    assert by_name["Rieger-C3"]["modifiers"] == {"power": 25.0, "window_size": -1.0}
    assert by_name["Brandt"]["active"] is True and by_name["Brandt"]["charges"] == 5
    assert by_name["Brandt"]["manufacturer_code"] == "MISC"      # from the ref, not the class
    assert by_name["Brandt"]["modifiers"] == {"power": 35.0, "resistance": 15.5, "shatter": -30.0}
    # the trailing " Module" is stripped; a modifier-less filter is still kept (slottable).
    assert by_name["FLTR"]["modifiers"] == {} and by_name["FLTR"]["tier"] == 1


def test_modules_sorted_passive_first(tmp_path):
    gear = _build(tmp_path)
    actives = [m["active"] for m in gear["modules"]]
    assert actives == sorted(actives)  # False (passive) before True (active)


# --- catalog cache round-trip ----------------------------------------------- #
def test_save_load_round_trip(tmp_path):
    gear = _build(tmp_path)
    path = str(tmp_path / "mining_gear.json")
    mining_gear.save_mining_gear(gear["heads"], gear["modules"],
                                 game_version="4.8.0", path=path)
    assert mining_gear.mining_gear_version(path) == "4.8.0"
    assert mining_gear.mining_gear_extract_version(path) == mining_gear.EXTRACT_VERSION
    assert len(mining_gear.heads(path)) == 2
    assert mining_gear.head_by_class("Mining_Laser_GRIN_Arbor_S1", path)["power"] == 1890.0
    assert mining_gear.module_by_class("Mining_Modules_Passive_Focus_MK3", path)["name"] == "Focus III"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
