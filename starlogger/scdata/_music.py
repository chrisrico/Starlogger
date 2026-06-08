"""Game music: decode the Wwise soundtrack out of the p4k into playable Ogg files.

Every musical cue the game ships lives, as streamed Ogg-Vorbis WEM media, in ONE soundbank
-- ``Data\\Sounds\\wwise\\MUS_Music_Global.bnk`` (the region banks Stanton/Pyro/Nyx are just
playlist/switch logic that *reference* this shared media pool, so they carry ~no media of
their own). StarBreaker's ``wwise extract --decode`` resolves those streamed WEMs straight
from the p4k and writes one ``<wem_id>.ogg`` per track; ``wwise list`` gives us the id ->
duration table and ``wwise dump`` the HIRC playlist hierarchy.

The bank holds 1203 media, but they are adaptive *building blocks*: the same media is reused
across thousands of track-instances, and a ``MusicSegment`` (one cue) usually bundles several
co-length *stem layers* the game mixes live. Release banks carry only FNV-hashed ids, so there
are no track names. So we don't surface the raw pool -- we keep only the **full songs**: a
media that is the SOLE member of every segment it appears in (a standalone cue, not one stem of
a stack) AND runs at least ``FULL_SONG_MIN_DUR``. That distils the pool to ~33 long-form
instrumental pieces (see select_full_songs). Everything below is decoded then pruned so disk
holds only those.

The build runs niced in the background via ``catalogs.refresh_loop`` (once on first run, then on
a major game-version move) -- not on a button click.
"""

from __future__ import annotations

import glob
import json
import os
import threading
from collections import defaultdict

from ._p4k import _run, ensure_binary

# The single soundbank that holds the whole music media pool (backslashes: p4k-internal path).
MUSIC_BANK = "Data\\Sounds\\wwise\\MUS_Music_Global.bnk"

# A "full song" must run at least this long (seconds). Below it sit loops, stingers and the
# sparse sub-pieces; 5:00 is the floor the user picked after auditioning the standalone set.
FULL_SONG_MIN_DUR = 300.0


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


def _durations(p4k: str, sb: str) -> dict[str, float]:
    """``{wem_id(str): duration}`` from a (cheap, ~1s) ``wwise list`` of the music bank."""
    rows = _parse_wwise_list(_run(sb, p4k, ["wwise", "list", MUSIC_BANK], timeout=600))
    return {r["id"]: r["duration"] for r in rows}


def dump_music_hirc(p4k: str, sb: str) -> list:
    """The bank's HIRC object list (``wwise dump`` -> JSON). Each element is ``{TypeName: {...}}``
    for a MusicTrack / MusicSegment / MusicPlaylistContainer / MusicSwitchContainer / etc. Cheap
    (~seconds); feeds select_full_songs."""
    return json.loads(_run(sb, p4k, ["wwise", "dump", MUSIC_BANK], timeout=600))


def select_full_songs(hirc: list, durations: dict, min_dur: float = FULL_SONG_MIN_DUR) -> set[str]:
    """The set of WEM ids that are *full songs*: standalone (the sole media in every
    ``MusicSegment`` they belong to -- i.e. a complete cue, not one stem layer of a stack) AND at
    least ``min_dur`` seconds long. Pure / unit-testable against a parsed ``wwise dump``.

    Walks each ``MusicTrack`` up its ``direct_parent_id`` chain to the nearest ``MusicSegment``,
    so a media that shares any segment with other media (a layered cue) is excluded."""
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

    def parent(i):
        b = objs[i]
        if typ[i] == "MusicTrack":
            nb = b.get("node_base") or {}
        else:
            mp = b.get("music_params") or (b.get("trans_params") or {}).get("music_params") or {}
            nb = mp.get("node_base") or {}
        return nb.get("direct_parent_id")

    def nearest_segment(i):
        seen = set()
        while i in objs and i not in seen:
            seen.add(i)
            if typ.get(i) == "MusicSegment":
                return i
            i = parent(i)
        return None

    seg_media: dict = defaultdict(set)
    media_seg: dict = defaultdict(set)
    for i, b in objs.items():
        if typ[i] != "MusicTrack":
            continue
        sg = nearest_segment(parent(i))   # a track's parent is its segment
        if sg is None:
            continue
        for s in (b.get("sources") or []):
            m = s.get("media_id")
            if m:
                seg_media[sg].add(m)
                media_seg[m].add(sg)

    songs: set[str] = set()
    for m, segs in media_seg.items():
        dur = durations.get(str(m))
        if dur is None or dur < min_dur:
            continue
        if all(len(seg_media[s]) == 1 for s in segs):   # standalone in every segment
            songs.add(str(m))
    return songs


def scan_songs(p4k: str, sb: str | None = None, min_dur: float = FULL_SONG_MIN_DUR) -> set[str]:
    """The cheap (no-decode) half: list + dump the music bank and return the *full-song* WEM ids
    (same rule build_music_from_p4k keeps). Lets a game-update refresh detect new/changed songs
    before paying the full re-decode."""
    sb = sb or ensure_binary()
    durations = _durations(p4k, sb)
    return select_full_songs(dump_music_hirc(p4k, sb), durations, min_dur)


def build_music_from_p4k(p4k: str, out_dir: str, sb: str | None = None,
                         min_dur: float = FULL_SONG_MIN_DUR, progress=lambda done, total: None,
                         timeout: int = 3600) -> list[dict]:
    """Decode the music bank into ``out_dir``, keeping only the *full songs* (see
    select_full_songs), and return their manifest rows (``{id, file, duration, size}``, sorted
    longest-first).

    StarBreaker decodes the whole bank in one blocking pass, so we compute the keep-set up front
    and, in the progress poller, **delete every non-song ogg as it lands** -- peak disk stays near
    the ~0.4 GB final size instead of the ~2.8 GB whole-bank spike. ``progress(done, total)`` ticks
    off the kept-so-far count against the song total."""
    sb = sb or ensure_binary()
    durations = _durations(p4k, sb)
    hirc = dump_music_hirc(p4k, sb)   # dumped once; reused for the context-label pass below
    keep = select_full_songs(hirc, durations, min_dur)
    total = len(keep)

    os.makedirs(out_dir, exist_ok=True)
    # Clean slate so a re-extract can't leave orphaned oggs behind (and the progress count
    # starts from zero). The decode rewrites everything we keep.
    for f in glob.glob(os.path.join(out_dir, "*.ogg")):
        os.remove(f)

    # Poll the output dir alongside the blocking decode: drop anything not in the keep-set the
    # instant it appears (caps disk), and report how many songs we've kept so far.
    stop = threading.Event()

    def _reap() -> tuple[int, set]:
        kept = set()
        for f in glob.glob(os.path.join(out_dir, "*.ogg")):
            wid = os.path.basename(f)[:-4]
            if wid in keep:
                kept.add(wid)
            else:
                try:
                    os.remove(f)
                except OSError:
                    pass
        return len(kept), kept

    def _poll() -> None:
        while not stop.wait(1.0):
            n, _ = _reap()
            progress(n, total)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()
    try:
        _run(sb, p4k, ["wwise", "extract", "--decode", "-o", out_dir, MUSIC_BANK],
             timeout=timeout)
    finally:
        stop.set()
        poller.join(timeout=2)

    _reap()  # final sweep: drop any non-song ogg the poller didn't catch before the decode ended
    # The gameplay context each song plays under (region/mood/cue), mined from the switch
    # hierarchy in the same HIRC dump -- a readable hint where the FNV-hashed ids give none.
    from ._music_context import context_for_media
    context = context_for_media(p4k, sb, hirc)
    rows: list[dict] = []
    for f in glob.glob(os.path.join(out_dir, "*.ogg")):
        wid = os.path.basename(f)[:-4]
        if wid not in keep:
            os.remove(f)
            continue
        rows.append({"id": wid, "file": os.path.basename(f),
                     "duration": durations.get(wid), "size": os.path.getsize(f),
                     "context": context.get(wid) or ""})
    rows.sort(key=lambda r: -(r["duration"] or 0))
    progress(len(rows), total)
    return rows
