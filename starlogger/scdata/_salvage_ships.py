"""Resolve each salvageable wreck ship's removable (salvage-beam-strippable) components.

When a salvage contract's wrecks spawn at the site, the Game.log names each one's ship
class+variant on its resource-host line (``<BASE>_Unmanned_Salvage_<id>``; see
``patterns.SALVAGE_SPAWN`` / ``state.salvage_targets``). This catalog answers "what can I
strip off that hull": for every ship that has an ``*_Unmanned_Salvage`` variant in the
DataCore (the finite set a salvage contract can spawn), its stock loadout filtered to the
components the salvage beam can remove.

Removability is the game's own per-item ``SHealthComponentParams.IsSalvagable`` flag
(captured in ``build_component_index``). The size cap is NOT in the data -- non-weapon
components exist up to size 4 and the salvage-beam config (``sglobalsalvagerepairbeamparams``)
filters by type, not size -- so ``pullable`` (currently strippable in-game: weapons any
size, other components only size<=2) is OUR rule, baked here so the UI can grey out the
rest rather than hide it. See NOTES.

Closely coupled to ``_ships`` (salvage wrecks ARE ships): it reuses that module's loadout
parser, component index, and naming rather than duplicating them.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess

from ._p4k import (
    _load_json, _run, ensure_binary, extract_records, load_localization, scratch_dir,
)
from ._ships import (
    LOADOUT_FAILURE_LIMIT, _parse_loadout_blocks, build_component_index,
    display_name, manufacturer,
)

# Slots the salvage beam treats as "weapon-class" -- removable at ANY size. Everything else
# (power plant / cooler / shield / quantum drive / radar) is capped at size 2 by the
# data-absent gameplay rule that ``pullable`` encodes for the UI's grey-out.
_WEAPON_SLOTS = frozenset({
    "weapon", "missile", "missile_rack", "turret", "turret_base",
    "utility_turret", "countermeasure",
})

_SALVAGE_SUFFIX = "_unmanned_salvage"


def is_pullable(category: str, size) -> bool:
    """Whether the salvage beam can currently strip a salvagable component: weapons/ordnance
    at any size, other components only at size <= 2. The size cap is a gameplay rule, NOT in
    the DataCore (components go to size 4) -- see the module docstring / NOTES."""
    if category in _WEAPON_SLOTS:
        return True
    return isinstance(size, (int, float)) and size <= 2


def _component_name(child: str, info: dict, loc: dict) -> str:
    """Localised component name, mirroring ``resolve_ship_components`` fallbacks (a weapon's
    loc key rarely matches its class id, so the AttachDef ``loc_key`` is the workhorse)."""
    bare = child.removesuffix("_scitem")
    loc_key = (info.get("loc_key") or "").lstrip("@").lower()
    return (loc.get(f"item_name{child}")
            or loc.get(f"item_name{bare}")
            or loc.get(f"item_name_{bare}")
            or (loc.get(loc_key) if loc_key else None)
            or child)


def _removable_components(ship_class: str, loadout_text: str, component_index: dict,
                         loc: dict) -> list:
    """The salvagable components installed on a ship -> a flat list (deduped by class with
    counts), each ``{category, name, size, grade, count, pullable}``. Reads the ship's own
    loadout block, which ``_parse_loadout_blocks`` flattens to include nested installs (turret
    guns, racked missiles). Non-salvagable items are dropped."""
    blocks = _parse_loadout_blocks(loadout_text or "")
    by_class: dict[str, dict] = {}
    order: list[str] = []
    for child in blocks.get(ship_class.lower(), []):
        info = component_index.get(child)
        if not info or not info.get("salvagable"):
            continue
        if child in by_class:
            by_class[child]["count"] += 1
            continue
        by_class[child] = {
            "category": info["slot"],
            "name": _component_name(child, info, loc),
            "size": info["size"],
            "grade": info["grade"],
            "count": 1,
            "pullable": is_pullable(info["slot"], info["size"]),
        }
        order.append(child)
    return [by_class[c] for c in order]


def _salvage_base_classes(records_root: str) -> dict:
    """{base_class_lower: (base_class, base_record_path)} for every ship that has an
    ``*_Unmanned_Salvage`` spaceship variant in the DataCore. The base is the variant class
    minus the ``_Unmanned_Salvage`` tail; its own spaceship record (for naming) is matched
    by class. The log's spawn lines name this base, so keying by it lets the snapshot resolve
    a detected wreck straight to its components."""
    recs: dict[str, tuple] = {}   # cls_lower -> (cls, path)
    for p in glob.glob(os.path.join(records_root, "**", "entities", "spaceships", "*.json"),
                       recursive=True):
        try:
            cls = _load_json(p)["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        recs[cls.lower()] = (cls, p)
    out: dict[str, tuple] = {}
    for low, rec in recs.items():
        if not low.endswith(_SALVAGE_SUFFIX):
            continue
        base = recs.get(low[: -len(_SALVAGE_SUFFIX)])
        if base:
            out[base[0].lower()] = base
    return out


def build_salvage_ships(records_root: str, p4k: str, sb: str | None = None,
                        loc: dict | None = None, progress=lambda m: None) -> dict:
    """{base_class_lower: {class, name, name_full, manufacturer, components}} for every ship
    that can spawn as an ``*_Unmanned_Salvage`` wreck. ``components`` is the flat removable
    list (salvagable only; each flagged ``pullable``). Needs ``p4k``+``sb`` for the per-ship
    ``entity loadout`` calls (like ``build_ships``); ``records_root`` supplies the component
    index + naming, so callers with an existing extract can skip re-extracting.

    Raises ``RuntimeError`` if more than ``LOADOUT_FAILURE_LIMIT`` of the loadout extractions
    fail -- a degraded run must not overwrite a complete catalog with a partial one."""
    sb = sb or ensure_binary()
    loc = load_localization(records_root) if loc is None else loc
    component_index = build_component_index(records_root)
    bases = _salvage_base_classes(records_root)
    progress(f"resolving removable components for {len(bases)} salvage ships")
    out: dict[str, dict] = {}
    failures = 0
    for i, (base_low, (cls, rec_path)) in enumerate(sorted(bases.items())):
        progress(f"salvage ship {i + 1}/{len(bases)}: {cls}")
        try:
            loadout_text = _run(sb, p4k, ["entity", "loadout", cls], timeout=120)
        except (RuntimeError, subprocess.TimeoutExpired):
            failures += 1
            continue
        name, name_full = display_name(cls, loc, rec_path)
        mfr_short, _ = manufacturer(cls, loc, rec_path)
        out[base_low] = {
            "class": cls,
            "name": name,
            "name_full": name_full,
            "manufacturer": mfr_short,
            "components": _removable_components(cls, loadout_text, component_index, loc),
        }
    if bases and failures > LOADOUT_FAILURE_LIMIT * len(bases):
        raise RuntimeError(
            f"{failures}/{len(bases)} salvage-ship loadout extractions failed -- "
            f"StarBreaker/p4k degraded; refusing to emit a partial salvage catalog")
    progress(f"built {len(out)} salvage ships")
    return out


def build_salvage_ships_from_p4k(p4k: str, sb: str | None = None,
                                 progress=lambda m: None) -> dict:
    """Full-extract orchestrator: extract the DataCore from the local install, then resolve
    each salvage ship's removable components. Heavy (full extract + per-ship loadout calls),
    so gated like the ship catalog -- see ``catalogs.refresh_loop``."""
    sb = sb or ensure_binary()
    workdir = scratch_dir("starlogger-salvage-ships-")
    try:
        progress("extracting DataCore for salvage ships")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return build_salvage_ships(recs, p4k, sb, loc, progress)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
