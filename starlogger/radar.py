"""Local cache of ship radar components, in radar.json.

The radar slot of the per-ship mining loadout (head + modules live in mining_gear.json). The
mining-relevant stat is ``rs`` -- the resource-signature detection sensitivity (0-1) that
governs how far off a deposit's composition is readable; the stock Surveyor-Lite is weak
(0.8) and most radars max it (1.0). Built from the game's ``Data.p4k`` via
``scdata.build_radar`` on the same full-extract trigger as mineables/mining_gear -- see
``catalogs``. See ``scdata._radar`` for the field shapes + the RS-channel derivation.
"""

from __future__ import annotations

import time

from .config import RADAR_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None,
          "data": {"radars": [], "fetched_at": None, "game_version": None}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild on a code update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
EXTRACT_VERSION = 1  # v1: rs / rs_piercing / sensitivity_max + size / grade / ping_cooldown


def save_radar(radars: list, game_version: str | None = None,
               path: str = RADAR_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "radars": radars,
    })


def load_radar(path: str = RADAR_PATH) -> dict:
    """The full cache dict ({radars, game_version, ...}); empty until built."""
    return load_cached(path, _cache)


def radars(path: str = RADAR_PATH) -> list:
    """The radar component list (each {class, name, size, rs, rs_piercing, ...})."""
    return (load_radar(path) or {}).get("radars") or []


def radar_by_class(cls: str, path: str = RADAR_PATH) -> dict | None:
    return next((r for r in radars(path) if r.get("class") == cls), None)


def radar_version(path: str = RADAR_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_radar(path) or {}).get("game_version")


def radar_extract_version(path: str = RADAR_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_radar(path) or {}).get("extract_version") or 0)
