"""Contract-template taxonomy (route/grade/legal shape) + cargo manifests."""

from __future__ import annotations

import glob
import os
import shutil
import tempfile

from ..patterns import camel_split
from ._p4k import (
    _load_json, ensure_binary, extract_records, load_localization,
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


def build_contract_taxonomy(records_root: str, loc: dict) -> list:
    """Every contract template -> ``{template, route, graded, scu_sized, rep_gated,
    illegal}``, read from an extracted DataCore records root. Only the statically-known
    shape is captured (token *values* are runtime); ``route`` is None for non-cargo types."""
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
        rows.append({
            "template": name,
            "route": next((lbl for tok, lbl in _ROUTE_TOKENS.items() if tok in tokens), None),
            "graded": bool(tokens & {"CargoGradeToken", "CourierGradeToken"}),
            "scu_sized": "MissionMaxSCUSize" in tokens,
            "rep_gated": "ReputationRank" in tokens,
            "illegal": bool((cv.get("contractDisplayInfo") or {}).get("illegal")),
        })
    rows.sort(key=lambda r: r["template"])
    return rows


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


def build_contracts_from_p4k(p4k: str, sb: str | None = None,
                             progress=lambda m: None) -> dict:
    """Full-extract orchestrator for the contract taxonomy + cargo manifests (gated like
    mineables/blueprints). Returns ``{templates, cargo_manifests}``."""
    sb = sb or ensure_binary()
    workdir = tempfile.mkdtemp(prefix="starlogger-contracts-")
    try:
        progress("extracting DataCore for contracts")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return {"templates": build_contract_taxonomy(recs, loc),
                "cargo_manifests": build_cargo_manifests(recs, loc)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
