"""Local cache of ship cargo grids, read from the game's own ``Data.p4k`` via
``scdata`` (StarBreaker) instead of scraping a third-party site. It serves the
per-ship total SCU, the physical grid geometry (deck-positioned sub-grids), and the
localised name / manufacturer / role. Refreshed only when the game's major version
changes (cargo layouts change with patches, not sessions).

The on-disk file is keyed by display name (so ``/api/ships`` and the front-end stay
unchanged); each entry also carries its ``class`` (the DataCore entity class, e.g.
``MISC_Freelancer``) so the log's vehicle entity can be looked up directly."""

from __future__ import annotations

import time

from .config import SHIP_CARGO_PATH
from .jsonstore import atomic_write, load_cached
from . import scdata

_cache = {"mtime": None, "data": {"ships": {}, "fetched_at": None, "game_version": None},
          "by_class": {}, "by_name": {}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild the cache on update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
# v1: the ``mining`` flag became a dict carrying mining-laser hardpoint sizes.
EXTRACT_VERSION = 1


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
        "extract_version": EXTRACT_VERSION,
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


def ships_extract_version(path: str = SHIP_CARGO_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_ship_cargo(path) or {}).get("extract_version") or 0)


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


def mining_hardpoints(name: str | None, internal: str | None = None,
                      db: dict | None = None) -> list:
    """The sizes of a ship's mining-laser hardpoints (e.g. the MOLE -> [2, 2, 2], the
    Prospector / Golem -> [1]); empty for non-miners or handheld-only miners (ROC). Read
    from the cargo DB's ``mining`` record, which ``scdata.build_ships`` fills from the
    ship's default loadout. Drives the equipment popup's per-ship head filter."""
    hit = _lookup(name, db) or _lookup(internal, db)
    mining = (hit or {}).get("mining")
    return list(mining.get("hardpoints") or []) if isinstance(mining, dict) else []


def known_ship_names(db: dict | None = None) -> set:
    return set((db or load_ship_cargo()).get("ships", {}))
