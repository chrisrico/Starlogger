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
import time

from .config import SHIP_CARGO_PATH
from .jsonstore import atomic_write, load_cached
from . import scdata


class PartialCatalogError(RuntimeError):
    """Raised when a rebuild would replace the on-disk ship catalog with a drastically
    smaller one -- the signature of a degraded/partial extract. Refusing to write keeps the
    good cache (and stops the version gate from marking the decimated file 'current')."""


# Smallest fraction of the existing catalog a rebuild may shrink to before it's rejected as
# a partial extract. A real game patch never removes half the ships; a wedged extract does.
RETAIN_FRACTION = 0.5

_cache = {"mtime": None, "data": {"ships": {}, "fetched_at": None, "game_version": None},
          "by_class": {}, "by_name": {}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild the cache on update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
# v1: the ``mining`` flag became a dict carrying mining-laser hardpoint sizes.
# v2: ships carry a ``radar`` {size, stock} slot (the radar half of the mining loadout).
# v3: the ``mining`` record carries the factory ``head`` class (for per-ship head compat).
EXTRACT_VERSION = 3


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


def _ondisk_ship_count(path: str) -> int:
    """How many ships the current ``ships.json`` holds (0 if absent/unreadable). Read fresh
    off disk, bypassing the mtime cache, so the shrink guard sees the real prior catalog."""
    try:
        with open(path, encoding="utf-8") as f:
            return len(json.load(f).get("ships") or {})
    except (OSError, ValueError):
        return 0


def save_ship_cargo(ships: dict, game_version: str | None = None,
                    path: str = SHIP_CARGO_PATH) -> None:
    prev = _ondisk_ship_count(path)
    if prev and len(ships) < prev * RETAIN_FRACTION:
        raise PartialCatalogError(
            f"refusing to overwrite a {prev}-ship catalog with {len(ships)} ships ({path}) "
            f"-- likely a partial extract; keeping the existing cache")
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


def is_salvage_ship(name: str | None, internal: str | None = None,
                    db: dict | None = None) -> bool:
    """True when the effective ship is a salvage vessel (Vulture, Reclaimer, MOTH, ...), per
    the cargo DB's role ('... Salvage'). One trigger for the dashboard's Salvage mode -- the
    other being wrecks detected in the log (see snapshot ``detected_salvage``). Mirrors
    ``is_mining_ship``; salvage roles are distinct from mining ones."""
    hit = _lookup(name, db) or _lookup(internal, db)
    return bool(hit and "salvage" in (hit.get("role") or "").lower())


def mining_hardpoints(name: str | None, internal: str | None = None,
                      db: dict | None = None) -> list:
    """The sizes of a ship's mining-laser hardpoints (e.g. the MOLE -> [2, 2, 2], the
    Prospector / Golem -> [1]); empty for non-miners or handheld-only miners (ROC). Read
    from the cargo DB's ``mining`` record, which ``scdata.build_ships`` fills from the
    ship's default loadout. Drives the equipment popup's per-ship head filter."""
    hit = _lookup(name, db) or _lookup(internal, db)
    mining = (hit or {}).get("mining")
    return list(mining.get("hardpoints") or []) if isinstance(mining, dict) else []


def mining_head(name: str | None, internal: str | None = None,
                db: dict | None = None) -> str | None:
    """A ship's factory mining-laser class (lower-case), or None -- the head it ships with (the
    Prospector's Arbor, the Golem's bespoke Pitman). Read from the cargo DB's ``mining`` record.
    Lets the equipment popup restrict head choices to those sharing this head's mount tag."""
    hit = _lookup(name, db) or _lookup(internal, db)
    mining = (hit or {}).get("mining")
    return mining.get("head") if isinstance(mining, dict) else None


def radar_slot(name: str | None, internal: str | None = None,
               db: dict | None = None) -> dict | None:
    """A ship's radar hardpoint ``{size, stock}`` (e.g. the Prospector -> size 1, stock
    ``radr_chco_s01_surveyorlite``), or None if unknown. Read from the cargo DB's ``radar``
    record, which ``scdata.build_ships`` fills from the ship's default loadout. Drives the
    equipment popup's per-ship radar filter + stock marker (``stock`` is lower-case; match the
    radar catalog case-insensitively)."""
    hit = _lookup(name, db) or _lookup(internal, db)
    radar = (hit or {}).get("radar")
    return dict(radar) if isinstance(radar, dict) else None


def known_ship_names(db: dict | None = None) -> set:
    return set((db or load_ship_cargo()).get("ships", {}))
