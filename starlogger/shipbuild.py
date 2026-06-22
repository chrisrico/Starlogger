"""Shipbuilder: which crafting blueprints outfit a ship's components to a chosen class.

The Blueprints planner's "outfit a ship" control. Given a ship and a desired component class
(Civilian / Military / Industrial / Competition / Stealth), it walks the ship's stock component
slots -- power plant, cooler, shield, quantum drive, and the radar -- and picks the Grade-A
blueprint to craft for each: the chosen class where it makes that part at that size, else the
*closest* class that does (so every slot still gets a build). Grade A only -- the only tier worth
crafting (~85% of the catalog and the best parts); the user opted out of crafting lower grades.

A small consumer joining the ships catalog (which gives each slot's size + count) with the
blueprints catalog (the craftable parts), kept in its own module to avoid a ships<->blueprints
import cycle -- mirrors ``mine_locations``.
"""

from __future__ import annotations

from .blueprints import component_blueprints
from .config import BLUEPRINTS_PATH
from .ships import _lookup

# Ship component slot -> the blueprint `crafts` prefix that makes that component.
_SLOT_PREFIX = {"power_plant": "powr", "cooler": "cool", "shield": "shld",
                "quantum_drive": "qdrv", "radar": "radr"}
_SLOT_LABEL = {"power_plant": "Power Plant", "cooler": "Cooler", "shield": "Shield",
               "quantum_drive": "Quantum Drive", "radar": "Radar"}

# The five component classes and, for each, the order to fall back through when the chosen class
# has no Grade-A blueprint for a slot ("get the next closest"). Two rules from the user: (1) prefer
# crafting Military as the substitute -- it's the best-stocked specialised class (Stealth in
# particular falls back to Military); (2) Civilian is the LAST resort everywhere it isn't the chosen
# class, since Civilian parts are cheap to just buy rather than craft. The middle ordering keeps the
# performance class (Competition) and the efficiency one (Industrial) near their kin. A heuristic
# (no performance stats in the catalog to compute true similarity); keyed in display ("title") case
# to match the blueprints' `cls`.
CLASSES = ("Civilian", "Military", "Industrial", "Competition", "Stealth")
_FALLBACK = {
    "Military":    ["Military", "Competition", "Industrial", "Stealth", "Civilian"],
    "Competition": ["Competition", "Military", "Industrial", "Stealth", "Civilian"],
    "Civilian":    ["Civilian", "Military", "Industrial", "Competition", "Stealth"],
    "Industrial":  ["Industrial", "Military", "Stealth", "Competition", "Civilian"],
    "Stealth":     ["Stealth", "Military", "Industrial", "Competition", "Civilian"],
}
_DEFAULT_CLASS = "Military"


def _slots(hit: dict) -> list:
    """A ship record's craftable component slots as ``(key, size, count)`` -- the four headline
    components (each list entry is one variant with its own size/count) plus the single radar."""
    out = []
    comps = hit.get("components") or {}
    for key in ("power_plant", "cooler", "shield", "quantum_drive"):
        for c in comps.get(key) or []:
            if c.get("size") is not None:
                out.append((key, int(c["size"]), max(1, int(c.get("count") or 1))))
    radar = hit.get("radar")
    if isinstance(radar, dict) and radar.get("size") is not None:
        out.append(("radar", int(radar["size"]), 1))
    return out


def _pick(prefix: str, size: int, cls: str, bp_path: str) -> tuple:
    """The Grade-A blueprint to craft for a (prefix, size) slot at class ``cls``: returns
    ``(blueprint, used_cls, alternatives)`` where ``used_cls`` is ``cls`` (or the closest
    fallback class that has one) and ``alternatives`` the other Grade-A names of ``used_cls``.
    ``(None, None, [])`` when no class makes a Grade-A part for the slot."""
    by_cls = component_blueprints(prefix, size, bp_path)
    for cand in _FALLBACK.get(cls, [cls]):
        group = by_cls.get(cand)
        if group:
            return group[0], cand, [b["name"] for b in group[1:]]
    return None, None, []


def ship_build_plan(ship: str, cls: str, db: dict | None = None,
                    bp_path: str = BLUEPRINTS_PATH) -> dict:
    """The Grade-A blueprint builds that outfit ``ship``'s components to class ``cls``. One build
    per slot (``qty`` = the slot's stock count), each tagged with the class it actually uses --
    the chosen class, or the closest fallback where the chosen class makes no Grade-A part that
    size (``substituted``). Builds sharing a blueprint are merged (two identical slots -> one
    name at qty 2). ``unmatched`` lists any slot no class can craft (none expected for catalogued
    ships); ``buildable`` is False when the ship has no craftable components at all."""
    cls = (cls or "").strip().title()
    if cls not in _FALLBACK:
        cls = _DEFAULT_CLASS
    hit = _lookup(ship, db) or {}
    builds: dict[str, dict] = {}
    order: list = []
    unmatched: list = []
    for key, size, count in _slots(hit):
        bp, used, alts = _pick(_SLOT_PREFIX[key], size, cls, bp_path)
        if not bp:
            unmatched.append({"slot": _SLOT_LABEL[key], "size": size})
            continue
        name = bp["name"]
        if name in builds:
            builds[name]["qty"] += count
        else:
            order.append(name)
            builds[name] = {"name": name, "qty": count, "slot": _SLOT_LABEL[key],
                            "size": size, "cls": used, "substituted": used != cls,
                            "alternatives": alts}
    return {
        "ship": hit.get("name", ship),
        "ship_class": hit.get("class"),
        "cls": cls,
        "buildable": bool(builds or unmatched),
        "builds": [builds[n] for n in order],
        "unmatched": unmatched,
    }
