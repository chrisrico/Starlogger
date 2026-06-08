"""Game music: decode the Wwise soundtrack out of the p4k into playable Ogg files.

Every musical cue the game ships lives, as streamed Ogg-Vorbis WEM media, in ONE soundbank
-- ``Data\\Sounds\\wwise\\MUS_Music_Global.bnk`` (the region banks Stanton/Pyro/Nyx are just
playlist/switch logic that *reference* this shared media pool, so they carry ~no media of
their own). StarBreaker's ``wwise extract --decode`` resolves those streamed WEMs straight
from the p4k and writes one ``<wem_id>.ogg`` per track; ``wwise list`` gives us the id ->
duration table up front (release banks carry only FNV-hashed ids, so duration + id is all the
label we get -- there are no track names).

Unlike the DataCore catalogs this is NOT version-gated background work: it's ~2.6 GB and
runs once, on an explicit user click (see tracker.MusicState / ON_EXTRACT_MUSIC), decoding
straight into the persistent MUSIC_DIR.
"""

from __future__ import annotations

import glob
import os
import threading

from ._p4k import _run, ensure_binary

# The single soundbank that holds the whole music media pool (backslashes: p4k-internal path).
MUSIC_BANK = "Data\\Sounds\\wwise\\MUS_Music_Global.bnk"


def _parse_wwise_list(stdout: str) -> list[dict]:
    """Parse ``wwise list`` output into ``[{id, size, codec, duration}]`` (pure / testable).

    Columns are ``WEM ID | Source | Offset | Size | Codec | Duration`` -- Offset is ``-`` for
    streamed media, a number when embedded; either way the row has six whitespace fields and
    a numeric leading id, which the header (``WEM ID``) and the ``----`` rule never do."""
    rows: list[dict] = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        try:
            dur = float(parts[5].rstrip("s"))
        except ValueError:
            continue
        rows.append({
            "id": parts[0],
            "size": int(parts[3]) if parts[3].isdigit() else None,
            "codec": parts[4],
            "duration": dur,
        })
    return rows


def scan_music(p4k: str, sb: str | None = None, min_dur: float = 30.0) -> list[dict]:
    """The cheap (~1s) half: list the music bank's tracks WITHOUT decoding, returning the
    ``{id, duration}`` rows that pass the ``min_dur`` floor (matching what ``build_music_from_p4k``
    would keep). Lets a game-update refresh detect new/changed tracks before paying the full
    re-decode. Keeps unknown-duration rows (same as the build's prune rule)."""
    sb = sb or ensure_binary()
    rows = _parse_wwise_list(_run(sb, p4k, ["wwise", "list", MUSIC_BANK], timeout=600))
    return [{"id": r["id"], "duration": r["duration"]} for r in rows
            if r["duration"] is None or r["duration"] >= min_dur]


def build_music_from_p4k(p4k: str, out_dir: str, sb: str | None = None,
                         min_dur: float = 30.0, progress=lambda done, total: None,
                         timeout: int = 3600) -> list[dict]:
    """Decode the music bank into ``out_dir`` and return the kept-track manifest rows
    (``{id, file, duration, size}``, sorted longest-first). Tracks shorter than ``min_dur``
    seconds (UI blips / short stingers) are pruned after the decode so disk matches the
    manifest. ``progress(done, total)`` is polled off the decoded-file count while the single
    blocking StarBreaker subprocess runs (it has no incremental output of its own)."""
    sb = sb or ensure_binary()
    listing = _parse_wwise_list(_run(sb, p4k, ["wwise", "list", MUSIC_BANK], timeout=600))
    by_id = {r["id"]: r for r in listing}
    total = len(listing)

    os.makedirs(out_dir, exist_ok=True)
    # Clean slate so a re-extract can't leave orphaned oggs behind (and the progress count
    # starts from zero). The decode rewrites everything we want.
    for f in glob.glob(os.path.join(out_dir, "*.ogg")):
        os.remove(f)

    # Poll the output dir for decoded-file count -> progress, alongside the blocking decode.
    stop = threading.Event()

    def _poll() -> None:
        while not stop.wait(1.0):
            progress(len(glob.glob(os.path.join(out_dir, "*.ogg"))), total)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()
    try:
        _run(sb, p4k, ["wwise", "extract", "--decode", "-o", out_dir, MUSIC_BANK],
             timeout=timeout)
    finally:
        stop.set()
        poller.join(timeout=2)

    rows: list[dict] = []
    for f in glob.glob(os.path.join(out_dir, "*.ogg")):
        wid = os.path.basename(f)[:-4]
        dur = (by_id.get(wid) or {}).get("duration")
        if dur is not None and dur < min_dur:
            os.remove(f)  # below the floor -> drop the file so disk == manifest
            continue
        rows.append({"id": wid, "file": os.path.basename(f),
                     "duration": dur, "size": os.path.getsize(f)})
    rows.sort(key=lambda r: -(r["duration"] or 0))
    progress(len(rows), total)
    return rows
