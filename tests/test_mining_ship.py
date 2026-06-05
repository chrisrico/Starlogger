"""is_mining_ship — the mining-vehicle detector that drives the dashboard's
mining-vs-hauling tab layout.

Run: .venv/bin/python -m pytest tests/test_mining_ship.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger.shipcargo import is_mining_ship


# A minimal cargo DB shaped like ships_cargo.json: only the `role`/`class` fields
# the detector reads. The MOLE carries a cargo grid (so it's in the catalog with a
# 'Medium Mining' role); the Prospector/ROC do not, so they're absent here.
_DB = {"ships": {
    "MOLE":       {"class": "ARGO_MOLE", "role": "Medium Mining"},
    "Reclaimer":  {"class": "AEGS_Reclaimer", "role": "Heavy Salvage"},
    "Vulture":    {"class": "DRAK_Vulture", "role": "Light Salvage"},
    "Golem OX":   {"class": "DRAK_Golem_OX", "role": "Light Freight"},
    "Freelancer": {"class": "MISC_Freelancer", "role": "Light Freight"},
}}


def test_mining_role_in_catalog():
    # The MOLE is classified by its catalog role, by display name or by entity class.
    assert is_mining_ship("MOLE", None, _DB)
    assert is_mining_ship(None, "ARGO_MOLE", _DB)


def test_surface_miners_absent_from_catalog():
    # Prospector / ROC carry no cargo grid → not in the catalog → matched by token.
    assert is_mining_ship("Prospector", "MISC_Prospector", _DB)
    assert is_mining_ship(None, "MISC_Prospector", _DB)
    assert is_mining_ship("Greycat ROC", "GRIN_ROC", _DB)
    assert is_mining_ship(None, "GRIN_ROC_DS", _DB)


def test_non_mining_ships():
    # Salvage roles must NOT count as mining, and "roc" can't bleed into "Reclaimer".
    assert not is_mining_ship("Reclaimer", "AEGS_Reclaimer", _DB)
    assert not is_mining_ship("Vulture", "DRAK_Vulture", _DB)
    assert not is_mining_ship("Golem OX", "DRAK_Golem_OX", _DB)
    assert not is_mining_ship("Freelancer", "MISC_Freelancer", _DB)
    assert not is_mining_ship(None, None, _DB)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
