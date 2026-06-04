"""Local cache mapping a commodity ``resourceGUID`` to its display name.

The trade log (manual buy/sell at a terminal) records only a commodity's
``resourceGUID``; its name lives in the game's ``ResourceTypeDatabase`` record,
read from the local ``Data.p4k`` via ``scdata`` (StarBreaker). Cheap to build (one
``dcb query``), so it's refreshed alongside the ship cargo on a major version bump.

Resolution is best-effort: an unknown GUID falls back to a short ``Commodity
xxxxxxxx`` label so a trade still renders before / without the map.
"""

from __future__ import annotations

import json
import os
import time

from .config import COMMODITIES_PATH
from . import scdata

_cache = {"mtime": None, "map": {}, "meta": {}}


def save_commodities(cmap: dict, game_version: str | None = None,
                     names: list | None = None, path: str = COMMODITIES_PATH) -> None:
    data = {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "count": len(cmap),
        "commodities": cmap,
        "names": sorted(names) if names else sorted(set(cmap.values())),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic: readers always see a complete file


def load_commodities(path: str = COMMODITIES_PATH) -> dict:
    """{guid(lower) -> name}. Empty if the cache file doesn't exist yet."""
    try:
        mt = os.stat(path).st_mtime
    except FileNotFoundError:
        return _cache["map"]
    if _cache["mtime"] != mt:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _cache["map"] = {k.lower(): v for k, v in data.get("commodities", {}).items()}
            _cache["meta"] = {k: v for k, v in data.items() if k != "commodities"}
            _cache["mtime"] = mt
        except (OSError, json.JSONDecodeError):
            pass
    return _cache["map"]


def resolve_commodity(guid: str | None, cmap: dict | None = None) -> str:
    """Display name for a resourceGUID; a short ``Commodity xxxxxxxx`` fallback when
    the map is missing or the GUID is unknown."""
    if not guid:
        return "Unknown commodity"
    cmap = load_commodities() if cmap is None else cmap
    return cmap.get(guid.lower()) or f"Commodity {guid[:8]}"


def commodity_names(path: str = COMMODITIES_PATH) -> list:
    """Clean trade-commodity display names for the cargo autocomplete."""
    load_commodities(path)
    return _cache["meta"].get("names") or sorted(set(_cache["map"].values()))


def commodities_version(path: str = COMMODITIES_PATH) -> str | None:
    load_commodities(path)
    return _cache["meta"].get("game_version")
