"""Ephemeral edit overlay for archive/replay editing.

A replayed past session is editable exactly like the live dashboard, but every edit is
applied to an in-memory *overlay* — ``{overrides, station_names, lost, selected_ship}`` —
that :func:`starlogger.snapshot.build_snapshot` consumes instead of the on-disk stores,
so nothing is ever persisted. The overlay starts as a sandbox copy of the live edit state
(:func:`seed_overlay`) and each UI edit is one op applied by :func:`apply_replay_op`, which
reuses the very same merge logic as the live POST endpoints (no behavioural drift).

Kept flask-free (and out of ``server.py``) so the edit logic is unit-testable on its own.
"""

from __future__ import annotations

import copy

from .overrides import (apply_leg_field, apply_leg_states, apply_override,
                        apply_override_edit, get_overrides)
from .settings import get_settings
from .snapshot import dest_signature, origin_label
from .stations import get_station_names
from .tradeflags import lost_trade_ids


def apply_override_with_siblings(data: dict, state, mid: str, override: dict | None) -> dict:
    """Set mission ``mid``'s override in ``data`` (in place), then propagate an origin
    correction to same-route siblings — active missions whose *displayed* (origin,
    destinations) match the edited mission's under the overrides currently in effect.
    Origin alone is too coarse (many missions share an "Unknown station" origin while
    running different routes). Shared by the live /api/override writer and the overlay."""
    origin = (override or {}).get("origin")
    origin = origin.strip() if isinstance(origin, str) else None
    before: dict = {}
    if origin:
        with state.lock:
            zone_names = {**get_station_names(), **state.zone_names}
            for oid, m in state.missions.items():
                if m.status != "active":
                    continue
                ov = data.get(oid)
                eff = apply_override(m, ov) if ov else m
                before[oid] = (origin_label(eff, zone_names), dest_signature(eff, zone_names))
    apply_override_edit(data, mid, override)
    key = before.get(mid)
    if origin and key:
        for oid, sib_key in before.items():
            if oid == mid or sib_key != key:
                continue
            sib = dict(data.get(oid) or {})
            sib["origin"] = origin
            apply_override_edit(data, oid, sib)
    return data


def seed_overlay() -> dict:
    """A fresh ephemeral edit overlay initialised from the current on-disk stores, so an
    archive-replay session starts as an exact sandbox copy of the live edit state."""
    return {
        "overrides": copy.deepcopy(get_overrides()),
        "station_names": {},
        "lost": list(lost_trade_ids()),
        "selected_ship": get_settings().get("selected_ship"),
    }


def apply_replay_op(overlay: dict, op: dict, state) -> dict:
    """Apply one edit op to an ephemeral overlay (mutates and returns it), mirroring the
    live POST endpoints exactly but never touching disk. ``state`` is the reconstructed
    State at the current checkpoint, used for origin sibling-matching."""
    kind = op.get("kind")
    ov = overlay["overrides"]
    if kind == "override":
        apply_override_with_siblings(ov, state, op["mission_id"], op.get("override"))
    elif kind == "leg_state":
        apply_leg_states(ov, op["legs"], bool(op.get("done", True)))
    elif kind == "leg_field":
        apply_leg_field(ov, op["mission_id"], op["oid"], op["field"], op.get("value"))
    elif kind == "station_name":
        name = (op.get("name") or "").strip() or None
        if name:
            overlay["station_names"][str(op["zone"])] = name
        else:
            overlay["station_names"].pop(str(op["zone"]), None)
    elif kind == "trade_lost":
        tid, lost = op["trade_id"], bool(op.get("lost", True))
        cur = set(overlay["lost"])
        cur.add(tid) if lost else cur.discard(tid)
        overlay["lost"] = sorted(cur)
    elif kind == "select_ship":
        overlay["selected_ship"] = (op.get("ship") or "").strip() or None
    else:
        raise ValueError(f"unknown edit op: {kind!r}")
    return overlay
