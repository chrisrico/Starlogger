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
    track_context, _humanize_cue,
    is_quality_song, has_cinematic_cue, has_ui_context, best_song_ids,
    QUALITY_CINEMATIC_MIN_DUR, QUALITY_MIN_DUR,
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


def test_track_context_prefers_cinematic_cue_scoped_to_system():
    labels = ["SC_Music_Cinematic/MX_SC_DL_Biome_Savana",
              "SC_Music_PU_StarSystem/MUS_PU_StantonSystem",
              "SC_Music_Mood/Normal"]
    assert track_context(labels) == ("Stanton", "Biome Savana")


def test_track_context_falls_back_to_ambient_region():
    labels = ["SC_Music_PU_StarSystem/MUS_PU_StantonSystem",
              "SC_Music_Ambient_State_New/Ambient_Default"]
    assert track_context(labels) == ("Stanton", "Ambient")


def test_track_context_empty_when_nothing_readable():
    assert track_context([]) == ("", "")


def test_track_context_star_marine_mode_has_no_system():
    assert track_context(["SC_Music_Master/Star_Marine"]) == ("", "Star Marine")


def test_track_context_joins_up_to_two_systems():
    labels = ["SC_Music_Cinematic/MX_SC_DL_Biome_Savana",
              "SC_Music_PU_StarSystem/MUS_PU_StantonSystem",
              "SC_Music_PU_StarSystem/MUS_PU_PyroSystem"]
    assert track_context(labels) == ("Pyro/Stanton", "Biome Savana")


def test_humanize_cue_strips_wwise_plumbing_tokens():
    assert _humanize_cue("MXGS_PU_Cine_Location_Lorville") == "Lorville"
    assert _humanize_cue("MX_PU_Cine_Rest_Stops") == "Rest Stops"
    assert _humanize_cue("MX_SC_DL_Biome_Savana") == "Biome Savana"


# --- "best track" selection: pinned allowlist + p4k-only quality heuristic ---
CINE = ["SC_Music_Cinematic/MX_SC_DL_Biome_Savana", "SC_Music_PU_StarSystem/MUS_PU_StantonSystem"]
AMBIENT = ["SC_Music_Mood/Normal", "SC_Music_PU_StarSystem/MUS_PU_StantonSystem"]
UI = ["SC_Music_Menu/Loading_Default", "SC_Music_Master/Front_End"]


def test_is_quality_song_cinematic_needs_two_minutes():
    assert is_quality_song(QUALITY_CINEMATIC_MIN_DUR, CINE) is True
    assert is_quality_song(QUALITY_CINEMATIC_MIN_DUR - 1, CINE) is False   # cue but too short


def test_is_quality_song_noncinematic_needs_four_minutes():
    assert is_quality_song(QUALITY_MIN_DUR, AMBIENT) is True               # long enough on length
    assert is_quality_song(QUALITY_MIN_DUR - 1, AMBIENT) is False          # mid-length, no cue -> out


def test_is_quality_song_ui_always_rejected():
    # menu/loading/commercial music is never a "song", even when long and cinematic-flagged.
    assert has_ui_context(UI) and not is_quality_song(600, CINE + UI)


def test_has_cinematic_cue_detects_cinematic_group():
    assert has_cinematic_cue(CINE) is True
    assert has_cinematic_cue(AMBIENT) is False


# Tiny HIRC: three standalone segments -> a long ambient cue, a short non-cue, a short UI cue.
def _seg(sid): return {"MusicSegment": {"id": sid, "music_params": {"node_base": {"direct_parent_id": 0}}}}
def _trk(tid, parent, media):
    return {"MusicTrack": {"id": tid, "node_base": {"direct_parent_id": parent},
                           "sources": [{"media_id": media}]}}
HIRC_SEL = [_seg(1), _trk(11, 1, 100), _seg(2), _trk(22, 2, 200), _seg(3), _trk(33, 3, 300)]
DURS_SEL = {"100": 300.0, "200": 30.0, "300": 600.0}
LABELS_SEL = {"100": AMBIENT, "200": AMBIENT, "300": UI}   # 100 long ambient, 200 short, 300 long UI


def test_best_song_ids_applies_heuristic():
    keep = best_song_ids(HIRC_SEL, DURS_SEL, labels=LABELS_SEL, allowlist=set())
    assert "100" in keep        # 300s ambient standalone -> quality
    assert "200" not in keep    # 30s -> too short
    assert "300" not in keep    # long but UI music -> rejected


def test_best_song_ids_pins_allowlist_even_when_rule_rejects():
    # "200" fails the heuristic (too short) but is pinned -> always kept; "300" UI stays out.
    keep = best_song_ids(HIRC_SEL, DURS_SEL, labels=LABELS_SEL, allowlist={"200", "999"})
    assert "200" in keep        # pinned wins over the rule
    assert "999" not in keep     # pinned but not present in the bank -> not invented


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
