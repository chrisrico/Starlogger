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
import json
import os

from .config import OVERRIDES_PATH
from .model import Leg, Mission

_cache = {"mtime": None, "data": {}}


def get_overrides(path: str = OVERRIDES_PATH) -> dict:
    try:
        mtime = os.stat(path).st_mtime
    except FileNotFoundError:
        return {}
    if _cache["mtime"] != mtime:
        try:
            with open(path, encoding="utf-8") as f:
                _cache["data"] = json.load(f)
            _cache["mtime"] = mtime
        except (OSError, json.JSONDecodeError):
            pass
    return _cache["data"]


def write_override(mission_id: str, override: dict | None, path: str = OVERRIDES_PATH) -> None:
    """Set (or, if override is falsy, remove) one mission's override entry."""
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if override:
        data[mission_id] = override
    else:
        data.pop(mission_id, None)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def prune_overrides(keep_mission_ids: set, path: str = OVERRIDES_PATH,
                    dry_run: bool = False) -> dict:
    """Drop override entries whose mission_id isn't in `keep_mission_ids` -- i.e.
    missions no longer present in the current log. Their corrected data was
    already frozen into the session archive when the session ended, and the
    override (keyed by a now-gone mission_id) can never apply again. A
    crash-relaunch re-writes the same ids into the new log, so those stay in
    `keep_mission_ids` and are preserved. Returns {removed: {id: title}, kept}."""
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"removed": {}, "kept": 0}
    removed = {mid: (ov.get("title") or ov.get("_note") or "")
               for mid, ov in data.items() if mid not in keep_mission_ids}
    if removed and not dry_run:
        kept = {mid: ov for mid, ov in data.items() if mid not in removed}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        _cache["mtime"] = None  # force reload on next read
    return {"removed": removed, "kept": len(data) - len(removed)}


def _legs(items: list[dict], kind: str, loc_key: str) -> dict[str, Leg]:
    out: dict[str, Leg] = {}
    for i, it in enumerate(items):
        oid = f"ovr_{kind}_{i}"
        out[oid] = Leg(
            objective_id=oid, kind=kind, cargo=it.get("cargo"), qty=it.get("qty"),
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
            leg.qty = f["qty"]
    # per-leg "mark delivered" overlay, applied last so it references the final
    # (possibly overridden) leg ids. Only forces "completed"; never un-completes
    # a leg the game itself marked done.
    for oid, st in (ov.get("leg_states") or {}).items():
        leg = m.legs.get(oid)
        if leg and st == "completed":
            leg.state = "completed"
    return m


def set_leg_states(items: list[dict], done: bool, path: str = OVERRIDES_PATH) -> None:
    """Mark/unmark specific legs delivered. `items` is a list of
    {"mission_id", "oid"}; reads the file once, merges, writes once."""
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
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
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def set_leg_field(mission_id: str, oid: str, field: str, value, path: str = OVERRIDES_PATH) -> None:
    """Merge one per-leg field correction (``cargo`` | ``qty``) into a mission's
    override, keyed by objective id; a falsy/None value clears it. Read-modify-write,
    pruning empty cells/entries so the file stays minimal."""
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
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
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
