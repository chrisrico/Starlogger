"""Music context labels: the pure links that turn the FNV-hashed switch hierarchy back into
readable ``"<group>/<state>"`` context, and the display summariser.

No p4k / StarBreaker here -- every function under test is pure. The decision-tree fixture is a
hand-built AkDecisionTree blob (depth 1) whose two leaves key on the real Explore/Ambient state
hashes, mirroring the validated layout in ``_music_context``.

Run: .venv/bin/python -m pytest tests/test_music_context.py
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger.scdata._music_context import (
    fnv1_32, parse_atl_names, build_hash_index, parse_decision_tree,
    switch_container_labels, _descendant_media, _index_objects, _build_children,
    primary_context, _humanize_cue,
)

# Hashes the game actually uses (FNV-1 32-bit, lowercased) -- the anchor the whole chain rests on.
H_GROUP = 4120821525   # SC_Music_Space_States
H_EXPLORE = 579523862
H_AMBIENT = 77978275


def test_fnv1_32_matches_known_wwise_hashes():
    # FNV-1 (multiply THEN xor), not FNV-1a -- and lowercased. Regressions here break every label.
    assert fnv1_32("SC_Music_Space_States") == H_GROUP
    assert fnv1_32("Explore") == H_EXPLORE
    assert fnv1_32("Ambient") == H_AMBIENT
    assert fnv1_32("explore") == fnv1_32("Explore")   # case-folded


def test_parse_atl_names_groups_states_by_wwise_switch():
    xml = """<ATLConfig><AudioSwitches>
      <ATLSwitch atl_name="music_space">
        <ATLSwitchState><WwiseSwitch wwise_name="SC_Music_Space_States">
          <WwiseValue wwise_name="Explore"/></WwiseSwitch></ATLSwitchState>
        <ATLSwitchState><WwiseSwitch wwise_name="SC_Music_Space_States">
          <WwiseValue wwise_name="Ambient"/></WwiseSwitch></ATLSwitchState>
      </ATLSwitch>
    </AudioSwitches></ATLConfig>"""
    groups = parse_atl_names(xml)
    assert groups["SC_Music_Space_States"] == ["Explore", "Ambient"]   # keyed by inner Wwise name


def _node(key, value, weight=50, prob=100):
    return struct.pack("<IIHH", key, value, weight, prob)


# A depth-1 AkDecisionTree: root (internal) -> two leaves keyed Explore / Ambient.
#   root.value packs (child_count<<16)|first_child_idx = (2<<16)|1
#   each leaf.value is the target HIRC node id (9001 / 9002)
TREE = _node(0, (2 << 16) | 1) + _node(H_EXPLORE, 9001) + _node(H_AMBIENT, 9002)


def test_parse_decision_tree_walks_paths_to_leaves():
    paths = parse_decision_tree(TREE, depth=1)
    assert ([H_EXPLORE], 9001) in paths
    assert ([H_AMBIENT], 9002) in paths
    assert len(paths) == 2


def test_switch_container_labels_maps_state_hash_to_label():
    groups = {"SC_Music_Space_States": ["Explore", "Ambient"]}
    gh, sh = build_hash_index(groups)
    body = {"group_ids": [H_GROUP], "tree_depth": 1, "decision_tree": list(TREE)}
    out = dict(switch_container_labels(body, gh, sh))
    assert out[9001] == ["SC_Music_Space_States/Explore"]
    assert out[9002] == ["SC_Music_Space_States/Ambient"]


def test_switch_container_labels_drops_nonmusic_groups():
    # A group whose name lacks the "music" marker is SFX/dialogue context -> no label emitted.
    gh, sh = build_hash_index({"SC_Weapon_States": ["Explore"]})
    body = {"group_ids": [fnv1_32("SC_Weapon_States")], "tree_depth": 1, "decision_tree": list(TREE)}
    assert switch_container_labels(body, gh, sh) == []


def test_descendant_media_collects_sources_under_a_leaf():
    # leaf playlist 9001 -> segment 500 -> track 50 -> media 7777
    hirc = [
        {"MusicPlaylistContainer": {"id": 9001, "music_params": {"node_base": {"direct_parent_id": 0}}}},
        {"MusicSegment": {"id": 500, "music_params": {"node_base": {"direct_parent_id": 9001}}}},
        {"MusicTrack": {"id": 50, "node_base": {"direct_parent_id": 500},
                        "sources": [{"media_id": 7777}]}},
    ]
    objs, typ = _index_objects(hirc)
    children = _build_children(objs, typ)
    assert _descendant_media({9001}, objs, typ, children) == {"7777"}


def test_primary_context_prefers_cinematic_cue_scoped_to_system():
    labels = ["SC_Music_Cinematic/MX_SC_DL_Biome_Savana",
              "SC_Music_PU_StarSystem/MUS_PU_StantonSystem",
              "SC_Music_Mood/Normal"]
    assert primary_context(labels) == "Biome Savana · Stanton"


def test_primary_context_falls_back_to_ambient_region():
    labels = ["SC_Music_PU_StarSystem/MUS_PU_StantonSystem",
              "SC_Music_Ambient_State_New/Ambient_Default"]
    assert primary_context(labels) == "Ambient · Stanton"


def test_primary_context_empty_when_nothing_readable():
    assert primary_context([]) == ""


def test_humanize_cue_strips_wwise_plumbing_tokens():
    assert _humanize_cue("MXGS_PU_Cine_Location_Lorville") == "Lorville"
    assert _humanize_cue("MX_PU_Cine_Rest_Stops") == "Rest Stops"
    assert _humanize_cue("MX_SC_DL_Biome_Savana") == "Biome Savana"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
