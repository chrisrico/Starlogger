"""Pure log-line patterns + decoders in starlogger/patterns.py.

This module is the parsing heartbeat (every Game.log line passes its regexes) yet
had no tests. These lock in the regex shapes and the hard-won decoder gotchas from
NOTES.md (multi-commodity contract decode, trade SCU = boxSize x unitAmount, the
word-order-insensitive ship resolution) so a future edit can't silently regress them.

Run: python -m pytest tests/test_patterns.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import patterns, reference, ships


# --------------------------------------------------------------------------- #
# Log-line regexes: one matching line + one non-matching line each.
# --------------------------------------------------------------------------- #

def test_ts():
    m = patterns.TS.match("<2026-06-01T16:20:49.543Z> [Notice] anything")
    assert m and m.group("ts") == "2026-06-01T16:20:49.543Z"
    assert patterns.TS.match("[Notice] no timestamp") is None


def test_accepted():
    line = ('Added notification "Contract Accepted: Local Delivery" [42] '
            'blah MissionId: [0a1b2c3d-4e5f-6789-abcd-ef0123456789]')
    m = patterns.ACCEPTED.search(line)
    assert m and m.group("title") == "Local Delivery"
    assert m.group("mid") == "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
    assert patterns.ACCEPTED.search('Added notification "Something Else" [1]') is None


def test_complete_failed_abandoned_notes():
    base = ' [9] x MissionId: [abc-123]'
    assert patterns.COMPLETE_NOTE.search('Added notification "Contract Complete: T"' + base).group("mid") == "abc-123"
    assert patterns.FAILED_NOTE.search('Added notification "Contract Failed: T"' + base).group("title") == "T"
    for word in ("Abandoned", "Cancelled", "Canceled"):
        line = f'Added notification "Contract {word}: T"' + base
        assert patterns.ABANDONED_NOTE.search(line).group("mid") == "abc-123"
    assert patterns.ABANDONED_NOTE.search('Added notification "Contract Complete: T"' + base) is None


def test_marker():
    line = ("Creating objective marker: missionId [0a1b-2c3d], generator name [gen_x], "
            "contract [Hauling_Foo], contractDefinitionId[cdef_1], objectiveId [obj_0], "
            "markerEntityId [12345], zoneHostId [67890], position [x: 1.5, y: -2.0, z: 3.25]")
    m = patterns.MARKER.search(line)
    assert m.group("mid") == "0a1b-2c3d"
    assert m.group("zone") == "67890"
    assert (m.group("x"), m.group("y"), m.group("z")) == ("1.5", "-2.0", "3.25")
    assert patterns.MARKER.search("Creating objective marker: missionId [x]") is None


def test_deliver_and_collect():
    dl = ('Added notification "New Objective: Deliver 0/77 SCU of Quartz to Seraphim Station: " '
          '[3] z MissionId: [ab-1], ObjectiveId: [o-1]')
    m = patterns.DELIVER.search(dl)
    assert (m.group("have"), m.group("need")) == ("0", "77")
    assert m.group("cargo") == "Quartz" and m.group("loc") == "Seraphim Station"
    assert m.group("mid") == "ab-1"

    cl = ('Added notification "New Objective: Collect 5/20 SCU of Processed Food from Port Tressler: " '
          '[3] z MissionId: [ab-2], ObjectiveId: [o-2]')
    m = patterns.COLLECT.search(cl)
    assert m.group("cargo") == "Processed Food" and m.group("loc") == "Port Tressler"
    assert patterns.DELIVER.search(cl) is None  # a Collect line is not a Deliver


def test_obj_upsert_and_mission_state():
    m = patterns.OBJ_UPSERT.search(
        "ObjectiveUpserted push message for: mission_id ab-1 - objective_id o-1 "
        "- state MISSION_OBJECTIVE_STATE_DONE")
    assert m.group("mid") == "ab-1" and m.group("state") == "DONE"

    me = patterns.MISSION_ENDED.search(
        "MissionEnded push message for: mission_id ab-1 - mission_state MISSION_STATE_COMPLETED")
    assert me.group("state") == "COMPLETED"

    em = patterns.END_MISSION.search(
        "Ending mission for player. MissionId[ab-1] Player[Foo] z "
        "CompletionType[Completed] Reason[Success]")
    assert em.group("ctype") == "Completed" and em.group("reason") == "Success"


def test_player_location():
    m = patterns.PLAYER_LOCATION.search(
        "<RequestLocationInventory> Player[Foo Bar] requested inventory for Location[Stanton2_Orison]")
    assert m.group("player") == "Foo Bar" and m.group("loc") == "Stanton2_Orison"
    assert patterns.PLAYER_LOCATION.search("requested inventory for nothing") is None


def test_award():
    assert patterns.AWARD.search('Added notification "Awarded 12345 aUEC').group("amt") == "12345"
    assert patterns.AWARD.search('Added notification "Awarded a medal') is None


def test_trade_buy_sell_capture_box_not_quantity():
    buy = ('<CEntityComponentCommodityUIProvider::SendCommodityBuyRequest> shopName[SCShop_x] '
           'kioskId[111] price[1067040.000000] resourceGUID[35121003-f1af-481a-b16f-7f48d8af0efb] '
           'quantity[28800.000000 cSCU] Cargo Box Data: boxSize[16.000000] | unitAmount[18]')
    m = patterns.TRADE_BUY.search(buy)
    assert m.group("box") == "16.000000" and m.group("units") == "18"
    # NOTES.md gotcha: SCU is boxSize x unitAmount, NOT the inconsistent quantity field.
    assert int(float(m.group("box")) * int(m.group("units"))) == 288
    assert "28800" not in (m.group("box") + m.group("units"))

    sell = ('<CEntityComponentCommodityUIProvider::SendCommoditySellRequest> shopName[SCShop_Admin] '
            'kioskId[222] amount[793520.000000] resourceGUID[9e65a7bd-adcf-4129-9ef5-26f4fe13f85b] '
            'Cargo Box Data:  [boxSize[16] | unitAmount[14]]')
    m = patterns.TRADE_SELL.search(sell)
    assert (m.group("box"), m.group("units")) == ("16", "14")


def test_kiosk_bind():
    m = patterns.KIOSK_BIND.search("CommodityKiosk_kiosk_cordys_2_a-015 [342890646017]")
    assert m.group("ent") == "kiosk_cordys_2_a-015" and m.group("kid") == "342890646017"


def test_session_and_shutdown():
    assert patterns.SESSION.search('x eCVS_InGame y gamerules="SC_Default"').group("gr") == "SC_Default"
    assert patterns.SESSION.search('x eCVS_InGame y gamerules="SC_Frontend"').group("gr") == "SC_Frontend"
    assert patterns.SHUTDOWN.search("CCIGBroker::FastShutdown") is not None
    assert patterns.SHUTDOWN.search("a normal line") is None


def test_quantum_travel():
    route = ("RSI_Hermes_123[456]|CSCItemNavigation::CalculateRoute|Projected Start Location "
             "is Stanton Gateway for route to destination pyro3 [QuantumTravel]")
    m = patterns.QT_ROUTE.search(route)
    assert m.group("ship") == "RSI_Hermes" and m.group("frm") == "Stanton Gateway" and m.group("to") == "pyro3"

    fuel = ("RSI_Hermes_123[456]|CSCItemNavigation::CalculateRoute|Successfully calculated "
            "route to pyro3 fuel estimate 1234.5")
    m = patterns.QT_FUEL.search(fuel)
    assert m.group("to") == "pyro3" and m.group("fuel") == "1234.5"

    assert patterns.QT_ARRIVED.search(
        "RSI_Hermes_123[456]|CSCItemNavigation::OnQuantumDriveArrived|done").group("ship") == "RSI_Hermes"


def test_version_and_changelist():
    assert patterns.VERSION.search("Branch: sc-alpha-4.8.0-hotfix").group(1) == "4.8.0"
    assert patterns.CHANGELIST.search("Changelist: 11875683").group(1) == "11875683"


def test_ship_channel_and_vehicle_ctrl():
    sc = patterns.SHIP_CHANNEL.search("joined channel 'Crusader C1 Spirit : Owner Name'")
    assert sc.group("ship") == "Crusader C1 Spirit" and sc.group("player") == "Owner Name"

    cj = patterns.CHANNEL_JOIN.search("You have joined channel 'Crusader C1 Spirit : Other'")
    assert cj.group("ship") == "Crusader C1 Spirit" and cj.group("owner") == "Other"
    assert patterns.CHANNEL_LEAVE.search("You have left the channel 'Crusader C1 Spirit : Other'").group("ship") == "Crusader C1 Spirit"
    assert patterns.CHANNEL_LEAVE.search("You have left channel 'Crusader C1 Spirit : Other'") is not None

    vc = patterns.VEHICLE_CTRL.search(
        "Vehicle Control Flow> CVehicleMovementBase::SetDriver: Local client node [9] x 'RSI_Hermes_456'")
    assert vc.group("act") == "SetDriver" and vc.group("ent") == "RSI_Hermes"


# --------------------------------------------------------------------------- #
# Pure decoders (no external deps).
# --------------------------------------------------------------------------- #

def test_camel_split():
    assert patterns.camel_split("PortOlisar") == "Port Olisar"
    assert patterns.camel_split("FPSWeapons") == "FPSWeapons"  # acronym run left intact
    assert patterns.camel_split("already spaced") == "already spaced"


def test_qt_system():
    assert patterns.qt_system("pyro3") == "Pyro"
    assert patterns.qt_system("stanton2") == "Stanton"
    assert patterns.qt_system("nyx1") == "Nyx"
    assert patterns.qt_system("terra_gateway") == "Terra"
    assert patterns.qt_system("pyro-stan_jp1") == "Jump Point"
    assert patterns.qt_system("RR_CRU_LEO") == "Stanton"  # cru -> home system
    assert patterns.qt_system("mysteryplace") == ""


def test_decode_qt_dest():
    assert patterns.decode_qt_dest("pyro3") == "Pyro III"
    assert patterns.decode_qt_dest("rs_ext_pyro5_l2") == "Pyro V L2"
    assert patterns.decode_qt_dest("pyro-stan_jp1") == "Pyro–Stanton Jump Point"
    assert patterns.decode_qt_dest("Rayari_Cluster_001_Frost_{abc-guid}.socpak") == "Rayari Cluster 001 Frost"


def test_classify_end():
    assert patterns.classify_end("Abandoned") == "abandoned"
    assert patterns.classify_end("Cancelled") == "abandoned"
    assert patterns.classify_end("Expired") == "expired"
    assert patterns.classify_end("Failed") == "failed"
    assert patterns.classify_end("Completed") == "completed"
    assert patterns.classify_end("Success") == "completed"
    assert patterns.classify_end("") == "completed"                 # default
    assert patterns.classify_end("weird", default="x") == "x"       # unknown -> caller default


def test_classify_contract():
    assert patterns.classify_contract(is_trade=True) == "Hauling"
    assert patterns.classify_contract(title="Bounty: eliminate target") == "Bounty / Combat"
    assert patterns.classify_contract(title="Courier delivery run") == "Delivery"
    assert patterns.classify_contract(org="Random Org", title="Mystery") == "Other"
    # hauling (is_trade) wins even over combat words
    assert patterns.classify_contract(title="bounty", is_trade=True) == "Hauling"


def test_decode_contract():
    out = patterns.decode_contract("Hauling_AToB_RefinedOre_MediumGrade2_Stanton")
    assert out == {"structure": "A → B", "category": "Refined Ore", "grade": "Medium Grade 2"}
    out = patterns.decode_contract("SingleToMulti3_RawOre_LargeGrade")
    assert out["structure"] == "1 → 3 drops" and out["grade"] == "Large Grade"
    assert patterns.decode_contract("nothing recognizable") == {
        "structure": None, "category": None, "grade": None}


def test_decode_cargo_from_contract():
    # NOTES.md gotcha: a compound multi-commodity token splits into one entry per atom.
    multi = patterns.decode_cargo_from_contract("Haul_Processed_Mixed_PressIceProcFood_Stanton_x")
    assert multi == ["Pressurized Ice", "Processed Food"]
    single = patterns.decode_cargo_from_contract("Haul_RefinedOre_Titanium_Stanton_x")
    assert single == ["Titanium"]
    assert patterns.decode_cargo_from_contract("no cargo token here") == []


def test_major_version_and_clean_title():
    assert patterns.major_version("4.8.0") == "4.8"
    assert patterns.major_version("") == ""
    assert patterns.major_version("garbage") == "garbage"
    assert patterns.clean_title("<EM4>Haul Run</EM4> [BP]*") == "Haul Run"
    assert patterns.clean_title("  Spaced   Title : ") == "Spaced Title"


def test_friendly_shop_and_kiosk():
    assert patterns.friendly_shop("SCShop_ht_delta_shubin_m_store") == "Shubin"
    assert patterns.friendly_kiosk("kiosk_cordys_2_a-015") == "Cordys"
    assert patterns.friendly_shop("") == "Trade terminal"          # nothing meaningful -> fallback


# --------------------------------------------------------------------------- #
# Decoders that lazily consult the cargo DB / station catalog (monkeypatched).
# --------------------------------------------------------------------------- #

def test_friendly_ship(monkeypatch):
    monkeypatch.setattr(ships, "ship_display_name", lambda e: "")  # force the fallback path
    assert patterns.friendly_ship("MISC_Freelancer") == "MISC Freelancer"
    monkeypatch.setattr(ships, "ship_display_name", lambda e: "Freelancer MAX")
    assert patterns.friendly_ship("MISC_Freelancer_Max") == "Freelancer MAX"


def test_canonical_ship_name(monkeypatch):
    monkeypatch.setattr(ships, "known_ship_names",
                        lambda: {"Mercury Star Runner", "Freelancer MAX", "Nomad"})
    assert patterns.canonical_ship_name("Crusader Mercury Star Runner") == "Mercury Star Runner"
    assert patterns.canonical_ship_name("Consolidated Outland Nomad") == "Nomad"  # two-word mfr
    assert patterns.canonical_ship_name("Freelancer MAX") == "Freelancer MAX"     # already canonical
    assert patterns.canonical_ship_name("Totally Unknown") == "Totally Unknown"   # unchanged


def test_resolve_ship_name(monkeypatch):
    monkeypatch.setattr(ships, "known_ship_names", lambda: {"C1 Spirit", "Freelancer MAX"})
    assert patterns.resolve_ship_name("Crusader C1 Spirit") == "C1 Spirit"
    # word-order-insensitive: "Spirit C1" resolves back to "C1 Spirit"
    assert patterns.resolve_ship_name("Crusader Spirit C1") == "C1 Spirit"
    # no manufacturer prefix -> not a ship channel
    assert patterns.resolve_ship_name("Party Chat") is None
    # real manufacturer-prefixed ship the DB lacks -> falls back to the stripped model
    assert patterns.resolve_ship_name("Crusader Unknownship") == "Unknownship"


def test_decode_location(monkeypatch):
    # catalogued code -> authoritative station name
    monkeypatch.setattr(reference, "resolve_code", lambda c: "ARC-L1 Wide Forest Station"
                        if c == "RR_ARC_L1" else None)
    assert patterns.decode_location("RR_ARC_L1") == ("ARC-L1 Wide Forest Station", True)
    # structural heuristic for a precise "<System><n>_<Place>" code
    assert patterns.decode_location("Stanton2_Orison") == ("Orison", True)
    # vaguer orbital code -> body only, not a station
    assert patterns.decode_location("RR_CRU_LEO") == ("Crusader", False)
    assert patterns.decode_location("Garbage") == (None, False)


def test_qt_patterns_are_not_quadratic():
    """The QT ship token is bounded so a long hostile line can't backtrack quadratically.
    A ~64 KB line that lures the pattern (contains CSCItemNavigation) but never matches must
    return fast. Pre-fix this took tens of seconds; locks the audit fix (regex DoS)."""
    line = "x_" * 32000 + "CSCItemNavigation::CalculateRoute|nope"
    for rx in (patterns.QT_ROUTE, patterns.QT_FUEL, patterns.QT_ARRIVED):
        t0 = time.perf_counter()
        assert rx.search(line) is None
        assert time.perf_counter() - t0 < 1.0, f"{rx.pattern[:30]} too slow"
