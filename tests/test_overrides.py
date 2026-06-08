"""Manual-override application invariants.

The inline cell editor and the override drop/pickup forms post qty as user-supplied
JSON (the cell editor sends a raw *string*). Every Leg built from an override must
still satisfy the model contract ``qty: int | None`` so downstream SCU math
(planner/snapshot, ``int += qty``) never does ``int += str`` and 500s.

Run: python3 -m pytest tests/test_overrides.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger.model import Leg, Mission
from starlogger.overrides import _coerce_qty, apply_override


def test_coerce_qty_normalizes_to_int_or_none():
    assert _coerce_qty("32") == 32          # the cell editor's raw string
    assert _coerce_qty("  7 ") == 7         # whitespace-padded
    assert _coerce_qty(4) == 4              # already int -> unchanged
    assert _coerce_qty(4.0) == 4            # float -> int
    assert _coerce_qty(None) is None
    assert _coerce_qty("") is None          # blank clears the field
    assert _coerce_qty("abc") is None       # unparseable -> None, never raises
    assert _coerce_qty(True) is None        # bool is not a quantity


def _haul_with_dropoff():
    return Mission(mission_id="m1", contract="HaulCargo_AToB", accepted_at="t1",
                   status="active",
                   legs={"d": Leg("d", "dropoff", cargo="Gold", qty=None)})


def test_leg_field_qty_string_coerced_to_int():
    """A qty correction stored as a string (what the cell editor posts) lands as int."""
    m = apply_override(_haul_with_dropoff(), {"leg_fields": {"d": {"qty": "32"}}})
    leg = m.legs["d"]
    assert leg.qty == 32 and isinstance(leg.qty, int)


def test_override_drops_qty_string_coerced_to_int():
    """Override drop/pickup forms also coerce qty so rebuilt legs satisfy int|None."""
    m = apply_override(_haul_with_dropoff(),
                       {"drops": [{"cargo": "Tin", "qty": "16", "to": "Port Tressler"}]})
    (leg,) = m.legs.values()
    assert leg.qty == 16 and isinstance(leg.qty, int)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
