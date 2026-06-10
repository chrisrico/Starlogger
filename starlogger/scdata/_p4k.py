"""StarBreaker binary + p4k extraction plumbing and the shared record-tree helpers."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

from ..config import DATA_DIR, IS_WINDOWS, USER_AGENT
from ..patterns import camel_split


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

# Stock ship "components" worth surfacing with their grade. The item record's
# SAttachableComponentParams.AttachDef carries an integer ``Grade`` (1-4) which the
# game's UI shows as a letter; map it and key the four headline component slots by a
# friendly name. (Verified vs the localised item description: Grade 1 -> "Grade: A".)
_GRADE_LETTER = {1: "A", 2: "B", 3: "C", 4: "D"}


def _load_json(path: str):
    """Resource-safe ``json.load``. The extractors walk tens of thousands of record
    files, so leaking a descriptor per read (the old ``json.load(open(p))`` form)
    adds up. Raises the same OSError/ValueError the call sites already guard against."""
    with open(path) as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# Nested-record traversal -- DataCore records are arbitrarily deep dict/list trees,
# so several extractors need the same depth-first recursion. These two cover it:
# _deep_walk for "visit every node" and _deep_search for "first matching node".
# --------------------------------------------------------------------------- #
def _deep_walk(o, visit) -> None:
    """Call ``visit(node)`` on every dict nested anywhere in ``o`` (depth-first)."""
    if isinstance(o, dict):
        visit(o)
        for v in o.values():
            _deep_walk(v, visit)
    elif isinstance(o, list):
        for v in o:
            _deep_walk(v, visit)


def _deep_search(o, probe):
    """First truthy ``probe(node)`` over every dict nested in ``o`` (depth-first), else None."""
    if isinstance(o, dict):
        r = probe(o)
        if r:
            return r
        for v in o.values():
            r = _deep_search(v, probe)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _deep_search(v, probe)
            if r:
                return r
    return None


def _deep_find(o, key: str, want) -> dict | None:
    """First dict in a nested structure whose ``o[key] == want``."""
    return _deep_search(o, lambda d: d if d.get(key) == want else None)


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


# Every catalog extract lands in a fresh subdir of this, NOT the system ``/tmp``. On this
# host (and many Linux setups) ``/tmp`` is ``tmpfs`` -- RAM-backed -- so a multi-GB DataCore
# extract there competes with the game for memory, races the OOM killer, and can leave a
# half-written record tree that silently yields a decimated catalog (the 2026-06-09 one-ship
# ``ships.json``). ``DATA_DIR`` is real disk, so extracts are bounded by disk, not RAM.
EXTRACT_TMP_DIR = os.path.join(DATA_DIR, "scratch")


def scratch_dir(prefix: str) -> str:
    """A fresh extraction work directory under ``DATA_DIR`` (never the system tmpfs ``/tmp``).
    The caller owns cleanup (``shutil.rmtree`` in a ``finally``), as with ``mkdtemp``."""
    os.makedirs(EXTRACT_TMP_DIR, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=EXTRACT_TMP_DIR)


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


def _loc_text(val: str, loc: dict) -> str:
    """Resolve a ``@key`` localisation reference to its string (or '' )."""
    if isinstance(val, str) and val.startswith("@"):
        return loc.get(val[1:].lower(), "")
    return ""


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


def extract_localization(p4k: str, sb: str, workdir: str) -> dict:
    """Pull just english ``global.ini`` from the p4k and load it (case-insensitive
    key->value). Fast: it's one file, after StarBreaker reads the central directory."""
    recs = os.path.join(workdir, "loc")
    os.makedirs(recs, exist_ok=True)
    _run(sb, p4k, ["p4k", "extract", "--p4k", p4k, "--filter", "**/english/global.ini", "-o", recs])
    return load_localization(recs)


# --------------------------------------------------------------------------- #
# Mineable rocks: RS (radar signature) + composition
# --------------------------------------------------------------------------- #
# Each mineable rock is an EntityClassDefinition under entities/mineable/. Its RS
# base value (the number the in-game radar shows for one rock; a cluster reads
# base x count) lives on the SSCSignatureSystemParams component; its mineral makeup
# is a file ref on the MineableParams component to a MineableComposition preset
# (a list of elements with min/max % and spawn probability). All three record
# kinds come out of the one full `dcb extract` -- the values can't be pulled via
# the cheap `dcb query` path (querying EntityClassDefinition components materialises
# all ~28k entities, ballooning to multi-GB), so this rides the ship-cargo extract.

def _component(rv: dict, type_name: str) -> dict | None:
    """First component of the given ``_Type_`` in an entity record value."""
    for c in (rv.get("Components") or []):
        if isinstance(c, dict) and c.get("_Type_") == type_name:
            return c
    return None


def _ref_basename(ref: str | None) -> str | None:
    """``file://.../foo.json`` (or ``foo.json?query``) -> ``foo.json`` (lower)."""
    if not isinstance(ref, str) or not ref:
        return None
    return os.path.basename(ref.split("?")[0]).lower()


def _index_by_basename(records_root: str, *subdirs: str) -> dict:
    """{file basename(lower) -> full path} for json anywhere under each
    ``records_root/**/<subdir>/`` (recursing into nested subfolders, e.g. presets
    split into ``rockcompositionpresets/asteroidshipmining/``)."""
    idx: dict[str, str] = {}
    for sub in subdirs:
        for p in glob.glob(os.path.join(records_root, "**", sub, "**", "*.json"), recursive=True):
            idx[os.path.basename(p).lower()] = p
    return idx


def _record_value(path: str | None) -> dict:
    """A record file's ``_RecordValue_`` dict, or {} (missing file / not a record)."""
    try:
        return _load_json(path)["_RecordValue_"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _record_token_name(path: str) -> str:
    """Friendly name from a record's ``_RecordName_`` token (``MineableElement.Iron_Ore``
    -> "Iron Ore"); falls back to the filename stem. CamelCase + underscores split."""
    try:
        rn = _load_json(path).get("_RecordName_", "")
    except (OSError, ValueError):
        rn = ""
    tok = rn.split(".", 1)[1] if "." in rn else (rn or os.path.basename(path)[:-5])
    return camel_split(tok.replace("_", " ")).strip().title()
