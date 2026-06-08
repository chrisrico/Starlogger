"""Jukebox: parse the `wwise list` table + the music.json cache/idempotency helpers.

``_parse_wwise_list`` is the pure layer (no p4k / no StarBreaker) -- it turns the columnar
``wwise list`` output into ``{id, size, codec, duration}`` rows. Fixtures are captured from a
real ``MUS_Music_*.bnk`` listing (streamed MediaFile rows: Offset is ``-``).

Run: .venv/bin/python -m pytest tests/test_music.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from starlogger import music
from starlogger.scdata._music import _parse_wwise_list, select_full_songs

# A verbatim slice of `starbreaker wwise list MUS_Music_Stanton.bnk` (header + rule + rows).
SAMPLE = """WEM ID       Source          Offset     Size       Codec        Duration
----------------------------------------------------------------------------
11083731     MediaFile       -          563109     Vorbis       25.05s
51358986     MediaFile       -          3941651    Vorbis       203.64s
323693222    MediaFile       -          243356     Vorbis       211.51s
"""


def test_parse_skips_header_and_rule():
    rows = _parse_wwise_list(SAMPLE)
    assert len(rows) == 3                       # the "WEM ID" header + "----" rule are dropped
    assert [r["id"] for r in rows] == ["11083731", "51358986", "323693222"]


def test_parse_reads_size_codec_duration():
    first = _parse_wwise_list(SAMPLE)[0]
    assert first == {"id": "11083731", "size": 563109, "codec": "Vorbis", "duration": 25.05}


def test_parse_ignores_blank_and_malformed_lines():
    junk = SAMPLE + "\n\nnot a row\n12345 partial\n"
    rows = _parse_wwise_list(junk)
    assert len(rows) == 3                       # only the three well-formed data rows survive


def test_is_extracted_roundtrip(tmp_path, monkeypatch):
    path = str(tmp_path / "music.json")
    mdir = tmp_path / "music"
    mdir.mkdir()
    monkeypatch.setattr(music, "_cache", {"mtime": None, "data": {"tracks": [], "count": 0}})

    # nothing written yet -> not extracted
    assert music.is_extracted(path=path, music_dir=str(mdir)) is False

    tracks = [{"id": "1", "file": "1.ogg", "duration": 200.0, "size": 10},
              {"id": "2", "file": "2.ogg", "duration": 100.0, "size": 10}]
    music.save_music(tracks, game_version="4.8.0", path=path)

    # manifest present but the oggs aren't on disk yet -> still not extracted
    assert music.is_extracted("4.8.0", path=path, music_dir=str(mdir)) is False
    (mdir / "1.ogg").write_bytes(b"x")
    (mdir / "2.ogg").write_bytes(b"x")
    assert music.is_extracted("4.8.0", path=path, music_dir=str(mdir)) is True
    # a major game-version move invalidates the cached extraction
    assert music.is_extracted("4.9.0", path=path, music_dir=str(mdir)) is False


def test_music_cache_helpers_roundtrip(tmp_path, monkeypatch):
    path = str(tmp_path / "music.json")
    monkeypatch.setattr(music, "_cache", {"mtime": None, "data": {"tracks": [], "count": 0}})
    tracks = [{"id": "a", "file": "a.ogg", "duration": 200.0, "size": 1},
              {"id": "b", "file": "b.ogg", "duration": 100.0, "size": 1}]
    music.save_music(tracks, game_version="4.8.0", path=path)
    assert music.music_version(path) == "4.8.0"
    assert music.music_extract_version(path) == music.EXTRACT_VERSION
    assert music.track_ids(path) == {"a", "b"}
    # restamp bumps the version for a new build WITHOUT touching the track set
    music.restamp_version("4.9.0", path=path)
    assert music.music_version(path) == "4.9.0"
    assert music.track_ids(path) == {"a", "b"}


def test_music_catalog_is_always_present():
    """Music is a first-class background catalog now (auto-extracts on first run), so it's always
    in the refresh set -- its has_cache (is_extracted) drives the build, not an opt-in gate."""
    from starlogger import catalogs
    cats = {c.label: c for c in catalogs._build_catalogs(catalogs.SHIP_CARGO_PATH)}
    assert "music" in cats
    assert cats["music"].has_cache is music.is_extracted   # first run False -> "no cache" -> build


# A tiny HIRC slice (parsed `wwise dump`): one standalone long cue, one layered 2-stem cue, one
# short standalone. Tracks carry node_base.direct_parent_id -> their MusicSegment; a media is a
# "full song" only if it's the sole member of its segment AND long enough.
def _track(tid, parent, media):
    return {"MusicTrack": {"id": tid, "node_base": {"direct_parent_id": parent},
                           "sources": [{"media_id": media}]}}


def _segment(sid):
    return {"MusicSegment": {"id": sid, "music_params": {"node_base": {"direct_parent_id": 0}}}}


HIRC = [
    _segment(100), _track(10, 100, 1000),                 # standalone, 360s -> SONG
    _segment(200), _track(20, 200, 2000), _track(21, 200, 2001),   # 2 media share seg -> layered
    _segment(300), _track(30, 300, 3000),                 # standalone but only 60s -> too short
]
DURS = {"1000": 360.0, "2000": 360.0, "2001": 360.0, "3000": 60.0}


def test_select_full_songs_keeps_only_standalone_long():
    songs = select_full_songs(HIRC, DURS, min_dur=300.0)
    assert songs == {"1000"}                               # layered + short are excluded


def test_select_full_songs_floor_is_inclusive():
    assert "1000" in select_full_songs(HIRC, {"1000": 300.0}, min_dur=300.0)
    assert "1000" not in select_full_songs(HIRC, {"1000": 299.9}, min_dur=300.0)


def test_curation_merge_local_over_default(tmp_path, monkeypatch):
    default = tmp_path / "default.json"
    local = tmp_path / "local.json"
    default.write_text(json.dumps({"order": ["a", "b", "c"], "hidden": ["c"], "names": {"a": "Default A"}}))
    monkeypatch.setattr(music, "_curation_cache", {"mtime": None, "data": {}})
    monkeypatch.setattr(music, "_default_cache", {"mtime": None, "data": {}})

    # no local yet -> shipped default shows through
    eff = music.load_curation(path=str(local), default_path=str(default))
    assert eff["order"] == ["a", "b", "c"] and eff["hidden"] == ["c"] and eff["names"]["a"] == "Default A"

    # local rename + reorder + extra hide overlays the default (names merge, hidden unions)
    music.set_curation(order=["b", "a", "c"], hidden=["b"], names={"a": "My A"}, path=str(local))
    eff = music.load_curation(path=str(local), default_path=str(default))
    assert eff["order"] == ["b", "a", "c"]
    assert eff["names"]["a"] == "My A"                     # local wins
    assert set(eff["hidden"]) == {"b", "c"}                # union of default + local

    # blank name drops it back to the default-or-handle
    music.set_curation(names={"a": ""}, path=str(local))
    eff = music.load_curation(path=str(local), default_path=str(default))
    assert eff["names"].get("a") == "Default A"            # local override gone -> default resurfaces


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
