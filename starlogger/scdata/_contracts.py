"""Contract-template taxonomy (route/grade/legal shape) + cargo manifests."""

from __future__ import annotations

import glob
import os
import re
import shutil
import tempfile

from ..patterns import camel_split
from ._p4k import (
    _deep_walk, _load_json, _run, ensure_binary, extract_records, load_localization,
)


# --------------------------------------------------------------------------- #
# Contract templates: the authoritative hauling/delivery taxonomy
# --------------------------------------------------------------------------- #
# Each contract is a ContractTemplate under contracts/contracttemplates/. Its
# contractProperties[] are MissionPropertys, each named by a string `extendedTextToken`
# (Contractor, CargoRouteToken, CargoGradeToken, MissionMaxSCUSize, ReputationRank, ...).
# What's *static* per template is the SHAPE -- which tokens are present and the route token
# -- plus the `illegal` flag. The token *values* (the actual grade word, SCU number, rep
# rank, chosen org) are runtime-bound (`@LOC_UNINITIALIZED` / empty in the records), so we
# do NOT read them here; the contract-id heuristic + the live log still supply those.
# CargoManifest records (what mixed/illegal/salvage cargo is made of) ride the same extract.

# Route-shape token -> display label (the one route token a cargo/courier template carries).
_ROUTE_TOKENS = {
    "CargoRouteToken": "A → B",
    "CourierRouteToken": "A → B",
    "SingleToMultiToken": "1 → many",
    "MultiToSingleToken": "many → 1",
}

# --------------------------------------------------------------------------- #
# Mission TYPE / class (authoritative, replaces the keyword heuristic)
# --------------------------------------------------------------------------- #
# Each ContractTemplate's `contractDisplayInfo.type` is a file ref to a MissionType record
# under missiontype/ (e.g. .../missiontype/pu/hauling.json). That record holds the
# `LocalisedTypeName` (mobiGlas type label) and `svgIconPath` (the in-p4k icon asset). This
# is the real mission class the game shows -- far better than scanning org/title strings.
#
# missiontype basename (lower, sans .json) -> (collapsed display label, icon slug). Per the
# agreed taxonomy the four hauling* variants fold into "Hauling" and salvage + local into
# "Salvage"; every other type stays distinct. The slug names BOTH the exported icon file
# (mission_icons/<slug>.svg) and the frontend colour/CSS class -- keep it in sync with the
# JS mirror in web/app.js (TYPE_SLUG / contractType / ctSlug).
_TYPE_MAP = {
    "hauling": ("Hauling", "haul"),
    "hauling_solar": ("Hauling", "haul"),
    "hauling_planetary": ("Hauling", "haul"),
    "hauling_interstellar": ("Hauling", "haul"),
    "priority": ("Priority", "priority"),
    "mercenary": ("Mercenary", "mercenary"),
    "collection": ("Collection", "collection"),
    "missiontype.delivery": ("Delivery", "deliver"),
    "investigation": ("Investigation", "investigation"),
    "salvage": ("Salvage", "salvage"),
    "local": ("Salvage", "salvage"),
    # the three mining variants fold into "Mining" (like hauling*), each keeping its own
    # method icon (hand / ground vehicle / spaceship) -- they reach us only via a
    # ContractGenerator missionTypeOverride (no ContractTemplate carries a mining type).
    "fpsmining": ("Mining", "fpsmining"),
    "groundmining": ("Mining", "groundmining"),
    "shipmining": ("Mining", "shipmining"),
    "bountyhunter": ("Bounty Hunter", "bounty"),
    "refueling": ("Refueling", "refuel"),
    "maintenance": ("Maintenance", "maintenance"),
    "race": ("Racing", "race"),
    "appointment": ("Appointment", "appointment"),
    "ecn": ("ECN Alert", "ecn"),
    "missiontype.search": ("Search", "search"),
}


def _type_basename(ref) -> str:
    """missiontype basename (lower, sans .json) from a ``contractDisplayInfo.type`` file
    ref (``file://.../missiontype.delivery.json``) or a record name."""
    if not isinstance(ref, str) or not ref:
        return ""
    b = os.path.basename(ref.split("?")[0])
    return (b[:-5] if b.endswith(".json") else b).lower()


def _mission_type_index(records_root: str, loc: dict) -> dict:
    """Every MissionType record -> ``{label, slug, svg}``: the collapsed display label +
    icon slug (from ``_TYPE_MAP``; an unmapped/future type localises its own
    ``LocalisedTypeName`` and slugs its basename) and the in-p4k icon asset path. Drives
    both the per-template ``type`` and the icon export."""
    idx: dict = {}
    for p in glob.glob(os.path.join(records_root, "**", "missiontype", "**", "*.json"),
                       recursive=True):
        try:
            cv = _load_json(p)["_RecordValue_"]
        except (OSError, ValueError, KeyError):
            continue
        base = os.path.basename(p)[:-5].lower()
        label, slug = _TYPE_MAP.get(base, (None, None))
        if label is None:  # unmapped (e.g. a newly-added type): localise its own name
            tok = (cv.get("LocalisedTypeName") or "").lstrip("@")
            label = loc.get(tok.lower()) or loc.get(tok) or base.replace("_", " ").title()
            slug = re.sub(r"[^a-z0-9]", "", base) or "other"
        idx[base] = {"label": label, "slug": slug, "svg": cv.get("svgIconPath") or ""}
    return idx


def build_contract_taxonomy(records_root: str, loc: dict, mtidx: dict | None = None) -> list:
    """Every contract template -> ``{template, type, icon, route, graded, scu_sized,
    rep_gated, illegal}``, read from an extracted DataCore records root. ``type`` is the
    authoritative mission class (via ``contractDisplayInfo.type`` -> MissionType) and
    ``icon`` its slug; both None when a template names no type. Only the statically-known
    shape is captured (token *values* are runtime); ``route`` is None for non-cargo types."""
    if mtidx is None:
        mtidx = _mission_type_index(records_root, loc)
    rows: list[dict] = []
    for p in glob.glob(os.path.join(records_root, "**", "contracttemplates", "*.json"),
                       recursive=True):
        try:
            d = _load_json(p)
            cv = d["_RecordValue_"]
            name = d["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        tokens = {mp.get("extendedTextToken") for mp in (cv.get("contractProperties") or [])
                  if isinstance(mp.get("extendedTextToken"), str)}
        info = mtidx.get(_type_basename((cv.get("contractDisplayInfo") or {}).get("type")))
        rows.append({
            "template": name,
            "type": info["label"] if info else None,
            "icon": info["slug"] if info else None,
            "route": next((lbl for tok, lbl in _ROUTE_TOKENS.items() if tok in tokens), None),
            "graded": bool(tokens & {"CargoGradeToken", "CourierGradeToken"}),
            "scu_sized": "MissionMaxSCUSize" in tokens,
            "rep_gated": "ReputationRank" in tokens,
            "illegal": bool((cv.get("contractDisplayInfo") or {}).get("illegal")),
        })
    rows.sort(key=lambda r: r["template"])
    return rows


# --------------------------------------------------------------------------- #
# Named / scripted contracts (ContractGenerator -> per-contract debugName)
# --------------------------------------------------------------------------- #
# Guild- and story-given missions (Hockrow facility-delve, Gilly's Pilot School, Redwind
# intro, Eckhart/Foxwell mercenary intros, BHG bounty intro, Shubin mining...) don't ride a
# generic ContractTemplate name. They live under contracts/contractgenerator/ as
# ContractGenerator records: generators[] -> {contracts, introContracts}[] -> each a
# ``Contract`` with a ``debugName`` (== the log's ``contract [...]`` token, plus a runtime
# location/loop suffix) and a base ``template`` ref. The authoritative mission class is the
# contract's ``paramOverrides.missionTypeOverride`` (a MissionType ref) when present, else
# the base template's own type. Without this map these tokens match no template and fall to
# the keyword heuristic (mis-typed as the coarse "Bounty / Combat"/"Delivery"/"Other").
_MIN_GEN_KEY = 5  # skip ultra-short debugNames (e.g. "RoX"); too generic for a safe prefix


def build_contract_generators(records_root: str, templates: list,
                              mtidx: dict | None = None, loc: dict | None = None) -> list:
    """Every named/scripted ContractGenerator contract -> ``{template: debugName, type,
    icon}``. ``decode()`` matches the log token to ``debugName`` by prefix (the token is the
    debugName + runtime suffix), so these resolve the same authoritative MissionType the
    templates do. Type = the contract's ``missionTypeOverride`` (MissionType) when set, else
    the type of the base ContractTemplate it instantiates (looked up in ``templates``)."""
    if mtidx is None:
        mtidx = _mission_type_index(records_root, loc or load_localization(records_root))
    by_base = {t["template"].lower(): t for t in templates if t.get("type")}

    def _basename(ref) -> str:
        if not isinstance(ref, str) or not ref:
            return ""
        b = os.path.basename(ref.split("?")[0])
        return (b[:-5] if b.endswith(".json") else b).lower()

    rows: dict[str, dict] = {}

    def _visit(node) -> None:
        dn = node.get("debugName")
        if isinstance(dn, str) and dn and len(_slug(dn)) >= _MIN_GEN_KEY:
            label = slug = None
            ov = (node.get("paramOverrides") or {}).get("missionTypeOverride")
            info = mtidx.get(_type_basename(ov)) if isinstance(ov, str) and ov else None
            if info:
                label, slug = info["label"], info["slug"]
            else:                                  # inherit the base template's type
                base = by_base.get(_basename(node.get("template")))
                if base:
                    label, slug = base["type"], base["icon"]
            if label:
                rows.setdefault(dn, {"template": dn, "type": label, "icon": slug})

    for p in glob.glob(os.path.join(records_root, "**", "contractgenerator", "**", "*.json"),
                       recursive=True):
        try:
            cv = _load_json(p)["_RecordValue_"]
        except (OSError, ValueError, KeyError):
            continue
        _deep_walk(cv, _visit)
    return sorted(rows.values(), key=lambda r: r["template"])


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _resource_name(res) -> str:
    """Commodity display name from a CargoResource.resource ref -- an inline record
    (``{_RecordName_: ResourceType.Scrap_Metal}``) or a ``file://...`` string."""
    if isinstance(res, dict):
        rn = res.get("_RecordName_", "")
        tok = rn.split(".", 1)[1] if "." in rn else rn
    elif isinstance(res, str) and res:
        base = os.path.basename(res.split("?")[0])
        tok = base[:-5] if base.endswith(".json") else base
    else:
        tok = ""
    return camel_split(tok.replace("_", " ")).strip().title()


def build_cargo_manifests(records_root: str, loc: dict) -> list:
    """Every CargoManifest -> ``{manifest, resources: [{commodity, probability}]}`` --
    what mixed/illegal/salvage/scrap contract cargo is made of (C2)."""
    out: list[dict] = []
    for p in glob.glob(os.path.join(records_root, "**", "cargomanifest", "*.json"),
                       recursive=True):
        try:
            d = _load_json(p)
            cv = d["_RecordValue_"]
            name = d["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        resources = [{"commodity": _resource_name(cr.get("resource")),
                      "probability": cr.get("probability")}
                     for cr in ((cv.get("cargoFillCapacity") or {}).get("resources") or [])]
        out.append({"manifest": name, "resources": resources})
    out.sort(key=lambda m: m["manifest"])
    return out


def extract_mission_icons(p4k: str, sb: str, workdir: str, mtidx: dict) -> dict:
    """``{slug: svg_text}`` for every distinct mission-type icon referenced by ``mtidx`` --
    the game's own mobiGlas type icons, pulled straight from the p4k (small vector files).
    Returned as text so the caller writes them wherever it keeps p4k-derived data; the
    workdir is transient. Best-effort: a missing/odd asset is simply skipped."""
    want = {info["slug"]: info["svg"] for info in mtidx.values()
            if info.get("slug") and info.get("svg")}
    if not want:
        return {}
    out = os.path.join(workdir, "icons")
    os.makedirs(out, exist_ok=True)
    # The icons live under a couple of MobiGlas subdirs (App_ContractsManager, Starmap);
    # one glob over MobiGlas/*.svg grabs them all in a single fast pass.
    try:
        _run(sb, p4k, ["p4k", "extract", "--p4k", p4k,
                       "--filter", "**/MobiGlas/**/*.svg", "-o", out], timeout=120)
    except RuntimeError:
        return {}
    found = {os.path.basename(f).lower(): f
             for f in glob.glob(os.path.join(out, "**", "*.svg"), recursive=True)}
    icons: dict = {}
    for slug, svg in want.items():
        f = found.get(os.path.basename(svg).lower())
        if not f:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                icons[slug] = fh.read()
        except OSError:
            continue
    return icons


def build_contracts_from_p4k(p4k: str, sb: str | None = None,
                             progress=lambda m: None) -> dict:
    """Full-extract orchestrator for the contract taxonomy + named/scripted generators +
    cargo manifests + mission-type icons (gated like mineables/blueprints). Returns
    ``{templates, generators, cargo_manifests, icons}`` -- ``icons`` is ``{slug: svg_text}``
    for the caller to persist (gitignored)."""
    sb = sb or ensure_binary()
    workdir = tempfile.mkdtemp(prefix="starlogger-contracts-")
    try:
        progress("extracting DataCore for contracts")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        mtidx = _mission_type_index(recs, loc)
        templates = build_contract_taxonomy(recs, loc, mtidx)
        return {"templates": templates,
                "generators": build_contract_generators(recs, templates, mtidx, loc),
                "cargo_manifests": build_cargo_manifests(recs, loc),
                "icons": extract_mission_icons(p4k, sb, workdir, mtidx)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
