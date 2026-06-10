"""Craftable blueprint recipes (requirements + minerals) for the planner."""

from __future__ import annotations

import glob
import os
import re
import shutil

from ._p4k import (
    _GRADE_LETTER, _deep_find, _deep_search, _index_by_basename,
    _load_json, _loc_text, ensure_binary, extract_records,
    load_localization, scratch_dir,
)


# --------------------------------------------------------------------------- #
# Crafting blueprints + their material requirements
# --------------------------------------------------------------------------- #
# A CraftingBlueprintRecord crafts an item (named via its entityClass entity's
# Localization.Name -- blueprintName itself is a placeholder) and its single tier's
# recipe is a tree of CraftingCost_Select slots bottoming out in CraftingCost_Resource
# leaves: a ResourceType (the same DB reference.py reads) + an SCU quantity + a
# minQuality (the quality band the material must meet). In 4.8 every recipe is a flat
# per-slot list (no alternatives) and every resource is a mined mineral, so a blueprint
# maps cleanly to "these minerals, this much, this quality" -> the rocks that yield them.

def _craft_seconds(bp: dict) -> int:
    """Total craft time in seconds from a recipe's TimeValue_Partitioned, if present."""
    tv = _deep_find(bp, "_Type_", "TimeValue_Partitioned")
    if not tv:
        return 0
    return int((tv.get("days") or 0) * 86400 + (tv.get("hours") or 0) * 3600
               + (tv.get("minutes") or 0) * 60 + (tv.get("seconds") or 0))


def _loc_name(rv: dict, loc: dict) -> str:
    """Crafted item's display name: the localised ``Localization.Name`` on a component."""
    def probe(o):
        lz = o.get("Localization")
        return _loc_text(lz.get("Name"), loc) if isinstance(lz, dict) else ""
    return _deep_search(rv, probe) or ""


def _recipe_costs(bp: dict) -> list:
    """Flatten a blueprint's cost tree to ``[{slot, resource, scu, min_quality}]``."""
    out = []

    def walk(o, slot=None):
        if isinstance(o, dict):
            if o.get("_Type_") == "CraftingCost_Resource":
                rn = (o.get("resource") or {}).get("_RecordName_", "")
                res = rn.split(".", 1)[1] if "." in rn else rn
                out.append({
                    "slot": (slot or "").title() or None,
                    "resource": res.replace("_", " "),
                    "scu": (o.get("quantity") or {}).get("standardCargoUnits"),
                    "min_quality": o.get("minQuality") or 0,
                })
            slot = (o.get("nameInfo") or {}).get("debugName") or slot
            for v in o.values():
                walk(v, slot)
        elif isinstance(o, list):
            for v in o:
                walk(v, slot)

    walk(bp)
    return out


def build_blueprints(records_root: str, loc: dict) -> list:
    """Every craftable blueprint -> {name, category, crafts, craft_seconds, requirements,
    minerals}, read from an extracted DataCore records root. ``requirements`` is the flat
    material list (slot/resource/scu/min_quality); ``minerals`` is the distinct resource
    names for feeding the mining planner. Placeholders/unnamed blueprints are skipped."""
    ent_index = _index_by_basename(records_root, "entities")
    meta_cache: dict[str, dict] = {}   # entity basename -> {name, grade, grade_num}
    out = []
    for p in glob.glob(os.path.join(records_root, "**", "crafting", "blueprints",
                                    "crafting", "**", "*.json"), recursive=True):
        try:
            bp = _load_json(p)["_RecordValue_"]["blueprint"]
        except (OSError, ValueError, KeyError):
            continue
        if bp.get("_Type_") != "CraftingBlueprint":
            continue
        reqs = _recipe_costs(bp)
        if not reqs:
            continue
        ec = (bp.get("processSpecificData") or {}).get("entityClass") or ""
        base = os.path.basename(ec).lower() if ec else ""
        if base not in meta_cache:
            # The crafted item's record gives both its display name and (for components /
            # weapons) its grade, from the same SAttachableComponentParams.AttachDef.Grade
            # the ships build reads -- so resolve both in one open.
            meta = {"name": "", "grade": None, "grade_num": None}
            ep = ent_index.get(base)
            if ep:
                try:
                    rv = _load_json(ep)["_RecordValue_"]
                    meta["name"] = _loc_name(rv, loc)
                    ad = _deep_find(rv, "_Type_", "SAttachableComponentParams")
                    g = ((ad or {}).get("AttachDef") or {}).get("Grade")
                    if g is not None:
                        meta["grade_num"] = g
                        meta["grade"] = _GRADE_LETTER.get(g)
                except (OSError, ValueError, KeyError):
                    pass
            meta_cache[base] = meta
        cmeta = meta_cache[base]
        name = cmeta["name"]
        if not name or "PLACEHOLDER" in name.upper():
            continue
        cat = (bp.get("category") or {}).get("_RecordName_", "")
        cat = cat.split(".", 1)[1] if "." in cat else cat
        # camel-split but keep acronym runs and size codes intact ("FPSWeapons" ->
        # "FPS Weapons"; "VehicleComponentS2" -> "Vehicle Component S2").
        cat = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", cat)
        entry = {
            "name": name,
            "category": cat,
            "crafts": os.path.basename(ec)[:-5] if ec else "",
            "craft_seconds": _craft_seconds(bp),
            "requirements": reqs,
            "minerals": sorted({r["resource"] for r in reqs}),
        }
        # Grade only varies for vehicle components; weapons and FPS items are uniformly
        # Grade A, so emitting it there is noise -- only the component filter consumes it.
        if cat.startswith("Vehicle Component"):
            entry["grade"] = cmeta["grade"]
            entry["grade_num"] = cmeta["grade_num"]
        out.append(entry)
    out.sort(key=lambda b: (b["name"], b["category"]))
    return out


def build_blueprints_from_p4k(p4k: str, sb: str | None = None,
                              progress=lambda m: None) -> list:
    """Full-extract orchestrator for the blueprint catalog (gated like mineables)."""
    sb = sb or ensure_binary()
    workdir = scratch_dir("starlogger-blueprints-")
    try:
        progress("extracting DataCore for blueprints")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return build_blueprints(recs, loc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
