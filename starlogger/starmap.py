"""Real Lagrange-point placement for the space-mineables catalog.

The static ``Data.p4k`` data names each asteroid-mining field only by its spawn archetype --
``HPP_Lagrange_A``..``G`` in Stanton, ``HPP_Pyro_*`` in Pyro -- and does NOT record which real
Lagrange point that archetype is placed at (the placement is engine-side at runtime). The
community starmap API (api.star-citizen.wiki) does: every named point (ARC-L4, CRU-L1, ...)
exposes the archetype that spawns there. We attach those points to each catalogued field,
GROUPED BY THEIR REAL PLANET, so "Lagrange E" surfaces as "Crusader L1/L2, Hurston L3" rather
than a bare archetype label -- the opaque archetype name (esp. Pyro's "Warm 01") never reaches
the UI.

ZERO network at build/runtime: the point map is captured once and committed as a static bundle
(``default_lagrange_points.json``); ``add_field_points`` just reads it. The live API is only
touched by a MAINTAINER refreshing that bundle (``python -m starlogger.starmap``) -- the
placements are stable across patches, and re-bundling is a deliberate, committed step. This keeps
the build offline-deterministic and spares the community API ~45 calls per install. Planet names
are resolved from the extract's localization at refresh time and baked into the bundle, so the
runtime needs neither the network nor the p4k extract to name a place.

Join key: the API's ``provider_names[0]`` IS the preset record token (e.g. ``HPP_Lagrange_F``),
so rendering it through the SAME ``_field_name`` the catalog builder uses yields the catalog
name -- we match on (system, field name). Re-using ``_field_name`` (rather than re-deriving the
mapping) is deliberate: if the two ever diverged the join would silently break. Pyro points use
``HPP_Pyro_*`` archetypes, not Lagrange ones; they enrich wherever the rendered names line up,
with no special-casing.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import config
from .jsonstore import atomic_write
from .scdata._p4k import load_localization
from .scdata._space_mineables import _field_name  # MUST match the catalog's naming (see above)


# --- runtime: read the committed bundle (no network) ------------------------ #
def _bundled(path: str = config.DEFAULT_LAGRANGE_POINTS_PATH) -> dict:
    """``{(system, field_name): [{planet, lpoints}, ...]}`` from the shipped bundle; ``{}`` if it's
    missing or unreadable (the fields then simply carry no points)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {(e["system"], e["name"]): e["points"]
            for e in data.get("fields") or [] if e.get("points")}


def add_field_points(fields: list, path: str = config.DEFAULT_LAGRANGE_POINTS_PATH) -> int:
    """Attach a ``points`` list -- the real Lagrange points grouped by planet,
    ``[{planet, lpoints}]`` -- to each catalogued field the bundle knows, matched on (system,
    name). Mutates ``fields`` in place; returns the count enriched. Reads only the static bundle."""
    pts = _bundled(path)
    if not pts:
        return 0
    n = 0
    for f in fields:
        match = pts.get((f.get("system"), f.get("name")))
        if match:
            f["points"] = list(match)
            n += 1
    return n


# --- maintainer: refresh the bundle from the live starmap API --------------- #
# Location detail; ?include=resources is REQUIRED -- the bare response only sets has_resources,
# the include carries the mining provider archetype + ores.
_LOC_URL = config.STARMAP_API.rstrip("/") + "/locations/{slug}?include=resources"

# The Lagrange points we query, by system. Stanton: 4 planets x L1-L5. Pyro: planets 1,2,3,5,6 x
# L1-L5 (Pyro has no planet 4). Slugs are arc-l1.., cru-l1.., pyr1-l1.., etc.
_SYSTEMS = {
    "Stanton": ("arc", "cru", "hur", "mic"),
    "Pyro": ("pyr1", "pyr2", "pyr3", "pyr5", "pyr6"),
}

# Point-code prefix -> planet display name. Stanton's are fixed brand names (mirrored in
# planner.py); Pyro's come from localization at refresh time (pyro1..6), falling back to these.
_PLANET = {
    "ARC": "ArcCorp", "CRU": "Crusader", "HUR": "Hurston", "MIC": "microTech",
    "PYR1": "Pyro I", "PYR2": "Monox", "PYR3": "Bloom",
    "PYR4": "Pyro IV", "PYR5": "Pyro V", "PYR6": "Terminus",
}


def _slugs() -> list:
    """(system, slug) for every Lagrange point we query."""
    return [(system, f"{p}-l{n}")
            for system, prefixes in _SYSTEMS.items()
            for p in prefixes for n in range(1, 6)]


def _fetch(slug: str, timeout: int = 30):
    """The location-detail JSON for one point, or None on any HTTP/parse failure (404 included).
    Injectable so the refresh can be tested without the network."""
    req = urllib.request.Request(
        _LOC_URL.format(slug=slug),
        headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


def _archetype(detail: dict) -> str | None:
    """The ship-mining spawn archetype at a point: the ``provider_names`` token of the
    "Ship Mining" resource group (e.g. "HPP_Lagrange_F"); None if the point has none (the
    other groups, e.g. "Salvage", are ignored)."""
    for group in (detail.get("data") or {}).get("resources") or []:
        if group.get("mining_type") != "Ship Mining":
            continue
        for res in group.get("resources") or []:
            names = res.get("provider_names") or []
            if names:
                return names[0]
    return None


def field_points(fetch=_fetch):
    """``({(system, field_name): [code, ...]}, api_version)`` -- the real Lagrange point codes
    each field spawns at, fetched live from the starmap API (~45 calls). Points that 404 or carry
    no ship-mining archetype are skipped. ``api_version`` is the patch the API built its data from
    (``meta.resource.version``), or None. An empty map means every fetch failed."""
    out: dict = {}
    api_version = None
    for system, slug in _slugs():
        detail = fetch(slug)
        if not detail:
            continue
        api_version = api_version or (
            (detail.get("meta") or {}).get("resource") or {}).get("version")
        token = _archetype(detail)
        if not token:
            continue
        out.setdefault((system, _field_name(token, {})), []).append(slug.upper())
    for pts in out.values():
        pts.sort()
    return out, api_version


def _planet_name(prefix: str, loc: dict) -> str:
    """Planet a point-code prefix belongs to. Pyro names are read from localization (so a future
    rename is picked up on the next refresh); Stanton brand names + any gap use the static map."""
    m = re.fullmatch(r"PYR(\d)", prefix)
    if m:
        return (loc.get(f"pyro{m.group(1)}") or "").strip() or _PLANET.get(prefix, prefix)
    return _PLANET.get(prefix, prefix)


def _group_by_planet(codes: list, loc: dict) -> list:
    """Flat point codes -> ``[{planet, lpoints}]`` grouped by real planet, planet order by first
    appearance (codes arrive sorted): ["CRU-L1","CRU-L2","HUR-L3"] ->
    [{"planet":"Crusader","lpoints":["L1","L2"]}, {"planet":"Hurston","lpoints":["L3"]}]."""
    groups: dict = {}
    order: list = []
    for c in codes:
        prefix, _, lp = c.partition("-")          # "CRU-L1" -> "CRU","L1"; "PYR1-L1" -> "PYR1","L1"
        planet = _planet_name(prefix, loc)
        if planet not in groups:
            groups[planet] = []
            order.append(planet)
        groups[planet].append(lp)
    return [{"planet": p, "lpoints": groups[p]} for p in order]


def _localization() -> dict:
    """The extract's localization for planet-name resolution, or {} if the local p4k records
    aren't present (maintainer ran outside the repo) -- the static map then covers it."""
    try:
        return load_localization(os.path.join(config.BASE_DIR, "p4k", "records"))
    except Exception:
        return {}


def refresh_bundle(path: str = config.DEFAULT_LAGRANGE_POINTS_PATH, fetch=_fetch,
                   stamp: str | None = None, loc: dict | None = None) -> int:
    """Rebuild the committed bundle from the live API and write it (planet-grouped, sorted, for
    clean diffs). Returns the number of fields written; raises on a total fetch failure (nothing
    to write). Planet names come from the extract's localization (``loc``); when None it's loaded
    from the local p4k records, falling back to the static map. ``stamp`` fixes the timestamp."""
    pts, api_version = field_points(fetch=fetch)
    if not pts:
        raise RuntimeError("starmap API returned no points (network down?) -- bundle unchanged")
    if loc is None:
        loc = _localization()
    fields = [{"system": s, "name": n, "points": _group_by_planet(codes, loc)}
              for (s, n), codes in sorted(pts.items())]
    atomic_write(path, {
        "source": config.STARMAP_API + "/locations",
        "generated_at": stamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": api_version,
        "fields": fields,
    })
    return len(fields)


if __name__ == "__main__":   # maintainer: refresh the shipped bundle, then commit the result
    count = refresh_bundle()
    print(f"[starmap] wrote {count} fields to {config.DEFAULT_LAGRANGE_POINTS_PATH}")
