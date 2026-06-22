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

# --------------------------------------------------------------------------- #
# Where a blueprint comes from: the mission/faction that rewards it
# --------------------------------------------------------------------------- #
# A blueprint is dropped by one or more reward *pools* (BlueprintPoolRecord under
# crafting/blueprintrewards/), each listing blueprintRewards[].blueprintRecord (the
# bp_craft_* record). A pool is wired to a faction's missions via a `blueprintPool` ref
# inside a ContractGenerator (grouped under a faction org dir), or stands alone for an
# event (XenoThreat, Wikelo's collector exchange). So: blueprint <- pool <- contract <-
# faction. We invert that to a {blueprint-record-stem: [{faction, contracts:[titles]}]} index
# for the planner -- grouping the player-facing contract Titles (resolved from global.ini)
# under the faction that grants them. Pools carry no display name, so the faction comes from
# the generator's org dir (curated for the run-together names) or, for a standalone event pool
# with no contract, the pool's own name (with an empty contract list).
_ORG_NAMES = {
    "bitzeros": "BitZeros",
    "bountyhunterguild": "Bounty Hunters Guild",
    "citizensforprosperity": "Citizens for Prosperity",
    "deadsaints": "Dead Saints",
    "eckhartsecurity": "Eckhart Security",
    "foxwellenforcement": "Foxwell Enforcement",
    "shipbattles": "Foxwell Enforcement",   # a Foxwell sub-activity dir
    "ftl": "FTL",
    "headhunters": "Headhunters",
    "hockrowagency": "Hockrow Agency",
    "intersecdefensesolutions": "Intersec Defense Solutions",
    "lingfamilyhauling": "Ling Family Hauling",
}

_POOL_PREFIX = re.compile(r"^(bp_missionreward_|bp_rewards_|bp_reward_)")


def _stem(ref: str) -> str:
    """Basename of a record file ref, minus the ``.json`` (``a/b/foo.json?x`` -> ``foo``)."""
    return os.path.basename(ref.split("?")[0])[:-5]


def _pool_label(pool: str, dirname: str, orgs: "set[str] | None") -> str:
    """Human label for a reward pool: the faction whose missions grant it, an event name
    for the standalone pools, or a prettified pool name when nothing else is known."""
    if dirname == "xenothreat2rewards":
        return "XenoThreat"
    if dirname == "collectorwikelo":
        return "Wikelo (Collector)"
    real = {o for o in (orgs or set()) if o != "contractgenerator"}
    if real:   # dedup AFTER mapping -- distinct org dirs can share a display name
        return ", ".join(sorted({_ORG_NAMES.get(o, o.title()) for o in real}))
    nm = _POOL_PREFIX.sub("", pool)
    return _ORG_NAMES.get(nm, nm.replace("_", " ").title())


def _org_label(org: str) -> "str | None":
    """Faction display name for a contract-generator org dir, or None when the generator sits
    directly under ``contractgenerator/`` (no faction dir) -- the caller then falls back to the
    pool label."""
    if not org or org == "contractgenerator":
        return None
    return _ORG_NAMES.get(org, org.title())


# Contract Titles carry runtime fill-ins (``~mission(TargetName)``) and the odd bracketed tag
# prefix; render them as readable static text for the planner.
_MISSION_TOKEN = {"targetname": "the target", "target": "the target", "item": "an item",
                  "location": "a location", "locationname": "a location",
                  "destination": "a destination", "quantity": "some"}
_MISSION_RE = re.compile(r"~mission\(([^)]*)\)")
_TAG_PREFIX_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")


def _clean_contract_title(s: str) -> str:
    """A player-facing contract title cleaned for display: drop any leading ``[..]`` tags,
    replace ``~mission(Token)`` runtime fill-ins with readable words, collapse whitespace."""
    if not s:
        return ""
    s = _TAG_PREFIX_RE.sub("", s)
    s = _MISSION_RE.sub(lambda m: _MISSION_TOKEN.get(m.group(1).strip().lower(), "…"), s)
    return re.sub(r"\s+", " ", s).strip()


def _contract_title_and_pools(contract: dict) -> "tuple[str | None, list[str]]":
    """A generator contract's Title loc key + the reward pools it grants. The Title is a
    ``stringParamOverrides`` entry (``param == 'Title'``); pools are the ``blueprintPool`` of
    each ``BlueprintRewards`` reward, found anywhere in the contract subtree."""
    found: dict = {"title": None, "pools": []}

    def walk(o):
        if isinstance(o, dict):
            if o.get("param") == "Title" and "value" in o:
                found["title"] = o["value"]
            if o.get("_Type_") == "BlueprintRewards" and o.get("blueprintPool"):
                found["pools"].append(_stem(o["blueprintPool"]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(contract)
    return found["title"], found["pools"]


def build_blueprint_sources(records_root: str, loc: dict) -> "dict[str, list[dict]]":
    """``{blueprint-record-stem: [{faction, contracts:[titles]}]}`` -- the contracts that reward
    each blueprint, grouped by the faction that grants them. Reads the reward pools and the
    contract generators that point at them, resolving each contract's player-facing Title."""
    # pool stem -> {(org_dir, title_loc_key)} for every contract that grants the pool.
    pool_contracts: "dict[str, set[tuple[str, str | None]]]" = {}
    for f in glob.glob(os.path.join(records_root, "**", "contractgenerator", "**", "*.json"),
                       recursive=True):
        try:
            s = open(f, encoding="utf-8").read()
        except OSError:
            continue
        if "blueprintrewards" not in s:
            continue
        org = os.path.basename(os.path.dirname(f))
        try:
            rv = _load_json(f)["_RecordValue_"]
        except (OSError, ValueError, KeyError):
            continue
        for gen in (rv.get("generators") or []):
            for c in (gen.get("contracts") or []):
                title, pools = _contract_title_and_pools(c)
                for p in pools:
                    pool_contracts.setdefault(p, set()).add((org, title))
    # pool -> blueprints, built up as {blueprint: {faction: {titles}}}.
    grouped: "dict[str, dict[str, set[str]]]" = {}
    for f in glob.glob(os.path.join(records_root, "**", "blueprintrewards", "**", "*.json"),
                       recursive=True):
        try:
            rv = _load_json(f)["_RecordValue_"]
        except (OSError, ValueError, KeyError):
            continue
        bps = [_stem(b["blueprintRecord"]) for b in (rv.get("blueprintRewards") or [])
               if b.get("blueprintRecord")]
        if not bps:
            continue
        pool = _stem(f)
        dirname = os.path.basename(os.path.dirname(f))
        contracts = pool_contracts.get(pool)
        for b in bps:
            fac = grouped.setdefault(b, {})
            if contracts:
                for org, title_key in contracts:
                    label = _org_label(org) or _pool_label(pool, dirname, None)
                    titles = fac.setdefault(label, set())
                    t = _clean_contract_title(_loc_text(title_key, loc)) if title_key else ""
                    if t:
                        titles.add(t)
            else:   # standalone event pool -- faction/event name, no contract context
                fac.setdefault(_pool_label(pool, dirname, None), set())
    return {b: [{"faction": fa, "contracts": sorted(ts)} for fa, ts in sorted(fac.items())]
            for b, fac in grouped.items()}


def _craft_seconds(bp: dict) -> int:
    """Total craft time in seconds from a recipe's TimeValue_Partitioned, if present."""
    tv = _deep_find(bp, "_Type_", "TimeValue_Partitioned")
    if not tv:
        return 0
    return int((tv.get("days") or 0) * 86400 + (tv.get("hours") or 0) * 3600
               + (tv.get("minutes") or 0) * 60 + (tv.get("seconds") or 0))


def _loc_name_desc(rv: dict, loc: dict) -> "tuple[str, str]":
    """Crafted item's localised (Name, Description) from its component Localization block --
    the Description carries the ``Class:`` line read for the Class column."""
    found = {"desc": ""}
    def probe(o):
        lz = o.get("Localization")
        if isinstance(lz, dict) and lz.get("Name"):
            found["desc"] = _loc_text(lz.get("Description"), loc) if lz.get("Description") else ""
            return _loc_text(lz.get("Name"), loc)
        return ""
    return (_deep_search(rv, probe) or ""), found["desc"]


_CLASS_RE = re.compile(r"Class:\s*(.+?)(?:\\n|\n|$)")


def _parse_class(desc: str) -> "str | None":
    """The item's Class -- Military/Civilian/Industrial/Stealth/Competition for vehicle
    components, the damage type (Ballistic, Energy …) for weapons -- from its description's
    ``Class: X`` line (descriptions store line breaks as a literal ``\\n``)."""
    m = _CLASS_RE.search(desc or "")
    return (m.group(1).replace("\xa0", " ").strip() or None) if m else None


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
    names for feeding the mining planner. A blueprint also carries ``sources`` -- the contracts
    that reward it, grouped by faction (``[{faction, contracts:[titles]}]``) -- when any are
    known. Placeholders/unnamed blueprints are skipped."""
    ent_index = _index_by_basename(records_root, "entities")
    meta_cache: dict[str, dict] = {}   # entity basename -> {name, grade, grade_num}
    sources = build_blueprint_sources(records_root, loc)   # bp stem -> [{faction, contracts}]
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
            meta = {"name": "", "grade": None, "grade_num": None, "size": None, "cls": None}
            ep = ent_index.get(base)
            if ep:
                try:
                    rv = _load_json(ep)["_RecordValue_"]
                    meta["name"], desc = _loc_name_desc(rv, loc)
                    ad = ((_deep_find(rv, "_Type_", "SAttachableComponentParams") or {})
                          .get("AttachDef") or {})
                    g = ad.get("Grade")
                    if g is not None:
                        meta["grade_num"] = g
                        meta["grade"] = _GRADE_LETTER.get(g)
                    if isinstance(ad.get("Size"), int):
                        meta["size"] = ad["Size"]
                    meta["cls"] = _parse_class(desc)
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
        # Grade (A-D), Size, and Class drive the table's Quality/Size/Class columns -- emit
        # whatever the crafted item declares (most weapons are Grade A; many items have no Class).
        if cmeta["grade"]:
            entry["grade"] = cmeta["grade"]
            entry["grade_num"] = cmeta["grade_num"]
        if cmeta["size"] is not None:
            entry["size"] = cmeta["size"]
        if cmeta["cls"]:
            entry["cls"] = cmeta["cls"]
        src = sources.get(os.path.basename(p)[:-5])
        if src:
            entry["sources"] = src
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
