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

``build_context_labels`` returns the full ``{media_id: [labels...]}``; ``primary_context``
distils one track's many labels (the same bed is reused across dozens of switch leaves) to a
single short string for display -- preferring the specific cinematic *cue* name, falling back to
star-system / ambient. The HIRC object/parent model is shared with ``_music`` (we accept an
already-dumped ``hirc`` so a build that already called ``dump_music_hirc`` pays for it once).
"""

from __future__ import annotations

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


def primary_context(labels: list[str]) -> str:
    """One short, human context for a track, from its many ``"<group>/<state>"`` labels.

    The same media bed is reused across dozens of switch leaves, so most labels are generic; we
    surface the most *identifying* one: a cinematic *cue* name (``Hospitals LowTech``,
    ``Rest Stops``) when present, scoped to its star system; otherwise the game mode / ambient
    region. Returns ``""`` when nothing readable is reachable."""
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
    cue = next((c for c in cues if c), None)
    if cue:
        return f"{cue} · {sysn[0]}" if len(sysn) == 1 else cue
    if "Star Marine" in modes:
        return "Star Marine"
    if sysn:
        region = "/".join(sysn) if len(sysn) <= 2 else "PU"
        return f"Ambient · {region}" if ambient else region
    return next(iter(modes), "")


def context_for_media(p4k: str, sb: str | None = None,
                      hirc: list | None = None) -> dict[str, str]:
    """``{media_id: primary_context}`` -- the one-string-per-track view the jukebox shows."""
    return {m: primary_context(lbls)
            for m, lbls in build_context_labels(p4k, sb, hirc).items()}
