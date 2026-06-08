"""Local cache + serve layer for the decoded game music (music.json + the music/ oggs).

The heavy lifting (decoding the soundtrack out of the p4k) is in ``scdata._music``; this is
the thin per-user side: persist the track manifest, read it back mtime-cached, and answer
"is the music already extracted for this build?" so a re-click is a fast no-op. Mirrors
``contracts.py`` (same atomic-write / load_cached idiom). The oggs themselves are served
straight off disk by ``server.py`` (``/music/<id>.ogg``) -- this module only owns the JSON.
"""

from __future__ import annotations

import glob
import os
import time

from .config import MUSIC_DIR, MUSIC_PATH
from . import scdata
from .jsonstore import atomic_write, load_cached

# Manifest-schema version: bump when the music.json shape changes, so an install re-extracts
# (or at least re-reads) on update. Mirrors contracts.EXTRACT_VERSION.
EXTRACT_VERSION = 1

_cache = {"mtime": None,
          "data": {"tracks": [], "count": 0, "game_version": None}}


def save_music(tracks: list, game_version: str | None = None, min_duration: float = 30.0,
               path: str = MUSIC_PATH) -> None:
    atomic_write(path, {
        "source": f"Star Citizen Data.p4k via StarBreaker {scdata.SB_VERSION}",
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_version": game_version,
        "extract_version": EXTRACT_VERSION,
        "min_duration": min_duration,
        "count": len(tracks),
        "tracks": tracks,
    })


def load_music(path: str = MUSIC_PATH) -> dict:
    """The full manifest dict (``{tracks, count, ...}``); empty-ish until extracted."""
    return load_cached(path, _cache) or _cache["data"]


def is_extracted(game_version: str | None = None, path: str = MUSIC_PATH,
                 music_dir: str = MUSIC_DIR) -> bool:
    """True when the music is already decoded for this build: manifest present at the current
    schema, matching the live game version (when known), and the on-disk ogg count covers what
    the manifest claims. Lets a re-click skip the multi-minute, ~2.6 GB re-decode."""
    d = load_music(path)
    count = d.get("count") or 0
    if count <= 0 or d.get("extract_version") != EXTRACT_VERSION:
        return False
    if game_version and d.get("game_version") and d.get("game_version") != game_version:
        return False
    return len(glob.glob(os.path.join(music_dir, "*.ogg"))) >= count
