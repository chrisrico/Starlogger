"""Local cache of per-celestial-body mineables -- what each planet/moon yields, in
body_mineables.json.

The location side of the mining picture (the rock side is mineables.json, the gear side
mining_gear.json): which bodies yield a given mineral. Built from the game's ``Data.p4k``
starmap descriptions via ``scdata.build_body_mineables`` on the same full-extract trigger
as mineables/gear -- see ``catalogs``. See ``scdata._body_mineables`` for the field shapes.

Besides the raw catalog, this exposes the reverse map the dashboard uses to answer "where
do I mine this?": ``mineral_locations`` / ``locations_for`` key each body's *ship* mineables
through ``mineables._mineral_key`` so the body's spelling ("Aluminum", "Quantainium")
reconciles with the mineable-rock / blueprint spelling ("Aluminium", "Quantanium").
"""

from __future__ import annotations

import re
import time

from .config import BODY_MINEABLES_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached
from .mineables import _mineral_key

_cache = {"mtime": None,
          "data": {"bodies": [], "fetched_at": None, "game_version": None}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild on a code update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
EXTRACT_VERSION = 2  # v2: global.ini-driven (recovers ,P-variant + Pyro bodies) + ground_mineables

# Trailing parenthetical qualifier on an item ("Janalite (Caves only)") -- stripped before
# keying so the qualifier doesn't corrupt the mineral key.
_QUALIFIER = re.compile(r"\s*\(.*\)\s*$")


def save_body_mineables(bodies: list, game_version: str | None = None,
                        path: str = BODY_MINEABLES_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "bodies": bodies,
    })


def load_body_mineables(path: str = BODY_MINEABLES_PATH) -> dict:
    """The full cache dict ({bodies, game_version, ...}); empty until built."""
    return load_cached(path, _cache)


def bodies(path: str = BODY_MINEABLES_PATH) -> list:
    """The body list (each {name, system, ship_mineables, hand_mineables, ...})."""
    return (load_body_mineables(path) or {}).get("bodies") or []


def body_mineables_version(path: str = BODY_MINEABLES_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_body_mineables(path) or {}).get("game_version")


def body_mineables_extract_version(path: str = BODY_MINEABLES_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_body_mineables(path) or {}).get("extract_version") or 0)


def mineral_locations(path: str = BODY_MINEABLES_PATH) -> dict:
    """Reverse map ``{mineral_key: [{body, system}]}`` from every body's *ship* mineables
    (the ship-mining context the Find / Blueprint tools operate in). Keyed via
    ``mineables._mineral_key`` so the body spelling reconciles with the rock/blueprint
    spelling; bodies de-duplicated per mineral, in catalog (system, name) order."""
    out: dict[str, list] = {}
    for b in bodies(path):
        loc = {"body": b.get("name"), "system": b.get("system")}
        for item in b.get("ship_mineables") or []:
            key = _mineral_key(_QUALIFIER.sub("", item or ""))
            if not key:
                continue
            rows = out.setdefault(key, [])
            if loc not in rows:
                rows.append(loc)
    return out


def locations_for(name: str, path: str = BODY_MINEABLES_PATH) -> list:
    """The bodies whose ship mineables include ``name`` (spelling-tolerant) -- a list of
    ``{body, system}``. The single call the server uses to attach inline location info to a
    mineral lookup / blueprint-plan result."""
    return mineral_locations(path).get(_mineral_key(_QUALIFIER.sub("", name or "")), [])
