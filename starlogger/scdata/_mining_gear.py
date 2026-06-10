"""Mining equipment -- ship-mounted laser *heads* and their consumable *modules*.

The companion to ``_mineables.py``: where that reads the *rock* side (RS + cracking
mechanics), this reads the *gear* side a player brings to the rock. Both come out of the
one full ``dcb extract`` and ride the same version trigger.

Two item families, both ``EntityClassDefinition`` SCItems under ``entities/scitem``:

* **Heads** (``ships/weapons/mining_laser_<MFR>_<model>_s{1,2}``) -- the laser fitted to a
  mining turret. Its absolute extraction power is a weapon ``fireAction`` damage value
  (``DamageEnergy``); its handling is a set of percentage *modifiers* on the shared
  mining minigame (resistance / optimal-window / instability / cluster) plus a throttle
  floor. Each head exposes 0-3 ``miningConsumable`` module ports.
  We keep only ship-turret heads (size 1/2); the size-0 handheld multitool heads and the
  MPUV/ROC arm are out of scope (the dashboard tracks *ships*).
* **Modules** (``ships/utility/mining/miningarm/mining_modules_<active|passive>_<name>``) --
  the gadgets slotted into a head's module ports, applying their own percentage modifiers to
  the same minigame. Two families: *passive* (always-on, tiered mk1/2/3 -- Focus, Rieger,
  Torrent, Vaux, XTR, and the yield-only FLTR filters) and *active* (triggered consumables
  with a charge count -- Brandt, Lifeline, Stampede, Surge, …). The legacy 3.x
  ``weapons/devices/mining_gadget_*`` set (OptiMax/WaveShift/…) is NOT read -- it's superseded
  by this family. The two ``mining_modules_vehiclemod_*`` (ATLS GEO / ROC built-ins) are skipped.

Head manufacturer is taken from the class-name token (``Mining_Laser_GRIN_Arbor_S1`` -> GRIN),
NOT the ``Manufacturer`` ref -- that ref is unreliable for heads (the GRIN Arbor points at
``scitemmanufacturer.misc``). Module class names carry no manufacturer token, so modules read
it from the ref (reliable for them). Display names resolve the item's ``Localization.Name`` @key.
"""

from __future__ import annotations

import glob
import os
import re
import shutil

from ._p4k import (
    _component, _load_json, _loc_text, ensure_binary, extract_records,
    load_localization, scratch_dir,
)

# Placeholder / dev / out-of-scope entities (handheld + ROC arm handled separately by size).
_GEAR_SKIP = re.compile(r"(test|template|dummy|placeholder|_beam)", re.I)

# MFR code -> friendly manufacturer name (the brands that make mining gear). Keyed by the
# upper-cased class-name token (heads) or Manufacturer-ref basename token (modules).
_MFR_NAMES = {
    "GRIN": "Greycat Industrial",
    "SHIN": "Shubin Interstellar",
    "THCN": "Thermyte Concern",
    "DRAK": "Drake Interplanetary",
    "MISC": "MISC",
}

# miningLaserModifiers field -> our compact key. Each game field is a
# FloatModifierMultiplicative ({value: <percent>}) or null.
_MOD_KEYS = {
    "resistanceModifier": "resistance",
    "optimalChargeWindowSizeModifier": "window_size",
    "optimalChargeWindowRateModifier": "window_rate",
    "laserInstability": "instability",
    "clusterFactorModifier": "cluster",
    "shatterdamageModifier": "shatter",
    "catastrophicChargeWindowRateModifier": "catastrophic",
}


def _num(v):
    """``v`` if a real number (not bool), else None."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _mfr_from_class(cls: str) -> tuple[str, str]:
    """``Mining_Laser_GRIN_Arbor_S1`` -> ("GRIN", "Greycat Industrial"). The 3rd
    underscore token is the manufacturer code; fall back to the bare token. Used for HEADS,
    whose ``Manufacturer`` ref is unreliable in the data (the GRIN Arbor points at misc)."""
    parts = cls.split("_")
    code = parts[2].upper() if len(parts) > 2 else ""
    return code, _MFR_NAMES.get(code, code.title())


def _mfr_from_ref(rv: dict) -> tuple[str, str]:
    """Manufacturer from the item's ``AttachDef.Manufacturer`` ref basename
    (``scitemmanufacturer.shin.json`` / ``grin.json`` -> SHIN / GRIN). Used for MODULES,
    whose class names carry no manufacturer token but whose ref IS reliable."""
    ad = (_component(rv, "SAttachableComponentParams") or {}).get("AttachDef") or {}
    ref = ad.get("Manufacturer")
    if not isinstance(ref, str):
        return "", ""
    stem = os.path.basename(ref.split("?")[0])[:-5]            # drop dir + ".json"
    code = stem.rsplit(".", 1)[-1].upper()                     # "scitemmanufacturer.shin" -> SHIN
    return code, _MFR_NAMES.get(code, code.title())


_MK_RE = re.compile(r"_mk(\d)$", re.I)


def _laser_modifiers(struct: dict | None) -> dict:
    """A ``MiningLaserModifiers`` struct -> ``{our_key: percent}`` for the non-null
    entries. Used for both a head's own modifiers and a module's deltas (same struct)."""
    out: dict = {}
    for field, key in _MOD_KEYS.items():
        m = (struct or {}).get(field)
        val = _num(m.get("value")) if isinstance(m, dict) else None
        if val is not None:
            out[key] = val
    return out


def _item_name(rv: dict, loc: dict, cls: str) -> str:
    """Resolve the item's localised display name from ``AttachDef.Localization.Name``;
    fall back to a readable split of the class name."""
    ad = (_component(rv, "SAttachableComponentParams") or {}).get("AttachDef") or {}
    name = _loc_text(((ad.get("Localization") or {}).get("Name")) or "", loc)
    if name and not name.startswith("@"):
        return name
    return re.sub(r"_", " ", cls).replace("Mining Laser ", "").replace("Mining Gadget ", "").strip()


def _module_slots(rv: dict) -> int:
    """How many consumable-module ports a head exposes -- the ports whose
    ``RequiredPortTags`` carry ``miningConsumable`` (0-3; not the VEN weapon-attach port)."""
    ports = (_component(rv, "SItemPortContainerComponentParams") or {}).get("Ports") or []
    return sum(1 for p in ports
               if "miningConsumable" in (p.get("RequiredPortTags") or ""))


def _head(rv: dict, cls: str, loc: dict) -> dict | None:
    """One mining-laser head record -> the catalog entry, or None if out of scope
    (size 0 handheld, the MPUV/ROC arm, or no weapon power)."""
    ad = (_component(rv, "SAttachableComponentParams") or {}).get("AttachDef") or {}
    size = _num(ad.get("Size"))
    if size not in (1, 2) or "mpuv" in cls.lower():
        return None  # ship-turret heads only
    weapon = _component(rv, "SCItemWeaponComponentParams") or {}
    actions = weapon.get("fireActions") or []
    def _power(i):
        if i < len(actions):
            return _num((actions[i].get("damagePerSecond") or {}).get("DamageEnergy"))
        return None
    power = _power(0)
    if not power or power <= 1:
        return None  # no real extraction beam
    primary = actions[0]
    ml = _component(rv, "SEntityComponentMiningLaserParams") or {}
    code, mfr = _mfr_from_class(cls)
    return {
        "class": cls,
        "name": _item_name(rv, loc, cls),
        "manufacturer": mfr,
        "manufacturer_code": code,
        "size": int(size),
        "power": power,
        "secondary_power": _power(1),
        "optimal_range": [_num(primary.get("fullDamageRange")),
                          _num(primary.get("zeroDamageRange"))],
        "throttle_min": _num(ml.get("throttleMinimum")),
        "module_slots": _module_slots(rv),
        "modifiers": _laser_modifiers(ml.get("miningLaserModifiers")),
    }


def _module(rv: dict, cls: str, loc: dict) -> dict | None:
    """One mining-module record -> the catalog entry. Kept even with no cracking modifiers
    (e.g. the FLTR spectral filters affect yield, not the crack, but are still slottable);
    skips the vehicle-built-in mods (ATLS GEO / ROC). active vs passive is read from the
    class family, NOT charges (passive modules also have charges:1)."""
    low = cls.lower()
    if "_vehiclemod_" in low:
        return None                                 # vehicle-built-in, not a turret module
    am = _component(rv, "EntityComponentAttachableModifierParams") or {}
    mods: dict = {}
    for m in am.get("modifiers") or []:
        mods.update(_laser_modifiers(m.get("MiningLaserModifier")))
    code, mfr = _mfr_from_ref(rv)
    charges = _num(am.get("charges"))
    tier = int(_MK_RE.search(cls).group(1)) if _MK_RE.search(cls) else None
    # "Focus III Module" -> "Focus III"; the in-game item name drops the bare "Module" tail.
    name = re.sub(r"\s+Module$", "", _item_name(rv, loc, cls))
    return {
        "class": cls,
        "name": name,
        "manufacturer": mfr,
        "manufacturer_code": code,
        "tier": tier,                               # mk1/2/3 -> 1/2/3 (passive), else None
        "charges": int(charges) if charges is not None else None,
        "active": "_active_" in low,                # vs the passive (always-on) family
        "modifiers": mods,
    }


def _load_entities(records_root: str, subpath: str, *prefixes: str) -> list:
    """``(cls, rv)`` for every real ``<prefix>_*`` entity json under ``subpath`` (any of the
    given filename prefixes)."""
    out = []
    for prefix in prefixes:
        for p in glob.glob(os.path.join(records_root, "**", subpath, f"{prefix}_*.json"),
                           recursive=True):
            if _GEAR_SKIP.search(os.path.basename(p)):
                continue
            try:
                d = _load_json(p)
                rv = d["_RecordValue_"]
                cls = d["_RecordName_"].split(".", 1)[1]
            except (OSError, ValueError, KeyError, IndexError):
                continue
            out.append((cls, rv))
    return out


def build_mining_gear(records_root: str, loc: dict) -> dict:
    """Read every ship-turret mining head + slottable module from an extracted DataCore
    records root. Returns ``{"heads": [...], "modules": [...]}`` (heads sorted by size then
    power; modules grouped active-first then by name). See module docstring for the shapes.

    Modules are the current ``mining_modules_<active|passive>_*`` family (Focus, Rieger,
    Torrent, Vaux, XTR, FLTR + the active consumables); the legacy ``mining_gadget_*`` set
    (3.x OptiMax/WaveShift/…) is intentionally NOT read."""
    heads = []
    for cls, rv in _load_entities(
            records_root, os.path.join("scitem", "ships", "weapons"), "mining_laser"):
        h = _head(rv, cls, loc)
        if h:
            heads.append(h)
    modules = []
    for cls, rv in _load_entities(
            records_root, os.path.join("scitem", "ships", "utility", "mining", "miningarm"),
            "mining_modules"):
        m = _module(rv, cls, loc)
        if m:
            modules.append(m)
    heads.sort(key=lambda h: (h["size"], h["power"], h["class"]))
    # passive (always-on) first, then active consumables; alphabetical within each.
    modules.sort(key=lambda m: (m["active"], m["name"]))
    return {"heads": heads, "modules": modules}


def build_mining_gear_from_p4k(p4k: str, sb: str | None = None,
                               progress=lambda m: None) -> dict:
    """Full-extract orchestrator: extract the DataCore + localisation from the local
    install and build the mining-gear catalog. Heavy (a full ``dcb extract``), so gated on
    a major game-version bump like ship cargo -- see ``catalogs.refresh_loop``."""
    sb = sb or ensure_binary()
    workdir = scratch_dir("starlogger-mininggear-")
    try:
        progress("extracting DataCore for mining gear")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return build_mining_gear(recs, loc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
