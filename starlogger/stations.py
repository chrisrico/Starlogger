"""Persistent zoneHostId -> station-name map.

The game logs an objective's station NAME ("Deliver N SCU of X to <station>") and
its zoneHostId on separate lines, and frequently only the marker (the zone)
survives. We persist every name we ever learn -- plus any manual corrections --
keyed by zone id, so an "Unknown station (zone …)" resolves across sessions and
back-fills *every* mission that shares the zone (origins included).

Stored in station_names.json:  { "<zoneHostId>": "HUR-L1 Green Glade Station" }

Mirrors the read/write conventions in overrides.py (mtime-cached reads, atomic
temp-file writes) so it's re-read automatically with no restart.
"""

from __future__ import annotations

import json
import os
import re

from .config import STATION_NAMES_PATH

_cache: dict = {"mtime": None, "data": {}}

# Same two facts the live parser keys together (see starlogger/state.py): a marker line
# carries objectiveId + zoneHostId; the objective-text line carries objectiveId +
# station name. Joining them on objectiveId recovers zone -> name from old logs.
_MARK = re.compile(
    r"objectiveId \[((?:dropoff|pickup)_[0-9a-f-]+_\d+)\], "
    r"markerEntityId \[\d+\], zoneHostId \[(\d+)\]"
)
_TEXT = re.compile(
    r"(?:Deliver|Collect) \d+/\d+ SCU of [A-Za-z ]+ (?:to|from) "
    r"([A-Za-z0-9 '\-]+?): \"[^\"]*?ObjectiveId: \[((?:dropoff|pickup)_[0-9a-f-]+_\d+)\]"
)


def get_station_names(path: str = STATION_NAMES_PATH) -> dict:
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


def _write(data: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    _cache["mtime"] = None  # force a fresh read on next get_station_names()


def _load_raw(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def set_station_name(zone_id: str, name: str | None, path: str = STATION_NAMES_PATH) -> None:
    """Manually set (or, if name is falsy, clear) one zone's station name."""
    data = _load_raw(path)
    if name:
        data[str(zone_id)] = name
    else:
        data.pop(str(zone_id), None)
    _write(data, path)


def learn_station_names(learned: dict, path: str = STATION_NAMES_PATH) -> None:
    """Merge auto-learned {zone: name} pairs into the store; writes only when
    something is new or changed. The real game name wins over a prior value."""
    if not learned:
        return
    data = _load_raw(path)
    changed = False
    for z, n in learned.items():
        if n and data.get(str(z)) != n:
            data[str(z)] = n
            changed = True
    if changed:
        _write(data, path)


def recover_from_logs(paths: list[str]) -> dict[str, dict[str, int]]:
    """Mine log files for zone -> {name: count} by joining marker lines and
    objective-text lines on their shared objectiveId. A zone with more than one
    recovered name is ambiguous (the game reuses some host ids)."""
    oid_zone: dict[str, str] = {}
    oid_name: dict[str, str] = {}
    for fp in paths:
        try:
            text = open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in _MARK.finditer(text):
            oid_zone[m.group(1)] = m.group(2)
        for m in _TEXT.finditer(text):
            oid_name[m.group(2)] = m.group(1).strip()
    counts: dict[str, dict[str, int]] = {}
    for oid, zone in oid_zone.items():
        name = oid_name.get(oid)
        if name:
            counts.setdefault(zone, {})
            counts[zone][name] = counts[zone].get(name, 0) + 1
    return counts


def zone_epoch(zone_id) -> int | None:
    """The high bits of a zoneHostId -- a server-build 'epoch' that rolls several
    times per game patch. Zone ids only recur within one epoch (across epochs the
    high bits differ, so two epochs can never share an id), which means an entry
    whose epoch isn't the current one can never match a live lookup again."""
    try:
        return int(zone_id) >> 32
    except (TypeError, ValueError):
        return None


def prune_station_names(keep_epochs: set, path: str = STATION_NAMES_PATH,
                        dry_run: bool = False) -> dict:
    """Drop entries whose zoneHostId epoch isn't in `keep_epochs` -- those can
    never resolve a live lookup again (file hygiene; stale rows are inert, never
    mismatched). Returns {removed: {zone: name}, kept: int, skipped: bool}.
    No-op (and never writes) when keep_epochs is empty: we don't know the current
    epoch then, so everything is kept."""
    data = _load_raw(path)
    if not keep_epochs:
        return {"removed": {}, "kept": len(data), "skipped": True}
    removed = {z: n for z, n in data.items() if zone_epoch(z) not in keep_epochs}
    if removed and not dry_run:
        _write({z: n for z, n in data.items() if z not in removed}, path)
    return {"removed": removed, "kept": len(data) - len(removed), "skipped": False}


def seed_station_names(paths: list[str], path: str = STATION_NAMES_PATH) -> dict:
    """One-time backfill: recover zone -> name from `paths` and fill in any zone
    not already known (existing manual/live names are never overwritten). For an
    ambiguous zone the most-frequently-seen name is used. Returns a summary."""
    counts = recover_from_logs(paths)
    data = _load_raw(path)
    added: dict[str, str] = {}
    ambiguous: dict[str, dict] = {}
    for zone, names in counts.items():
        best = max(names, key=names.get)
        if len(names) > 1:
            ambiguous[zone] = names
        if zone not in data:           # never clobber a name we already trust
            data[zone] = best
            added[zone] = best
    if added:
        _write(data, path)
    return {"added": added, "ambiguous": ambiguous, "total_recovered": len(counts)}
