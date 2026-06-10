"""Local cache of mineable-rock RS (radar signature) + composition, in mineables.json.

When a miner looks at a rock or asteroid cluster on the radar, the game shows a
single **RS** number. Each rock class has a fixed *base* RS, and a cluster reads
``base x number_of_rocks`` -- so the number identifies the rock **class** and (given
the base) implies the **cluster size**. Each class also has an authored, probabilistic
mineral **composition**. All of this is mined from the game's own ``Data.p4k`` via
``scdata.build_mineables`` (which reads the full DataCore extract -- the RS value lives
on entity records that can't be pulled cheaply, so this rides the ship-cargo extract and
gets its own file/trigger, unlike the cheap-query reference.json).

``lookup_rs`` is the reverse map the API exposes: an observed RS value -> candidate
rock class(es), the inferred cluster size, and each class's makeup. RS is class-level
(many minerals in a class share one base RS) and makeup is a range, so a lookup yields
*candidates*, not a single exact answer -- mirroring how the in-game radar behaves.
"""

from __future__ import annotations

import re
import time

from .config import MINEABLES_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None, "data": {"rocks": [], "fetched_at": None, "game_version": None}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild the cache on update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
EXTRACT_VERSION = 2   # v2: drop ground-vehicle (ROC) + FPS gem rocks — ship-mining catalog only
# v1: mechanics.resistance/instability/window now per-material, not the shared curve


def save_mineables(rocks: list, game_version: str | None = None,
                   path: str = MINEABLES_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "count": len(rocks),
        "rocks": rocks,
    })


def load_mineables(path: str = MINEABLES_PATH) -> dict:
    """The full cache dict ({rocks, game_version, ...}); empty rocks until built."""
    return load_cached(path, _cache)


def catalog(path: str = MINEABLES_PATH) -> list:
    """The mineable-rock list (each {class, name, deposit_name, rs, min_distinct,
    composition}); empty until the cache is built."""
    return (load_mineables(path) or {}).get("rocks") or []


def mineables_version(path: str = MINEABLES_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_mineables(path) or {}).get("game_version")


def mineables_extract_version(path: str = MINEABLES_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_mineables(path) or {}).get("extract_version") or 0)


def rock_signatures(path: str = MINEABLES_PATH) -> list:
    """Sorted distinct base RS values across the catalog -- seeds the Identify input's
    inline prediction (a single rock reads its base; a cluster reads base x count)."""
    return sorted({r["rs"] for r in catalog(path) if r.get("rs")})


def lookup_rs(rs_value: float, tol: float = 0.5, max_count: int = 500,
              path: str = MINEABLES_PATH) -> list:
    """Candidate rock classes for an observed HUD RS reading.

    Base signatures are integers and a homogeneous cluster reads ``base x count``
    exactly, so for each distinct base RS ``b`` the cluster size is ``round(rs_value/b)``
    and the base matches only when that reproduces the reading within ``tol`` *absolute*
    units (small by default, since distinct bases can sit only ~15 apart -- a relative
    tolerance would conflate them). Rock classes sharing a base are grouped (RS can't tell
    minerals of one class apart). Returns ``[{base_rs, count, residual, rocks: [{class,
    name, deposit_name, min_distinct, composition}]}]`` sorted best-fit first (smallest
    residual, then fewest rocks)."""
    try:
        rs_value = float(rs_value)
    except (TypeError, ValueError):
        return []
    if rs_value <= 0:
        return []
    rocks = catalog(path)
    by_base: dict[int, list] = {}
    for r in rocks:
        by_base.setdefault(int(r["rs"]), []).append(r)

    out = []
    for base, group in by_base.items():
        count = round(rs_value / base)
        if count < 1 or count > max_count:
            continue
        residual = abs(rs_value - base * count)
        if residual > tol:
            continue
        out.append({
            "base_rs": base,
            "count": count,
            "residual": round(residual, 2),
            "rocks": [{
                "class": r["class"],
                "name": r["name"],
                "deposit_name": r["deposit_name"],
                "min_distinct": r["min_distinct"],
                "composition": r["composition"],
                "mechanics": r.get("mechanics"),
            } for r in group],
        })
    out.sort(key=lambda c: (c["residual"], c["count"]))
    return out


# Mineral-name reconciliation. The same mineral is spelled differently across the
# game's datasets: mineable elements ("Aluminium Ore", "Quantainium Raw"), blueprint
# resources ("Aluminum", "Quantanium"), deposits ("Pressurized Ice"). Normalise to a
# bare key (drop spaces/case, the Ore/Raw/Deposit suffix, and known spelling variants)
# so a mineral matches across all of them.
_MINERAL_ALIASES = {"aluminum": "aluminium", "quantanium": "quantainium"}


def _mineral_key(name: str) -> str:
    k = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    for suf in ("ore", "raw", "deposit"):
        if k.endswith(suf) and len(k) > len(suf):
            k = k[:-len(suf)]
    return _MINERAL_ALIASES.get(k, k)


def _mineral_matches(query: str, element: str) -> bool:
    """True if `query` names the same mineral as `element` (spelling-tolerant; allows a
    partial like 'gold' to match 'Gold Ore')."""
    qk, ek = _mineral_key(query), _mineral_key(element)
    return bool(qk) and (qk in ek or ek in qk)


def _yield_score(part: dict) -> float:
    """A rough "how good a source is this" score for one mineral in a rock: spawn
    probability x the midpoint of its percentage range. Used to rank sources."""
    p = part.get("probability") or 0
    lo = part.get("min_pct") or 0
    hi = part.get("max_pct") or 0
    return round(p * (lo + hi) / 2.0, 1)


def all_minerals(path: str = MINEABLES_PATH) -> list:
    """Sorted distinct mineral-element names across every rock (for autocomplete)."""
    names = {e["element"] for r in catalog(path) for e in r["composition"] if e.get("element")}
    return sorted(names)


def _source_row(rock: dict, part: dict) -> dict:
    """One 'rock X yields mineral Y' row: where to find it (rs/name) + how richly. Carries
    the rock's ``mechanics`` so the Find tab can rank/badge sources by minability with the
    player's current mining gear (see web/feasibility.js)."""
    return {
        "class": rock["class"], "name": rock["name"], "deposit_name": rock["deposit_name"],
        "rs": rock["rs"], "element": part.get("element"),
        "min_pct": part.get("min_pct"), "max_pct": part.get("max_pct"),
        "probability": part.get("probability"), "score": _yield_score(part),
        "mechanics": rock.get("mechanics"),
    }


def lookup_mineral(name: str, path: str = MINEABLES_PATH) -> dict:
    """Forward lookup: a mineral -> the rocks that yield it and the RS value(s) to hunt
    for. Case-insensitive substring match on the element name. Returns
    ``{mineral, signatures: [rs...], rocks: [source-row...]}`` with rocks ranked by yield
    score (richest source first), so you know both *what number* to scan for and *which
    rock* is the best source."""
    if not (name or "").strip():
        return {"mineral": name, "signatures": [], "rocks": []}
    rows, sigs = [], set()
    for r in catalog(path):
        for e in r["composition"]:
            if _mineral_matches(name, e.get("element")):
                rows.append(_source_row(r, e))
                sigs.add(r["rs"])
    rows.sort(key=lambda x: (-x["score"], x["rs"], x["deposit_name"]))
    return {"mineral": name, "signatures": sorted(sigs), "rocks": rows}


def mineral_index(path: str = MINEABLES_PATH) -> list:
    """Every mineral -> the rocks that contain it: ``[{mineral, count, signatures,
    rocks: [source-row...]}]`` sorted by mineral name (rocks ranked by yield score). The
    full reverse map from ingredient to where it's mined."""
    idx: dict[str, list] = {}
    for r in catalog(path):
        for e in r["composition"]:
            el = e.get("element")
            if el:
                idx.setdefault(el, []).append(_source_row(r, e))
    out = []
    for mineral, rows in idx.items():
        rows.sort(key=lambda x: (-x["score"], x["rs"]))
        out.append({"mineral": mineral, "count": len(rows),
                    "signatures": sorted({x["rs"] for x in rows}), "rocks": rows})
    out.sort(key=lambda x: x["mineral"])
    return out


def _combo(parts: list, value: float, by_base: dict) -> dict:
    """Package a list of (base_rs, count) tuples into a decompose result, attaching the
    deposit names that read at each base so the cluster can be identified."""
    total = sum(b * c for b, c in parts)
    return {
        "count": sum(c for _, c in parts),
        "total": total,
        "residual": round(abs(value - total), 2),
        "parts": [{"base_rs": b, "count": c, "names": sorted(by_base.get(b, []))[:8]}
                  for b, c in sorted(parts)],
    }


def decompose_rs(value: float, tol: float = 0.5, max_rocks: int = 8,
                 max_results: int = 40, path: str = MINEABLES_PATH) -> list:
    """Break an observed RS reading into plausible clusters: homogeneous (one rock class
    x count) and two-class mixes whose base RS values sum to the reading within ``tol``.
    Because the ship-mining asteroid bases sit close together, a single reading can decode
    several ways -- this enumerates them, capped to two distinct classes and ``max_rocks``
    total (3+ class mixes explode and aren't actionable). Sorted fewest-residual, then
    fewest rocks. ``parts`` carries the deposit names reading at each base."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return []
    if value <= 0:
        return []
    by_base: dict[int, set] = {}
    for r in catalog(path):
        by_base.setdefault(int(r["rs"]), set()).add(r["deposit_name"] or r["name"])
    bases = sorted(by_base)
    combos, seen = [], set()
    n = len(bases)
    for i in range(n):
        b1 = bases[i]
        for c1 in range(1, min(max_rocks, int((value + tol) // b1)) + 1):
            rem = value - b1 * c1
            if abs(rem) <= tol:                       # homogeneous
                key = ((b1, c1),)
                if key not in seen:
                    seen.add(key)
                    combos.append(_combo([(b1, c1)], value, by_base))
            if rem <= tol:
                continue
            for j in range(i + 1, n):                 # + a second, distinct class
                b2 = bases[j]
                c2 = round(rem / b2)
                if c2 < 1 or c1 + c2 > max_rocks:
                    continue
                if abs(value - (b1 * c1 + b2 * c2)) <= tol:
                    key = tuple(sorted([(b1, c1), (b2, c2)]))
                    if key not in seen:
                        seen.add(key)
                        combos.append(_combo([(b1, c1), (b2, c2)], value, by_base))
    combos.sort(key=lambda c: (c["residual"], c["count"], len(c["parts"])))
    return combos[:max_results]


def mining_plan(minerals: list, path: str = MINEABLES_PATH) -> dict:
    """Turn a list of wanted minerals (e.g. a blueprint's ingredients) into a mining plan:
    per mineral, the best source rocks + RS to scan for; and a *coverage* ranking of rock
    deposit types by how many of the wanted minerals each can yield (so one stop can cover
    several ingredients). Returns ``{targets, per_mineral, coverage}``."""
    targets = [m.strip() for m in (minerals or []) if m and m.strip()]
    per_mineral = []
    for m in targets:
        res = lookup_mineral(m, path)
        per_mineral.append({"mineral": m, "signatures": res["signatures"],
                            "rocks": res["rocks"][:6]})

    cov: dict[str, dict] = {}
    for r in catalog(path):
        deposit = r["deposit_name"] or r["name"]
        for e in r["composition"]:
            for t in targets:
                if _mineral_matches(t, e.get("element")):
                    d = cov.setdefault(deposit, {"covers": set(), "rs": set(), "score": 0.0})
                    d["covers"].add(t)
                    d["rs"].add(r["rs"])
                    d["score"] += _yield_score(e)
    coverage = [{"deposit": dep, "covers": sorted(v["covers"]), "n_covers": len(v["covers"]),
                 "signatures": sorted(v["rs"]), "score": round(v["score"], 1)}
                for dep, v in cov.items()]
    coverage.sort(key=lambda x: (-x["n_covers"], -x["score"]))
    return {"targets": targets, "per_mineral": per_mineral, "coverage": coverage}
