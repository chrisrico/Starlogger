"""Ship radar components -- the radar fitted to a ship's radar hardpoint.

The mining-relevant axis is the **resource-signature (RS)** detection channel: how well the
radar reads a mineable deposit's signature, which is what lets you identify a rock's
composition from farther away (a prospecting quality-of-life lever, not a yield one -- the
head + modules drive yield; see ``_mining_gear``).

In the DataCore each radar's ``SCItemRadarComponentParams.signatureDetection`` is an
8-slot array indexed by a fixed signature-type enum. **Index 4 is the resource channel**,
derived empirically and matching the community "RS%" tables: the stock mining radar
``CHCO Surveyor-Lite`` reads 0.8 there and the stealth ``Observer-Lite`` 0.6, while every
other size-1 radar reads 1.0; index 4 is also the only channel with elevated piercing, and
indices 3/7 are always the disabled (0.0) channels. We surface ``rs`` (index-4 sensitivity,
0-1) + ``rs_piercing`` as the mining stat, plus ``sensitivity_max`` (best channel) as a
generic fallback ranking key.

Built from the same full ``Data.p4k`` DataCore extract as mineables/mining_gear, on the same
version trigger (see ``catalogs``). Manufacturer resolves from the item's ``AttachDef``
ref + the ``manufacturer_name<code>`` localisation key; the display name from
``AttachDef.Localization.Name``.
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

# Index into SCItemRadarSignatureDetection[] carrying the mineable/resource signature -- the
# mining detection channel. See the module docstring for the empirical derivation; a test
# pins it (Surveyor-Lite 0.8 < a 1.0-RS radar). Don't change without re-deriving.
RS_CHANNEL = 4

# Placeholder / non-player radars to skip: dev templates, fakes, AI/unmanned hulls, the
# Vanduul Idris stand-in, the anti-personnel turret.
_RADAR_SKIP = re.compile(r"(default|template|fake|unmanned|antipersonnel|idris_temp)", re.I)


def _num(v):
    """``v`` if a real number (not bool), else None."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _attach_def(rv: dict) -> dict:
    return (_component(rv, "SAttachableComponentParams") or {}).get("AttachDef") or {}


def _mfr(rv: dict, loc: dict) -> tuple[str, str]:
    """(code, full) manufacturer from the item's ``AttachDef.Manufacturer`` ref basename
    (``.../scitemmanufacturer/chco.json`` -> CHCO) + the ``manufacturer_name<code>``
    localisation entry (CHCO -> "Chimera Communications"); falls back to a title-cased code."""
    ref = _attach_def(rv).get("Manufacturer")
    if not isinstance(ref, str):
        return "", ""
    stem = os.path.basename(ref.split("?")[0])[:-5]          # ".../chco.json" -> "chco"
    code = stem.rsplit(".", 1)[-1].upper()                   # tolerate a "pkg.chco" form too
    return code, loc.get(f"manufacturer_name{code}".lower(), code.title())


def _name(rv: dict, loc: dict, cls: str) -> str:
    """Localised display name from ``AttachDef.Localization.Name``; fall back to the class."""
    name = _loc_text(((_attach_def(rv).get("Localization") or {}).get("Name")) or "", loc)
    if name and not name.startswith("@"):
        return name
    return re.sub(r"_", " ", cls).replace("RADR ", "").strip()


def _radar(rv: dict, cls: str, loc: dict) -> dict | None:
    """One radar entity -> the catalog entry, or None if it has no size or radar component."""
    ad = _attach_def(rv)
    size = _num(ad.get("Size"))
    rc = _component(rv, "SCItemRadarComponentParams")
    if size is None or not rc:
        return None
    sd = rc.get("signatureDetection") or []
    if len(sd) <= RS_CHANNEL:
        return None
    rs_entry = sd[RS_CHANNEL] or {}
    sens = [(_num(e.get("sensitivity")) or 0.0) for e in sd]
    code, mfr = _mfr(rv, loc)
    grade = _num(ad.get("Grade"))
    ping = _num((rc.get("pingProperties") or {}).get("cooldownTime"))
    return {
        "class": cls,
        "name": _name(rv, loc, cls),
        "manufacturer": mfr,
        "manufacturer_code": code,
        "size": int(size),
        "grade": int(grade) if grade is not None else None,
        "sub_type": ad.get("SubType") or None,
        "rs": round(_num(rs_entry.get("sensitivity")) or 0.0, 3),
        "rs_piercing": round(_num(rs_entry.get("piercing")) or 0.0, 3),
        "sensitivity_max": round(max(sens) if sens else 0.0, 3),
        "ping_cooldown": ping,
    }


def _load_entities(records_root: str, subpath: str, prefix: str) -> list:
    """``(cls, rv)`` for every real ``<prefix>_*`` entity json under ``subpath`` (skipping
    the placeholder/template radars)."""
    out = []
    for p in glob.glob(os.path.join(records_root, "**", subpath, f"{prefix}_*.json"),
                       recursive=True):
        if _RADAR_SKIP.search(os.path.basename(p)):
            continue
        try:
            d = _load_json(p)
            rv = d["_RecordValue_"]
            cls = d["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        out.append((cls, rv))
    return out


def build_radar(records_root: str, loc: dict) -> list:
    """Read every player ship-radar component from an extracted DataCore records root,
    returned best-for-mining first (resource-signature ``rs`` desc, then piercing, then
    size, then name). See the module docstring for the field shape + RS-channel rationale."""
    out = []
    for cls, rv in _load_entities(records_root, os.path.join("scitem", "ships", "radar"), "radr"):
        r = _radar(rv, cls, loc)
        if r:
            out.append(r)
    out.sort(key=lambda r: (-r["rs"], -r["rs_piercing"], r["size"], r["name"]))
    return out


def build_radar_from_p4k(p4k: str, sb: str | None = None,
                         progress=lambda m: None) -> list:
    """Full-extract orchestrator: extract the DataCore + localisation from the local install
    and build the radar catalog. Heavy (a full ``dcb extract``), gated on a major game-version
    bump like the other full-extract catalogs -- see ``catalogs.refresh_loop``."""
    sb = sb or ensure_binary()
    workdir = scratch_dir("starlogger-radar-")
    try:
        progress("extracting DataCore for radar")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return build_radar(recs, loc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
