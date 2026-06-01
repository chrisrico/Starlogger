"""Local cache of ship cargo grids, read from the game's own ``Data.p4k`` via
``scdata`` (StarBreaker) instead of scraping a third-party site. It serves the
per-ship total SCU, the physical grid geometry (deck-positioned sub-grids), and the
localised name / manufacturer / role. Refreshed only when the game's major version
changes (cargo layouts change with patches, not sessions).

The on-disk file is keyed by display name (so ``/api/ships`` and the front-end stay
unchanged); each entry also carries its ``class`` (the DataCore entity class, e.g.
``MISC_Freelancer``) so the log's vehicle entity can be looked up directly."""

from __future__ import annotations

import json
import os
import threading
import time

from .config import SHIP_CARGO_PATH
from .patterns import major_version
from . import scdata

_cache = {"mtime": None, "data": {"ships": {}, "fetched_at": None, "game_version": None},
          "by_class": {}, "by_name": {}}


def build_ship_cargo(p4k: str, progress=lambda m: None) -> dict:
    """Extract every cargo ship from the local install and re-key by display name."""
    by_class = scdata.build_ships(p4k, progress=progress)
    ships: dict[str, dict] = {}
    for entry in by_class.values():
        name = entry["name"]
        # Display-name collisions (variants) -> keep the larger-capacity ship.
        if name not in ships or entry["scu"] > ships[name]["scu"]:
            ships[name] = entry
    return ships


def save_ship_cargo(ships: dict, game_version: str | None = None,
                    path: str = SHIP_CARGO_PATH) -> None:
    data = {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "count": len(ships),
        "ships": ships,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic: readers always see a complete file (live reload)


def _reindex(data: dict) -> None:
    ships = data.get("ships", {})
    _cache["by_name"] = {name.lower(): e for name, e in ships.items()}
    _cache["by_class"] = {e["class"].lower(): e for e in ships.values() if e.get("class")}


def load_ship_cargo(path: str = SHIP_CARGO_PATH) -> dict:
    try:
        mt = os.stat(path).st_mtime
    except FileNotFoundError:
        return _cache["data"]
    if _cache["mtime"] != mt:
        try:
            with open(path, encoding="utf-8") as f:
                _cache["data"] = json.load(f)
            _cache["mtime"] = mt
            _reindex(_cache["data"])
        except (OSError, json.JSONDecodeError):
            pass
    return _cache["data"]


def _lookup(name: str | None, db: dict | None) -> dict | None:
    """Resolve a ship by display name (Freelancer) or DataCore class (MISC_Freelancer)."""
    if not name:
        return None
    if db is not None:
        ships = db.get("ships", {})
        by_name = {n.lower(): e for n, e in ships.items()}
        by_class = {e["class"].lower(): e for e in ships.values() if e.get("class")}
    else:
        load_ship_cargo()
        by_name, by_class = _cache["by_name"], _cache["by_class"]
    key = name.lower()
    return by_name.get(key) or by_class.get(key)


def ship_capacity(name: str | None, db: dict | None = None) -> int | None:
    hit = _lookup(name, db)
    return hit["scu"] if hit else None


def ship_grid(name: str | None, db: dict | None = None) -> list | None:
    """The ship's cargo-grid geometry: a list of bays, each ``{x, z, grids:[...]}``."""
    hit = _lookup(name, db)
    return hit.get("groups") if hit else None


def ship_display_name(entity_class: str | None, db: dict | None = None) -> str | None:
    """Map a DataCore vehicle entity class (from the log) to its display name."""
    hit = _lookup(entity_class, db)
    return hit.get("name") if hit else None


def ship_layout(name: str | None, db: dict | None = None) -> str | None:
    """'deck' if the grid bays are at real ship positions (forward = +z), else 'synth'."""
    hit = _lookup(name, db)
    return hit.get("layout") if hit else None


def known_ship_names(db: dict | None = None) -> set:
    return set((db or load_ship_cargo()).get("ships", {}))


def refresh_loop(state, stop: threading.Event, log_path: str | None = None,
                 path: str = SHIP_CARGO_PATH) -> None:
    """Rebuild the cache only on a MAJOR game-version change (or if missing), reading
    the local install. Runs the heavy StarBreaker extraction niced in the background
    (see scdata); the tracker keeps serving the old file until the atomic replace."""
    for _ in range(20):  # ~10s for the tailer to parse the version header
        if state.game_version or stop.is_set():
            break
        stop.wait(0.5)

    while not stop.is_set():
        ver = state.game_version
        cached = load_ship_cargo(path)
        cached_ver = cached.get("game_version")
        if not cached.get("ships"):
            reason = "no cache"
        elif ver and major_version(ver) != major_version(cached_ver):
            reason = f"version {cached_ver or '?'} -> {ver}"
        else:
            reason = None

        # Commodity GUID->name map (for manual-trade tracking). Cheap to build (one
        # dcb query), gated like ship cargo: rebuild when missing or the major
        # version moved on. Independent of `reason` so a fresh data dir with current
        # ships still gets it.
        from . import commodities
        if not commodities.load_commodities():
            cmty_reason = "no cache"
        elif ver and major_version(ver) != major_version(commodities.commodities_version()):
            cmty_reason = f"version {commodities.commodities_version() or '?'} -> {ver}"
        else:
            cmty_reason = None

        if reason or cmty_reason:
            p4k = scdata.find_p4k(log_path)
            if not p4k:
                print("[ship cargo] skip refresh: Data.p4k not found next to Game.log")
            else:
                if reason:
                    try:
                        print(f"[ship cargo] rebuilding from local install ({reason}) -- niced, ~minutes")
                        ships = build_ship_cargo(p4k)
                        if ships:
                            save_ship_cargo(ships, game_version=ver)
                            print(f"[ship cargo] rebuilt {len(ships)} ships ({reason})")
                    except Exception as e:  # keep old cache, retry next check
                        print(f"[ship cargo] rebuild failed: {e}")
                if cmty_reason:
                    try:
                        cmap = scdata.build_commodity_map(p4k)
                        if cmap:
                            commodities.save_commodities(cmap, game_version=ver)
                            print(f"[commodities] built {len(cmap)} commodity names ({cmty_reason})")
                    except Exception as e:
                        print(f"[commodities] build failed: {e}")
        stop.wait(300)  # re-check for a version bump (e.g. after a patch + relaunch)
