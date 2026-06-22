"""Space mining locations -- which asteroid fields / belts / Lagrange fields yield what.

The space counterpart to ``_body_mineables.py`` (planet/moon *surface* mineables): where a
mineral can be ship-mined out in the black. Unlike the surface lists -- which live as prose in
``global.ini`` -- the space data is structured, in the **HarvestableProviderPreset** DataCore
records under ``harvestable/providerpresets/system/<sys>/asteroidfield/``. Each preset is the
spawn config for one field and references ship-mineable rock archetypes by file:

    mining_asteroid<common|uncommon|rare>_<mineral>.json

so the mineral AND its rarity tier fall straight out of the archetype name (``..._rare_bexalite``
-> Bexalite, rare). The field display name comes from the record token (``HPP_Nyx_KeegerBelt``
-> "Keeger Belt"), with the per-body belts resolved to the body name (``HPP_Stanton2c_Belt`` ->
"Yela Belt"); the system from the record path.

Note: nothing in the *static* data binds a field archetype to a *named* Lagrange point (e.g.
"Hurston L1") -- that placement is runtime -- so the Lagrange fields are catalogued here as their
archetype (Lagrange A..G). The real points are layered on after the build from a committed
starmap bundle (``starmap.add_field_points``), which keys back to this archetype name. The generic
``AsteroidCluster_*`` / ``ResourceRush_*`` presets are not real destinations, so they're skipped.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil

from ..patterns import camel_split
from ._p4k import (
    _load_json, ensure_binary, extract_records, load_localization, scratch_dir,
)

# Ship-mineable rock archetype: mining_asteroid<rarity>_<mineral>(.json). Rarity + mineral.
_ARCHETYPE = re.compile(r"mining_asteroid(common|uncommon|rare)_([a-z]+)")
_RARITY_ORDER = {"common": 0, "uncommon": 1, "rare": 2}

# Generic spawn archetypes that aren't a navigable destination -> not catalogued.
_SKIP = re.compile(r"^(AsteroidCluster_|HPP_ResourceRush)", re.I)
# Per-body belt token: HPP_Stanton2c_Belt -> resolve "Stanton2c" to the body name ("Yela").
_BODY_BELT = re.compile(r"^HPP_(Stanton\w+?)_Belt$", re.I)


def _field_name(token: str, loc: dict) -> str:
    """Readable field name from a preset's record token. ``HPP_AaronHalo`` -> "Aaron Halo",
    ``HPP_Lagrange_E`` -> "Lagrange E", ``HPP_Nyx_KeegerBelt`` -> "Keeger Belt",
    ``HPP_Stanton2c_Belt`` -> "Yela Belt" (the body name)."""
    m = _BODY_BELT.match(token)
    if m:
        body = (loc.get(m.group(1).lower()) or "").strip()
        if body:
            return f"{body} Belt"
    t = re.sub(r"^HPP_", "", token, flags=re.I)        # drop the preset prefix
    t = re.sub(r"^(Nyx|Pyro|Stanton)_", "", t)         # system is carried separately
    t = camel_split(t.replace("_", " "))               # CamelCase + underscores -> words
    t = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", t)         # "Cool01" -> "Cool 01"
    return re.sub(r"\s+", " ", t).strip().title()


def _system_from_path(path: str) -> str:
    """Star-system from the record path: ``.../system/stanton/asteroidfield/...`` -> "Stanton"."""
    parts = path.replace("\\", "/").split("/")
    try:
        return parts[parts.index("system") + 1].title()
    except (ValueError, IndexError):
        return ""


def _yields(record: dict) -> list:
    """The ship-mineable yields of one preset: ``[{mineral, rarity}]`` from its
    ``mining_asteroid<rarity>_<mineral>`` archetype references, best (most common) tier kept
    per mineral, ordered common -> rare then by name."""
    best: dict[str, str] = {}
    for rarity, mineral in _ARCHETYPE.findall(json.dumps(record)):
        name = mineral.title()
        if name not in best or _RARITY_ORDER[rarity] < _RARITY_ORDER[best[name]]:
            best[name] = rarity
    return [{"mineral": m, "rarity": r}
            for m, r in sorted(best.items(), key=lambda x: (_RARITY_ORDER[x[1]], x[0]))]


def build_space_mineables(records_root: str, loc: dict) -> list:
    """Every space mining field -> ``{name, system, ship_mineables: [{mineral, rarity}]}``,
    read from the HarvestableProviderPreset records under ``providerpresets/**/asteroidfield/``.

    Skips the generic ``AsteroidCluster_*`` / ``ResourceRush_*`` presets (not destinations) and
    any preset with no ship-mineable archetype. Sorted by (system, name)."""
    fields = []
    pat = os.path.join(records_root, "**", "providerpresets", "**", "asteroidfield", "*.json")
    for p in glob.glob(pat, recursive=True):
        try:
            d = _load_json(p)
            token = d["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        if _SKIP.match(token):
            continue
        ship = _yields(d)
        if not ship:
            continue
        fields.append({
            "name": _field_name(token, loc),
            "system": _system_from_path(p),
            "ship_mineables": ship,
        })
    fields.sort(key=lambda f: (f["system"], f["name"]))
    return fields


def build_space_mineables_from_p4k(p4k: str, sb: str | None = None,
                                   progress=lambda m: None) -> list:
    """Full-extract orchestrator: the provider presets are DataCore records (not localisation),
    so this rides the same full ``dcb extract`` as mineables/gear -- gated on a major
    game-version bump (see ``catalogs.refresh_loop``)."""
    sb = sb or ensure_binary()
    workdir = scratch_dir("starlogger-spacemineables-")
    try:
        progress("extracting DataCore for space mineables")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        return build_space_mineables(recs, loc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
