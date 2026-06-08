"""Manual mission overrides: fill in details the game failed to log, edit fields,
or hide (delete) a mission. Stored in overrides.json keyed by mission_id.

Schema per mission:
  { "title": "...", "status": "completed", "origin": "Everus Harbor",
    "reward": 37250, "hidden": true,
    "drops":   [ {"cargo":"Stims","qty":4,"to":"HUR-L4 Melodic Fields Station"} ],
    "pickups": [ {"cargo":"Waste","qty":null,"from":"HUR-L1 Green Glade Station"} ] }
The file is re-read automatically whenever it changes (no restart needed).
"""

from __future__ import annotations

import copy

from .config import OVERRIDES_PATH
from .jsonstore import atomic_write, load_cached, read_json
from .model import Leg, Mission

_cache = {"mtime": None, "data": {}}


def get_overrides(path: str = OVERRIDES_PATH) -> dict:
    return load_cached(path, _cache)


def apply_override_edit(data: dict, mission_id: str, override: dict | None) -> dict:
    """Set (or, if override is falsy, remove) one mission's override entry in `data`,
    in place. The pure core shared by the disk writer and the ephemeral replay overlay."""
    if override:
        data[mission_id] = override
    else:
        data.pop(mission_id, None)
    return data


def write_override(mission_id: str, override: dict | None, path: str = OVERRIDES_PATH) -> None:
    """Set (or, if override is falsy, remove) one mission's override entry."""
    atomic_write(path, apply_override_edit(read_json(path, dict), mission_id, override))


def prune_overrides(keep_mission_ids: set, path: str = OVERRIDES_PATH,
                    dry_run: bool = False) -> dict:
    """Drop override entries whose mission_id isn't in `keep_mission_ids` -- i.e.
    missions no longer present in the current log. Their corrected data was
    already frozen into the session archive when the session ended, and the
    override (keyed by a now-gone mission_id) can never apply again. A
    crash-relaunch re-writes the same ids into the new log, so those stay in
    `keep_mission_ids` and are preserved. Returns {removed: {id: title}, kept}."""
    data = read_json(path)
    if not isinstance(data, dict):
        return {"removed": {}, "kept": 0}
    removed = {mid: (ov.get("title") or ov.get("_note") or "")
               for mid, ov in data.items() if mid not in keep_mission_ids}
    if removed and not dry_run:
        kept = {mid: ov for mid, ov in data.items() if mid not in removed}
        atomic_write(path, kept)
        _cache["mtime"] = None  # force reload on next read
    return {"removed": removed, "kept": len(data) - len(removed)}


def _coerce_qty(v):
    """Normalize a JSON/user-supplied qty to the ``Leg.qty`` contract (int | None).
    The inline cell editor posts qty as a raw string (e.g. "32"); coerce it here so
    every Leg built from an override satisfies int|None and downstream SCU math
    (planner/snapshot, ``int += qty``) never blows up on a string. Unparseable -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _legs(items: list[dict], kind: str, loc_key: str) -> dict[str, Leg]:
    out: dict[str, Leg] = {}
    for i, it in enumerate(items):
        oid = f"ovr_{kind}_{i}"
        out[oid] = Leg(
            objective_id=oid, kind=kind, cargo=it.get("cargo"), qty=_coerce_qty(it.get("qty")),
            location=it.get(loc_key), state="completed" if it.get("done") else "pending",
        )
    return out


def apply_override(mis: Mission, ov: dict) -> Mission:
    """Return a copy of `mis` with the override applied (non-destructive)."""
    m = copy.deepcopy(mis)
    if ov.get("title"):
        m.title = ov["title"]
    if ov.get("status"):
        m.status = ov["status"]
    if ov.get("origin"):
        m.origin_name = ov["origin"]
    if ov.get("reward") is not None:
        m.reward = ov["reward"]
    if "drops" in ov:
        m.legs = {k: v for k, v in m.legs.items() if v.kind != "dropoff"}
        m.legs.update(_legs(ov["drops"], "dropoff", "to"))
    if "pickups" in ov:
        m.legs = {k: v for k, v in m.legs.items() if v.kind != "pickup"}
        m.legs.update(_legs(ov["pickups"], "pickup", "from"))
    # per-leg field corrections (cargo / qty) overlaid by objective id, set by the
    # unified inline editor on the cargo-ops screens. Applied to whatever legs exist
    # (game legs, or the override's own drops/pickups), so a single unknown can be
    # fixed in place without rebuilding the whole leg list.
    for oid, f in (ov.get("leg_fields") or {}).items():
        leg = m.legs.get(oid)
        if not leg:
            continue
        if f.get("cargo"):
            leg.cargo = f["cargo"]
        if "qty" in f:
            leg.qty = _coerce_qty(f["qty"])
    # per-leg "mark delivered" overlay, applied last so it references the final
    # (possibly overridden) leg ids. Only forces "completed"; never un-completes
    # a leg the game itself marked done.
    for oid, st in (ov.get("leg_states") or {}).items():
        leg = m.legs.get(oid)
        if leg and st == "completed":
            leg.state = "completed"
    return m


def apply_leg_states(data: dict, items: list[dict], done: bool) -> dict:
    """Mark/unmark specific legs delivered, mutating `data` in place. `items` is a list
    of {"mission_id", "oid"}. The pure core shared by the disk writer and the overlay."""
    for it in items:
        mid, oid = it.get("mission_id"), it.get("oid")
        if not mid or not oid:
            continue
        entry = data.get(mid) or {}
        states = entry.get("leg_states") or {}
        if done:
            states[oid] = "completed"
        else:
            states.pop(oid, None)
        if states:
            entry["leg_states"] = states
        else:
            entry.pop("leg_states", None)
        if entry:
            data[mid] = entry
        else:
            data.pop(mid, None)
    return data


def set_leg_states(items: list[dict], done: bool, path: str = OVERRIDES_PATH) -> None:
    """Mark/unmark specific legs delivered. `items` is a list of
    {"mission_id", "oid"}; reads the file once, merges, writes once."""
    atomic_write(path, apply_leg_states(read_json(path, dict), items, done))


def apply_leg_field(data: dict, mission_id: str, oid: str, field: str, value) -> dict:
    """Merge one per-leg field correction (``cargo`` | ``qty``) into `data` in place,
    keyed by objective id; a falsy/None value clears it, pruning empty cells/entries.
    The pure core shared by the disk writer and the ephemeral replay overlay."""
    entry = data.get(mission_id) or {}
    fields = entry.get("leg_fields") or {}
    cell = fields.get(oid) or {}
    if value in (None, ""):
        cell.pop(field, None)
    else:
        cell[field] = value
    if cell:
        fields[oid] = cell
    else:
        fields.pop(oid, None)
    if fields:
        entry["leg_fields"] = fields
    else:
        entry.pop("leg_fields", None)
    if entry:
        data[mission_id] = entry
    else:
        data.pop(mission_id, None)
    return data


def set_leg_field(mission_id: str, oid: str, field: str, value, path: str = OVERRIDES_PATH) -> None:
    """Merge one per-leg field correction (``cargo`` | ``qty``) into a mission's
    override, keyed by objective id; a falsy/None value clears it. Read-modify-write,
    pruning empty cells/entries so the file stays minimal."""
    atomic_write(path, apply_leg_field(read_json(path, dict), mission_id, oid, field, value))
