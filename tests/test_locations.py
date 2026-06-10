"""Shared origin/destination/zone resolution (snapshot.py).

These pin the host-artifact / pending / override rules that the live snapshot and the
server's same-route sibling detection both depend on — previously two hand-kept copies.
Constructed straight from the Mission/Leg dataclasses so each rule is isolated.

Run: python3 -m pytest tests/test_locations.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger.model import Leg, Mission
from starlogger.snapshot import (PENDING_DEST, PENDING_ORIGIN, dest_signature,
                                  dleg_label, origin_label, resolve_zone)

ZONES = {"Z1": "Port Olisar", "Z2": "Everus Harbor", "Z3": "Port Tressler"}


def _mis(mid="m", origin_name=None, legs=None) -> Mission:
    return Mission(mission_id=mid, origin_name=origin_name, legs=legs or {})


def test_resolve_zone():
    assert resolve_zone(ZONES, "Z1") == "Port Olisar"
    assert resolve_zone(ZONES, "ZX") == "Unknown station (zone ZX)"   # known id, no name
    assert resolve_zone(ZONES, None) == "Unknown station"             # no zone at all


def test_origin_label_resolved_pickup():
    m = _mis(legs={"p": Leg("p", "pickup", zone_host_id="Z1"),
                   "d": Leg("d", "dropoff", zone_host_id="Z2")})
    assert origin_label(m, ZONES) == "Port Olisar"


def test_origin_label_pending_when_only_pickup_is_host_artifact():
    # pickup + dropoff share one zone (the acceptance host) -> not a real origin
    m = _mis(legs={"p": Leg("p", "pickup", zone_host_id="ZA"),
                   "d": Leg("d", "dropoff", zone_host_id="ZA")})
    assert m.has_pending_origin is True
    assert origin_label(m, ZONES) == PENDING_ORIGIN


def test_origin_label_override_wins_over_pending():
    m = _mis(origin_name="Custom Hub",
             legs={"p": Leg("p", "pickup", zone_host_id="ZA"),
                   "d": Leg("d", "dropoff", zone_host_id="ZA")})
    assert origin_label(m, ZONES) == "Custom Hub"


def test_origin_label_unknown_when_no_pickup():
    m = _mis(legs={"d": Leg("d", "dropoff", zone_host_id="Z2")})
    assert origin_label(m, ZONES) == "Unknown station"


def test_dleg_label_location_wins():
    m = _mis(legs={"p": Leg("p", "pickup", zone_host_id="ZA"),
                   "d": Leg("d", "dropoff", zone_host_id="ZA", location="Reclaimer Wreck")})
    assert dleg_label(m, m.legs["d"], ZONES) == "Reclaimer Wreck"


def test_dleg_label_host_artifact_is_pending():
    m = _mis(legs={"p": Leg("p", "pickup", zone_host_id="ZA"),
                   "d": Leg("d", "dropoff", zone_host_id="ZA")})
    assert dleg_label(m, m.legs["d"], ZONES) == PENDING_DEST


def test_dleg_label_resolves_real_zone():
    m = _mis(legs={"p": Leg("p", "pickup", zone_host_id="Z1"),
                   "d": Leg("d", "dropoff", zone_host_id="Z2")})
    assert dleg_label(m, m.legs["d"], ZONES) == "Everus Harbor"


def test_dleg_label_unknown_zone():
    m = _mis(legs={"d": Leg("d", "dropoff", zone_host_id="ZX")})
    assert dleg_label(m, m.legs["d"], ZONES) == "Unknown station (zone ZX)"


def test_dest_signature_sorted_and_deduped():
    m = _mis(legs={"p": Leg("p", "pickup", zone_host_id="Z1"),
                   "d2": Leg("d2", "dropoff", zone_host_id="Z2"),
                   "d3": Leg("d3", "dropoff", zone_host_id="Z3"),
                   "d2b": Leg("d2b", "dropoff", zone_host_id="Z2")})  # dup zone collapses
    assert dest_signature(m, ZONES) == ("Everus Harbor", "Port Tressler")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
