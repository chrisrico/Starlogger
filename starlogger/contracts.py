"""Local cache of the contract taxonomy + cargo manifests, in contracts.json.

The ContractTemplate records authoritatively encode the hauling/delivery taxonomy --
grade x route-shape x SCU cap x rep rank x legal flag -- in structured tokens, in place
of decoding the contract-id *string* by hand (``patterns.decode_contract``). CargoManifest
records say what mixed/illegal/salvage contract cargo is made of. Both are mined from the
game's own ``Data.p4k`` via ``scdata.build_contracts_from_p4k`` on the same full DataCore
extract / major-version trigger as mineables/blueprints -- its own file/trigger, like those
(the cheap ``dcb query`` path reference.json uses can't reach these records).

``decode`` matches a live log contract id (which carries a template name plus a runtime
location/loop suffix) to its template and returns the structured fields, layered *over*
``patterns.decode_contract``'s text heuristics in ``model.Mission.decoded`` -- so the
authoritative data wins when present and the heuristic stays as the offline fallback.
Numeric token values (``MissionMaxSCUSize``) are static *caps*: the per-offer SCU and
payout are filled at runtime, so treat the SCU number as an upper bound, not the exact haul.
"""

from __future__ import annotations

import os
import re
import time

from .config import CONTRACTS_PATH, MISSION_ICONS_DIR
from . import scdata
from .jsonstore import atomic_write, load_cached

_cache = {"mtime": None,
          "data": {"templates": [], "cargo_manifests": [],
                   "fetched_at": None, "game_version": None},
          "by_template": {}}

# Extract-schema version: bump when this extraction's output SHAPE changes (new / renamed /
# dropped fields), so installs rebuild the cache on update even without a major game-version
# move. 0 == absent (files written before this stamp existed); see ``catalogs._reason``.
# v1: templates gained authoritative mission ``type``/``icon`` (commit f401e3c, 2026-06-07).
EXTRACT_VERSION = 1


def save_contracts(templates: list, cargo_manifests: list,
                   game_version: str | None = None, icons: dict | None = None,
                   path: str = CONTRACTS_PATH, icons_dir: str = MISSION_ICONS_DIR) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "count": len(templates),
        "templates": templates,
        "cargo_manifests": cargo_manifests,
    })
    save_icons(icons or {}, icons_dir)


def save_icons(icons: dict, icons_dir: str = MISSION_ICONS_DIR) -> None:
    """Write each ``{slug: svg_text}`` to ``icons_dir/<slug>.svg`` (the game's own mission-
    type icons, p4k-derived => gitignored). Skips silently if there's nothing to write."""
    if not icons:
        return
    os.makedirs(icons_dir, exist_ok=True)
    for slug, svg in icons.items():
        if not slug or not isinstance(svg, str):
            continue
        # slug is our own controlled token (see scdata._TYPE_MAP); guard anyway.
        safe = re.sub(r"[^a-z0-9]", "", slug.lower())
        if not safe:
            continue
        dst = os.path.join(icons_dir, f"{safe}.svg")
        tmp = dst + ".tmp"  # atomic: a reader sees the whole file or none (atomic_write is JSON-only)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(svg)
        os.replace(tmp, dst)


def _norm(s: str) -> str:
    """Lowercase, alphanumerics only -- so a live contract id and a template name compare
    free of separators/casing (HaulCargo_AtoB_Bulk_PressIce ~ haulcargoatobbulkpressice)."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _parse(data: dict) -> dict:
    # Index normalized template name -> structured fields, longest key first so decode()'s
    # containment match prefers the most specific (longest) template name.
    by_t = {}
    for t in data.get("templates", []):
        key = _norm(t.get("template", ""))
        if key:
            by_t[key] = t
    _cache["by_template"] = dict(sorted(by_t.items(), key=lambda kv: -len(kv[0])))
    return data


def load_contracts(path: str = CONTRACTS_PATH) -> dict:
    """The full cache dict ({templates, cargo_manifests, ...}); empty until built."""
    return load_cached(path, _cache, _parse)


def contracts_version(path: str = CONTRACTS_PATH) -> str | None:
    """Game version the data was built for -- gates the rebuild on a major bump."""
    return (load_contracts(path) or {}).get("game_version")


def contracts_extract_version(path: str = CONTRACTS_PATH) -> int:
    """Extract-schema version the cache was built with (0 == absent / pre-stamp)."""
    return int((load_contracts(path) or {}).get("extract_version") or 0)


def catalog(path: str = CONTRACTS_PATH) -> list:
    """The contract-template taxonomy rows; empty until the cache is built."""
    return (load_contracts(path) or {}).get("templates") or []


def cargo_manifests(path: str = CONTRACTS_PATH) -> list:
    """The cargo-manifest composition rows; empty until the cache is built."""
    return (load_contracts(path) or {}).get("cargo_manifests") or []


def decode(contract_id: str, path: str = CONTRACTS_PATH) -> dict:
    """Authoritative taxonomy for a live contract id, matched to its ContractTemplate (the
    id carries the template name plus a runtime suffix). Returns the statically-known bits
    -- the mission ``type``/class + its ``icon`` slug (from the MissionType record), the
    legal/illegal flag (the heuristic can't tell) and the route shape -- so it layers
    cleanly over the heuristic in ``model.Mission.decoded``. ``{}`` when nothing matches
    (offline / unknown id, where the heuristic still classifies). Note: the grade word, SCU
    cap and rep rank are runtime-bound in the records, so they are NOT here -- the
    contract-id heuristic and the live log remain their source."""
    key = _norm(contract_id)
    if not key:
        return {}
    load_contracts(path)
    tmpl = next((t for tkey, t in _cache["by_template"].items() if tkey in key), None)
    if not tmpl:
        return {}
    out = {"legal": not tmpl.get("illegal")}
    if tmpl.get("route"):
        out["route"] = tmpl["route"]
    if tmpl.get("type"):           # authoritative mission class (vs the keyword heuristic)
        out["type"] = tmpl["type"]
    if tmpl.get("icon"):           # icon slug -> /mission-icons/<icon>.svg
        out["icon"] = tmpl["icon"]
    return out
