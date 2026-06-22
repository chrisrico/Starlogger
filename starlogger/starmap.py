"""Real Lagrange-point placement for the space-mineables catalog, from the Star Citizen
Wiki starmap API (api.star-citizen.wiki).

The static ``Data.p4k`` data names each asteroid-mining field only by its spawn archetype --
``HPP_Lagrange_A``..``G`` in Stanton, ``HPP_Pyro_*`` in Pyro -- and does NOT record which real
Lagrange point that archetype is placed at (the placement is engine-side at runtime). The
community starmap API does: every named point (ARC-L4, CRU-L1, PYR1-L1, ...) exposes the
archetype that spawns there. We fetch the points once per game-version build (see
``catalogs._space_mineables``) and attach the real point list to each catalogued field, so
"Lagrange E" surfaces as "CRU-L1, CRU-L2, HUR-L3" rather than a bare archetype label.

Join key: the API's ``provider_names[0]`` IS the preset record token (e.g. ``HPP_Lagrange_F``),
so rendering it through the SAME ``_field_name`` the catalog builder uses yields the catalog
name -- we match on (system, field name). Re-using ``_field_name`` (rather than re-deriving the
mapping) is deliberate: if the two ever diverged the join would silently break. Pyro points use
``HPP_Pyro_*`` archetypes, not Lagrange ones; they enrich wherever the rendered names line up,
with no special-casing.

Best-effort and offline-tolerant: any network/parse failure leaves the fields unenriched (like
the StarStrings fetch in ``ignition``). Stdlib ``urllib`` only -- no new dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config
from .scdata._space_mineables import _field_name  # MUST match the catalog's naming (see above)

# Location detail; ?include=resources is REQUIRED -- the bare response only sets has_resources,
# the include carries the mining provider archetype + ores.
_LOC_URL = config.STARMAP_API.rstrip("/") + "/locations/{slug}?include=resources"

# The Lagrange points we query, by system. Stanton: 4 planets x L1-L5. Pyro: planets 1,2,3,5,6 x
# L1-L5 (Pyro has no planet 4). Slugs are arc-l1.., cru-l1.., pyr1-l1.., etc.
_SYSTEMS = {
    "Stanton": ("arc", "cru", "hur", "mic"),
    "Pyro": ("pyr1", "pyr2", "pyr3", "pyr5", "pyr6"),
}


def _slugs() -> list:
    """(system, slug) for every Lagrange point we query."""
    return [(system, f"{p}-l{n}")
            for system, prefixes in _SYSTEMS.items()
            for p in prefixes for n in range(1, 6)]


def _fetch(slug: str, timeout: int = 30):
    """The location-detail JSON for one point, or None on any HTTP/parse failure (404 included).
    Injectable so the merge can be tested without the network."""
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


def field_points(fetch=_fetch) -> dict:
    """``{(system, field_name): [point, ...]}`` -- the real Lagrange points each catalogued
    field spawns at, from the starmap API. Points that 404 or carry no ship-mining archetype
    are skipped; an empty dict means the whole fetch failed (treated as "no enrichment")."""
    out: dict = {}
    for system, slug in _slugs():
        detail = fetch(slug)
        if not detail:
            continue
        token = _archetype(detail)
        if not token:
            continue
        out.setdefault((system, _field_name(token, {})), []).append(slug.upper())
    for pts in out.values():
        pts.sort()
    return out


def add_field_points(fields: list, fetch=_fetch) -> int:
    """Attach a sorted ``points`` list of real Lagrange points (["CRU-L1", ...]) to each field
    the starmap knows, matched on (system, name). Mutates ``fields`` in place; returns the count
    enriched. Best-effort: a failed/empty fetch leaves every field unchanged."""
    pts = field_points(fetch=fetch)
    if not pts:
        return 0
    n = 0
    for f in fields:
        match = pts.get((f.get("system"), f.get("name")))
        if match:
            f["points"] = match
            n += 1
    return n
