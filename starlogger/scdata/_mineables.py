"""Mineable-rock radar signatures, composition, and cracking mechanics."""

from __future__ import annotations

import glob
import os
import re
import shutil
import tempfile

from ..patterns import camel_split
from ._p4k import (
    _component, _index_by_basename, _load_json, _loc_text,
    _record_token_name, _record_value, _ref_basename, ensure_binary,
    extract_records, load_localization,
)


def _rs_signature(rv: dict) -> float:
    """The rock's base RS value: the single non-zero entry of the signature vector
    (index 4 in practice, but taken as max-nonzero to be robust to slot shuffles)."""
    sig = _component(rv, "SSCSignatureSystemParams") or {}
    bsp = (sig.get("radarProperties") or {}).get("baseSignatureParams") or {}
    sigs = bsp.get("signatures") or []
    return max((s for s in sigs if isinstance(s, (int, float))), default=0.0)


# Class-name family tokens stripped to get a readable rock label when localisation
# has no depositName (or for the per-mineral suffix). Order-independent token drop.
_MINEABLE_NOISE = {"mineablerock", "mineable", "rock", "fps", "groundvehicle", "ground",
                   "vehicle", "deposit", "felsic", "minable", "asteroid", "legendary",
                   "epic", "rare", "uncommon", "common", "pure", "small", "large",
                   "ore", "raw"}
# Placeholder / dev entities that aren't real mineables -- skip them.
_MINEABLE_SKIP = re.compile(r"(test|template|dummy|placeholder|abandon|angular_smooth)", re.I)


def _mineable_label(cls: str, deposit_name: str) -> str:
    """Readable rock name. Prefers the localised deposit name (e.g. "Asteroid (C-Type)",
    "Granite Deposit"), appending the per-mineral suffix from the class only when it adds
    information the deposit name doesn't already carry (``AsteroidCTypeMineableRock_Iron``
    -> "Asteroid (C-Type) — Iron"; ``GraniteMineableRock_Granite`` -> "Granite Deposit").
    Falls back to a best-effort split of the class name when there's no localisation."""
    toks = [t for t in re.split(r"[_\s]+", camel_split(cls)) if t]
    mineral_toks = [t for t in toks if t.lower() not in _MINEABLE_NOISE
                    and not re.fullmatch(r"[A-Za-z]Type", t)]
    mineral = " ".join(mineral_toks).title().strip()
    if not deposit_name:
        return mineral or cls
    if mineral and mineral.lower() not in deposit_name.lower():
        return f"{deposit_name} — {mineral}"
    return deposit_name


def _composition(preset_path: str, elem_index: dict, loc: dict,
                 elem_cache: dict) -> dict:
    """Parse a MineableComposition preset into {deposit_name, min_distinct, elements}."""
    try:
        cv = _load_json(preset_path)["_RecordValue_"]
    except (OSError, ValueError, KeyError):
        return {"deposit_name": "", "min_distinct": 0, "elements": []}
    elements = []
    for part in cv.get("compositionArray") or []:
        base = _ref_basename(part.get("mineableElement"))
        if base in elem_cache:
            name = elem_cache[base]
        else:
            ep = elem_index.get(base or "")
            name = elem_cache[base] = _record_token_name(ep) if ep else (base or "")
        elements.append({
            "element": name,
            "min_pct": part.get("minPercentage"),
            "max_pct": part.get("maxPercentage"),
            "probability": part.get("probability"),
        })
    return {
        "deposit_name": _loc_text(cv.get("depositName"), loc),
        "min_distinct": cv.get("minimumDistinctElements") or 0,
        "elements": elements,
    }


def _num(v):
    """``v`` if it's a real number (not bool / Vec4 / struct), else None -- so an
    unexpectedly-structured field is dropped rather than dumped raw into the output."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _mechanics(rv: dict, gp_index: dict, gp_cache: dict) -> dict | None:
    """Per-rock cracking mechanics (M1): the break-difficulty model the mining HUD
    doesn't show. Laser power needed comes from the rock's own
    ``SMineableHealthComponentParams`` health map; the shared balance (resistance,
    optimal-window shape, instability pulse, mass, SCU/volume) from the
    ``MiningGlobalParams`` record its ``MineableParams.globalParams`` ref points at.
    Every field is scalar-guarded (``damageStrength`` is a Vec4 curve and
    ``mineableInstabilityParams`` a struct, so we take the instability *wave period* and
    skip the curve). Returns a compact dict, or None when the rock carries none of it."""
    mp = _component(rv, "MineableParams") or {}
    center = (_component(rv, "SMineableHealthComponentParams") or {}).get(
        "damageMapParamsCenter") or {}
    out: dict = {
        "laser_power": _num(center.get("laserDamageFullValue")),
        "filled_factor": _num(mp.get("filledFactor")),
    }
    gp_base = _ref_basename(mp.get("globalParams"))
    if gp_base:
        gp = gp_cache.get(gp_base)
        if gp is None:
            gp = gp_cache[gp_base] = _record_value(gp_index.get(gp_base))
        inst = gp.get("mineableInstabilityParams")
        out.update({
            "resistance": _num(gp.get("resistanceCurveFactor")),
            "window_size": _num(gp.get("optimalWindowSize")),
            "window_max": _num(gp.get("optimalWindowMaxSize")),
            "instability": _num(inst.get("instabilityWavePeriod")) if isinstance(inst, dict)
                           else _num(inst),
            "mass": _num(gp.get("defaultMass")),
            "scu_per_volume": _num(gp.get("cSCUPerVolume")),
        })
    out = {k: v for k, v in out.items() if v is not None}
    return out or None


def build_mineables(records_root: str, loc: dict) -> list:
    """Every mineable rock -> {class, name, deposit_name, rs, min_distinct, composition,
    mechanics}, read from an extracted DataCore records root (the same one ``build_ships``
    uses).

    RS is the rock's base radar signature; the in-game HUD shows ``rs x cluster size``.
    Composition is the probabilistic mineral makeup of the rock's class. ``mechanics`` is
    the break-difficulty model (see :func:`_mechanics`). Rocks with no RS (a handful of
    test/placeholder entities) are skipped."""
    comp_index = _index_by_basename(records_root, "rockcompositionpresets")
    elem_index = _index_by_basename(records_root, "mineableelements")
    # "mining" also holds the presets/elements indexed above; keep only the global-params
    # records so a same-basename preset can't be loaded in their place.
    gp_index = {k: v for k, v in _index_by_basename(records_root, "mining").items()
                if k.startswith("miningglobalparams")}
    comp_cache: dict[str, dict] = {}
    elem_cache: dict[str, str] = {}
    gp_cache: dict[str, dict] = {}
    rocks: list[dict] = []
    for p in glob.glob(os.path.join(records_root, "**", "entities", "mineable", "*.json"),
                       recursive=True):
        try:
            d = _load_json(p)
            rv = d["_RecordValue_"]
            cls = d["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        if _MINEABLE_SKIP.search(cls):
            continue
        rs = _rs_signature(rv)
        if rs <= 0:
            continue
        mp = _component(rv, "MineableParams") or {}
        comp_base = _ref_basename(mp.get("composition"))
        if comp_base and comp_base in comp_cache:
            comp = comp_cache[comp_base]
        elif comp_base and comp_base in comp_index:
            comp = comp_cache[comp_base] = _composition(comp_index[comp_base], elem_index,
                                                        loc, elem_cache)
        else:
            comp = {"deposit_name": "", "min_distinct": 0, "elements": []}
        rocks.append({
            "class": cls,
            "name": _mineable_label(cls, comp["deposit_name"]),
            "deposit_name": comp["deposit_name"],
            "rs": round(rs),
            "min_distinct": comp["min_distinct"],
            "composition": comp["elements"],
            "mechanics": _mechanics(rv, gp_index, gp_cache),
        })
    rocks.sort(key=lambda r: (r["rs"], r["class"]))
    return rocks


def build_mineables_from_p4k(p4k: str, sb: str | None = None,
                             progress=lambda m: None) -> list:
    """Full-extract orchestrator: extract the DataCore + localisation from the local
    install and build the mineable-rock list. Heavy (a full ``dcb extract``), so gated on
    a major game-version bump like ship cargo -- see ``catalogs.refresh_loop``."""
    sb = sb or ensure_binary()
    workdir = tempfile.mkdtemp(prefix="starlogger-mineables-")
    try:
        progress("extracting DataCore for mineables")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return build_mineables(recs, loc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
