"""Manual flags on individual manual-trade transactions (trade_flags.json).

Currently one flag: ``lost`` — marks a buy whose cargo was destroyed/stolen before
it could be sold, so the trade-load view realises the unsold remainder as a loss
instead of leaving it sitting as "holding" forever.

Keyed by the trade's stable id (``ts|action|guid|shop`` — the same id State assigns;
the frontend reconstructs it from the trade fields). Mirrors the mtime-cached read /
atomic write conventions in overrides.py / settings.py, so edits apply live.
"""

from __future__ import annotations

from .config import TRADE_FLAGS_PATH
from .jsonstore import atomic_write, load_cached, read_json

_cache: dict = {"mtime": None, "data": {}}


def get_trade_flags(path: str = TRADE_FLAGS_PATH) -> dict:
    return load_cached(path, _cache)


def lost_trade_ids(path: str = TRADE_FLAGS_PATH) -> list:
    """Trade ids currently flagged lost."""
    return [tid for tid, f in get_trade_flags(path).items() if f.get("lost")]


def set_lost(trade_id: str, lost: bool, path: str = TRADE_FLAGS_PATH) -> None:
    """Flag (or unflag) a trade as lost. Removes the entry entirely when unset."""
    data = read_json(path, dict)
    if lost:
        data[trade_id] = {"lost": True}
    else:
        data.pop(trade_id, None)
    atomic_write(path, data)
    _cache["mtime"] = None  # force fresh read next get
