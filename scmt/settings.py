"""Tiny persistent key/value settings store (settings.json in DATA_DIR).

Currently holds `selected_ship` — the ship the user picked by hand, used to drive
the capacity gauge and cargo-grid view when the game log hasn't detected a ship
(a detected ship always wins; see snapshot.build_snapshot). Mirrors the
mtime-cached read / atomic write conventions in stations.py and overrides.py, so
changes are picked up live with no restart.
"""

from __future__ import annotations

import json
import os

from .config import SETTINGS_PATH

_cache: dict = {"mtime": None, "data": {}}


def get_settings(path: str = SETTINGS_PATH) -> dict:
    try:
        mtime = os.stat(path).st_mtime
    except FileNotFoundError:
        return {}
    if _cache["mtime"] != mtime:
        try:
            with open(path, encoding="utf-8") as f:
                _cache["data"] = json.load(f)
            _cache["mtime"] = mtime
        except (OSError, json.JSONDecodeError):
            pass
    return _cache["data"]


def set_setting(key: str, value, path: str = SETTINGS_PATH) -> None:
    """Set one key (or remove it when value is None/empty). Atomic write."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if value is None or value == "":
        data.pop(key, None)
    else:
        data[key] = value
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    _cache["mtime"] = None  # force a fresh read on next get_settings()
