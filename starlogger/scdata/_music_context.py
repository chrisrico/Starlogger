"""Map music WEM media ids -> a readable gameplay-context label (region / mood / cue).

The music pool in ``MUS_Music_Global.bnk`` is FNV-hashed, nameless adaptive building blocks; the
game picks what to play by walking a Wwise *music switch* hierarchy. A ``MusicSwitchContainer``
carries an ordered list of switch *groups* (e.g. ``SC_Music_Space_States``) and a fixed-depth
``AkDecisionTree`` whose path keys are the *state* hashes (``Explore``, ``Ambient``, ...); each
leaf points at a playlist/segment that resolves to media. So a media's context is the set of
(group, state) pairs on every decision path that reaches it.

We rebuild that mapping with no runtime data, in three pure/testable links:

  1. ``parse_atl_names`` -- readable Wwise names. Release banks carry only FNV hashes, but
     ``Data\\Libs\\GameAudio\\ATL_Global_SC_SwitchesAndStates.xml`` (CryXmlB) names every switch
     group and its states; extract+convert it once and read group -> [states].
  2. ``fnv1_32`` -- name -> id. Wwise hashes lowercased names with FNV-1 (multiply *then* xor)
     32-bit; this matches the readable names against the numeric ``group_ids`` / decision-tree
     keys in the bank dump.
  3. ``parse_decision_tree`` + ``switch_container_labels`` -- walk the (raw) AkDecisionTree blob to
     recover, per reachable leaf, the ordered state-key path, map each (position->group,
     key->state) to a ``"<group>/<state>"`` label, and propagate it to every descendant media.

``build_context_labels`` returns the full ``{media_id: [labels...]}``; ``track_context``
distils one track's many labels (the same bed is reused across dozens of switch leaves) to a
``(system, detail)`` pair for display -- the star system / region plus the specific cinematic
*cue* name, falling back to ambient / game mode. The HIRC object/parent model is shared with ``_music`` (we accept an
already-dumped ``hirc`` so a build that already called ``dump_music_hirc`` pays for it once).
"""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
from collections import defaultdict

from ._p4k import _run, ensure_binary
from ._music import MUSIC_BANK, dump_music_hirc

# The one ATL config that names every switch group + state (CryXmlB inside the p4k).
ATL_FILTER = "**/ATL_Global_SC_SwitchesAndStates.xml"
ATL_RELPATH = "Data/Libs/GameAudio/ATL_Global_SC_SwitchesAndStates.xml"

# A switch group is "music context" (vs SFX/dialogue) if its name carries one of these markers.
_MUSIC_MARKERS = ("music", "musiclogic")


# --------------------------------------------------------------------------- #
# Link 1: readable names from the ATL XML
# --------------------------------------------------------------------------- #
def parse_atl_names(xml_text: str) -> dict[str, list[str]]:
    """Parse the (already CryXmlB-converted) ATL switches XML into ``{group: [state, ...]}``.

    Each ``<WwiseSwitch wwise_name="GROUP">`` is the Wwise group that gets hashed into the bank;
    its nested ``<WwiseValue wwise_name="STATE"/>`` children are that switch's states. Pure."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    groups: dict[str, list[str]] = defaultdict(list)
    for ws in root.iter("WwiseSwitch"):
        g = ws.get("wwise_name")
        if not g:
            continue
        for wv in ws.iter("WwiseValue"):
            s = wv.get("wwise_name")
            if s and s not in groups[g]:
                groups[g].append(s)
    return dict(groups)


def is_music_group(name: str) -> bool:
    """Is this switch-group a *music* context group (vs an SFX/dialogue switch)?"""
    low = name.lower()
    return any(m in low for m in _MUSIC_MARKERS)


def extract_atl_xml(p4k: str, sb: str, workdir: str) -> str:
    """Extract + CryXmlB-convert the single ATL switches XML; return its text. Cheap (~0.4 MB)."""
    os.makedirs(workdir, exist_ok=True)
    _run(sb, p4k, ["p4k", "extract", "--p4k", p4k, "--filter", ATL_FILTER,
                   "--convert", "cryxml", "-o", workdir], timeout=300)
    path = os.path.join(workdir, *ATL_RELPATH.split("/"))
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# --------------------------------------------------------------------------- #
# Link 2: Wwise name -> id hash (FNV-1 32-bit over the lowercased ASCII name)
# --------------------------------------------------------------------------- #
def fnv1_32(name: str) -> int:
    """Wwise's short-id hash: FNV-1 (not 1a) 32-bit over the lowercased name -- multiply *then*
    xor. VERIFIED: SC_Music_Space_States->4120821525, Explore->579523862, Ambient->77978275."""
    h = 2166136261
    for b in name.lower().encode("ascii", "ignore"):
        h = (h * 16777619) & 0xFFFFFFFF
        h ^= b
    return h


def build_hash_index(groups: dict[str, list[str]]) -> tuple[dict[int, str], dict[int, str]]:
    """``(hash->group_name, hash->state_name)`` reverse maps, to turn the bank's numeric ids back
    into readable names."""
    gh: dict[int, str] = {}
    sh: dict[int, str] = {}
    for g, states in groups.items():
        gh[fnv1_32(g)] = g
        for s in states:
            sh.setdefault(fnv1_32(s), s)
    return gh, sh


# --------------------------------------------------------------------------- #
# Link 3: the AkDecisionTree blob
# --------------------------------------------------------------------------- #
# Each tree node is 12 bytes, little-endian:
#   key:u32   -- the switch-*state* hash this branch matches (0 == default/"any")
#   value:u32 -- internal node: high16 = child count, low16 = first-child node index;
#                leaf node (level == tree_depth): the child audioNodeId (a HIRC object id)
#   weight:u16, probability:u16  -- unused for the mapping
_NODE = struct.Struct("<IIHH")
_NODE_SIZE = 12


def parse_decision_tree(blob: bytes, depth: int) -> list[tuple[list[int], int]]:
    """Walk an AkDecisionTree byte blob into ``[(state_key_path, leaf_node_id), ...]``.

    ``state_key_path[i]`` is the state hash chosen at level ``i`` (0 == default/any); the leaf is
    the HIRC object the path resolves to. A node at ``level < depth`` is internal and its
    ``value`` packs ``(child_count<<16)|first_child_idx``; at ``level == depth`` it is a leaf whose
    ``value`` is the target audioNodeId. Pure / unit-testable. VALIDATED on container 573503732:
    Explore(579523862) and Ambient(77978275) both resolve to real playlist ids in the dump."""
    nnodes = len(blob) // _NODE_SIZE
    out: list[tuple[list[int], int]] = []

    def node(i: int) -> tuple[int, int]:
        key, value, _w, _p = _NODE.unpack_from(blob, i * _NODE_SIZE)
        return key, value

    def walk(i: int, level: int, path: list[int]) -> None:
        if i >= nnodes:
            return
        key, value = node(i)
        if level >= depth:
            out.append((path, value))  # leaf: value is the target node id
            return
        count = value >> 16
        first = value & 0xFFFF
        for c in range(first, first + count):
            if c >= nnodes or c <= i:   # guard against a malformed/over-deep blob
                continue
            ck, _ = node(c)
            walk(c, level + 1, path + [ck])

    walk(0, 0, [])
    return out


# --------------------------------------------------------------------------- #
# Object model (media descent) -- same shape as _music.select_full_songs
# --------------------------------------------------------------------------- #
def _index_objects(hirc: list) -> tuple[dict, dict]:
    """``(objs{id:body}, typ{id:TypeName})`` over the HIRC list."""
    objs: dict = {}
    typ: dict = {}
    for o in hirc:
        if not isinstance(o, dict) or not o:
            continue
        t = next(iter(o))
        b = o[t]
        if isinstance(b, dict) and b.get("id") is not None:
            objs[b["id"]] = b
            typ[b["id"]] = t
    return objs, typ


def _build_children(objs: dict, typ: dict) -> dict[int, set[int]]:
    """``{parent_id: {child_id,...}}`` -- the ``direct_parent_id`` chain _music walks, inverted."""
    children: dict[int, set[int]] = defaultdict(set)
    for i, b in objs.items():
        if typ[i] == "MusicTrack":
            nb = b.get("node_base") or {}
        else:
            mp = (b.get("music_params")
                  or (b.get("trans_params") or {}).get("music_params") or {})
            nb = mp.get("node_base") or {}
        pid = nb.get("direct_parent_id")
        if pid:
            children[pid].add(i)
    return children


def _descendant_media(roots: set[int], objs: dict, typ: dict,
                      children: dict[int, set[int]]) -> set[str]:
    """All ``media_id``s under ``roots`` -- follow children to every MusicTrack's sources."""
    media: set[str] = set()
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        i = stack.pop()
        if i in seen or i not in objs:
            continue
        seen.add(i)
        if typ[i] == "MusicTrack":
            for s in (objs[i].get("sources") or []):
                m = s.get("media_id")
                if m:
                    media.add(str(m))
        stack.extend(children.get(i, ()))
    return media


def switch_container_labels(b: dict, group_hashes: dict[int, str],
                            state_hashes: dict[int, str]) -> list[tuple[int, list[str]]]:
    """For one MusicSwitchContainer body, return ``[(leaf_node_id, [labels...]), ...]``.

    A label is ``"<group>/<state>"`` for each non-default (key, group) pair on the leaf's path;
    if the group is named but the state hash is unknown we keep just the group. Non-music groups
    are dropped."""
    gids = b.get("group_ids") or []
    depth = b.get("tree_depth") or 0
    blob = bytes(b.get("decision_tree") or [])
    if not gids or not blob:
        return []
    out: list[tuple[int, list[str]]] = []
    for path, leaf in parse_decision_tree(blob, depth):
        labels: list[str] = []
        for pos, key in enumerate(path):
            if key == 0 or pos >= len(gids):
                continue  # default/"any" branch carries no specific context
            g = group_hashes.get(gids[pos])
            if not g or not is_music_group(g):
                continue
            s = state_hashes.get(key)
            labels.append(f"{g}/{s}" if s else g)
        if labels and leaf:
            out.append((leaf, labels))
    return out


def build_context_labels(p4k: str, sb: str | None = None,
                         hirc: list | None = None) -> dict[str, list[str]]:
    """``{media_id(str): [sorted unique "<group>/<state>" labels]}`` for the whole music pool.

    Extract+parse the ATL names -> hash them -> for every MusicSwitchContainer in the bank HIRC,
    parse its decision tree, turn each leaf path into labels, and propagate to descendant media.
    Pass ``hirc`` to reuse a dump the caller already paid for (else we fetch it)."""
    sb = sb or ensure_binary()
    with tempfile.TemporaryDirectory() as wd:
        groups = parse_atl_names(extract_atl_xml(p4k, sb, wd))
    group_hashes, state_hashes = build_hash_index(groups)

    if hirc is None:
        hirc = dump_music_hirc(p4k, sb)
    objs, typ = _index_objects(hirc)
    children = _build_children(objs, typ)

    media_labels: dict[str, set[str]] = defaultdict(set)
    for i, b in objs.items():
        if typ[i] != "MusicSwitchContainer":
            continue
        for leaf, labels in switch_container_labels(b, group_hashes, state_hashes):
            for m in _descendant_media({leaf}, objs, typ, children):
                media_labels[m].update(labels)

    return {m: sorted(lbls) for m, lbls in media_labels.items()}


# --------------------------------------------------------------------------- #
# Display: distil a track's many raw labels to one short readable context string
# --------------------------------------------------------------------------- #
_SYSTEMS = {"stanton": "Stanton", "pyro": "Pyro", "nyx": "Nyx"}
# Leading tokens that are Wwise plumbing, not part of the human cue name.
_CUE_NOISE = {"mx", "mxgs", "pu", "sc", "dl", "cine", "cinematic", "location", "loc", "gs", "mus"}


def _humanize_cue(state: str) -> str:
    """``MXGS_PU_Cine_Location_Lorville`` -> ``Lorville``; ``MX_SC_DL_Biome_Savana`` ->
    ``Biome Savana`` -- drop the leading plumbing tokens, space the rest."""
    toks = state.split("_")
    while toks and toks[0].lower() in _CUE_NOISE:
        toks.pop(0)
    return " ".join(toks).strip()


def track_context(labels: list[str]) -> tuple[str, str]:
    """``(system, detail)`` for a track, from its many ``"<group>/<state>"`` labels.

    The same media bed is reused across dozens of switch leaves, so most labels are generic; we
    surface the most *identifying* split: ``system`` is the star system / region (the jukebox
    sorts and groups by it), ``detail`` is the specific cinematic *cue* name (``Hospitals
    LowTech``, ``Rest Stops``) when present, else the game mode / ambient marker. Either part may
    be ``""`` when nothing readable is reachable for it; the UI joins them as ``System · Detail``."""
    cues: list[str] = []
    systems: set[str] = set()
    modes: set[str] = set()
    ambient = False
    for lab in labels:
        grp, _, st = lab.partition("/")
        if grp == "SC_Music_Cinematic" and st:
            cues.append(_humanize_cue(st))
        elif grp == "SC_Music_PU_StarSystem":
            m = re.search(r"MUS_PU_(\w+?)System", st)
            if m:
                systems.add(m.group(1))
        elif grp.endswith("_StarMap_Location"):
            low = grp.lower()
            for k, v in _SYSTEMS.items():
                if k in low:
                    systems.add(v)
        elif grp == "SC_Music_Master" and st:
            modes.add(st.replace("_", " "))
        if "ambient" in lab.lower():   # group OR state may carry the ambient marker
            ambient = True
    sysn = sorted({_SYSTEMS.get(s.lower(), s) for s in systems})
    system = ("/".join(sysn) if len(sysn) <= 2 else "PU") if sysn else ""
    cue = next((c for c in cues if c), None)
    if cue:
        detail = cue
    elif "Star Marine" in modes:
        detail = "Star Marine"
    elif sysn:
        detail = "Ambient" if ambient else ""
    else:
        detail = next(iter(modes), "")
    return system, detail


def context_for_media(p4k: str, sb: str | None = None,
                      hirc: list | None = None) -> dict[str, tuple[str, str]]:
    """``{media_id: (system, detail)}`` -- the per-track context view the jukebox shows."""
    return {m: track_context(lbls)
            for m, lbls in build_context_labels(p4k, sb, hirc).items()}


# --------------------------------------------------------------------------- #
# "Best track" selection: a pinned allowlist + a p4k-only heuristic for new music
#
# We hand-curated 74 "best" tracks (full, distinct, composed pieces) by fingerprint-matching the
# decoded songs against community OST compilations -- a one-off that needed the internet. To keep
# the jukebox good *without* that crutch as the game ships new music, two parts:
#   1. ALLOWLIST -- every WEM id named in the shipped default_music_curation.json is ALWAYS
#      extracted. (That file is also the source of the jukebox's default track names, so the
#      curated set and its titles live in one place; edit it to add/remove pinned tracks.)
#   2. is_quality_song -- a rule over features we can read straight from the p4k (duration + the
#      context labels) that predicts "best". Fitted to the 74: a track qualifies if it is NOT a
#      menu/loading/commercial/UI cue AND is either a cinematic location cue >=2:00 or any
#      standalone piece >=4:00. On the labelled set: ~72% recall at ~85% precision -- and recall
#      is moot for known-best (the allowlist pins them), so the rule is tuned to add *new* music
#      without dragging in stingers/ambient beds.
# --------------------------------------------------------------------------- #

# A cinematic cue this long counts; any standalone piece this long counts on length alone.
QUALITY_CINEMATIC_MIN_DUR = 120.0
QUALITY_MIN_DUR = 240.0
# Label markers for UI / non-song music (menu, loading screen, ship commercials, race, etc.).
_UI_MARKERS = ("menu", "loading", "frontend", "front_end", "front/", "commercial",
               "race", "logic_theme", "charactercustomizer")


def load_allowlist(path: str | None = None) -> set[str]:
    """The pinned 'best track' WEM ids (always extracted): every id named (or ordered) in the
    shipped default curation. Empty set if the file is absent."""
    if path is None:
        from ..config import DEFAULT_MUSIC_CURATION_PATH
        path = DEFAULT_MUSIC_CURATION_PATH
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return set()
    return set(d.get("names") or {}) | set(d.get("order") or [])


def has_cinematic_cue(labels: list[str]) -> bool:
    """Does this media play under a cinematic location cue (a composed-piece signal)?"""
    return any(l.split("/")[0] == "SC_Music_Cinematic" for l in labels)


def has_ui_context(labels: list[str]) -> bool:
    """Is this media menu / loading / commercial / UI music (a not-a-song signal)?"""
    return any(any(u in l.lower() for u in _UI_MARKERS) for l in labels)


def is_quality_song(dur: float, labels: list[str]) -> bool:
    """p4k-only 'best track' predicate (duration + context labels). See the section header for
    the rationale/accuracy. Pure / unit-testable."""
    if has_ui_context(labels):
        return False
    if has_cinematic_cue(labels) and dur >= QUALITY_CINEMATIC_MIN_DUR:
        return True
    return dur >= QUALITY_MIN_DUR


def best_song_ids(hirc: list, durs: dict, labels: dict | None = None,
                  p4k: str | None = None, sb: str | None = None,
                  allowlist: set | None = None) -> set[str]:
    """The WEM ids the jukebox should extract: every pinned allowlist track that still exists, plus
    every *standalone* song the heuristic accepts. ``labels`` may be passed to reuse a dump the
    caller already paid for (else built from ``p4k``/``sb``)."""
    from ._music import select_full_songs
    if labels is None:
        labels = build_context_labels(p4k, sb, hirc)
    if allowlist is None:
        allowlist = load_allowlist()
    standalone = select_full_songs(hirc, durs, 0.0)
    quality = {m for m in standalone if is_quality_song(durs.get(m, 0), labels.get(m, []))}
    pinned = {m for m in allowlist if m in durs}   # always extract identified best (if still present)
    return quality | pinned
