"""Local cache of salvageable wreck ships and their removable components, in salvage_ships.json.

Built by ``scdata._salvage_ships``: for every ship that can spawn as an ``*_Unmanned_Salvage``
wreck, its stock loadout filtered to the components the salvage beam can strip (each flagged
``pullable`` -- see that module / NOTES for why the size cap is ours, not the game's). The
Game.log names a detected wreck by its base ship class (``patterns.SALVAGE_SPAWN``), which keys
straight into ``ships`` here; ``lookup`` also resolves a display name (for manual RS lookups).
"""

from __future__ import annotations

import time

from .config import SALVAGE_SHIPS_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None, "data": {"ships": {}, "fetched_at": None, "game_version": None}}

# Extract-schema version: bump when the per-ship/component output SHAPE changes (fields
# added/renamed/dropped) so installs rebuild on update even without a major game-version move.
# 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
EXTRACT_VERSION = 1


def save_salvage_ships(ships: dict, game_version: str | None = None,
                       path: str = SALVAGE_SHIPS_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "count": len(ships),
        "ships": ships,
    })


def load_salvage_ships(path: str = SALVAGE_SHIPS_PATH) -> dict:
    """The full cache dict ({ships, game_version, ...}); empty ships until built."""
    return load_cached(path, _cache)


def catalog(path: str = SALVAGE_SHIPS_PATH) -> dict:
    """{base_class_lower: {class, name, components, ...}}; empty until built."""
    return (load_salvage_ships(path) or {}).get("ships") or {}


def salvage_ships_version(path: str = SALVAGE_SHIPS_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_salvage_ships(path) or {}).get("game_version")


def salvage_ships_extract_version(path: str = SALVAGE_SHIPS_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_salvage_ships(path) or {}).get("extract_version") or 0)


def lookup(ship: str | None, path: str = SALVAGE_SHIPS_PATH) -> dict | None:
    """Resolve a detected wreck's base class (e.g. ``AEGS_Gladius`` from the log) -- or its
    display name (``Gladius``, from a manual RS candidate) -- to its catalog entry, or None
    when it isn't a known salvage ship."""
    if not ship:
        return None
    ships = catalog(path)
    hit = ships.get(ship.lower())
    if hit:
        return hit
    target = ship.strip().lower()
    for entry in ships.values():
        if target in ((entry.get("name") or "").lower(), (entry.get("name_full") or "").lower()):
            return entry
    return None
