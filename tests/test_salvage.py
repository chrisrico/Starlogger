"""Salvage Ship-ID feature: the wreck-spawn log pattern, its session-scoped state, the
component-index/loadout resolution that powers the removable-component list, and the
snapshot's detected_salvage resolution. Locks the invariants from the feature design
(IsSalvagable filters removability; the size-2 'pullable' cap is OURS, weapons exempt;
spawn lines dedupe by entity id and reset per session).

Disk-isolated: build tests use synthetic DataCore records (no StarBreaker), and the
snapshot helper is called directly with an in-memory catalog (no real ships.json).

Run: .venv/bin/python -m pytest tests/test_salvage.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import patterns, scdata, snapshot
from starlogger.scdata._salvage_ships import _removable_components
from starlogger.scdata._ships import build_component_index
from starlogger.state import State
from scdata_helpers import write_record


# --------------------------------------------------------------------------- #
# 1. SALVAGE_SPAWN log pattern
# --------------------------------------------------------------------------- #
def test_salvage_spawn_pattern():
    line = ("<2026-06-03T20:59:39.684Z> [CItemResourceHost::AddHostedNode] Resource container "
            "component was already registered! Entity :QTNK_AEGS_Gladius_387873708460  -- "
            "Host  :AEGS_Gladius_Unmanned_Salvage_387873708417")
    m = patterns.SALVAGE_SPAWN.search(line)
    assert m and m.group("base") == "AEGS_Gladius" and m.group("eid") == "387873708417"

    # the variant suffix stays part of the base class (keys straight into salvage_ships.json)
    v = patterns.SALVAGE_SPAWN.search("a AddHostedNode b Host  :CRUS_Starlifter_C2_Unmanned_Salvage_42")
    assert v and v.group("base") == "CRUS_Starlifter_C2"


def test_salvage_spawn_ignores_non_salvage_hosts():
    # a normal component host (no _Unmanned_Salvage) must not match
    assert patterns.SALVAGE_SPAWN.search(
        "AddHostedNode Entity :QTNK_AEGS_Gladius_1 -- Host  :AEGS_Gladius_PowerPlant_2") is None


def test_salvage_spawn_not_quadratic():
    # bounded ({0,63}) so a hostile line can't backtrack quadratically (NOTES: log DoS)
    hostile = "AddHostedNode Host  :" + "A_" * 40000 + "x"
    t0 = time.perf_counter()
    assert patterns.SALVAGE_SPAWN.search(hostile) is None
    assert time.perf_counter() - t0 < 1.0


# --------------------------------------------------------------------------- #
# 2. State: session-scoped wreck sightings (dedupe by entity id, clear on reset)
# --------------------------------------------------------------------------- #
def _spawn(ts: str, host: str) -> str:
    return f"<{ts}> [CItemResourceHost::AddHostedNode] x -- Host  :{host}"


def test_salvage_targets_dedupe_by_entity_id():
    s = State()
    # one wreck logs several host lines (a tank per child) at the SAME id -> one count
    s.feed(_spawn("2026-01-01T00:00:01Z", "AEGS_Gladius_Unmanned_Salvage_111"))
    s.feed(_spawn("2026-01-01T00:00:01Z", "AEGS_Gladius_Unmanned_Salvage_111"))
    s.feed(_spawn("2026-01-01T00:00:05Z", "AEGS_Gladius_Unmanned_Salvage_222"))  # a 2nd wreck
    s.feed(_spawn("2026-01-01T00:00:07Z", "CRUS_Starlifter_C2_Unmanned_Salvage_333"))
    assert s.salvage_targets["AEGS_Gladius"].count == 2          # two distinct ids
    assert s.salvage_targets["CRUS_Starlifter_C2"].count == 1
    assert s.salvage_targets["AEGS_Gladius"].first_seen == "2026-01-01T00:00:01Z"
    assert s.salvage_targets["AEGS_Gladius"].last_seen == "2026-01-01T00:00:05Z"


def test_salvage_targets_cleared_on_reset():
    s = State()
    s.feed(_spawn("2026-01-01T00:00:01Z", "AEGS_Gladius_Unmanned_Salvage_1"))
    assert s.salvage_targets
    s.reset(full=True)
    assert s.salvage_targets == {}


# --------------------------------------------------------------------------- #
# 3. is_pullable rule (OURS, not the game's: weapons any size, others <= size 2)
# --------------------------------------------------------------------------- #
def test_is_pullable_rule():
    assert scdata.is_pullable("weapon", 12) is True        # weapons strip at any size
    assert scdata.is_pullable("turret", 8) is True
    assert scdata.is_pullable("missile_rack", 10) is True
    assert scdata.is_pullable("power_plant", 2) is True     # non-weapon <= 2 ok
    assert scdata.is_pullable("shield", 3) is False         # non-weapon > 2 locked
    assert scdata.is_pullable("radar", 4) is False
    assert scdata.is_pullable("cooler", None) is False      # unknown size -> not pullable


# --------------------------------------------------------------------------- #
# 4. build_component_index (weapons/turrets/radar + IsSalvagable + loc fallback)
#    and _removable_components (filter to salvagable, dedupe with counts, flag pullable)
# --------------------------------------------------------------------------- #
def _comp(root: str, rel: str, cls: str, ctype: str, size, grade, salv, loc_name=None):
    attach = {"Type": ctype, "Size": size, "Grade": grade}
    if loc_name:
        attach["Localization"] = {"Name": loc_name}
    write_record(
        os.path.join(root, "libs/foundry/records/entities/scitem", rel, cls.lower() + ".json"),
        "EntityClassDefinition." + cls,
        {"Components": [
            {"_Type_": "SAttachableComponentParams", "AttachDef": attach},
            {"_Type_": "SHealthComponentParams", "IsSalvagable": salv},
        ]})


def _fixture_components(root: str) -> None:
    _comp(root, "ships/powerplant", "POWR_Test", "PowerPlant", 1, 1, True)
    _comp(root, "ships/shieldgenerator", "SHLD_Big_S3", "Shield", 3, 2, True)
    _comp(root, "ships/radar", "RADR_Test_S2", "Radar", 2, 1, True)
    _comp(root, "weapons", "GUN_Test_S5", "WeaponGun", 5, 1, True, "@item_NameTestGun")
    _comp(root, "ships/cooler", "COOL_NotSalv", "Cooler", 1, 1, False)   # not salvagable
    _comp(root, "weapons", "AMMO_Box", "AmmoBox", 1, None, True)         # unmapped type


def test_build_component_index_covers_weapons_and_flags(tmp_path):
    root = str(tmp_path)
    _fixture_components(root)
    idx = build_component_index(root)

    assert idx["gun_test_s5"] == {"slot": "weapon", "size": 5, "grade": "A", "grade_num": 1,
                                  "salvagable": True, "loc_key": "@item_NameTestGun"}
    assert idx["radr_test_s2"]["slot"] == "radar" and idx["radr_test_s2"]["size"] == 2
    assert idx["shld_big_s3"]["slot"] == "shield"
    assert idx["cool_notsalv"]["salvagable"] is False
    assert "ammo_box" not in idx                                    # AmmoBox type is unmapped


_LOADOUT = (
    "EntityClassDefinition.TEST_Ship root\n"
    "    powr_test [hardpoint_power]\n"
    "    gun_test_s5 [hardpoint_weapon_l]\n"
    "    shld_big_s3 [hardpoint_shield]\n"
    "    cool_notsalv [hardpoint_cooler]\n"
    "    gun_test_s5 [hardpoint_weapon_r]\n"        # same gun again -> count 2
)


def test_removable_components_filters_and_flags(tmp_path):
    root = str(tmp_path)
    _fixture_components(root)
    idx = build_component_index(root)
    comps = _removable_components("TEST_Ship", _LOADOUT, idx, {"item_nametestgun": "Test Gun"})
    by_cat = {c["category"]: c for c in comps}

    assert set(by_cat) == {"power_plant", "weapon", "shield"}       # non-salvagable cooler dropped
    assert by_cat["weapon"]["count"] == 2                            # duplicate install counted
    assert by_cat["weapon"]["name"] == "Test Gun"                    # loc_key name fallback
    assert by_cat["weapon"]["pullable"] is True                      # weapon, size 5
    assert by_cat["shield"]["size"] == 3 and by_cat["shield"]["pullable"] is False  # >size2 greyed
    assert by_cat["power_plant"]["pullable"] is True


# --------------------------------------------------------------------------- #
# 5. snapshot._detected_salvage: resolve sightings against the catalog, newest first
# --------------------------------------------------------------------------- #
def test_detected_salvage_resolves_and_sorts():
    s = State()
    s.feed(_spawn("2026-01-01T00:00:01Z", "AEGS_Gladius_Unmanned_Salvage_1"))
    s.feed(_spawn("2026-01-01T00:00:09Z", "ANVL_Carrack_Unmanned_Salvage_2"))   # seen later
    db = {"aegs_gladius": {"name": "Gladius", "manufacturer": "Aegis",
                           "components": [{"category": "weapon", "name": "X", "size": 5,
                                           "pullable": True}]}}
    out = snapshot._detected_salvage(s, db)

    assert [d["ship_class"] for d in out] == ["ANVL_Carrack", "AEGS_Gladius"]   # newest last_seen first
    g = next(d for d in out if d["ship_class"] == "AEGS_Gladius")
    assert g["resolved"] is True and g["name"] == "Gladius" and g["count"] == 1
    assert g["components"][0]["name"] == "X"
    c = next(d for d in out if d["ship_class"] == "ANVL_Carrack")
    assert c["resolved"] is False and c["components"] == [] and c["name"] == "ANVL_Carrack"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
