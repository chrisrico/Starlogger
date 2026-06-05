"""Local cache of crafting blueprints + their material requirements, in blueprints.json.

Built from the game's ``Data.p4k`` (via ``scdata.build_blueprints``) on the same full
DataCore extract / major-version trigger as the mineables catalog -- its own file, like
ships_cargo.json / mineables.json. Each blueprint records what it crafts and the flat
material list its recipe needs: ``{slot, resource, scu, min_quality}`` per ingredient,
plus a ``minerals`` shortcut. That feeds the Mining tab's blueprint planner: pick a
blueprint -> its required minerals -> the rocks (and RS values) that yield them.

``lookup_blueprint`` resolves a (case-insensitive) blueprint name; ``blueprint_catalog``
({name, category} rows) backs the planner's grouped picker.
"""

from __future__ import annotations

import time

from .config import BLUEPRINTS_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None, "data": {"blueprints": [], "fetched_at": None, "game_version": None},
          "by_name": {}}


def save_blueprints(blueprints: list, game_version: str | None = None,
                    path: str = BLUEPRINTS_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "count": len(blueprints),
        "blueprints": blueprints,
    })


def _parse(data: dict) -> dict:
    # First blueprint wins on a duplicate name (a few items share a name across tiers).
    by_name: dict[str, dict] = {}
    for b in data.get("blueprints", []):
        by_name.setdefault(b["name"].lower(), b)
    _cache["by_name"] = by_name
    return data


def load_blueprints(path: str = BLUEPRINTS_PATH) -> dict:
    """The full cache dict ({blueprints, game_version, ...}); empty until built."""
    return load_cached(path, _cache, _parse)


def blueprints_version(path: str = BLUEPRINTS_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_blueprints(path) or {}).get("game_version")


def blueprint_names(path: str = BLUEPRINTS_PATH) -> list:
    """Sorted distinct blueprint names (for the autocomplete)."""
    load_blueprints(path)
    return sorted({b["name"] for b in _cache["by_name"].values()})


def blueprint_catalog(path: str = BLUEPRINTS_PATH) -> list:
    """Sorted ``{name, category}`` rows -- backs the planner's grouped picker, which
    organises blueprints by category (type + size)."""
    load_blueprints(path)
    return sorted(({"name": b["name"], "category": b.get("category", "")}
                   for b in _cache["by_name"].values()), key=lambda r: r["name"].lower())


def lookup_blueprint(name: str, path: str = BLUEPRINTS_PATH) -> dict | None:
    """The blueprint for a name (case-insensitive exact match, else first name that
    contains the query), or None. Carries its requirements + minerals."""
    q = (name or "").strip().lower()
    if not q:
        return None
    load_blueprints(path)
    hit = _cache["by_name"].get(q)
    if hit:
        return hit
    for key, b in _cache["by_name"].items():
        if q in key:
            return b
    return None
