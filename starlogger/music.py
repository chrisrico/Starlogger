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

from .config import (DEFAULT_MUSIC_CURATION_PATH, MUSIC_CURATION_PATH, MUSIC_DIR, MUSIC_PATH)
from . import scdata
from .jsonstore import atomic_write, load_cached

# Manifest-schema version: bump when the music.json shape changes, so an install re-extracts
# (or at least re-reads) on update. Mirrors contracts.EXTRACT_VERSION.
EXTRACT_VERSION = 2

_cache = {"mtime": None,
          "data": {"tracks": [], "count": 0, "game_version": None}}
# Separate mtime caches for the two curation files (shipped default + local overlay).
_curation_cache = {"mtime": None, "data": {}}
_default_cache = {"mtime": None, "data": {}}


def save_music(tracks: list, game_version: str | None = None, min_duration: float = 300.0,
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


def music_version(path: str = MUSIC_PATH) -> str | None:
    """Game version the music was extracted for -- gates the auto-refresh on a major bump."""
    return (load_music(path) or {}).get("game_version")


def music_extract_version(path: str = MUSIC_PATH) -> int:
    """Manifest-schema version on disk (0 == absent / pre-stamp)."""
    return int((load_music(path) or {}).get("extract_version") or 0)


def track_ids(path: str = MUSIC_PATH) -> set:
    """The set of extracted WEM ids -- compared against a fresh scan to spot new/changed music."""
    return {t.get("id") for t in (load_music(path) or {}).get("tracks", [])}


def restamp_version(game_version: str | None, path: str = MUSIC_PATH) -> None:
    """Re-save the existing manifest under a new ``game_version`` (no decode). Used when a game
    update brought no new music, so the extraction is marked current for the new build."""
    d = load_music(path)
    save_music(d.get("tracks", []), game_version=game_version,
               min_duration=d.get("min_duration", scdata.FULL_SONG_MIN_DUR), path=path)


# --------------------------------------------------------------------------- #
# Jukebox curation: playlist order + skipped tracks + custom names.
#
# The data has NO real track names (FNV-hashed ids) and the songs can't be told apart by ear
# without playing them, so the user curates: reorder, skip duds, rename keepers. Their edits
# live in the LOCAL sidecar (MUSIC_CURATION_PATH); a shipped DEFAULT (committed in the package)
# seeds a fresh install. load_curation() overlays local on top of default. Everything is keyed
# by stable WEM id, so curation survives a re-extract (orphaned ids are simply kept).
# --------------------------------------------------------------------------- #
_EMPTY_CURATION = {"order": [], "skipped": [], "names": {}}


def _read_curation(path: str, cache: dict) -> dict:
    d = load_cached(path, cache) or cache["data"] or {}
    return {"order": list(d.get("order") or []),
            "skipped": list(d.get("skipped") or []),
            "names": dict(d.get("names") or {})}


def load_curation(path: str = MUSIC_CURATION_PATH,
                  default_path: str = DEFAULT_MUSIC_CURATION_PATH) -> dict:
    """The effective curation: the local sidecar overlaid on the shipped default. ``names`` merge
    (local wins per id); ``order`` is the local order if set else the default; ``skipped`` is the
    union. Ids are strings (WEM ids)."""
    base = _read_curation(default_path, _default_cache)
    over = _read_curation(path, _curation_cache)
    names = {**base["names"], **over["names"]}
    order = over["order"] or base["order"]
    skipped = list(dict.fromkeys([*base["skipped"], *over["skipped"]]))
    return {"order": order, "skipped": skipped, "names": names}


def set_curation(*, order: list | None = None, skipped: list | None = None,
                 names: dict | None = None, path: str = MUSIC_CURATION_PATH) -> dict:
    """Merge a partial curation update into the LOCAL sidecar and persist it (atomic). Only the
    provided fields change; ``names`` merges key-by-key (a value of "" or None drops that name).
    Returns the new effective curation."""
    cur = _read_curation(path, _curation_cache)
    if order is not None:
        cur["order"] = [str(x) for x in order]
    if skipped is not None:
        cur["skipped"] = [str(x) for x in skipped]
    if names is not None:
        merged = dict(cur["names"])
        for k, v in names.items():
            if v:
                merged[str(k)] = str(v)
            else:
                merged.pop(str(k), None)
        cur["names"] = merged
    atomic_write(path, cur)
    _curation_cache["mtime"] = None   # force re-read on next load
    return load_curation(path)


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
