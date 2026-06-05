"""Local cache of ship cargo grids, read from the game's own ``Data.p4k`` via
``scdata`` (StarBreaker) instead of scraping a third-party site. It serves the
per-ship total SCU, the physical grid geometry (deck-positioned sub-grids), and the
localised name / manufacturer / role. Refreshed only when the game's major version
changes (cargo layouts change with patches, not sessions).

The on-disk file is keyed by display name (so ``/api/ships`` and the front-end stay
unchanged); each entry also carries its ``class`` (the DataCore entity class, e.g.
``MISC_Freelancer``) so the log's vehicle entity can be looked up directly."""

from __future__ import annotations

import re
import threading
import time

from .config import SHIP_CARGO_PATH
from .jsonstore import atomic_write, load_cached
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
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "count": len(ships),
        "ships": ships,
    })


def _reindex(data: dict) -> None:
    ships = data.get("ships", {})
    _cache["by_name"] = {name.lower(): e for name, e in ships.items()}
    _cache["by_class"] = {e["class"].lower(): e for e in ships.values() if e.get("class")}


def _parse_ships(data: dict) -> dict:
    _reindex(data)  # refresh the by-name / by-class lookup tables on each (re)load
    return data


def load_ship_cargo(path: str = SHIP_CARGO_PATH) -> dict:
    return load_cached(path, _cache, _parse_ships)


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


# Mining vehicles the cargo-grid catalog can't classify by role: they carry no
# standard cargo grid, so they're absent from ships_cargo.json (the MOLE, which does,
# is caught by its 'Medium Mining' role below instead). Matched as whole tokens
# against the friendly name and the log's entity class, so "roc" can't hit "Reclaimer".
_MINING_TOKENS = {"prospector", "roc"}


def is_mining_ship(name: str | None, internal: str | None = None,
                   db: dict | None = None) -> bool:
    """True when the effective ship/vehicle is used for mining — by the cargo DB's
    role (e.g. the MOLE's 'Medium Mining'; salvage roles deliberately don't count),
    or by a known surface miner the grid catalog doesn't carry (the Prospector, the
    Greycat ROC / ROC-DS). Drives the dashboard's mining-vs-hauling tab layout."""
    hit = _lookup(name, db) or _lookup(internal, db)
    if hit and "mining" in (hit.get("role") or "").lower():
        return True
    tokens: set[str] = set()
    for s in (name, internal):
        if s:
            tokens |= {t for t in re.split(r"[\s_/-]+", s.lower()) if t}
    return bool(tokens & _MINING_TOKENS)


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

        # Commodity + station reference data (commodity GUID->name + cargo-name list;
        # station code->name + station list). Cheap to build (one global.ini extract +
        # one dcb query), gated like ship cargo: rebuild when missing or the major
        # version moved on. Independent of `reason` so a fresh data dir with current
        # ships still gets it.
        from . import reference
        if not reference.load_commodities() or not reference.location_codes():
            ref_reason = "no cache"
        elif ver and major_version(ver) != major_version(reference.commodities_version()):
            ref_reason = f"version {reference.commodities_version() or '?'} -> {ver}"
        else:
            ref_reason = None

        # Mineable-rock RS + composition. Built from a full DataCore extract (its own
        # file/trigger -- the RS value can't be pulled via the cheap reference query),
        # gated like ship cargo: rebuild when missing or the major version moved on.
        from . import mineables
        if not mineables.load_mineables().get("rocks"):
            min_reason = "no cache"
        elif ver and major_version(ver) != major_version(mineables.mineables_version()):
            min_reason = f"version {mineables.mineables_version() or '?'} -> {ver}"
        else:
            min_reason = None

        # Crafting blueprints + requirements. Same full-extract source/trigger as
        # mineables; own file (blueprints.json). Feeds the Mining tab's blueprint planner.
        from . import blueprints
        if not blueprints.load_blueprints().get("blueprints"):
            bp_reason = "no cache"
        elif ver and major_version(ver) != major_version(blueprints.blueprints_version()):
            bp_reason = f"version {blueprints.blueprints_version() or '?'} -> {ver}"
        else:
            bp_reason = None

        if reason or ref_reason or min_reason or bp_reason:
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
                if ref_reason:
                    try:
                        ref = scdata.build_reference_data(p4k)
                        reference.save_reference(
                            ref["commodities"], ref["location_codes"],
                            commodity_names=ref["commodity_names"],
                            station_names=ref["station_names"], game_version=ver)
                        print(f"[reference] built {len(ref['commodity_names'])} commodities + "
                              f"{len(ref['station_names'])} stations ({ref_reason})")
                    except Exception as e:
                        print(f"[reference] build failed: {e}")
                if min_reason:
                    try:
                        print(f"[mineables] rebuilding from local install ({min_reason}) -- niced, ~minutes")
                        rocks = scdata.build_mineables_from_p4k(p4k)
                        if rocks:
                            mineables.save_mineables(rocks, game_version=ver)
                            print(f"[mineables] built {len(rocks)} mineable rocks ({min_reason})")
                    except Exception as e:
                        print(f"[mineables] build failed: {e}")
                if bp_reason:
                    try:
                        print(f"[blueprints] rebuilding from local install ({bp_reason}) -- niced, ~minutes")
                        bps = scdata.build_blueprints_from_p4k(p4k)
                        if bps:
                            blueprints.save_blueprints(bps, game_version=ver)
                            print(f"[blueprints] built {len(bps)} blueprints ({bp_reason})")
                    except Exception as e:
                        print(f"[blueprints] build failed: {e}")
        stop.wait(300)  # re-check for a version bump (e.g. after a patch + relaunch)
