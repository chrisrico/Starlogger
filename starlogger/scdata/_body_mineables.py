"""Per-celestial-body mineables -- what each planet/moon yields, from the starmap.

The location side of the mining picture: where ``_mineables.py`` reads the *rock*
(RS + composition) and ``_mining_gear.py`` the *gear*, this reads *where in the 'verse*
a given mineral is found. The in-game starmap shows, for each body, a description naming
its mineable materials -- and that text is the only place the body->mineral mapping lives
(there is NO structured resource field on the record).

Each body is a ``StarMapObject`` record under
``libs/foundry/records/starmap/pu/system/<system>/<body>/starmapobject.*.json`` with a
localised ``name`` (``@Stanton1`` -> "Hurston") and ``description`` (``@Stanton1_Desc``)
@key. The description resolves (via ``global.ini``) to PROSE with fixed section headers,
each followed by ``\\n``-delimited items (the .ini stores literal backslash-n, not real
newlines):

    Potential Ship Mineables:   <- ship mining (Prospector/MOLE) -- the priority list
    Potential Hand Mineables:   <- handheld / FPS mining
    Potential Harvestables:     <- gatherables (optional)
    Potential Creatures:        <- fauna (optional)

Items may carry a parenthetical qualifier (``Janalite (Caves only)``), kept verbatim.
The system is taken from the record path; only bodies that actually list a ship or hand
mineable are emitted (the system root, jump points, and the ``demo`` system carry none).
"""

from __future__ import annotations

import glob
import os
import shutil

from ._p4k import (
    _load_json, _loc_text, ensure_binary, extract_records, load_localization, scratch_dir,
)

# Description section header -> our output key. Matched on an exact (stripped) line.
_SECTIONS = {
    "Potential Ship Mineables:": "ship_mineables",
    "Potential Hand Mineables:": "hand_mineables",
    "Potential Harvestables:": "harvestables",
    "Potential Creatures:": "creatures",
}


def _parse_sections(desc: str) -> dict:
    """Split a body description's prose into the four mineable/harvest/creature lists.

    Walks the lines tracking the current section: an exact header line switches section,
    a blank line ends it, any other non-empty line is an item of the active section (the
    leading prose before the first header has no active section, so it's ignored). The
    .ini stores literal ``\\n`` separators, so normalise those to real newlines first."""
    out: dict[str, list] = {v: [] for v in _SECTIONS.values()}
    cur: str | None = None
    for raw in desc.replace("\\n", "\n").split("\n"):
        line = raw.strip()
        if line in _SECTIONS:
            cur = _SECTIONS[line]
        elif not line:
            cur = None
        elif cur:
            out[cur].append(line)
    return out


def _system_from_path(path: str) -> str:
    """The star-system name from a record path: ``.../system/stanton/stanton1/...``
    -> "Stanton". Falls back to "" when the layout is unexpected."""
    parts = path.replace("\\", "/").split("/")
    try:
        return parts[parts.index("system") + 1].title()
    except (ValueError, IndexError):
        return ""


def build_body_mineables(records_root: str, loc: dict) -> list:
    """Every celestial body that lists mineables -> ``{name, system, ship_mineables,
    hand_mineables, harvestables, creatures, description}``, parsed from the starmap
    ``StarMapObject`` descriptions in an extracted DataCore records root.

    ``description`` is kept raw (with real newlines) so the source prose is preserved.
    Only bodies with a non-empty ship OR hand mineable list are returned -- the system
    root / jump points / dev ``demo`` system carry no such sections. Sorted by
    (system, name)."""
    bodies: list[dict] = []
    pat = os.path.join(records_root, "**", "starmap", "pu", "system", "**",
                       "starmapobject.*.json")
    for p in glob.glob(pat, recursive=True):
        try:
            rv = _load_json(p)["_RecordValue_"]
        except (OSError, ValueError, KeyError):
            continue
        name = _loc_text(rv.get("name"), loc).strip()   # some names carry a trailing nbsp
        desc = _loc_text(rv.get("description"), loc)
        if not name or not desc:
            continue
        desc = desc.replace("\\n", "\n")
        sections = _parse_sections(desc)
        if not (sections["ship_mineables"] or sections["hand_mineables"]):
            continue
        bodies.append({
            "name": name,
            "system": _system_from_path(p),
            "ship_mineables": sections["ship_mineables"],
            "hand_mineables": sections["hand_mineables"],
            "harvestables": sections["harvestables"],
            "creatures": sections["creatures"],
            "description": desc,
        })
    bodies.sort(key=lambda b: (b["system"], b["name"]))
    return bodies


def build_body_mineables_from_p4k(p4k: str, sb: str | None = None,
                                  progress=lambda m: None) -> list:
    """Full-extract orchestrator: extract the DataCore + localisation from the local
    install and build the per-body mineables catalog. The starmap records come from the
    DataCore (``dcb extract``), so this rides the same full extract as mineables/gear --
    gated on a major game-version bump (see ``catalogs.refresh_loop``)."""
    sb = sb or ensure_binary()
    workdir = scratch_dir("starlogger-bodymineables-")
    try:
        progress("extracting DataCore for body mineables")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return build_body_mineables(recs, loc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
