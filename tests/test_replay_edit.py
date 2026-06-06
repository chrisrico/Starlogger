"""Ephemeral archive-editing overlay: build_snapshot(overlay=…) applies the edit set in
place of the on-disk stores and persists NOTHING, and apply_replay_op mirrors the live
edit endpoints (including origin sibling-propagation) on an in-memory overlay.

Disk-isolated: every store dependency is monkeypatched, and the only writer
(learn_station_names) is wired to fail if called — proving the ephemeral path never writes.

Run: python3 -m pytest tests/test_replay_edit.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import replay_edit, snapshot
from starlogger.model import Leg, Mission
from starlogger.replay_edit import apply_replay_op, seed_overlay
from starlogger.snapshot import build_snapshot
from starlogger.state import State

ZONES = {"Z1": "Port Olisar", "Z2": "Everus Harbor", "Z3": "Port Tressler"}


def _haul(mid, accepted, legs, status="active"):
    return Mission(mission_id=mid, contract="HaulCargo_AToB", accepted_at=accepted,
                   status=status, legs=legs)


def _state():
    st = State()
    st.zone_names = dict(ZONES)
    st.missions = {
        "m1": _haul("m1", "t1", {"m1p": Leg("m1p", "pickup", zone_host_id="Z1"),
                                 "m1d": Leg("m1d", "dropoff", cargo="Gold", qty=100, zone_host_id="Z2")}),
        "m2": _haul("m2", "t2", {"m2p": Leg("m2p", "pickup", zone_host_id="Z1"),
                                 "m2d": Leg("m2d", "dropoff", cargo="Tin", qty=30, zone_host_id="Z2")}),
        "m3": _haul("m3", "t3", {"m3d": Leg("m3d", "dropoff", cargo="Iron", qty=40, zone_host_id="Z3")}),
    }
    return st


def _no_disk(monkeypatch):
    """Stub every store read; make the only writer explode if the ephemeral path calls it."""
    def boom(*a, **k):
        raise AssertionError("ephemeral build_snapshot must not persist (learn_station_names)")
    for name, val in [("get_overrides", lambda: {"DISK": {"hidden": True}}),
                      ("get_settings", lambda: {"selected_ship": "DiskShip"}),
                      ("load_ship_cargo", lambda: {}), ("get_station_names", lambda: {}),
                      ("learn_station_names", boom), ("station_names", lambda: []),
                      ("commodity_names", lambda: []), ("lost_trade_ids", lambda: ["DISK"])]:
        monkeypatch.setattr(snapshot, name, val)


def test_overlay_applies_edits_and_never_persists(monkeypatch):
    _no_disk(monkeypatch)
    overlay = {
        "overrides": {"m1": {"hidden": True}, "m2": {"origin": "Custom Hub"}},
        "station_names": {"Z2": "Renamed Depot"},
        "lost": ["t|buy|x|shop"],
        "selected_ship": "Caterpillar",
    }
    d = build_snapshot(_state(), overlay=overlay)              # must not raise (no learn_*)
    by_id = {m["mission_id"]: m for m in d["missions"]}
    assert by_id["m1"]["hidden"] is True                       # overlay override applied
    assert by_id["m2"]["origin"] == "Custom Hub"               # overlay origin applied
    assert by_id["m1"]["destinations"] == ["Renamed Depot"]    # overlay station rename wins
    assert d["counts"]["hidden"] == 1 and d["counts"]["active"] == 2
    assert d["lost_trades"] == ["t|buy|x|shop"]                # overlay lost list, not disk
    assert d["selected_ship"] == "Caterpillar"                 # overlay ship, not disk


def test_overlay_none_reads_disk(monkeypatch):
    """Without an overlay the disk stores drive the snapshot (unchanged live behaviour)."""
    monkeypatch.setattr(snapshot, "learn_station_names", lambda z: None)  # allowed live
    for name, val in [("get_overrides", lambda: {}), ("get_settings", lambda: {}),
                      ("load_ship_cargo", lambda: {}), ("get_station_names", lambda: {}),
                      ("station_names", lambda: []), ("commodity_names", lambda: []),
                      ("lost_trade_ids", lambda: ["DISK"])]:
        monkeypatch.setattr(snapshot, name, val)
    d = build_snapshot(_state())
    assert d["lost_trades"] == ["DISK"]


def test_seed_overlay_copies_disk_state(monkeypatch):
    monkeypatch.setattr(replay_edit, "get_overrides", lambda: {"m1": {"hidden": True}})
    monkeypatch.setattr(replay_edit, "get_settings", lambda: {"selected_ship": "Hull C"})
    monkeypatch.setattr(replay_edit, "lost_trade_ids", lambda: ["t1"])
    ov = seed_overlay()
    assert ov == {"overrides": {"m1": {"hidden": True}}, "station_names": {},
                  "lost": ["t1"], "selected_ship": "Hull C"}
    ov["overrides"]["m1"]["hidden"] = False        # mutating the copy must not touch disk source
    assert seed_overlay()["overrides"]["m1"]["hidden"] is True


def test_op_override_propagates_origin_to_siblings(monkeypatch):
    monkeypatch.setattr(replay_edit, "get_station_names", lambda: {})
    st = _state()                                  # m1 & m2 share origin (Z1) and dest (Z2)
    overlay = {"overrides": {}, "station_names": {}, "lost": [], "selected_ship": None}
    apply_replay_op(overlay, {"kind": "override", "mission_id": "m1",
                              "override": {"origin": "Seraphim Station"}}, st)
    assert overlay["overrides"]["m1"]["origin"] == "Seraphim Station"
    assert overlay["overrides"]["m2"]["origin"] == "Seraphim Station"   # same-route sibling
    assert "m3" not in overlay["overrides"]        # different route -> untouched


def test_op_leg_state_and_field_and_station_and_ship():
    st = _state()
    ov = {"overrides": {}, "station_names": {}, "lost": [], "selected_ship": None}
    apply_replay_op(ov, {"kind": "leg_state", "legs": [{"mission_id": "m1", "oid": "m1d"}],
                         "done": True}, st)
    assert ov["overrides"]["m1"]["leg_states"] == {"m1d": "completed"}
    apply_replay_op(ov, {"kind": "leg_field", "mission_id": "m1", "oid": "m1d",
                         "field": "qty", "value": 7}, st)
    assert ov["overrides"]["m1"]["leg_fields"] == {"m1d": {"qty": 7}}
    apply_replay_op(ov, {"kind": "station_name", "zone": "Z9", "name": "New Stop"}, st)
    assert ov["station_names"] == {"Z9": "New Stop"}
    apply_replay_op(ov, {"kind": "trade_lost", "trade_id": "tX", "lost": True}, st)
    assert ov["lost"] == ["tX"]
    apply_replay_op(ov, {"kind": "select_ship", "ship": "MOLE"}, st)
    assert ov["selected_ship"] == "MOLE"
    apply_replay_op(ov, {"kind": "trade_lost", "trade_id": "tX", "lost": False}, st)
    assert ov["lost"] == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
