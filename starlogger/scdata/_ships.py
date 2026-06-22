"""Resolve every cargo/mining vehicle's SCU + per-bay grid geometry from the DataCore."""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess

from ._p4k import (
    SCU_M, _GRADE_LETTER, _deep_find, _deep_walk,
    _load_json, _loc_text, _run, ensure_binary,
    extract_records, load_localization, scratch_dir,
)


# AttachDef.Type -> friendly slot. The first four are the headline non-weapon
# components surfaced (with their grade) in the cargo ship view; the rest are the
# weapons/ordnance/radar a salvage wreck can be stripped of (see the salvage Ship-ID
# feature / _salvage_ships.py). build_ships keeps only `_HEADLINE_SLOTS` so ships.json
# is unchanged; build_salvage_ships consumes the whole index.
_COMPONENT_SLOTS = {
    "PowerPlant": "power_plant",
    "Cooler": "cooler",
    "Shield": "shield",
    "QuantumDrive": "quantum_drive",
    "Radar": "radar",
    "WeaponGun": "weapon",
    "Missile": "missile",
    "MissileLauncher": "missile_rack",
    "Turret": "turret",
    "TurretBase": "turret_base",
    "UtilityTurret": "utility_turret",
    "WeaponDefensive": "countermeasure",
}

# The slots build_ships writes into ships.json `components` (unchanged shape); the wider
# weapon/turret/radar slots above are for the salvage catalog only.
_HEADLINE_SLOTS = ("power_plant", "cooler", "shield", "quantum_drive")

# A handful of capital / modular ships whose grids live in same-class sub-assemblies
# that the flattened loadout text can't disambiguate. Hand-pinned total SCU; rare
# enough that geometry is synthesised as one block. Revisit if StarBreaker gains a
# structured loadout export.
SCU_OVERRIDES = {
    "ORIG_890Jump": 388,
    "MISC_Starlancer_MAX": 224,
}

# Manufacturer short-names as they appear prefixed in localised vehicle names, so we
# can strip them to get the bare model ("MISC Freelancer" -> "Freelancer").
_MFR_PREFIXES = {
    "misc", "drake", "rsi", "origin", "crusader", "aegis", "anvil", "argo", "banu",
    "greycat", "mirai", "tumbril", "xi'an", "aopoa", "esperia", "kruger", "gatac",
    "consolidated", "cnou",
}

# Ship-record variants we never want as distinct cargo entries (AI, derelicts, event
# skins, in-game-boarded copies, ...). Matched against the lower-cased class name.
_VARIANT_RE = re.compile(
    r"(pu_ai|unmanned|derelict|simpod|hijacked|_qt$|test|template|dummy|_ai_|swarm"
    r"|turret|_pet|wreck|nodebris|tutorial|noai|showdown|_pir_|crewless|boarded"
    r"|_teach|bis29\d\d|bis20\d\d|_exec_|_fw_|_tsg|advocacy|indestructible|bombless"
    r"|_ea_|s3bombs|wikelo|renegade|_collector|civilian|_temp$|gamemaster|invictus"
    # redundant duplicates of a base ship already in the catalogue (variant skins,
    # PU/tier/edition records) — verified against the survey, base ship is kept.
    r"|_pu$|_tier_\d|_temp_|temporary|nointerior|_military$|_executive|_drug_\d|_gs_se$)",
    re.I,
)

# Ships with no localisation entry fall back to a code-split name + raw manufacturer
# code. Fix the ones that matter by hand (e.g. the Hammerhead's only cargo record is
# the "GS" variant, mislabelled "AEGS").
_NAME_FIXUPS = {
    "AEGS_Hammerhead_GS": {"name": "Hammerhead", "name_full": "Aegis Hammerhead",
                           "manufacturer": "Aegis", "manufacturer_full": "Aegis Dynamics"},
}


def build_grid_index(records_root: str) -> dict:
    """Map a cargo-grid entity class (lower) -> (x, y, z) interior dims in metres,
    by following each grid entity's container ref to its InventoryContainer record."""
    containers: dict[str, tuple] = {}  # file basename -> dims
    for p in glob.glob(os.path.join(records_root, "**", "inventorycontainers", "**", "*.json"),
                       recursive=True):
        try:
            rv = _load_json(p)["_RecordValue_"]
        except (OSError, ValueError, KeyError):
            continue
        dim = rv.get("interiorDimensions")
        if dim:
            containers[os.path.basename(p).lower()] = (dim["x"], dim["y"], dim["z"])

    grids: dict[str, tuple] = {}
    for p in glob.glob(os.path.join(records_root, "**", "*.json"), recursive=True):
        bn = os.path.basename(p).lower()
        if "cargogrid" not in bn and "cargo_grid" not in bn:
            continue
        try:
            d = _load_json(p)
        except (OSError, ValueError):
            continue
        name = d.get("_RecordName_", "")
        if "." not in name:
            continue
        cls = name.split(".", 1)[1].lower()
        ref = _find_container_ref(d.get("_RecordValue_"))
        if ref:
            base = os.path.basename(ref.split("?")[0]).lower()
            if base in containers:
                grids[cls] = containers[base]
    return grids


def _find_container_ref(o) -> str | None:
    if isinstance(o, dict):
        if o.get("_Type_") == "SCItemInventoryContainerComponentParams":
            cp = o.get("containerParams")
            if isinstance(cp, str):
                return cp
        for v in o.values():
            r = _find_container_ref(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_container_ref(v)
            if r:
                return r
    return None


def build_component_index(records_root: str) -> dict:
    """Map a ship-component/weapon item class (lower) -> {slot, size, grade, grade_num,
    salvagable, loc_key}, read from each item's ``SAttachableComponentParams.AttachDef``
    (``Type``/``Size``/``Grade``/``Localization.Name``). Covers the headline components
    (power plant / cooler / shield / quantum drive / radar) AND ship weapons / turrets /
    missiles / countermeasures -- the whole ``_COMPONENT_SLOTS`` map.

    ``salvagable`` is the item's ``SHealthComponentParams.IsSalvagable`` -- the game's own
    per-item flag for whether the salvage beam can strip it off a wreck (see _salvage_ships).
    ``loc_key`` is the AttachDef's localised-name pointer, used as a name fallback for items
    (most weapons) whose loc key doesn't match their class id."""
    idx: dict[str, dict] = {}
    # Scan both the ship-component tree (powerplant/cooler/shield/quantumdrive/radar/turret/
    # missile_racks/...) and the weapons tree (guns/missiles). Items whose AttachDef.Type
    # isn't in _COMPONENT_SLOTS (ammo boxes, magazines, ...) are skipped.
    roots = (
        os.path.join(records_root, "**", "entities", "scitem", "ships", "**", "*.json"),
        os.path.join(records_root, "**", "entities", "scitem", "weapons", "**", "*.json"),
    )
    for pat in roots:
        for p in glob.glob(pat, recursive=True):
            try:
                d = _load_json(p)
            except (OSError, ValueError):
                continue
            name = d.get("_RecordName_", "")
            if "." not in name:
                continue
            rv = d.get("_RecordValue_")
            ad = _deep_find(rv, "_Type_", "SAttachableComponentParams")
            ad = (ad or {}).get("AttachDef") or {}
            slot = _COMPONENT_SLOTS.get(ad.get("Type"))
            if not slot:
                continue
            grade = ad.get("Grade")
            health = _deep_find(rv, "_Type_", "SHealthComponentParams") or {}
            idx[name.split(".", 1)[1].lower()] = {
                "slot": slot,
                "size": ad.get("Size"),
                "grade": _GRADE_LETTER.get(grade),
                "grade_num": grade,
                "salvagable": bool(health.get("IsSalvagable")),
                "loc_key": (ad.get("Localization") or {}).get("Name"),
            }
    return idx


_ROOT_RE = re.compile(r"^EntityClassDefinition\.(\S+)\s")
_INST_RE = re.compile(r"^(\s+)(\S+)\s+\[([^\]]*)\]")


def _parse_loadout_blocks(text: str) -> dict:
    """`entity loadout` text -> {root_class_lower: [installed_child_class_lower, ...]}.
    Each block is a root header line plus indented ``<Class> [<port>]`` install lines
    (only lines with a non-empty port are real installs)."""
    blocks: dict[str, list] = {}
    cur = None
    for line in text.splitlines():
        m = _ROOT_RE.match(line)
        if m:
            cur = m.group(1).lower()
            blocks.setdefault(cur, [])
        elif cur is not None:
            mm = _INST_RE.match(line)
            if mm and mm.group(3):
                blocks[cur].append(mm.group(2).lower())
    return blocks


# A ship's installed mining laser -> its size, by the ``_s<N>`` class suffix
# (mining_laser_grin_arbor_s2 -> 2). Drives the equipment popup's per-ship head filter.
_MINING_HEAD_RE = re.compile(r"^mining_laser_.*_s(\d)$")


def mining_hardpoints(ship_class: str, loadout_text: str) -> list:
    """Sizes of a ship's mining-laser hardpoints, from its default loadout (the MOLE's three
    Arbor S2 -> [2, 2, 2]; the Prospector -> [1]; the Golem -> [1]). Reads only the ship's OWN
    loadout block so AI/teach/derelict variants don't inflate the count; the laser may sit on a
    mining-arm sub-assembly, but ``_parse_loadout_blocks`` flattens every descendant into the
    root's install list, so one block scan catches it. Empty for handheld-only miners (ROC)."""
    installs = _parse_loadout_blocks(loadout_text or "").get(ship_class.lower(), [])
    sizes = [int(m.group(1)) for child in installs
             if (m := _MINING_HEAD_RE.match(child))]
    return sorted(sizes)


def mining_head(ship_class: str, loadout_text: str) -> str | None:
    """The ship's factory mining-laser class (lower-cased), or None -- the head it ships with,
    e.g. the Prospector's ``mining_laser_grin_arbor_s1`` or the Golem's bespoke
    ``mining_laser_drak_golem_s1`` (the Pitman). Lets the equipment popup restrict head choices
    to those sharing the factory head's mount tag (a Golem fits only its Pitman). Match it to the
    mining-gear catalog case-insensitively."""
    installs = _parse_loadout_blocks(loadout_text or "").get(ship_class.lower(), [])
    return next((child for child in installs if _MINING_HEAD_RE.match(child)), None)


# A ship's installed radar -> its size + class, by the radar class' ``_s<NN>_`` token
# (radr_chco_s01_surveyorlite -> size 1). Drives the equipment popup's per-ship radar filter.
_RADAR_RE = re.compile(r"^(radr_.*_s(\d+)_.*)$")


def radar_slot(ship_class: str, loadout_text: str) -> dict | None:
    """The ship's radar hardpoint -- ``{size, stock}`` from its default loadout (the Prospector
    -> ``{size: 1, stock: radr_chco_s01_surveyorlite}``). Reads only the ship's OWN loadout
    block, like ``mining_hardpoints``; ``None`` when no radar is installed. ``stock`` is the
    lower-cased install class (the loadout text is lower-cased) -- match it to the radar catalog
    case-insensitively. Drives the equipment popup's per-ship radar filter + stock marker."""
    installs = _parse_loadout_blocks(loadout_text or "").get(ship_class.lower(), [])
    for child in installs:
        if (m := _RADAR_RE.match(child)):
            return {"size": int(m.group(2)), "stock": m.group(1)}
    return None


def grid_cells_scu(grid_index: dict, cls: str) -> int:
    x, y, z = grid_index[cls]
    return round(x * y * z / SCU_M ** 3)


# --- geometry: reconstruct real deck positions from hardpoint transforms ----- #
def _strip_trailing_commas(text: str) -> str:
    """StarBreaker's hierarchy JSON emits trailing commas; make it parseable."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _world_aabb(matrix, local_dims):
    """Transform a corner-anchored local box [0,d] by a 3x4 bone_to_world matrix;
    return (min_x, min_y, min_z, size_x, size_y, size_z) of its world AABB."""
    pts = []
    for cx in (0, local_dims[0]):
        for cy in (0, local_dims[1]):
            for cz in (0, local_dims[2]):
                pts.append((
                    matrix[0][0] * cx + matrix[0][1] * cy + matrix[0][2] * cz + matrix[0][3],
                    matrix[1][0] * cx + matrix[1][1] * cy + matrix[1][2] * cz + matrix[1][3],
                    matrix[2][0] * cx + matrix[2][1] * cy + matrix[2][2] * cz + matrix[2][3],
                ))
    xs, ys, zs = (sorted(p[i] for p in pts) for i in range(3))
    return xs[0], ys[0], zs[0], xs[-1] - xs[0], ys[-1] - ys[0], zs[-1] - zs[0]


def bay_name(port: str | None, entity: str | None = None) -> str:
    """Human-readable cargo-bay label from the hardpoint port (preferred) or the grid
    entity class. ``hardpoint_cargogrid_mid_left`` -> "Mid Left"; ``..._module_01`` ->
    "Module 1"; ``MISC_Hull_C_CargoGrid_Outer`` -> "Outer"; bare grid -> "Cargo"."""
    raw = ""
    if port:
        raw = re.sub(r"^hardpoint_", "", port, flags=re.I)
        raw = re.sub(r"^cargo_?grid_?", "", raw, flags=re.I)
    if not raw and entity:
        m = re.search(r"cargo_?grid_?(.*)$", entity, re.I)
        raw = m.group(1) if m else ""
    raw = re.sub(r"_0*(\d+)", lambda m: " " + m.group(1), raw)  # module_01 -> module 1
    words = [w for w in re.split(r"[_\s]+", raw) if w]
    return " ".join(w.capitalize() for w in words) or "Cargo"


def reconstruct_bays(hierarchy_json: str, grid_index: dict) -> list | None:
    """Build deck-positioned cargo-grid cells from a ship's `entity export
    --dump-hierarchy` output: each cargo-grid loadout entry is matched to its
    hardpoint node's world transform, the grid's interior box is rotated into world
    space, and metres are quantised to SCU cells. Returns renderer cells
    ``[{x, y, z, width, length, height}]`` (x=across, z=depth, y=level) or ``None``
    if no grid had a resolvable transform."""
    try:
        d = json.loads(_strip_trailing_commas(hierarchy_json))
    except ValueError:
        return None
    nodes = {n["node"].lower(): n for n in d.get("root_nmc", []) if n.get("node")}
    boxes = []
    for e in d.get("loadout", []):
        ent = (e.get("entity") or "").lower()
        if ent not in grid_index:
            continue
        node = nodes.get((e.get("port") or "").lower())
        m = node.get("bone_to_world") if node else None
        if not m:
            continue
        boxes.append((_world_aabb(m, grid_index[ent]), bay_name(e.get("port"), e.get("entity"))))
    if not boxes:
        return None
    # +Y is the ship's forward axis (CryEngine): mapping world Y -> deck "z" (depth)
    # the same way for every ship keeps the layout consistently bow-forward.
    mnx = min(b[0][0] for b in boxes)
    mny = min(b[0][1] for b in boxes)
    mnz = min(b[0][2] for b in boxes)
    cells = [{
        "x": round((b[0] - mnx) / SCU_M), "z": round((b[1] - mny) / SCU_M),
        "y": round((b[2] - mnz) / SCU_M),
        "width": round(b[3] / SCU_M), "length": round(b[4] / SCU_M),
        "height": round(b[5] / SCU_M), "name": nm,
    } for b, nm in boxes]
    # Drop zero-volume strips (walkways / ladders share the CargoGrid name but hold no SCU).
    return [c for c in cells if c["width"] * c["length"] * c["height"] > 0]


def resolve_cargo_grids(ship_class: str, loadout_text: str, grid_index: dict) -> list:
    """Robust list of a ship's cargo-grid classes (with multiplicity) from its
    `entity loadout` tree -- the capacity source that also catches grids on
    sub-assemblies the hierarchy export omits.

    Rule (validated 66/104 exact vs the old scrape, see memory): take grid installs
    in the ship's OWN block with multiplicity (repeated identical lines are real pods,
    e.g. the Hull-C's eight spindles), then BFS into installed non-grid sub-assemblies
    and add only grid *classes not already seen directly* -- this picks up grids that
    live solely on a sub-assembly (e.g. the 400i's cargo lift) without double-counting
    ships whose grids are listed both places (e.g. the MOLE)."""
    blocks = _parse_loadout_blocks(loadout_text)
    s = ship_class.lower()
    if s not in blocks:
        return []
    own = [c for c in blocks[s] if c in grid_index]
    own_classes = set(own)
    grids = list(own)

    visited = {s}
    stack = [c for c in blocks[s] if c in blocks and c not in grid_index]
    while stack:
        k = stack.pop()
        if k in visited:
            continue
        visited.add(k)
        for c in blocks.get(k, []):
            if c in grid_index and c not in own_classes:
                grids.append(c)
            elif c in blocks and c not in grid_index and c not in visited:
                stack.append(c)
    return grids


def resolve_ship_components(ship_class: str, loadout_text: str, component_index: dict,
                           loc: dict) -> dict:
    """A ship's stock components grouped by slot -> [{name, grade, grade_num, size, count}].
    Reads only the ship's OWN loadout block (power plant / cooler / shield / quantum drive
    install directly on the hull, not on sub-assemblies), counting identical installs."""
    blocks = _parse_loadout_blocks(loadout_text or "")
    out: dict[str, dict] = {}   # slot -> {component_class: entry}
    for child in blocks.get(ship_class.lower(), []):
        info = component_index.get(child)
        if not info:
            continue
        bucket = out.setdefault(info["slot"], {})
        if child in bucket:
            bucket[child]["count"] += 1
            continue
        # localised item name: the key is inconsistently formed across components --
        # it may keep or drop the `_scitem` suffix and may carry an extra underscore
        # after `item_name` (e.g. `item_Name_POWR_AEGS_S03_Centurion`). Try each form
        # before falling back to the raw class.
        bare = child.removesuffix("_scitem")
        loc_key = (info.get("loc_key") or "").lstrip("@").lower()
        name = (loc.get(f"item_name{child}")
                or loc.get(f"item_name{bare}")
                or loc.get(f"item_name_{bare}")
                or (loc.get(loc_key) if loc_key else None)
                or child)
        bucket[child] = {
            "name": name,
            "grade": info["grade"],
            "grade_num": info["grade_num"],
            "size": info["size"],
            "count": 1,
        }
    return {slot: list(by_class.values()) for slot, by_class in out.items()}


def _wrap_cells(boxes: list) -> list:
    """Lay (w, length, h, name) tiles left-to-right, wrapping to a new z-row once a row
    would exceed a squarish width budget, with 1-cell gaps and no overlap. Keeps the bay
    from rendering as a long 1-deep/1-wide sliver. Self-limiting: ships whose single row
    already fits the budget stay one row."""
    boxes = sorted(boxes, key=lambda b: (-b[0], -b[1]))
    if not boxes:
        return []
    maxw = max(b[0] for b in boxes)
    area = sum((b[0] + 1) * (b[1] + 1) for b in boxes)   # footprint incl. gaps
    budget = max(2 * maxw + 1, int(area ** 0.5) + 1)     # squarish bay, fits widest cell
    cells, x, z, row_l = [], 0, 0, 0
    for w, length, h, name in boxes:
        if x > 0 and x + w > budget:       # wrap to the next z-row
            z += row_l + 1
            x, row_l = 0, 0
        cells.append({"x": x, "y": 0, "z": z, "width": w, "length": length,
                      "height": h, "name": name})
        x += w + 1
        row_l = max(row_l, length)
    return cells


def _aabb_overlap(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["width"] and b["x"] < a["x"] + a["width"] and
            a["z"] < b["z"] + b["length"] and b["z"] < a["z"] + a["length"] and
            a.get("y", 0) < b.get("y", 0) + b["height"] and b.get("y", 0) < a.get("y", 0) + a["height"])


def _deoverlap(cells: list) -> list:
    """The deck reconstruction takes each grid's world AABB at face value, so grids
    whose hardpoints coincide or nest (mirrored left/right pairs, a ladder inside a
    module, a locker inside a bay) come out as cells occupying the same 3D space and
    render on top of each other. Keep the first cell of any clash put, and slide each
    later offender out along +x past everything placed so far — preserves the rest of
    the layout, removes the overlap, and keeps total volume == capacity."""
    placed = []
    for c in cells:
        if any(_aabb_overlap(c, p) for p in placed):
            c = dict(c, x=max(p["x"] + p["width"] for p in placed) + 1)
        placed.append(c)
    return placed


def _compact(cells: list, allowance: int = 1) -> list:
    """Pull scattered cells together by collapsing large EMPTY gaps along each axis to
    `allowance` slabs, so multi-deck / long ships (Reclaimer's 12 decks, Freelancer's
    far-flung Mid bays) don't render as disconnected floating clusters. A monotonic
    per-axis coordinate remap: it never creates overlaps and leaves volume unchanged.
    Self-limiting — ships with no oversized gaps are untouched (e.g. the C2's 1-slab
    gap is kept)."""
    for axis, dim in (("x", "width"), ("z", "length"), ("y", "height")):
        occ = set()
        for c in cells:
            base = c.get(axis, 0)
            for s in range(base, base + c[dim]):
                occ.add(s)
        if not occ:
            continue
        remap, new, gap = {}, min(occ), 0
        for s in range(min(occ), max(occ) + 1):
            if s in occ:
                remap[s], new, gap = new, new + 1, 0
            else:
                gap += 1
                if gap <= allowance:
                    new += 1
        for c in cells:
            c[axis] = remap[c.get(axis, 0)]
    return cells


def _synth_layout(grid_classes: list, grid_index: dict) -> list:
    """Fallback deck layout for ships whose grid transforms we can't recover: tile each
    grid as a cell, wrapped into a roughly-square 2D block (correct dims/SCU/packing --
    only the deck arrangement is approximate, not ship-accurate)."""
    boxes = []
    for c in grid_classes:
        dx, dy, dz = grid_index[c]
        w, length, h = round(dx / SCU_M), round(dy / SCU_M), round(dz / SCU_M)
        if w * length * h == 0:  # skip zero-volume walkway/ladder strips
            continue
        boxes.append((w, length, h, bay_name(None, c)))
    return _wrap_cells(boxes)


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #
# Entity-record fields that hold the vehicle's localised-name pointer (a ``@vehicle_Name*``
# ref), in priority order. The record is the authoritative class->loc-key mapping: it
# names the exact key even when its word order differs from the class id, so we read it
# rather than reconstructing the key. We take the full-name refs (not ``ShortName``) to
# keep the manufacturer-prefixed form the comms channel emits.
_NAME_REF_KEYS = ("vehicleName", "Name", "displayName")


def _record_vehicle_name(rec_path: str | None, loc: dict) -> str:
    """Localised full vehicle name from the entity record's own ``@vehicle_Name*`` pointer
    (e.g. ``"vehicleName": "@vehicle_NameCRUS_C1_Spirit"`` on class ``CRUS_Spirit_C1``), or
    "" when the record has no usable pointer. This is the game's own mapping, so it beats
    deriving the key from the class id."""
    if not rec_path:
        return ""
    try:
        rv = _load_json(rec_path)["_RecordValue_"]
    except (OSError, ValueError, KeyError):
        return ""
    found: dict = {}

    def visit(o):
        for k, v in o.items():
            if (k in _NAME_REF_KEYS and isinstance(v, str)
                    and v.lower().startswith("@vehicle_name")):
                found.setdefault(k, v)
    _deep_walk(rv, visit)
    for k in _NAME_REF_KEYS:
        txt = _loc_text(found[k], loc).strip() if k in found else ""
        if txt:
            return txt
    return ""


def display_name(cls: str, loc: dict, rec_path: str | None = None) -> tuple[str, str]:
    """Return (bare model name, full localised name) for a ship class. Falls back to
    a code-split of the class name when the record carries no usable name pointer."""
    full = _record_vehicle_name(rec_path, loc)
    if not full:
        parts = cls.split("_")
        return (" ".join(parts[1:]) or cls, cls)
    head, _, rest = full.partition(" ")
    return (rest if rest and head.lower() in _MFR_PREFIXES else full, full)


def manufacturer(cls: str, loc: dict, rec_path: str | None = None) -> tuple[str, str]:
    """Return (short, full) manufacturer names. Short is the prefix word the game uses
    in vehicle names ("Drake"); full is the localised company name."""
    full_name = _record_vehicle_name(rec_path, loc)
    head = full_name.split(" ", 1)[0] if full_name else ""
    short = head if head.lower() in _MFR_PREFIXES else cls.split("_", 1)[0]
    code = cls.split("_", 1)[0]
    return short, loc.get(f"manufacturer_name{code}".lower(), short)


# --------------------------------------------------------------------------- #
# Ship enumeration + orchestration
# --------------------------------------------------------------------------- #
def _ship_meta(record_path: str, loc: dict) -> dict:
    """Pull career/role from a ship record (best-effort; both are @loc refs). Ships and
    ground vehicles carry ``vehicleCareer``/``vehicleRole``; the ATLS exosuits (modelled
    as "actors") use plain ``career``/``role`` instead, so accept either, preferring the
    vehicle-prefixed keys."""
    meta = {}
    try:
        rv = _load_json(record_path)["_RecordValue_"]
    except (OSError, ValueError, KeyError):
        return meta

    found: dict = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("vehicleCareer", "vehicleRole", "career", "role"):
                    found.setdefault(k, v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(rv)
    career = found.get("vehicleCareer", found.get("career"))
    role = found.get("vehicleRole", found.get("role"))
    if career is not None:
        meta["career"] = _loc_text(career, loc)
    if role is not None:
        meta["role"] = _loc_text(role, loc)
    return meta


def _is_mining_role(meta: dict) -> bool:
    """A vehicle is a miner when its (localised) role mentions mining -- e.g. the MOLE's
    'Medium Mining', the Prospector's 'Light Mining', the ATLS GEO's 'Mining'. Salvage
    roles deliberately don't count."""
    return "mining" in (meta.get("role") or "").lower()


def _vehicle_classes(records_root: str, rel: str) -> list:
    """All non-variant vehicle entity classes under ``<records_root>/**/<rel>``."""
    out = []
    for p in glob.glob(os.path.join(records_root, "**", rel, "*.json"), recursive=True):
        stem = os.path.basename(p)[:-5]
        if _VARIANT_RE.search(stem):
            continue
        try:
            cls = _load_json(p)["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        out.append((cls, p))
    return out


def base_ship_classes(records_root: str) -> list:
    """All non-variant spaceship entity classes."""
    return _vehicle_classes(records_root, "entities/spaceships")


def _pad_block(short: int, x0: int) -> dict:
    """Represent `short` phantom SCU (capacity we know from an override but can't place
    from geometry) as one reasonably-shaped block at x=x0, instead of a 1×N strip that
    renders absurdly (e.g. the 890 Jump's 288-SCU shortfall). Volume == short exactly:
    prefer height 2, then a near-square footprint (largest divisor ≤ √area)."""
    h = 2 if short % 2 == 0 and short >= 4 else 1
    area = short // h
    w = int(area ** 0.5)
    while w > 1 and area % w:
        w -= 1
    return {"x": x0, "y": 0, "z": 0, "width": w, "length": area // w, "height": h,
            "name": "Cargo"}


def resolve_ship_groups(cls: str, p4k: str, sb: str, grid_index: dict,
                        workdir: str, loadout_text: str = "") -> tuple:
    """Resolve one ship -> (total_scu, groups). Prefers deck-accurate geometry from
    `entity export --dump-hierarchy`; cross-checks capacity against the text loadout
    and, if the hierarchy missed sub-assembly grids (or capacity is overridden), falls
    back to a synthesised layout that is correct on SCU/packing if not deck-accurate."""
    grids = resolve_cargo_grids(cls, loadout_text, grid_index) if loadout_text else []
    scu = SCU_OVERRIDES.get(cls, sum(grid_cells_scu(grid_index, c) for c in grids))
    if scu <= 0:
        return 0, [], "synth"   # grid-less (e.g. a mining vehicle); caller decides to keep

    cells = None
    if cls not in SCU_OVERRIDES:
        hpath = os.path.join(workdir, f"{cls}.hier.json")
        try:
            _run(sb, p4k, ["entity", "export", cls, hpath, "--dump-hierarchy"], timeout=180)
            with open(hpath) as f:
                cells = reconstruct_bays(f.read(), grid_index)
        except (RuntimeError, subprocess.TimeoutExpired, OSError):
            cells = None
        finally:
            if os.path.exists(hpath):
                os.remove(hpath)
    # Use deck-accurate cells only when they account for the full capacity; otherwise
    # the hierarchy missed sub-assembly grids, so synthesise the complete layout.
    deck = bool(cells) and sum(c["width"] * c["length"] * c["height"] for c in cells) == scu
    if not deck:
        cells = _synth_layout(grids, grid_index) if grids else []
    else:
        cells = _deoverlap(cells)   # spread any grids the reconstruction stacked in the same space
    cells = _compact(cells)         # close big empty gaps so scattered decks read as one hold
    # Keep the rendered cells consistent with the stated capacity (e.g. SCU overrides
    # whose grids we can't enumerate): pad any shortfall with a synthetic block.
    short = scu - sum(c["width"] * c["length"] * c["height"] for c in cells)
    if short > 0:
        right = max((c["x"] + c["width"] for c in cells), default=0)
        cells.append(_pad_block(short, right + 1))
    # Sanity pass: a cell that's far TALLER than it is wide or deep is an axis-swap
    # artifact from the hierarchy reconstruction (e.g. the RAFT's external rack came out
    # 8×2×12). Lay it flat — biggest two dims as the footprint — so it renders sensibly,
    # volume unchanged. Also tidy purely-numeric fallback bay names.
    for c in cells:
        dims = sorted((c["width"], c["length"], c["height"]), reverse=True)
        if c["height"] > 6 and c["height"] == dims[0] and c["height"] > dims[1]:
            c["width"], c["length"], c["height"] = dims[0], dims[1], dims[2]
        if str(c.get("name", "")).isdigit():
            c["name"] = "Cargo"
    # "deck": grids are at their real ship positions (forward = +z); "synth": row-tiled.
    return scu, [{"x": 0, "z": 0, "grids": cells}], "deck" if deck else "synth"


# A healthy run resolves an `entity loadout` for nearly every vehicle; the cargo geometry
# (and thus whether a ship is kept) hinges on it. If more than this fraction of those
# per-ship StarBreaker calls FAIL, the run is degraded (StarBreaker/p4k wedged, resource
# pressure) and would silently drop the unresolved ships -- emitting a decimated catalog
# that then overwrites a good one. Above the limit we raise instead, so the caller keeps the
# previous catalog. (Root cause of the 2026-06-09 one-ship ships.json.)
LOADOUT_FAILURE_LIMIT = 0.2


def build_ships(p4k: str, sb: str | None = None, workdir: str | None = None,
                progress=lambda msg: None) -> dict:
    """Extract + resolve every cargo-carrying ship into {class: {scu, groups, ...}}.

    Raises ``RuntimeError`` if more than ``LOADOUT_FAILURE_LIMIT`` of the per-ship loadout
    extractions fail -- a degraded run must not return a partial catalog the caller would
    save over a complete one."""
    sb = sb or ensure_binary()
    own_tmp = workdir is None
    workdir = workdir or scratch_dir("starlogger-scdata-")
    try:
        progress("extracting DataCore + localisation")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        grid_index = build_grid_index(recs)
        component_index = build_component_index(recs)
        progress(f"resolved {len(grid_index)} cargo-grid + "
                 f"{len(component_index)} component definitions")

        ships: dict[str, dict] = {}
        loadout_failures = 0
        bases = base_ship_classes(recs)
        # Mining vehicles that live outside entities/spaceships and carry no cargo grid:
        # the ROC / ROC-DS (ground vehicles) and the ATLS GEO (an "actor" exosuit). The
        # dashboard still needs them flagged as miners, so pull in the mining ones only --
        # non-mining ground vehicles and exosuits stay out of the cargo catalogue.
        extra = [(cls, p)
                 for rel in ("entities/groundvehicles", "actor/actors")
                 for cls, p in _vehicle_classes(recs, rel)
                 if _is_mining_role(_ship_meta(p, loc))]
        vehicles = bases + extra
        for i, (cls, rec_path) in enumerate(vehicles):
            progress(f"resolving {i + 1}/{len(vehicles)}: {cls}")
            meta = _ship_meta(rec_path, loc)
            mining = _is_mining_role(meta)
            try:
                loadout_text = _run(sb, p4k, ["entity", "loadout", cls], timeout=120)
            except (RuntimeError, subprocess.TimeoutExpired):
                loadout_text = ""
                loadout_failures += 1
            scu, groups, layout = resolve_ship_groups(cls, p4k, sb, grid_index, workdir,
                                                      loadout_text)
            # Keep cargo ships; also keep mining vehicles even with no cargo grid so the
            # UI can flag them (the MOLE has a grid; the Prospector / Golem / ROC / ATLS
            # GEO don't).
            if scu <= 0 and not mining:
                continue
            name, name_full = display_name(cls, loc, rec_path)
            mfr_short, mfr_full = manufacturer(cls, loc, rec_path)
            entry = {
                "class": cls,
                "scu": scu,
                "name": name,
                "name_full": name_full,
                "manufacturer": mfr_short,
                "manufacturer_full": mfr_full,
                "layout": layout,
                "groups": groups,
            }
            entry.update(meta)
            # component_index now also carries weapons/turrets/radar (for the salvage
            # catalog); ships.json keeps only the four headline component slots it always had.
            components = resolve_ship_components(cls, loadout_text, component_index, loc)
            components = {s: components[s] for s in _HEADLINE_SLOTS if s in components}
            if components:
                entry["components"] = components
            if mining:
                entry["mining"] = {"hardpoints": mining_hardpoints(cls, loadout_text),
                                   "head": mining_head(cls, loadout_text)}
            radar = radar_slot(cls, loadout_text)
            if radar:
                entry["radar"] = radar   # {size, stock} -- the radar slot of the mining loadout
            entry.update(_NAME_FIXUPS.get(cls, {}))   # hand-fix names that lack localisation
            # On a model+variant name clash keep the larger-capacity one.
            if cls not in ships or scu > ships[cls]["scu"]:
                ships[cls] = entry
        if vehicles and loadout_failures > LOADOUT_FAILURE_LIMIT * len(vehicles):
            raise RuntimeError(
                f"{loadout_failures}/{len(vehicles)} ship loadout extractions failed -- "
                f"StarBreaker/p4k degraded; refusing to emit a partial ship catalog")
        progress(f"built {len(ships)} cargo ships")
        return ships
    finally:
        if own_tmp:
            shutil.rmtree(workdir, ignore_errors=True)
