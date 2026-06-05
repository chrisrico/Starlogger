"""Local cache of ship cargo grids, read from the game's own ``Data.p4k`` via
``scdata`` (StarBreaker) instead of scraping a third-party site. It serves the
per-ship total SCU, the physical grid geometry (deck-positioned sub-grids), and the
localised name / manufacturer / role. Refreshed only when the game's major version
changes (cargo layouts change with patches, not sessions).

The on-disk file is keyed by display name (so ``/api/ships`` and the front-end stay
unchanged); each entry also carries its ``class`` (the DataCore entity class, e.g.
``MISC_Freelancer``) so the log's vehicle entity can be looked up directly."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

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


def is_mining_ship(name: str | None, internal: str | None = None,
                   db: dict | None = None) -> bool:
    """True when the effective ship/vehicle is used for mining, per the cargo DB's
    explicit ``mining`` flag (or, equivalently, a role mentioning mining — e.g. the
    MOLE's 'Medium Mining'; salvage roles deliberately don't count). Every mining
    vehicle, grid-bearing or not, is catalogued with this flag by scdata.build_ships
    (the Prospector, Golem, Greycat ROC / ROC-DS and ATLS GEO have no cargo grid).
    Drives the dashboard's mining-vs-hauling tab layout."""
    hit = _lookup(name, db) or _lookup(internal, db)
    return bool(hit and (hit.get("mining") or "mining" in (hit.get("role") or "").lower()))


def known_ship_names(db: dict | None = None) -> set:
    return set((db or load_ship_cargo()).get("ships", {}))


@dataclass
class _Catalog:
    """One rebuildable cache. ``rebuild(p4k, ver, reason)`` does the build + atomic save +
    logging and raises on failure; the orchestrator gates it on ``_reason`` and isolates it."""
    label: str
    has_cache: Callable[[], bool]            # a usable cache already exists
    cached_version: Callable[[], "str | None"]
    rebuild: Callable[[str, "str | None", str], None]


def _reason(cat: _Catalog, ver: str | None) -> str | None:
    """Why ``cat`` needs rebuilding: missing cache, or a MAJOR game-version move; else None."""
    if not cat.has_cache():
        return "no cache"
    if ver and major_version(ver) != major_version(cat.cached_version()):
        return f"version {cat.cached_version() or '?'} -> {ver}"
    return None


def _build_catalogs(path: str) -> list:
    """The catalogs the background loop keeps fresh, each gated/rebuilt the same way. The
    reference/mineables/blueprints modules are imported lazily (only the loop needs them)."""
    from . import blueprints, mineables, reference

    def _ship(p4k, ver, reason):
        print(f"[ship cargo] rebuilding from local install ({reason}) -- niced, ~minutes")
        ships = build_ship_cargo(p4k)
        if ships:
            save_ship_cargo(ships, game_version=ver)
            print(f"[ship cargo] rebuilt {len(ships)} ships ({reason})")

    def _reference(p4k, ver, reason):
        ref = scdata.build_reference_data(p4k)
        reference.save_reference(
            ref["commodities"], ref["location_codes"],
            commodity_names=ref["commodity_names"],
            station_names=ref["station_names"], game_version=ver)
        print(f"[reference] built {len(ref['commodity_names'])} commodities + "
              f"{len(ref['station_names'])} stations ({reason})")

    def _mineables(p4k, ver, reason):
        print(f"[mineables] rebuilding from local install ({reason}) -- niced, ~minutes")
        rocks = scdata.build_mineables_from_p4k(p4k)
        if rocks:
            mineables.save_mineables(rocks, game_version=ver)
            print(f"[mineables] built {len(rocks)} mineable rocks ({reason})")

    def _blueprints(p4k, ver, reason):
        print(f"[blueprints] rebuilding from local install ({reason}) -- niced, ~minutes")
        bps = scdata.build_blueprints_from_p4k(p4k)
        if bps:
            blueprints.save_blueprints(bps, game_version=ver)
            print(f"[blueprints] built {len(bps)} blueprints ({reason})")

    return [
        _Catalog("ship cargo",
                 lambda: bool(load_ship_cargo(path).get("ships")),
                 lambda: load_ship_cargo(path).get("game_version"), _ship),
        # Commodity + station reference data; cheap to build, gated like the rest.
        _Catalog("reference",
                 lambda: bool(reference.load_commodities()) and bool(reference.location_codes()),
                 reference.commodities_version, _reference),
        # Mineable-rock RS + composition (full DataCore extract; own file/trigger).
        _Catalog("mineables",
                 lambda: bool(mineables.load_mineables().get("rocks")),
                 mineables.mineables_version, _mineables),
        # Crafting blueprints + requirements (same full-extract source as mineables).
        _Catalog("blueprints",
                 lambda: bool(blueprints.load_blueprints().get("blueprints")),
                 blueprints.blueprints_version, _blueprints),
    ]


def _refresh_once(catalogs: list, ver: str | None, log_path: str | None) -> None:
    """One pass: find the stale catalogs, locate Data.p4k once, rebuild each (a failure in
    one doesn't stop the others). Callable on its own, which is what the tests drive."""
    stale = [(c, r) for c in catalogs if (r := _reason(c, ver))]
    if not stale:
        return
    p4k = scdata.find_p4k(log_path)
    if not p4k:
        print("[ship cargo] skip refresh: Data.p4k not found next to Game.log")
        return
    for cat, reason in stale:
        try:
            cat.rebuild(p4k, ver, reason)
        except Exception as e:  # keep the old cache, retry next check
            print(f"[{cat.label}] rebuild failed: {e}")


def refresh_loop(state, stop: threading.Event, log_path: str | None = None,
                 path: str = SHIP_CARGO_PATH) -> None:
    """Rebuild the local caches only on a MAJOR game-version change (or if missing), reading
    the local install. Runs the heavy StarBreaker extraction niced in the background (see
    scdata); the tracker keeps serving the old files until each atomic replace."""
    for _ in range(20):  # ~10s for the tailer to parse the version header
        if state.game_version or stop.is_set():
            break
        stop.wait(0.5)

    catalogs = _build_catalogs(path)
    while not stop.is_set():
        _refresh_once(catalogs, state.game_version, log_path)
        stop.wait(300)  # re-check for a version bump (e.g. after a patch + relaunch)
