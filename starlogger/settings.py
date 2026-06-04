"""Tiny persistent key/value settings store (settings.json in DATA_DIR).

Currently holds `selected_ship` — the ship the user picked by hand, used to drive
the capacity gauge and cargo-grid view when the game log hasn't detected a ship
(a detected ship always wins; see snapshot.build_snapshot). Mirrors the
mtime-cached read / atomic write conventions in stations.py and overrides.py, so
changes are picked up live with no restart.
"""

from __future__ import annotations

from .config import SETTINGS_PATH
from .jsonstore import atomic_write, load_cached, read_json

_cache: dict = {"mtime": None, "data": {}}


def get_settings(path: str = SETTINGS_PATH) -> dict:
    return load_cached(path, _cache)


def set_setting(key: str, value, path: str = SETTINGS_PATH) -> None:
    """Set one key (or remove it when value is None/empty). Atomic write."""
    data = read_json(path, dict)
    if value is None or value == "":
        data.pop(key, None)
    else:
        data[key] = value
    atomic_write(path, data)
    _cache["mtime"] = None  # force a fresh read on next get_settings()
