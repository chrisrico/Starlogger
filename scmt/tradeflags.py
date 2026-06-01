"""Manual flags on individual manual-trade transactions (trade_flags.json).

Currently one flag: ``lost`` — marks a buy whose cargo was destroyed/stolen before
it could be sold, so the trade-load view realises the unsold remainder as a loss
instead of leaving it sitting as "holding" forever.

Keyed by the trade's stable id (``ts|action|guid|shop`` — the same id State assigns;
the frontend reconstructs it from the trade fields). Mirrors the mtime-cached read /
atomic write conventions in overrides.py / settings.py, so edits apply live.
"""

from __future__ import annotations

import json
import os

from .config import TRADE_FLAGS_PATH

_cache: dict = {"mtime": None, "data": {}}


def get_trade_flags(path: str = TRADE_FLAGS_PATH) -> dict:
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


def lost_trade_ids(path: str = TRADE_FLAGS_PATH) -> list:
    """Trade ids currently flagged lost."""
    return [tid for tid, f in get_trade_flags(path).items() if f.get("lost")]


def set_lost(trade_id: str, lost: bool, path: str = TRADE_FLAGS_PATH) -> None:
    """Flag (or unflag) a trade as lost. Removes the entry entirely when unset."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if lost:
        data[trade_id] = {"lost": True}
    else:
        data.pop(trade_id, None)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    _cache["mtime"] = None  # force fresh read next get
