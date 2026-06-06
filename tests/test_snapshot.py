"""Golden coverage for build_snapshot's assembled output — counts, committed SCU, the
per-mission dicts, and the autocomplete catalog. Pins behavior before build_snapshot is
decomposed into helpers, so the refactor can't silently change the dashboard payload.

Disk-isolated: every catalog/override/settings dependency is monkeypatched, and missions
are constructed directly (not parsed from a log), so this never touches real user data.

Run: python3 -m pytest tests/test_snapshot.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import snapshot
from starlogger.model import Leg, Mission
from starlogger.snapshot import PENDING_DEST, PENDING_ORIGIN, build_snapshot
from starlogger.state import State

ZONES = {"Z1": "Port Olisar", "Z2": "Everus Harbor", "Z3": "Port Tressler"}


def _haul(mid, accepted, legs, status="active"):
    return Mission(mission_id=mid, contract="HaulCargo_AToB", accepted_at=accepted,
                   status=status, legs=legs)


def _fixture(monkeypatch):
    overrides = {"m3": {"origin": "Custom Hub"}, "m5": {"hidden": True}}
    for name, val in [("get_overrides", lambda: overrides), ("get_settings", lambda: {}),
                      ("load_ship_cargo", lambda: {}), ("get_station_names", lambda: {}),
                      ("learn_station_names", lambda z: None), ("station_names", lambda: []),
                      ("commodity_names", lambda: []), ("lost_trade_ids", lambda: [])]:
        monkeypatch.setattr(snapshot, name, val)

    st = State()
    st.zone_names = dict(ZONES)
    st.missions = {
        "m1": _haul("m1", "t1", {"m1p": Leg("m1p", "pickup", zone_host_id="Z1"),
                                 "m1d": Leg("m1d", "dropoff", cargo="Gold", qty=100, zone_host_id="Z2")}),
        # host-artifact (pickup+dropoff share the acceptance zone) AND partial (no cargo)
        "m2": _haul("m2", "t2", {"m2p": Leg("m2p", "pickup", zone_host_id="ZA"),
                                 "m2d": Leg("m2d", "dropoff", qty=50, zone_host_id="ZA")}),
        "m3": _haul("m3", "t3", {"m3p": Leg("m3p", "pickup", zone_host_id="Z1"),
                                 "m3d": Leg("m3d", "dropoff", cargo="Tin", qty=30, zone_host_id="Z3")}),
        "m4": _haul("m4", "t4", {"m4d": Leg("m4d", "dropoff", cargo="Gold", qty=80, zone_host_id="Z2")},
                    status="completed"),
        "m5": _haul("m5", "t5", {"m5d": Leg("m5d", "dropoff", cargo="Gold", qty=20, zone_host_id="Z2")}),
    }
    return st


def test_counts(monkeypatch):
    d = build_snapshot(_fixture(monkeypatch))
    assert d["counts"] == {"active": 3, "partial": 1, "completed": 1,
                           "abandoned": 0, "failed": 0, "hidden": 1, "total": 4}


def test_committed_and_peak_scu(monkeypatch):
    d = build_snapshot(_fixture(monkeypatch))
    assert d["active_scu"] == 180          # 100 + 50 + 30 (committed dropoff qty, active only)
    assert d["peak_scu"] >= 100            # at least the largest single leg must fit at once


def test_mission_dicts_origin_and_destinations(monkeypatch):
    d = build_snapshot(_fixture(monkeypatch))
    by_id = {m["mission_id"]: m for m in d["missions"]}
    assert (by_id["m1"]["origin"], by_id["m1"]["destinations"]) == ("Port Olisar", ["Everus Harbor"])
    assert (by_id["m2"]["origin"], by_id["m2"]["destinations"]) == (PENDING_ORIGIN, [PENDING_DEST])
    assert (by_id["m3"]["origin"], by_id["m3"]["destinations"]) == ("Custom Hub", ["Port Tressler"])
    assert by_id["m2"]["partial"] is True
    assert by_id["m5"]["hidden"] is True and by_id["m3"]["overridden"] is True


def test_autocomplete_catalog(monkeypatch):
    d = build_snapshot(_fixture(monkeypatch))
    cat = d["catalog"]
    assert {"Port Olisar", "Everus Harbor", "Port Tressler"} <= set(cat["stations"])
    assert {"Gold", "Tin"} <= set(cat["cargo"])
    assert cat["stations"] == sorted(cat["stations"])   # serialized sorted


def test_boarded_ship_drives_grid_and_capacity(monkeypatch):
    """Crewing another player's ship: effective_ship (and so capacity + grid) follow the
    boarded ship while aboard, then revert to your own when boarded clears."""
    db = {"ships": {
        "Ironclad": {"scu": 2200, "class": "DRAK_Ironclad", "layout": "deck",
                     "groups": [{"x": 0, "z": 0,
                                 "grids": [{"width": 6, "length": 20, "height": 6, "x": 0, "y": 0, "z": 0}]}]},
        "Freelancer MAX": {"scu": 120, "class": "MISC_Freelancer_MAX", "layout": "synth", "groups": []},
    }}
    for name, val in [("get_overrides", lambda: {}), ("get_settings", lambda: {}),
                      ("load_ship_cargo", lambda: db), ("get_station_names", lambda: {}),
                      ("learn_station_names", lambda z: None), ("station_names", lambda: []),
                      ("commodity_names", lambda: []), ("lost_trade_ids", lambda: [])]:
        monkeypatch.setattr(snapshot, name, val)

    st = State()
    st.ship = "Freelancer MAX"          # your own piloted ship
    st.boarded_ship = "Ironclad"        # but you're crewing a friend's Ironclad
    st.boarded_owner = "caged-danimal"
    d = build_snapshot(st)
    assert d["ship"] == "Ironclad" and d["ship_scu"] == 2200
    assert d["boarded"] is True and d["boarded_owner"] == "caged-danimal"
    assert d["ship_grid"][0]["grids"][0]["width"] == 6      # the Ironclad's hold

    st.boarded_ship = st.boarded_owner = None               # disembarked
    d2 = build_snapshot(st)
    assert d2["ship"] == "Freelancer MAX" and d2["ship_scu"] == 120 and d2["boarded"] is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
