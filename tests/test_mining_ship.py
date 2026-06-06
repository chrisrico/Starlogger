"""is_mining_ship — the mining-vehicle detector that drives the dashboard's
mining-vs-hauling tab layout.

Run: .venv/bin/python -m pytest tests/test_mining_ship.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger.ships import is_mining_ship


# A minimal cargo DB shaped like ships.json: only the `mining`/`role`/`class`
# fields the detector reads. Every mining vehicle is catalogued now -- the MOLE carries
# a cargo grid; the Prospector / ROC / ROC-DS / ATLS GEO don't, but scdata.build_ships
# still lists them (scu 0, empty grid) with an explicit `mining` flag.
_DB = {"ships": {
    "MOLE":       {"class": "ARGO_MOLE", "role": "Medium Mining", "mining": True},
    "Prospector": {"class": "MISC_Prospector", "role": "Light Mining", "mining": True},
    "ROC":        {"class": "GRIN_ROC", "role": "Light Mining", "mining": True},
    "ROC-DS":     {"class": "GRIN_ROC_DS", "role": "Light Mining", "mining": True},
    "ATLS GEO":   {"class": "ARGO_ATLS_GEO", "role": "Mining", "mining": True},
    "Reclaimer":  {"class": "AEGS_Reclaimer", "role": "Heavy Salvage"},
    "Vulture":    {"class": "DRAK_Vulture", "role": "Light Salvage"},
    "Golem OX":   {"class": "DRAK_Golem_OX", "role": "Light Freight"},
    "Freelancer": {"class": "MISC_Freelancer", "role": "Light Freight"},
}}


def test_mining_role_in_catalog():
    # The MOLE is classified by its catalog role, by display name or by entity class.
    assert is_mining_ship("MOLE", None, _DB)
    assert is_mining_ship(None, "ARGO_MOLE", _DB)


def test_grid_less_miners_flagged():
    # Prospector / ROC / ATLS GEO carry no cargo grid but are catalogued with the
    # `mining` flag, matched by display name or by entity class.
    assert is_mining_ship("Prospector", "MISC_Prospector", _DB)
    assert is_mining_ship(None, "MISC_Prospector", _DB)
    assert is_mining_ship("Greycat ROC", "GRIN_ROC", _DB)
    assert is_mining_ship(None, "GRIN_ROC_DS", _DB)
    assert is_mining_ship("ATLS GEO", "ARGO_ATLS_GEO", _DB)


def test_flag_drives_detection_independent_of_role():
    # The explicit flag is authoritative even if the role text doesn't say "mining".
    db = {"ships": {"Weird": {"class": "X_Weird", "role": "Industrial", "mining": True}}}
    assert is_mining_ship("Weird", "X_Weird", db)


def test_non_mining_ships():
    # Salvage / freight roles must NOT count as mining.
    assert not is_mining_ship("Reclaimer", "AEGS_Reclaimer", _DB)
    assert not is_mining_ship("Vulture", "DRAK_Vulture", _DB)
    assert not is_mining_ship("Golem OX", "DRAK_Golem_OX", _DB)
    assert not is_mining_ship("Freelancer", "MISC_Freelancer", _DB)
    assert not is_mining_ship(None, None, _DB)


def test_unknown_vehicle_not_mining():
    # With the token fallback gone, a vehicle absent from the catalog is never mining
    # (no more name/class token guessing).
    assert not is_mining_ship("Prospector", "MISC_Prospector", {"ships": {}})


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
