"""Archive of past play sessions.

When a session ends (logout / relaunch), `archive_session` snapshots a compact
summary to sessions.json. Entries are keyed by start time + player so replaying
the same log (e.g. on tracker restart) updates rather than duplicates them.
"""

from __future__ import annotations

import json
import os
import threading
import time

from .config import SESSIONS_KEEP, SESSIONS_PATH
from .overrides import apply_override, get_overrides
from .patterns import canonical_ship_name
from .state import State

_cache = {"mtime": None, "data": []}
_write_lock = threading.Lock()  # serialize sessions.json writers (live tailer + backfill)


def _session_key(state: State) -> str:
    return f"{state.session_started_at or '?'}|{state.player or '?'}"


def build_summary(state: State) -> dict:
    overrides = get_overrides()
    missions = []
    counts = {"completed": 0, "abandoned": 0, "failed": 0, "unfinished": 0, "total": 0}
    for m in state.missions.values():
        ov = overrides.get(m.mission_id)
        if ov and ov.get("hidden"):
            continue
        if ov:
            m = apply_override(m, ov)
        counts["total"] += 1
        # a mission still "active" when the session ended was never finished --
        # archive it as a distinct "unfinished" state.
        status = "unfinished" if m.status == "active" else m.status
        bucket = status if status in counts else ("failed" if status == "expired" else None)
        if bucket:
            counts[bucket] += 1
        drops = [l for l in m.legs.values() if l.kind == "dropoff"]
        missions.append({
            "title": m.title or m.contract,
            "status": status,
            "reward": m.reward,
            "cargo": m.cargo_types,
            "is_trade": m.is_trade,
            "origin": m.origin_name,
            "destinations": sorted({l.location for l in drops if l.location}),
        })
    missions.sort(key=lambda x: (x["status"] != "completed", x["title"]))
    return {
        "key": _session_key(state),
        "started_at": state.session_started_at,
        "ended_at": state.last_event_ts,
        "game_version": state.game_version,
        "player": state.player,
        "ships": sorted({canonical_ship_name(s) for s in state.ships_used}),
        "earned": state.total_awarded,
        "counts": counts,
        "missions": missions,
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def filter_sessions(sessions: list, trade_only: bool = False, show_unfinished: bool = True) -> list:
    """Filter each archived session's missions (trade-only and/or hiding unfinished),
    dropping sessions left with none, and recomputing their counts + earned."""
    if not trade_only and show_unfinished:
        return sessions
    out = []
    for s in sessions:
        ms = s.get("missions", [])
        if trade_only:
            ms = [m for m in ms if m.get("is_trade")]
        if not show_unfinished:
            ms = [m for m in ms if m.get("status") != "unfinished"]
        if not ms:
            continue
        counts = {"completed": 0, "abandoned": 0, "failed": 0, "unfinished": 0, "total": len(ms)}
        for m in ms:
            b = m["status"] if m["status"] in counts else ("failed" if m["status"] == "expired" else None)
            if b:
                counts[b] += 1
        earned = sum(m["reward"] or 0 for m in ms if m.get("reward"))
        out.append({**s, "missions": ms, "counts": counts, "earned": earned})
    return out


def load_sessions(path: str = SESSIONS_PATH) -> list:
    try:
        mt = os.stat(path).st_mtime
    except FileNotFoundError:
        return []
    if _cache["mtime"] != mt:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # normalize ship names so sessions archived before the canonical-name
            # fix don't show both "Crusader Mercury Star Runner" and "Mercury Star
            # Runner" (display-only; the stored file is left untouched).
            for s in data:
                if s.get("ships"):
                    s["ships"] = sorted({canonical_ship_name(x) for x in s["ships"]})
            _cache["data"] = data
            _cache["mtime"] = mt
        except (OSError, json.JSONDecodeError):
            pass
    return _cache["data"]


def archive_session(state: State, path: str = SESSIONS_PATH) -> None:
    summary = build_summary(state)
    with _write_lock:  # read-modify-write is atomic vs. other archiving threads
        sessions = []
        try:
            with open(path, encoding="utf-8") as f:
                sessions = json.load(f)
        except (OSError, json.JSONDecodeError):
            sessions = []
        # replace an existing entry with the same key, else append
        sessions = [s for s in sessions if s.get("key") != summary["key"]]
        sessions.append(summary)
        sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
        sessions = sessions[:SESSIONS_KEEP]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2)
        os.replace(tmp, path)
    _cache["mtime"] = None  # force reload on next read
