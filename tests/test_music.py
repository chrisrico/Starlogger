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

from starlogger import music
from starlogger.scdata._music import _parse_wwise_list

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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
