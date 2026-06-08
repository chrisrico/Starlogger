"""Shared JSON-store primitives.

Every small per-user store in this package (overrides, settings, trade flags,
station names, the session archive) and the p4k-derived reference cache repeat the
same two moves: an mtime-cached read and an atomic temp-file write. They live here
once -- one copy of the ``tmp`` + ``os.replace`` dance (so a reader never sees a
half-written file) and one ``(OSError, json.JSONDecodeError)`` guard (so a missing
or corrupt file degrades to a default instead of crashing a reader).
"""

from __future__ import annotations

import json
import os


def read_json(path: str, default=None):
    """Parse `path`, returning `default` if it's missing or unreadable/corrupt.
    `default` may be a zero-arg factory (e.g. `dict`) when a fresh mutable is needed."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default() if callable(default) else default


def atomic_write(path: str, data, *, sort_keys: bool = True) -> None:
    """Write `data` as indented JSON via a temp file + `os.replace`, so a concurrent
    reader always sees either the old file or the complete new one, never a partial.
    Locked by tests/test_jsonstore.py (roundtrip + a failed write leaves the prior file intact)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=sort_keys)
    os.replace(tmp, path)


def load_cached(path: str, cache: dict, parse=None):
    """mtime-cached read into `cache` (a dict with "mtime" and "data" slots).

    Re-reads only when the file's mtime changes; `parse(raw)` transforms the loaded
    JSON before caching (identity by default). A missing file or a corrupt/partial
    read leaves the cached value untouched. Returns `cache["data"]`.
    """
    try:
        mt = os.stat(path).st_mtime
    except FileNotFoundError:
        return cache["data"]
    if cache["mtime"] != mt:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return cache["data"]
        cache["data"] = parse(raw) if parse else raw
        cache["mtime"] = mt
    return cache["data"]
