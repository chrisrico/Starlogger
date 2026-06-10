"""Local cache of salvageable-wreck radar signatures, in salvageables.json.

The salvage counterpart to ``mineables.py`` -- but far thinner, because a salvageable
carries no mineral composition: its RS only **identifies a target**. A whole-ship hull's
RS names the ship (Avenger Titan 1700 ... 890 Jump 3000); ship-debris panels all read a
flat 2000, so an ``n x 2000`` reading means *n panels*. There's no yield/SCU data to mine
(see ``scdata._salvageables``), so this exists only to answer "what is that contact?" --
surfaced as a separate section in the rock-lookup Identify tab.

``salvage_lookup`` is the reverse map the API exposes (parallel to ``mineables.lookup_rs``):
an observed RS reading -> candidate salvage targets and the inferred count.
"""

from __future__ import annotations

import time

from .config import SALVAGEABLES_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None, "data": {"wrecks": [], "fetched_at": None, "game_version": None}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild the cache on update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
EXTRACT_VERSION = 0


def save_salvageables(wrecks: list, game_version: str | None = None,
                      path: str = SALVAGEABLES_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "count": len(wrecks),
        "wrecks": wrecks,
    })


def load_salvageables(path: str = SALVAGEABLES_PATH) -> dict:
    """The full cache dict ({wrecks, game_version, ...}); empty wrecks until built."""
    return load_cached(path, _cache)


def catalog(path: str = SALVAGEABLES_PATH) -> list:
    """The salvageable-wreck list (each {class, name, rs, kind, ...}); empty until built."""
    return (load_salvageables(path) or {}).get("wrecks") or []


def salvageables_version(path: str = SALVAGEABLES_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_salvageables(path) or {}).get("game_version")


def salvageables_extract_version(path: str = SALVAGEABLES_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_salvageables(path) or {}).get("extract_version") or 0)


def _members(group: list, limit: int = 12) -> list:
    """The distinct named targets behind one (base, kind) salvage candidate, for display."""
    out, seen = [], set()
    for w in group:
        key = w.get("ship") or w["name"]
        if key in seen:
            continue
        seen.add(key)
        out.append({"class": w["class"], "name": w["name"], "ship": w.get("ship"),
                    "size": w.get("size"), "part": w.get("part")})
        if len(out) >= limit:
            break
    return out


def salvage_lookup(rs_value: float, tol: float = 0.5, max_count: int = 500,
                   path: str = SALVAGEABLES_PATH) -> list:
    """Candidate salvage targets for an observed radar RS reading -- the salvage parallel of
    ``mineables.lookup_rs``.

    Base signatures are integers and a homogeneous cluster reads ``base x count`` exactly,
    so for each distinct base RS ``b`` the count is ``round(rs_value/b)`` and ``b`` matches
    only when that reproduces the reading within ``tol`` absolute units. Candidates are keyed
    by ``(base_rs, kind)`` -- so a 2000 reading yields BOTH the Redeemer hull (kind 'ship')
    and a debris panel (kind 'panel'), which share that base. Returns ``[{base_rs, count,
    residual, kind, label, targets: [...]}]`` sorted best-fit first (smallest residual, then
    fewest targets), ``targets`` naming the wrecks that read at that base."""
    try:
        rs_value = float(rs_value)
    except (TypeError, ValueError):
        return []
    if rs_value <= 0:
        return []
    groups: dict[tuple, list] = {}
    for w in catalog(path):
        groups.setdefault((int(w["rs"]), w["kind"]), []).append(w)

    out = []
    for (base, kind), group in groups.items():
        count = round(rs_value / base)
        if count < 1 or count > max_count:
            continue
        residual = abs(rs_value - base * count)
        if residual > tol:
            continue
        if kind == "panel":
            label = f"{count} ship-debris panel{'s' if count != 1 else ''}"
        else:
            ships = sorted({w.get("ship") or w["name"] for w in group})
            head = ships[0] + (f" +{len(ships) - 1} more" if len(ships) > 1 else "")
            label = f"{count} × {head}" if count > 1 else f"{head} hull"
        out.append({
            "base_rs": base, "count": count, "residual": round(residual, 2),
            "kind": kind, "label": label, "targets": _members(group),
        })
    out.sort(key=lambda c: (c["residual"], len(c["targets"]), c["base_rs"]))
    return out


def salvage_signatures(path: str = SALVAGEABLES_PATH) -> list:
    """Sorted distinct base RS values across the catalog (for the Identify prediction)."""
    return sorted({w["rs"] for w in catalog(path) if w.get("rs")})
