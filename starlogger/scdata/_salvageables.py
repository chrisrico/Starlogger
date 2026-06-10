"""Salvageable-wreck radar signatures (the salvage analog of mineable rocks).

Unlike a mineable rock -- which decodes from its RS into a probabilistic *mineral
composition* -- a salvageable carries no composition at all: its RS only **identifies
a target**. Two families exist in the DataCore (filter ``*Salvageable*``):

* **whole-ship structural hulls** (``SalvageableDebris_<Ship>``) -- the RS encodes the
  ship: Avenger Titan 1700, Ares Inferno 1850, Redeemer 2000, C2 2400, 890 Jump 3000.
* **ship-debris panels** (``SalvageableRepairable_ShipDebris_<size>_<Ship>_<part>``) --
  hull chunks you scrape; **every one reads a flat 2000** regardless of size label, so a
  reading of ``n x 2000`` means *n panels*, not a bigger panel.

There is no RMC/SCU yield field anywhere -- salvage yield is computed from hull surface
at runtime, not stored -- so this is deliberately a thin "what is that contact" catalog,
surfaced as a separate section in the rock-lookup Identify tab. The RS value lives on the
same ``SSCSignatureSystemParams`` radar component the rocks use, so this rides the same
full DataCore extract.
"""

from __future__ import annotations

import glob
import os
import re
import shutil

from ._p4k import (
    _component, _load_json, ensure_binary, extract_records, load_localization,
    scratch_dir,
)

# Keep only real salvage *targets*: whole-ship debris hulls and ship-debris panels.
# Everything else under ``*Salvageable*`` is a mission/level prop (pipes, doors, radiation
# vents, delving/prelude set dressing) or a template/test entity -- not a radar target.
_SALV_SHIP = re.compile(r"^SalvageableDebris_(?!test\b)(?P<ship>[A-Za-z0-9]+)$", re.I)
_SALV_PANEL = re.compile(r"ShipDebris_(?P<size>S|M|L|XL)_(?P<rest>[A-Za-z0-9_]+)$", re.I)
# Class-name spelling fixes present in the raw data.
_SHIP_FIXES = {"vangaurd": "Vanguard", "jav": "Javelin", "c2": "C2", "890": "890 Jump",
               "arestitan": "Ares", "aresinferno": "Ares Inferno",
               "avengertitan": "Avenger Titan"}


def _rs_signature(rv: dict) -> float:
    """The wreck's base RS: the single non-zero entry of its radar signature vector
    (same component/shape as a mineable rock; taken as max-nonzero to be slot-robust)."""
    sig = _component(rv, "SSCSignatureSystemParams") or {}
    bsp = (sig.get("radarProperties") or {}).get("baseSignatureParams") or {}
    sigs = bsp.get("signatures") or []
    return max((s for s in sigs if isinstance(s, (int, float))), default=0.0)


def _titleize(tok: str) -> str:
    """A raw class token -> a readable label, applying the known spelling fixes."""
    return _SHIP_FIXES.get(tok.lower(), tok[:1].upper() + tok[1:])


def _ship_label(token: str) -> str:
    """``AvengerTitan`` -> "Avenger Titan"; ``AresInferno`` -> "Ares Inferno". Whole-ship
    debris class suffixes are CamelCase ship codenames; split on case + digit boundaries,
    then apply the spelling fixes (so ``C2`` and ``890`` keep their names)."""
    fixed = _SHIP_FIXES.get(token.lower())
    if fixed:
        return fixed
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", token)
    return " ".join(parts) if parts else token


def _panel_entry(cls: str, rs: float, m: re.Match) -> dict:
    """A ship-debris panel -> {class, name, rs, kind:'panel', size, ship, part}. The class
    tail after the size token is ``<Ship>_<part...>[_<variant>]`` (a trailing single-letter
    a/b/c is a model variant, dropped from the readable part name)."""
    size = m.group("size").upper()
    toks = [t for t in m.group("rest").split("_") if t]
    ship = _titleize(toks[0]) if toks else ""
    rest = toks[1:]
    if rest and len(rest[-1]) == 1 and rest[-1].isalpha():   # drop a/b/c variant suffix
        rest = rest[:-1]
    part = " ".join(_titleize(t) for t in rest)
    name = f"{ship} {part}".strip() or ship or cls
    return {"class": cls, "name": f"{name} ({size})" if part or ship else cls,
            "rs": round(rs), "kind": "panel", "size": size, "ship": ship, "part": part}


def build_salvageables(records_root: str, loc: dict | None = None) -> list:
    """Every salvageable wreck target -> {class, name, rs, kind, ...}, read from an
    extracted DataCore records root (the same one ``build_mineables`` uses).

    ``kind`` is ``"ship"`` (a whole-ship structural hull, RS identifies the ship) or
    ``"panel"`` (a hull chunk; flat RS 2000, so RS / 2000 counts panels). Wrecks with no
    RS, and every non-target prop/template under ``*Salvageable*``, are skipped. ``loc`` is
    accepted for signature parity with ``build_mineables`` but unused (names derive from the
    class -- the localized ``@vehicle_Name*`` is the *donor ship*, not the wreck)."""
    # Linux glob is case-sensitive and StarBreaker writes CamelCase record filenames
    # (``SalvageableDebris_*``), so match the case-insensitive prefixes via character
    # classes and gate precisely on a lowercased basename. A panel file is prefixed both
    # ``Salvageable`` and ``ShipDebris``, so union the matches and dedupe.
    files: set[str] = set()
    for pat in ("**/[Ss]alvageable*.json", "**/*[Ss]hip[Dd]ebris*.json"):
        files.update(glob.glob(os.path.join(records_root, pat), recursive=True))
    seen: set[str] = set()
    out: list[dict] = []
    for p in sorted(files):
        base = os.path.basename(p).lower()
        if not (base.startswith("salvageable") or "shipdebris" in base):
            continue
        try:
            d = _load_json(p)
            rv = d["_RecordValue_"]
            cls = d["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        if cls in seen:
            continue
        rs = _rs_signature(rv)
        if rs <= 0:
            continue
        mp = _SALV_PANEL.search(cls)
        ms = _SALV_SHIP.match(cls)
        if mp:
            entry = _panel_entry(cls, rs, mp)
        elif ms:
            ship = _ship_label(ms.group("ship"))
            entry = {"class": cls, "name": ship, "rs": round(rs),
                     "kind": "ship", "ship": ship}
        else:
            continue
        seen.add(cls)
        out.append(entry)
    # Panels first (the common n x 2000 case), then ships, each by RS then name.
    out.sort(key=lambda w: (w["kind"] != "panel", w["rs"], w["name"]))
    return out


def build_salvageables_from_p4k(p4k: str, sb: str | None = None,
                                progress=lambda m: None) -> list:
    """Full-extract orchestrator: extract the DataCore from the local install and build the
    salvageable-wreck list. Heavy (a full ``dcb extract``), so gated on a major game-version
    bump like the mineable rocks -- see ``catalogs.refresh_loop``."""
    sb = sb or ensure_binary()
    workdir = scratch_dir("starlogger-salvageables-")
    try:
        progress("extracting DataCore for salvageables")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return build_salvageables(recs, loc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
