"""Local cache of crafting blueprints + their material requirements, in blueprints.json.

Built from the game's ``Data.p4k`` (via ``scdata.build_blueprints``) on the same full
DataCore extract / major-version trigger as the mineables catalog -- its own file, like
ships.json / mineables.json. Each blueprint records what it crafts and the flat
material list its recipe needs: ``{slot, resource, scu, min_quality}`` per ingredient,
plus a ``minerals`` shortcut. That feeds the Mining tab's blueprint planner: pick a
blueprint -> its required minerals -> the rocks (and RS values) that yield them.

``lookup_blueprint`` resolves a (case-insensitive) blueprint name; ``blueprint_catalog``
({name, category} rows) backs the planner's grouped picker.
"""

from __future__ import annotations

import re
import time

from .config import BLUEPRINTS_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None, "data": {"blueprints": [], "fetched_at": None, "game_version": None},
          "by_name": {}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild the cache on update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
EXTRACT_VERSION = 3  # v3: `sources` is now structured [{faction, contracts:[titles]}] (was a
                     # flat [label] list); v2 added size/class/grade for all items
                     # (Type/Subtype/Class/Quality/Size columns); v1 added `sources`


def save_blueprints(blueprints: list, game_version: str | None = None,
                    path: str = BLUEPRINTS_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
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


def blueprints_extract_version(path: str = BLUEPRINTS_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_blueprints(path) or {}).get("extract_version") or 0)


def blueprint_names(path: str = BLUEPRINTS_PATH) -> list:
    """Sorted distinct blueprint names (for the autocomplete)."""
    load_blueprints(path)
    return sorted({b["name"] for b in _cache["by_name"].values()})


# ---- picker grouping: tag each blueprint with its section (type + detail) + size ---- #
# The planner's picker lays blueprints out as sticky-header sections. We derive, per
# blueprint: a main {type}, a {detail} sub-label (component size, weapon model line, FPS
# weapon type, or armour set), and the numeric {size} (for ordering weapons within a line).
# Vehicle-component subtype, matched as a substring of `crafts` (a few share a kind across
# manufacturer prefixes, e.g. wep_/grin_ tractor beams, fuel_/nozzle_ fuel nozzles).
_VC_RULES = (("shld", "Shield"), ("powr", "Power Plant"), ("cool", "Cooler"), ("radr", "Radar"),
             ("qdrv", "Quantum Drive"), ("tractorbeam", "Tractor Beam"), ("mining", "Mining Laser"),
             ("salvage", "Salvage Module"), ("nozzle", "Fuel Nozzle"), ("fuel", "Fuel Nozzle"))
_VWEP_KINDS = (("massdriver", "Mass Driver"), ("scattergun", "Scattergun"),
               ("gatling", "Gatling"), ("repeater", "Repeater"), ("cannon", "Cannon"))
_FPSWEP = {"smg": "SMG", "lmg": "LMG", "sniper": "Sniper Rifle", "rifle": "Rifle",
           "pistol": "Pistol", "shotgun": "Shotgun", "crossbow": "Crossbow"}
_ARMOR_PIECES = {"helmet", "arms", "core", "legs", "torso", "undersuit", "backpack", "pants",
                 "jacket", "gloves", "boots", "hat", "cap", "mask", "suit", "flightsuit",
                 "armor", "armour"}
_LINE_FILLER = {"mark", "series", "model", "mod", "mk"}


def _size_num(category: str):
    last = (category or "").split()[-1] if category else ""
    return int(last[1:]) if len(last) > 1 and last[0] == "S" and last[1:].isdigit() else None


def _vc_subtype(crafts: str) -> str:
    for key, label in _VC_RULES:
        if key in crafts:
            return label
    return (crafts.split("_")[0] or "Other").title()


def _vweapon_kind(crafts: str) -> str:
    for key, label in _VWEP_KINDS:
        if key in crafts:
            return label
    return "Weapon"


def _line_label(names: list, kind: str) -> str:
    """A model-line label for a weapon group: the common leading words (minus filler/kind
    words), else the common leading characters, else the weapon kind."""
    splits = [n.split() for n in names]
    pre: list = []
    for i in range(min(len(s) for s in splits)):
        w = splits[0][i]
        if all(s[i] == w for s in splits):
            pre.append(w)
        else:
            break
    while pre and (pre[-1].lower() in _LINE_FILLER or pre[-1].lower() == kind.split()[-1].lower()):
        pre.pop()
    lab = " ".join(pre).strip(" -“”\"")
    if len(re.sub(r"[^A-Za-z]", "", lab)) >= 3:
        return lab
    a, b = min(names), max(names)
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    c = re.sub(r"[\s\-0-9]+$", "", names[0][:i])
    return c if len(c) >= 2 else kind


def _armor_set(name: str) -> str:
    toks = (name or "").split()
    for i, t in enumerate(toks):
        if t.strip("'\".,").lower() in _ARMOR_PIECES:
            return " ".join(toks[:i]) or t
    return (name or "").strip()


def _keep_component(b: dict) -> bool:
    """Vehicle components are shown Grade A only. Each blueprint carries the crafted item's
    ``grade`` letter (from the p4k build); a missing grade is kept defensively (e.g. an item
    whose record had no AttachDef)."""
    g = b.get("grade")
    return g is None or str(g).strip().upper() in ("A", "GRADE A")


def _armor_piece(name: str) -> str:
    """The armour piece kind (Helmet/Torso/Arms/Legs/Backpack/Undersuit/…) from its name."""
    for t in (name or "").split():
        w = t.strip("'\".,").lower()
        if w in _ARMOR_PIECES:
            return w.title()
    return ""


def blueprint_catalog(path: str = BLUEPRINTS_PATH) -> list:
    """Table rows ``{name, type, subtype, cls, quality, size}`` -- one per blueprint, tagged for
    the planner table's columns and per-column filters. All grades are included (Quality is a
    column); ``cls``/``quality``/``size`` are blank when the crafted item doesn't declare them."""
    load_blueprints(path)
    rows = []
    for b in _cache["by_name"].values():
        cat = b.get("category", "") or ""
        crafts = b.get("crafts", "") or ""
        if cat.startswith("Vehicle Component"):
            typ, sub = "Vehicle Component", _vc_subtype(crafts)
        elif cat.startswith("Vehicle Weapons"):
            typ, sub = "Vehicle Weapons", _vweapon_kind(crafts)
        elif cat == "FPS Weapons":
            toks = crafts.split("_")
            tok = toks[1] if len(toks) > 1 else ""
            typ, sub = "FPS Weapons", _FPSWEP.get(tok, tok.title() or "")
        elif cat == "FPS Armours":
            typ, sub = "FPS Armours", _armor_piece(b.get("name", ""))
        else:
            typ, sub = cat or "Other", ""
        size = b.get("size")
        rows.append({
            "name": b["name"],
            "type": typ,
            "subtype": sub,
            "cls": b.get("cls") or "",
            "quality": b.get("grade") or "",
            "size": size if size is not None else _size_num(cat),
        })
    return sorted(rows, key=lambda r: r["name"].lower())


# Vehicle-component `crafts` prefixes the shipbuilder fills a ship's slots from (power plant /
# cooler / shield / quantum drive / radar) -- the first `_`-token of `crafts` is the component
# kind for these five (manufacturer-prefixed kinds like tractor beams don't apply here).
_COMPONENT_PREFIXES = frozenset(("powr", "cool", "shld", "qdrv", "radr"))


def component_blueprints(prefix: str, size: int, path: str = BLUEPRINTS_PATH) -> dict:
    """Grade-A blueprints that craft a ``prefix`` (powr/cool/shld/qdrv/radr) component of the
    given ``size``, grouped by component class ``{cls: [blueprint, ...]}`` with each group sorted
    by name. The supply the shipbuilder picks a ship's slot from -- Grade A only (the only tier
    worth crafting), classless blueprints dropped (can't be matched to a chosen class)."""
    prefix = (prefix or "").lower()
    load_blueprints(path)
    out: dict[str, list] = {}
    for b in _cache["by_name"].values():
        crafts = b.get("crafts") or ""
        if crafts.split("_")[0].lower() != prefix:
            continue
        bsize = b.get("size")
        if bsize is None:
            bsize = _size_num(b.get("category") or "")
        if bsize != size:
            continue
        if str(b.get("grade") or "").strip().upper() != "A":
            continue
        cls = (b.get("cls") or "").strip().title()
        if cls:
            out.setdefault(cls, []).append(b)
    for grp in out.values():
        grp.sort(key=lambda b: b["name"].lower())
    return out


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


def aggregate_blueprints(items: list, path: str = BLUEPRINTS_PATH) -> dict:
    """Merge a build-list of ``{name, qty}`` into one crafting shopping list: every recipe's
    materials summed by resource (``scu`` × qty), tagged with the strictest ``min_quality`` any
    line asks for and which blueprints (and how many) need it. Unknown names are echoed with
    ``found: False`` and left out of the totals. Each resolved item carries its ``sources``
    (contracts that reward it, grouped by faction) when known, for the planner's reward card.
    The ``minerals`` shortcut feeds the same deposit-coverage plan (``/api/mining-plan``) as a
    single blueprint does."""
    by_res: dict[str, dict] = {}        # resource -> running total + contributors
    resolved: list = []
    total_seconds = 0.0
    for it in items or []:
        name = (it.get("name") or "").strip()
        try:
            qty = max(1, int(it.get("qty", 1)))
        except (TypeError, ValueError):
            qty = 1
        bp = lookup_blueprint(name, path) if name else None
        if not bp:
            resolved.append({"name": name, "qty": qty, "found": False})
            continue
        item = {"name": bp["name"], "qty": qty, "found": True,
                "category": bp.get("category"), "craft_seconds": bp.get("craft_seconds")}
        if bp.get("sources"):   # contracts that reward it, grouped by faction (planner card)
            item["sources"] = bp["sources"]
        resolved.append(item)
        total_seconds += (bp.get("craft_seconds") or 0) * qty
        for r in bp.get("requirements", []):
            agg = by_res.setdefault(r["resource"], {"resource": r["resource"], "scu": 0.0,
                                                    "min_quality": 0, "from": {}})
            agg["scu"] += (r.get("scu") or 0) * qty
            agg["min_quality"] = max(agg["min_quality"], r.get("min_quality") or 0)
            agg["from"][bp["name"]] = agg["from"].get(bp["name"], 0) + qty
    requirements = [{
        "resource": a["resource"],
        "scu": round(a["scu"], 2),
        "min_quality": a["min_quality"],
        "from": [{"name": n, "qty": q} for n, q in sorted(a["from"].items())],
    } for a in by_res.values()]
    # Heaviest material first -- that's the one that dictates the mining run.
    requirements.sort(key=lambda r: (-r["scu"], r["resource"]))
    return {
        "items": resolved,
        "requirements": requirements,
        "minerals": sorted(by_res),
        "craft_seconds": round(total_seconds),
        "total_scu": round(sum(a["scu"] for a in by_res.values()), 2),
    }
