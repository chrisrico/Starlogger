"""Read ship cargo data straight from the game's own files instead of scraping a
third-party site. The game ships a ~144 GB encrypted ``Data.p4k`` whose current
(4.x) central-directory format the stale Python readers can't parse, so we drive
**StarBreaker** (an actively maintained Rust toolkit) as a vendored single binary:

  * ``dcb extract``     -> the DataCore as ~60k JSON records (ship/grid/container defs)
  * ``p4k extract``     -> ``global.ini`` (localised ship + manufacturer names)
  * ``entity loadout``  -> per-ship resolved loadout tree (resolves geometry port
                           defaults the DataCore record alone doesn't carry)

A ship's SCU and per-bay grid geometry come from the cargo-grid *InventoryContainer*
records (``interiorDimensions`` in metres; 1 SCU = a 1.25 m cube). Heavy but only run
on a major game-version bump, niced into the background -- see ``shipcargo.py``.

The binary is fetched once into ``SCMT_DATA_DIR/bin`` and verified by SHA-256.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request

from .config import DATA_DIR, IS_WINDOWS, USER_AGENT

# --- vendored StarBreaker binary (pinned + checksummed, per platform) -------- #
# Same upstream release, different asset: a .tar.gz holding `starbreaker` on Linux,
# a .zip holding `starbreaker.exe` on Windows. SHA-256s pin both the archive and the
# extracted binary on each OS.
SB_VERSION = "v0.3.2"
_SB_ASSET = "windows-x86_64.zip" if IS_WINDOWS else "linux-x86_64.tar.gz"
SB_URL = (
    "https://github.com/diogotr7/StarBreaker/releases/download/"
    f"{SB_VERSION}/starbreaker-cli-{SB_VERSION}-{_SB_ASSET}"
)
SB_ARCHIVE_IS_ZIP = IS_WINDOWS
SB_ARCHIVE_MEMBER = "starbreaker.exe" if IS_WINDOWS else "starbreaker"
SB_ARCHIVE_SHA256 = (
    "fafd65ca002b9c3bb88ce32199b2345affcdb358655c68c41d0e93574a5d8a3d" if IS_WINDOWS
    else "f99168aacfe5732814dc65ad9731f367ccaaac8f3ce866a9490751655e92bf76"
)
SB_BINARY_SHA256 = (
    "82439e45cd5337f058f06ded63ee633bea8de8e4d75525f5a083aa7955b91a10" if IS_WINDOWS
    else "93cd5a7b756131a900e3131c05c994c1de17ad4f6cf2e47321ca5967a071990d"
)
SB_DIR = os.path.join(DATA_DIR, "bin")
SB_PATH = os.path.join(SB_DIR, f"starbreaker-{SB_VERSION}" + (".exe" if IS_WINDOWS else ""))

SCU_M = 1.25  # edge length in metres of a 1 SCU cube

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
    r"|_pu$|_tier_\d|_temp_|nointerior|_military$|_executive|_drug_\d|_gs_se$)",
    re.I,
)

# Ships with no localisation entry fall back to a code-split name + raw manufacturer
# code. Fix the ones that matter by hand (e.g. the Hammerhead's only cargo record is
# the "GS" variant, mislabelled "AEGS").
_NAME_FIXUPS = {
    "AEGS_Hammerhead_GS": {"name": "Hammerhead", "name_full": "Aegis Hammerhead",
                           "manufacturer": "Aegis", "manufacturer_full": "Aegis Dynamics"},
}


# --------------------------------------------------------------------------- #
# Binary management
# --------------------------------------------------------------------------- #
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_binary() -> str:
    """Return a path to the verified StarBreaker binary, downloading it once."""
    if os.path.exists(SB_PATH) and _sha256(SB_PATH) == SB_BINARY_SHA256:
        return SB_PATH
    os.makedirs(SB_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SB_DIR) as tmp:
        arc = os.path.join(tmp, "sb.archive")
        req = urllib.request.Request(SB_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp, open(arc, "wb") as out:
            shutil.copyfileobj(resp, out)
        if _sha256(arc) != SB_ARCHIVE_SHA256:
            raise ValueError("StarBreaker archive SHA-256 mismatch -- refusing to use it")
        if SB_ARCHIVE_IS_ZIP:
            import zipfile
            with zipfile.ZipFile(arc) as zf:
                zf.extract(SB_ARCHIVE_MEMBER, tmp)  # single top-level file
        else:
            import tarfile
            with tarfile.open(arc) as tf:
                tf.extract(SB_ARCHIVE_MEMBER, tmp)
        binp = os.path.join(tmp, SB_ARCHIVE_MEMBER)
        if _sha256(binp) != SB_BINARY_SHA256:
            raise ValueError("StarBreaker binary SHA-256 mismatch -- refusing to use it")
        if not IS_WINDOWS:
            os.chmod(binp, 0o755)  # Windows .exe is executable by extension
        os.replace(binp, SB_PATH)
    return SB_PATH


def find_p4k(log_path: str | None) -> str | None:
    """``Data.p4k`` sits beside the game's ``Game.log``."""
    if log_path:
        cand = os.path.join(os.path.dirname(log_path), "Data.p4k")
        if os.path.isfile(cand):
            return cand
    return None


def _run(sb: str, p4k: str, args: list[str], timeout: int = 1200) -> str:
    """Run StarBreaker at background priority so it yields to the game during a patch.
    Linux: `nice`/`ionice` prefix. Windows: IDLE_PRIORITY_CLASS (≈ nice -n19) plus
    CREATE_NO_WINDOW so the many per-ship subprocess calls don't flash a console."""
    prefix: list[str] = []
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
        )
    else:
        prefix = ["nice", "-n", "19"]
        if shutil.which("ionice"):
            prefix += ["ionice", "-c", "3"]
    env = {**os.environ, "SC_DATA_P4K": p4k}
    proc = subprocess.run(
        prefix + [sb, *args],
        capture_output=True, text=True, env=env, timeout=timeout, **kwargs,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"starbreaker {args[0]} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


# --------------------------------------------------------------------------- #
# Localisation + DataCore parsing (pure; unit-testable against extracted files)
# --------------------------------------------------------------------------- #
def load_localization(records_root: str) -> dict:
    """global.ini -> case-insensitive {key_lower: value}. Keys are inconsistently
    cased in the game files (``vehicle_Name`` vs ``vehicle_name``)."""
    hits = glob.glob(os.path.join(records_root, "**", "global.ini"), recursive=True)
    loc: dict[str, str] = {}
    if hits:
        with open(hits[0], encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line:
                    k, _, v = line.partition("=")
                    loc[k.strip().lower()] = v.rstrip("\n")
    return loc


def build_grid_index(records_root: str) -> dict:
    """Map a cargo-grid entity class (lower) -> (x, y, z) interior dims in metres,
    by following each grid entity's container ref to its InventoryContainer record."""
    containers: dict[str, tuple] = {}  # file basename -> dims
    for p in glob.glob(os.path.join(records_root, "**", "inventorycontainers", "**", "*.json"),
                       recursive=True):
        try:
            rv = json.load(open(p))["_RecordValue_"]
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
            d = json.load(open(p))
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
def display_name(cls: str, loc: dict) -> tuple[str, str]:
    """Return (bare model name, full localised name) for a ship class. Falls back to
    a code-split of the class name when localisation has no entry."""
    full = loc.get(f"vehicle_name{cls}".lower(), "").strip()
    if not full:
        parts = cls.split("_")
        return (" ".join(parts[1:]) or cls, cls)
    head, _, rest = full.partition(" ")
    return (rest if rest and head.lower() in _MFR_PREFIXES else full, full)


def manufacturer(cls: str, loc: dict) -> tuple[str, str]:
    """Return (short, full) manufacturer names. Short is the prefix word the game uses
    in vehicle names ("Drake"); full is the localised company name."""
    full_name = loc.get(f"vehicle_name{cls}".lower(), "").strip()
    head = full_name.split(" ", 1)[0] if full_name else ""
    short = head if head.lower() in _MFR_PREFIXES else cls.split("_", 1)[0]
    code = cls.split("_", 1)[0]
    return short, loc.get(f"manufacturer_name{code}".lower(), short)


def _loc_text(val: str, loc: dict) -> str:
    """Resolve a ``@key`` localisation reference to its string (or '' )."""
    if isinstance(val, str) and val.startswith("@"):
        return loc.get(val[1:].lower(), "")
    return ""


# --------------------------------------------------------------------------- #
# Ship enumeration + orchestration
# --------------------------------------------------------------------------- #
def _ship_meta(record_path: str, loc: dict) -> dict:
    """Pull career/role from a ship record (best-effort; both are @loc refs)."""
    meta = {}
    try:
        rv = json.load(open(record_path))["_RecordValue_"]
    except (OSError, ValueError, KeyError):
        return meta

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "vehicleCareer":
                    meta.setdefault("career", _loc_text(v, loc))
                elif k == "vehicleRole":
                    meta.setdefault("role", _loc_text(v, loc))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(rv)
    return meta


def base_ship_classes(records_root: str) -> list:
    """All non-variant spaceship entity classes."""
    out = []
    for p in glob.glob(os.path.join(records_root, "**", "entities", "spaceships", "*.json"),
                       recursive=True):
        stem = os.path.basename(p)[:-5]
        if _VARIANT_RE.search(stem):
            continue
        try:
            cls = json.load(open(p))["_RecordName_"].split(".", 1)[1]
        except (OSError, ValueError, KeyError, IndexError):
            continue
        out.append((cls, p))
    return out


def extract_records(workdir: str, p4k: str, sb: str) -> str:
    """Run the two bulk StarBreaker extracts into ``workdir``; return records root."""
    recs = os.path.join(workdir, "records")
    os.makedirs(recs, exist_ok=True)
    # `dcb extract --filter` is a no-op in v0.3.2, so we take the whole DataCore.
    _run(sb, p4k, ["dcb", "extract", "--p4k", p4k, "--format", "json", "-o", recs])
    _run(sb, p4k, ["p4k", "extract", "--p4k", p4k,
                   "--filter", "**/english/global.ini", "-o", recs])
    return recs


def query_resource_types(p4k: str, sb: str | None = None) -> dict:
    """Pull the single ``ResourceTypeDatabase`` DataCore record (commodity catalog)
    via ``dcb query`` -- seconds, vs the minutes a full ``dcb extract`` costs. Returns
    the parsed record dict (StarBreaker prints its match-count header to stderr, so
    stdout is the bare JSON; we still slice from the first ``{`` defensively)."""
    sb = sb or ensure_binary()
    out = _run(sb, p4k, ["dcb", "query", "ResourceTypeDatabase", "--p4k", p4k], timeout=600)
    return json.JSONDecoder().raw_decode(out[out.index("{"):])[0]


def build_commodity_map(p4k: str, sb: str | None = None, loc: dict | None = None) -> dict:
    """{resourceGUID(lower) -> commodity display name}, from the ResourceTypeDatabase.

    Maps every resource's ``_RecordId_`` (the UUID the trade log carries as
    ``resourceGUID``) to a name -- the localised ``displayName`` when a ``global.ini``
    (``loc``) is supplied, else the ``_RecordName_`` token (``ResourceType.Quartz`` ->
    ``Quartz``, CamelCase split). Walks nested groups."""
    rec = query_resource_types(p4k, sb)
    out: dict[str, str] = {}

    def walk(g: dict) -> None:
        for r in g.get("resources", []):
            guid = (r.get("_RecordId_") or "").lower()
            if not guid:
                continue
            name = _loc_text(r.get("displayName"), loc) if loc else ""
            if not name:
                rn = r.get("_RecordName_", "")
                tok = rn.split(".", 1)[1] if "." in rn else rn
                name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tok)
            out[guid] = name
        for sub in g.get("groups", []):
            walk(sub)

    for g in rec.get("_RecordValue_", {}).get("groups", []):
        walk(g)
    return out


def extract_localization(p4k: str, sb: str, workdir: str) -> dict:
    """Pull just english ``global.ini`` from the p4k and load it (case-insensitive
    key->value). Fast: it's one file, after StarBreaker reads the central directory."""
    recs = os.path.join(workdir, "loc")
    os.makedirs(recs, exist_ok=True)
    _run(sb, p4k, ["p4k", "extract", "--p4k", p4k, "--filter", "**/english/global.ini", "-o", recs])
    return load_localization(recs)


# Location CODE keys in global.ini map the same code shapes the log emits in
# `RequestLocationInventory Location[...]` to station names (RR_ARC_L1 -> "ARC-L1
# Wide Forest Station", Stanton2_Orison -> "Orison"). Keys are lower-cased by the
# loader. Restrict to real station/POI codes and drop sub-keys + non-place values.
_LOC_CODE = re.compile(
    r"^(rr_[a-z]{2,4}_(l\d_a|l\d|leo|heo|hub|gateway)|stanton\d+_[a-z][a-z0-9_]*"
    r"|pyro\d*_[a-z][a-z0-9_]*|(stanton_pyro|pyro_stanton)_jpstation"
    r"|nyx_[a-z][a-z0-9_]*|dfm_crusader_[a-z_]+)$")
_LOC_SUB = re.compile(
    r"(desc|clinic|addr|hint|long|short|marker|title|tip|name|info|sign|notif|comm"
    r"|greeting|terminal|elevator|hangar|kiosk)", re.I)
_LOC_PLACE = re.compile(
    r"(Station|Point|Harbor|Gateway|Hub|Depot|Outpost|Port|Spaceport|HEX|Babbage"
    r"|Area18|Orison|Lorville|Levski|Refueling|Service|Retreat|City|Platform)$|^[A-Z]{3}-L\d",
    re.I)


def build_location_names(loc: dict) -> dict:
    """{location_code(lower) -> station name}, mined from global.ini. The code shape
    matches the log's Location[...] codes, so it both resolves player locations and
    seeds the station-name autocomplete."""
    codes: dict[str, str] = {}
    for k, v in loc.items():
        if not _LOC_CODE.match(k) or _LOC_SUB.search(k):
            continue
        v = (v or "").strip()
        if not v or v.startswith("@") or len(v) > 46 or "=" in v:
            continue
        if _LOC_PLACE.search(v) or k.endswith("jpstation"):
            codes[k] = v
    return codes


def build_reference_data(p4k: str, sb: str | None = None) -> dict:
    """Commodity + station reference data from the local p4k, in one pass: extract
    global.ini once, query the ResourceTypeDatabase, and return localized commodity
    names (guid->name + a clean trade-commodity list) and station names (code->name +
    a clean station list)."""
    sb = sb or ensure_binary()
    workdir = tempfile.mkdtemp(prefix="scmt-ref-")
    try:
        loc = extract_localization(p4k, sb, workdir)
        rec = query_resource_types(p4k, sb)
        guid_map: dict[str, str] = {}
        commodity_names: set[str] = set()

        def walk(g: dict) -> None:
            for r in g.get("resources", []):
                guid = (r.get("_RecordId_") or "").lower()
                dn = r.get("displayName") or ""
                name = _loc_text(dn, loc)
                if not name:  # fall back to the record-name token
                    rn = r.get("_RecordName_", "")
                    tok = rn.split(".", 1)[1] if "." in rn else rn
                    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tok.replace("_", " "))
                if guid:
                    guid_map[guid] = name
                if name and isinstance(dn, str) and dn.lower().startswith("@items_commodities"):
                    commodity_names.add(name)
            for sub in g.get("groups", []):
                walk(sub)

        for g in rec.get("_RecordValue_", {}).get("groups", []):
            walk(g)
        codes = build_location_names(loc)
        return {
            "commodities": guid_map,
            "commodity_names": sorted(commodity_names),
            "location_codes": codes,
            "station_names": sorted(set(codes.values())),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
                        workdir: str) -> tuple:
    """Resolve one ship -> (total_scu, groups). Prefers deck-accurate geometry from
    `entity export --dump-hierarchy`; cross-checks capacity against the text loadout
    and, if the hierarchy missed sub-assembly grids (or capacity is overridden), falls
    back to a synthesised layout that is correct on SCU/packing if not deck-accurate."""
    grids = []
    try:
        text = _run(sb, p4k, ["entity", "loadout", cls], timeout=120)
        grids = resolve_cargo_grids(cls, text, grid_index)
    except (RuntimeError, subprocess.TimeoutExpired):
        pass
    scu = SCU_OVERRIDES.get(cls, sum(grid_cells_scu(grid_index, c) for c in grids))
    if scu <= 0:
        return 0, []

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


def build_ships(p4k: str, sb: str | None = None, workdir: str | None = None,
                progress=lambda msg: None) -> dict:
    """Extract + resolve every cargo-carrying ship into {class: {scu, groups, ...}}."""
    sb = sb or ensure_binary()
    own_tmp = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="scmt-scdata-")
    try:
        progress("extracting DataCore + localisation")
        recs = extract_records(workdir, p4k, sb)
        loc = load_localization(recs)
        grid_index = build_grid_index(recs)
        progress(f"resolved {len(grid_index)} cargo-grid definitions")

        ships: dict[str, dict] = {}
        bases = base_ship_classes(recs)
        for i, (cls, rec_path) in enumerate(bases):
            progress(f"resolving {i + 1}/{len(bases)}: {cls}")
            scu, groups, layout = resolve_ship_groups(cls, p4k, sb, grid_index, workdir)
            if scu <= 0:
                continue
            name, name_full = display_name(cls, loc)
            mfr_short, mfr_full = manufacturer(cls, loc)
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
            entry.update(_ship_meta(rec_path, loc))
            entry.update(_NAME_FIXUPS.get(cls, {}))   # hand-fix names that lack localisation
            # On a model+variant name clash keep the larger-capacity one.
            if cls not in ships or scu > ships[cls]["scu"]:
                ships[cls] = entry
        progress(f"built {len(ships)} cargo ships")
        return ships
    finally:
        if own_tmp:
            shutil.rmtree(workdir, ignore_errors=True)
