"""Local cache of mining equipment -- laser heads + consumable modules, in
mining_gear.json.

The gear side of the rock-feasibility calc (the rock side is mineables.json): a player
fits a mining *head* to a turret and slots *modules* into it; both apply percentage
modifiers to the cracking minigame, which the Identify tab weighs against a rock's
``mechanics`` to grade "can my ship mine this?". Built from the game's ``Data.p4k`` via
``scdata.build_mining_gear`` on the same full-extract trigger as mineables/blueprints --
see ``catalogs``. See ``scdata._mining_gear`` for the field shapes.
"""

from __future__ import annotations

import time

from .config import MINING_GEAR_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None,
          "data": {"heads": [], "modules": [], "fetched_at": None, "game_version": None}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild on a code update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
EXTRACT_VERSION = 0


def save_mining_gear(heads: list, modules: list, game_version: str | None = None,
                     path: str = MINING_GEAR_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "heads": heads,
        "modules": modules,
    })


def load_mining_gear(path: str = MINING_GEAR_PATH) -> dict:
    """The full cache dict ({heads, modules, game_version, ...}); empty until built."""
    return load_cached(path, _cache)


def heads(path: str = MINING_GEAR_PATH) -> list:
    """The mining-laser head list (each {class, name, size, power, module_slots, ...})."""
    return (load_mining_gear(path) or {}).get("heads") or []


def modules(path: str = MINING_GEAR_PATH) -> list:
    """The mining-module list (each {class, name, modifiers, ...})."""
    return (load_mining_gear(path) or {}).get("modules") or []


def head_by_class(cls: str, path: str = MINING_GEAR_PATH) -> dict | None:
    return next((h for h in heads(path) if h.get("class") == cls), None)


def module_by_class(cls: str, path: str = MINING_GEAR_PATH) -> dict | None:
    return next((m for m in modules(path) if m.get("class") == cls), None)


def mining_gear_version(path: str = MINING_GEAR_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_mining_gear(path) or {}).get("game_version")


def mining_gear_extract_version(path: str = MINING_GEAR_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_mining_gear(path) or {}).get("extract_version") or 0)
