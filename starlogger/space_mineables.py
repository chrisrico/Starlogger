"""Local cache of space mining locations -- asteroid fields / belts / Lagrange fields and
what each yields, in space_mineables.json.

The space counterpart to body_mineables.json (planet/moon surfaces): which field out in the
black yields a given mineral, with its rarity tier. Built from the game's ``Data.p4k``
HarvestableProviderPreset records via ``scdata.build_space_mineables`` on the same full-extract
trigger as mineables/gear -- see ``catalogs``. See ``scdata._space_mineables`` for field shapes.

``mineral_locations`` / ``locations_for`` is the reverse map the dashboard joins into the inline
"where to mine this" hints, keyed via ``mineables._mineral_key`` so the archetype spelling
reconciles with the rock/blueprint spelling (Aluminum/Aluminium, Quantanium/Quantainium).
"""

from __future__ import annotations

import time

from .config import SPACE_MINEABLES_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached
from .mineables import _mineral_key

_cache = {"mtime": None,
          "data": {"fields": [], "fetched_at": None, "game_version": None}}

# Extract-schema version: bump when the output SHAPE changes (new / renamed / dropped fields),
# so installs rebuild on a code update even without a major game-version move. 0 == absent.
# v2: fields gained the optional ``points`` list (real Lagrange points, via ``starmap``).
EXTRACT_VERSION = 2


def save_space_mineables(fields: list, game_version: str | None = None,
                         path: str = SPACE_MINEABLES_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "fields": fields,
    })


def load_space_mineables(path: str = SPACE_MINEABLES_PATH) -> dict:
    """The full cache dict ({fields, game_version, ...}); empty until built."""
    return load_cached(path, _cache)


def fields(path: str = SPACE_MINEABLES_PATH) -> list:
    """The field list (each {name, system, ship_mineables: [{mineral, rarity}]}, plus an
    optional ``points`` list of real Lagrange points where the starmap knows them)."""
    return (load_space_mineables(path) or {}).get("fields") or []


def space_mineables_version(path: str = SPACE_MINEABLES_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_space_mineables(path) or {}).get("game_version")


def space_mineables_extract_version(path: str = SPACE_MINEABLES_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_space_mineables(path) or {}).get("extract_version") or 0)


def mineral_locations(path: str = SPACE_MINEABLES_PATH) -> dict:
    """Reverse map ``{mineral_key: [{field, system, rarity}]}`` from every field's ship
    mineables, keyed via ``mineables._mineral_key`` so the spelling reconciles with the
    rock/blueprint side. In catalog (system, name) order. A field carrying real Lagrange
    points adds them as ``points`` (omitted otherwise, to keep the common case lean)."""
    out: dict[str, list] = {}
    for f in fields(path):
        for sm in f.get("ship_mineables") or []:
            key = _mineral_key(sm.get("mineral"))
            if key:
                entry = {"field": f.get("name"), "system": f.get("system"),
                         "rarity": sm.get("rarity")}
                if f.get("points"):
                    entry["points"] = f["points"]
                out.setdefault(key, []).append(entry)
    return out


def locations_for(name: str, path: str = SPACE_MINEABLES_PATH) -> list:
    """The space fields whose ship mineables include ``name`` (spelling-tolerant) -- a list of
    ``{field, system, rarity}``."""
    return mineral_locations(path).get(_mineral_key(name), [])
