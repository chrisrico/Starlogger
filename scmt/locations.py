"""Local cache of station/location names mined from the game's ``Data.p4k``.

``global.ini`` maps the same location CODE shapes the log emits in
``RequestLocationInventory Location[...]`` (``RR_ARC_L1``, ``Stanton2_Orison``, …)
to display names. We persist that ``code -> name`` map plus a flat station list, so
the tracker can (a) resolve a player's current location precisely and (b) seed the
station-name autocomplete from authoritative game data instead of only what the logs
have happened to mention.

Built alongside the commodity map on a major version bump (see ``shipcargo``).
"""

from __future__ import annotations

import json
import os
import time

from .config import LOCATIONS_PATH
from . import scdata

_cache = {"mtime": None, "codes": {}, "names": [], "meta": {}}


def save_locations(codes: dict, game_version: str | None = None,
                   path: str = LOCATIONS_PATH) -> None:
    data = {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "count": len(codes),
        "codes": codes,
        "names": sorted(set(codes.values())),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _load(path: str = LOCATIONS_PATH) -> None:
    try:
        mt = os.stat(path).st_mtime
    except FileNotFoundError:
        return
    if _cache["mtime"] != mt:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _cache["codes"] = {k.lower(): v for k, v in data.get("codes", {}).items()}
            _cache["names"] = data.get("names") or sorted(set(_cache["codes"].values()))
            _cache["meta"] = {k: v for k, v in data.items() if k not in ("codes", "names")}
            _cache["mtime"] = mt
        except (OSError, json.JSONDecodeError):
            pass


def location_codes(path: str = LOCATIONS_PATH) -> dict:
    """{location_code(lower) -> station name}. Empty until the cache is built."""
    _load(path)
    return _cache["codes"]


def station_names(path: str = LOCATIONS_PATH) -> list:
    """Flat list of station names for the autocomplete catalog."""
    _load(path)
    return _cache["names"]


def resolve_code(code: str | None) -> str | None:
    """Station name for a log Location[...] code, or None if unknown."""
    if not code:
        return None
    return location_codes().get(code.lower())


def locations_version(path: str = LOCATIONS_PATH) -> str | None:
    _load(path)
    return _cache["meta"].get("game_version")
