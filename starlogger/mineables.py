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

import time

from .config import MINEABLES_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None, "data": {"rocks": [], "fetched_at": None, "game_version": None}}


def save_mineables(rocks: list, game_version: str | None = None,
                   path: str = MINEABLES_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "count": len(rocks),
        "rocks": rocks,
    })


def load_mineables(path: str = MINEABLES_PATH) -> dict:
    """The full cache dict ({rocks, game_version, ...}); empty rocks until built."""
    return load_cached(path, _cache)


def mineables_version(path: str = MINEABLES_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_mineables(path) or {}).get("game_version")


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
    rocks = (load_mineables(path) or {}).get("rocks") or []
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
            } for r in group],
        })
    out.sort(key=lambda c: (c["residual"], c["count"]))
    return out
