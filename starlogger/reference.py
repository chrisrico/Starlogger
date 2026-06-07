"""Local cache of p4k-derived reference data, in reference.json.

Two maps the tracker resolves names against, both mined from the game's own
``Data.p4k`` (via ``scdata`` / StarBreaker):

  * **commodities** -- ``resourceGUID -> display name``. A manual terminal trade
    logs only the GUID; its name lives in ``ResourceTypeDatabase``.
  * **locations** -- location ``code -> station name`` (the ``RR_ARC_L1`` /
    ``Stanton2_Orison`` shapes the log emits in ``RequestLocationInventory``), from
    ``global.ini``. Resolves the player's current location and seeds the
    station-name autocomplete from authoritative game data.

Both fall out of a *single* ``scdata.build_reference_data()`` extraction (one
``global.ini`` extract + one ``dcb query``), are gated together on a major
game-version bump, and are read as a unit -- so they share one file and one
version stamp. (They were once commodities.json + locations.json; folded together
because they're always written and wiped together. The heavier ship-cargo
extraction stays in its own ships.json -- different trigger and cost.)

Resolution is best-effort: an unknown commodity GUID falls back to a short
``Commodity xxxxxxxx`` label so a trade still renders before / without the map.
"""

from __future__ import annotations

import time

from .config import REFERENCE_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None, "data": None,
          "commodities": {}, "commodity_names": [], "codes": {}, "station_names": [],
          "commodity_types": {}, "categories": []}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild the cache on update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
EXTRACT_VERSION = 0


def save_reference(commodities: dict, location_codes: dict,
                   commodity_names: list | None = None, station_names: list | None = None,
                   commodity_types: dict | None = None,
                   game_version: str | None = None, path: str = REFERENCE_PATH) -> None:
    types = commodity_types or {}
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "commodities": commodities,
        "commodity_names": sorted(commodity_names) if commodity_names else sorted(set(commodities.values())),
        "codes": location_codes,
        "station_names": sorted(station_names) if station_names else sorted(set(location_codes.values())),
        "commodity_types": types,
        "categories": sorted(set(types.values())),
    })


def _parse(data: dict) -> dict:
    # Derive the lowercased lookup maps + flat name lists once per (re)load, so the
    # hot accessors (load_commodities on every archive read, resolve_code per leg)
    # are plain dict returns. Stored back onto _cache via the parse side-effect.
    _cache["commodities"] = {k.lower(): v for k, v in (data.get("commodities") or {}).items()}
    _cache["commodity_names"] = data.get("commodity_names") or sorted(set(_cache["commodities"].values()))
    _cache["codes"] = {k.lower(): v for k, v in (data.get("codes") or {}).items()}
    _cache["station_names"] = data.get("station_names") or sorted(set(_cache["codes"].values()))
    _cache["commodity_types"] = {k.lower(): v for k, v in (data.get("commodity_types") or {}).items()}
    _cache["categories"] = data.get("categories") or sorted(set(_cache["commodity_types"].values()))
    return data


def _load(path: str = REFERENCE_PATH) -> None:
    load_cached(path, _cache, _parse)


def load_commodities(path: str = REFERENCE_PATH) -> dict:
    """{guid(lower) -> name}. Empty if the cache file doesn't exist yet."""
    _load(path)
    return _cache["commodities"]


def resolve_commodity(guid: str | None, cmap: dict | None = None) -> str:
    """Display name for a resourceGUID; a short ``Commodity xxxxxxxx`` fallback when
    the map is missing or the GUID is unknown."""
    if not guid:
        return "Unknown commodity"
    cmap = load_commodities() if cmap is None else cmap
    return cmap.get(guid.lower()) or f"Commodity {guid[:8]}"


def commodity_names(path: str = REFERENCE_PATH) -> list:
    """Clean trade-commodity display names for the cargo autocomplete."""
    _load(path)
    return _cache["commodity_names"]


def commodity_types(path: str = REFERENCE_PATH) -> dict:
    """{guid(lower) -> category} (Metal, Gas, Mineral, …). Empty until the cache is
    built with the category taxonomy (T1). Lets the UI group/colour commodities."""
    _load(path)
    return _cache["commodity_types"]


def commodity_categories(path: str = REFERENCE_PATH) -> list:
    """Sorted distinct commodity categories; empty until the taxonomy is built."""
    _load(path)
    return _cache["categories"]


def commodities_version(path: str = REFERENCE_PATH) -> str | None:
    """Game version the reference data was built for -- gates the rebuild (covers
    both commodities and locations, since they're built together)."""
    data = load_cached(path, _cache, _parse)
    return (data or {}).get("game_version")


def reference_extract_version(path: str = REFERENCE_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    data = load_cached(path, _cache, _parse)
    return int((data or {}).get("extract_version") or 0)


def location_codes(path: str = REFERENCE_PATH) -> dict:
    """{location_code(lower) -> station name}. Empty until the cache is built."""
    _load(path)
    return _cache["codes"]


def station_names(path: str = REFERENCE_PATH) -> list:
    """Flat list of station names for the autocomplete catalog."""
    _load(path)
    return _cache["station_names"]


def resolve_code(code: str | None) -> str | None:
    """Station name for a log Location[...] code, or None if unknown."""
    if not code:
        return None
    return location_codes().get(code.lower())
