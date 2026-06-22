"""Mining contracts end-to-end: parse the Shubin purchase-order objectives, join each ore to
its where-to-mine locations (method-aware: hand gems vs ship ore), and serialize that onto the
mission snapshot the Contracts section renders.

Disk-isolated via conftest (STARLOGGER_DATA_DIR -> temp); seeds tiny body/space catalogs so the
location join has data.

Run: python3 -m pytest tests/test_mining_contracts.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import body_mineables, config, space_mineables
from starlogger.mine_locations import mine_locations
from starlogger.snapshot import build_snapshot
from starlogger.state import State


def _seed():
    body_mineables.save_body_mineables([
        {"name": "Hurston", "system": "Stanton", "ship_mineables": ["Copper"],
         "hand_mineables": ["Aphorite"], "ground_mineables": [],
         "harvestables": [], "creatures": [], "description": ""},
        {"name": "Daymar", "system": "Stanton", "ship_mineables": ["Copper"],
         "hand_mineables": ["Aphorite"], "ground_mineables": [],
         "harvestables": [], "creatures": [], "description": ""},
    ], path=config.BODY_MINEABLES_PATH)
    body_mineables._cache["mtime"] = None
    space_mineables.save_space_mineables([
        {"name": "Aaron Halo", "system": "Stanton",
         "ship_mineables": [{"mineral": "Copper", "rarity": "Common"}]},
    ], path=config.SPACE_MINEABLES_PATH)
    space_mineables._cache["mtime"] = None


def _notif(text, mid):
    return (f'<2026-06-06T00:00:00.000Z> [Notice] <SHUDEvent_OnNotification> Added notification '
            f'"{text}: " [1] to queue. New queue size: 1, MissionId: [{mid}], ObjectiveId: [] [x]\n')


def _feed(st, mid, title, *objectives):
    st.feed(_notif(f"Contract Accepted:  {title}", mid))
    for o in objectives:
        st.feed(_notif(o, mid))


def _mission(snap, mid):
    return next((m for m in snap["missions"] if m["mission_id"] == mid), None)


# --- the where-to-mine join (method-aware) --------------------------------- #
def test_mine_locations_method_aware():
    _seed()
    # hand gem: surface bodies only, no asteroid fields
    hand = mine_locations("Aphorite", "hand")
    assert {l["place"] for l in hand} == {"Hurston", "Daymar"}
    assert all(l["kind"] == "body" for l in hand)
    assert mine_locations("Aphorite", "ship") == []          # not ship-mineable
    # ship ore: bodies + asteroid fields (field carries a rarity)
    ship = mine_locations("Copper", "ship")
    assert {l["kind"] for l in ship} == {"body", "field"}
    field = next(l for l in ship if l["kind"] == "field")
    assert field["place"] == "Aaron Halo" and field["rarity"] == "Common"


# --- snapshot serialization the Contracts section consumes ------------------ #
def test_hand_mining_contract_snapshot():
    _seed()
    st = State()
    _feed(st, "ab-1", "Small Purchase Order: Hand Mined Materials",
          "New Objective: Go to HDMS-Perlman",
          "New Objective: 0/15 of Aphorite",
          "New Objective: Collect and deliver one of the following:")
    md = _mission(build_snapshot(st), "ab-1")
    assert md is not None                                    # mining missions reach the live list
    assert md["mining_method"] == "hand" and md["ore_any"] is True
    assert md["mining_goto"] == "HDMS-Perlman"
    [ore] = md["ores"]
    assert ore["ore"] == "Aphorite" and ore["need"] == 15
    assert {l["place"] for l in ore["locations"]} == {"Hurston", "Daymar"}


def test_ship_mining_contract_has_field_locations():
    _seed()
    st = State()
    _feed(st, "cd-2", "XS Purchase Order: Ship Mined Ore", "New Objective: 0/2 of Copper")
    md = _mission(build_snapshot(st), "cd-2")
    assert md["mining_method"] == "ship"
    [ore] = md["ores"]
    assert {l["kind"] for l in ore["locations"]} == {"body", "field"}


def test_location_chips_capped_with_count():
    # a gem on many bodies -> the chip list is capped but loc_count reports the true total
    bodies = [{"name": f"Moon{i}", "system": "Stanton", "ship_mineables": [],
               "hand_mineables": ["Aphorite"], "ground_mineables": [],
               "harvestables": [], "creatures": [], "description": ""} for i in range(20)]
    body_mineables.save_body_mineables(bodies, path=config.BODY_MINEABLES_PATH)
    body_mineables._cache["mtime"] = None
    st = State()
    _feed(st, "ef-3", "Small Purchase Order: Hand Mined Materials",
          "New Objective: 0/15 of Aphorite")
    [ore] = _mission(build_snapshot(st), "ef-3")["ores"]
    assert ore["loc_count"] == 20 and len(ore["locations"]) == 8   # capped to 8, count is full
