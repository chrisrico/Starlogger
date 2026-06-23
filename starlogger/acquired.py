"""Crafting blueprints the player has ACQUIRED, parsed from the game log.

When the player receives a blueprint the game emits a HUD notification ("Received
Blueprint: <name>: "); ``state._blueprint`` collects those per session and
``archive.archive_session`` folds each ended session's haul into ``acquired_blueprints.json``
here. The file is a **cumulative union** -- ownership is permanent, so unlike the per-session
``sessions.json`` archive it only ever grows and never erodes. Keyed by the normalized log
name -> ``{name, acquired_at}`` (earliest sighting). Merges are idempotent, so the backfill
re-parsing a logbackup (e.g. on an ARCHIVE_SCHEMA bump) can't corrupt or inflate it.

The catalog (``blueprints.py``) names base items ("Surveyor-Max"); the log sometimes prefixes
a graded variant ("Ind/3/C Surveyor-Max"). ``resolve_owned`` reconciles the two at READ time
(exact, else grade-prefix-stripped), so the stored file stays a faithful record of what the
log said and the matching can improve without a rewrite.
"""

from __future__ import annotations

import threading

from .config import ACQUIRED_BLUEPRINTS_PATH
from .jsonstore import atomic_write, load_cached, read_json
from .patterns import norm_bp_name, strip_bp_grade

_cache = {"mtime": None, "data": {}}
_write_lock = threading.Lock()  # serialize acquired_blueprints.json writers (live + backfill)


def load_acquired(path: str = ACQUIRED_BLUEPRINTS_PATH) -> dict:
    """The cumulative owned-blueprint record ``{norm_name: {name, acquired_at}}``; ``{}`` until
    the first acquisition is parsed. mtime-cached like the other small stores."""
    return load_cached(path, _cache, lambda raw: raw if isinstance(raw, dict) else {})


def merge_acquired(session_bps: dict, path: str = ACQUIRED_BLUEPRINTS_PATH) -> None:
    """Union one session's parsed acquisitions (``state.blueprints``: ``{norm: {name,
    acquired_at}}``) into the cumulative file, keeping the earliest ``acquired_at`` per name.
    Idempotent: re-merging the same data writes nothing. No-op for an empty haul."""
    if not session_bps:
        return
    with _write_lock:
        cur = read_json(path)
        cur = cur if isinstance(cur, dict) else {}
        changed = False
        for key, rec in session_bps.items():
            ts = rec.get("acquired_at")
            ex = cur.get(key)
            if ex is None:
                cur[key] = {"name": rec.get("name") or key, "acquired_at": ts}
                changed = True
            elif ts and (not ex.get("acquired_at") or ts < ex["acquired_at"]):
                ex["acquired_at"] = ts
                changed = True
        if changed:
            atomic_write(path, cur)
    if changed:
        _cache["mtime"] = None  # force a reload on next read


def acquired_index(state=None, path: str = ACQUIRED_BLUEPRINTS_PATH) -> dict:
    """``{norm_name: acquired_at}`` from the persisted file unioned with the live (current,
    not-yet-archived) session's ``state.blueprints`` -- so a blueprint received this session
    reads as acquired immediately. Earliest timestamp wins."""
    out = {k: (v or {}).get("acquired_at") for k, v in load_acquired(path).items()}
    if state is not None:
        with state.lock:
            for k, rec in getattr(state, "blueprints", {}).items():
                ts = (rec or {}).get("acquired_at")
                if k not in out or (ts and (not out[k] or ts < out[k])):
                    out[k] = ts
    return out


def resolve_owned(index: dict, catalog_norm_names) -> dict:
    """Map acquired (normalized) log names onto the catalog names they own, returning
    ``{catalog_norm: acquired_at}``. Tries the name as-is, then with a leading grade-code
    prefix stripped ("ind/3/c surveyor-max" -> "surveyor-max"). Names that resolve to no
    catalog entry (e.g. "s00 hofstede", whose only catalog match is the ambiguous
    "Hofstede-S1/S2 Mining Laser") are simply left out -- still recorded in the file, just
    not decorating a row."""
    cat = set(catalog_norm_names)
    owned: dict = {}
    for raw, ts in (index or {}).items():
        key = raw if raw in cat else norm_bp_name(strip_bp_grade(raw))
        if key not in cat:
            continue
        cur = owned.get(key, ...)
        if cur is ... or (ts and (cur is None or ts < cur)):
            owned[key] = ts
    return owned
