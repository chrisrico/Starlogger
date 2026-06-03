"""Reconstruct a past session's dashboard state for scrub/replay.

A session summary (sessions.json) is far too compact to redraw the live dashboard,
but the raw log it was built from usually still lives in SC's ``logbackups/`` (or the
current ``Game.log``). We re-feed that log into a fresh :class:`State`, snapshotting
whenever the *visible* state changes, to build an ordered timeline of checkpoints the
UI can scrub through. Each checkpoint is a full :func:`build_snapshot`, cached so
dragging the slider is instant.

Public API:
    build_timeline(key, log_path) -> {"points": [...], "count": n} | None
    snapshot_at(key, log_path, i) -> snapshot dict | None

A session is identified by its archive key ``"<session_started_at>|<player>"`` (see
:func:`scmt.archive._session_key`). ``None`` is returned when no still-present log file
contains that session — the UI surfaces that as "original log no longer available".
"""

from __future__ import annotations

import os
import threading

from .commodities import load_commodities, resolve_commodity
from .config import find_log_backups
from .patterns import TS, decode_qt_dest
from .snapshot import build_snapshot
from .state import State

# key -> {"path", "mtime", "points": [...], "snapshots": [...]}; bounded LRU-ish.
_cache: dict[str, dict] = {}
_lock = threading.Lock()
_CACHE_MAX = 4  # keep a few replayed sessions hot; each holds all its snapshots


def _session_key(st: State) -> str:
    # Mirrors archive._session_key without importing it (avoids the heavier deps).
    return f"{st.session_started_at or '?'}|{st.player or '?'}"


def _first_ts(path: str) -> str | None:
    """The first timestamp in a log file (cheap: stops at the first dated line)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = TS.search(line)
                if m:
                    return m.group("ts")
    except OSError:
        return None
    return None


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _candidate_order(key: str, log_path: str | None) -> list[str]:
    """Log files to try for a session, best guess first. Logbackups are immutable
    rotations, so the file covering a session is the newest one whose first timestamp
    is at or before the session's start. We rank by first-ts and put that best guess
    first, then fall back to the rest (closest first) in case the guess misses."""
    files = list(find_log_backups(log_path)) if log_path else []
    if log_path and os.path.isfile(log_path):
        files.append(log_path)  # the current (newest) log
    seen: set[str] = set()
    ranked: list[tuple[str, str]] = []
    for f in files:
        if f in seen or not os.path.isfile(f):
            continue
        seen.add(f)
        ranked.append((_first_ts(f) or "", f))
    ranked.sort(key=lambda t: t[0])  # by first ts ascending (ISO sorts chronologically)
    started_at = key.split("|", 1)[0]
    best = None
    for fts, f in ranked:
        if fts and fts <= started_at:
            best = f
        elif fts and fts > started_at:
            break
    order = [best] if best else []
    order += [f for _, f in ranked if f != best]
    return order


def _facts(st: State, cmap: dict) -> dict:
    """The visible-state facts a checkpoint is keyed on, plus enough detail to label
    what changed. Excludes player location (it churns without affecting the dashboard)."""
    return {
        "missions": {
            mid: (m.status,
                  sum(1 for l in m.legs.values() if l.state == "completed"),
                  m.title or m.contract or "")
            for mid, m in st.missions.items()
        },
        "trades": {
            tid: (t.action, t.scu, resolve_commodity(t.commodity_guid, cmap), t.place or "")
            for tid, t in st.trades.items()
        },
        "awarded": st.total_awarded,
        "ship": st.ship,
        "routes": len(st.travel_routes),
        "arrivals": len(st.travel_arrivals),
    }


def _sig(facts: dict) -> tuple:
    """Change-detection signature: a checkpoint is recorded whenever this changes."""
    return (
        tuple(sorted(facts["missions"].items())),
        tuple(sorted(facts["trades"].items())),
        facts["awarded"], facts["ship"], facts["routes"], facts["arrivals"],
    )


_END_VERB = {"completed": "Completed", "failed": "Failed",
             "abandoned": "Abandoned", "expired": "Expired"}


def _label(prev: dict | None, cur: dict, st: State) -> str:
    """A short human label for what changed between two checkpoints."""
    if prev is None:
        return "Session start"
    # a newly accepted contract
    for mid, (status, done, title) in cur["missions"].items():
        if mid not in prev["missions"]:
            return f"Accepted · {title}" if title else "Accepted contract"
    # a contract reaching a terminal state
    for mid, (status, done, title) in cur["missions"].items():
        pstatus = prev["missions"].get(mid, (None,))[0]
        if status != pstatus and status in _END_VERB:
            return f"{_END_VERB[status]} · {title}" if title else f"{_END_VERB[status]} contract"
    # a delivery leg completed
    for mid, (status, done, title) in cur["missions"].items():
        if done > prev["missions"].get(mid, (None, 0))[1]:
            return f"Delivered · {title}" if title else "Delivered cargo"
    # a manual buy/sell
    for tid, (action, scu, name, place) in cur["trades"].items():
        if tid not in prev["trades"]:
            verb = "Bought" if action == "buy" else "Sold"
            at = f" @ {place}" if place else ""
            return f"{verb} {scu} SCU {name}{at}".strip()
    # boarded a different ship
    if cur["ship"] and cur["ship"] != prev["ship"]:
        return f"Boarded {cur['ship']}"
    # quantum travel (route calc / arrival)
    if cur["routes"] > prev["routes"]:
        last = st.travel_routes[-1] if st.travel_routes else None
        dest = decode_qt_dest(last["to"]) if last else ""
        return f"Quantum travel → {dest}".rstrip(" →") if dest else "Quantum travel"
    if cur["arrivals"] > prev["arrivals"]:
        return "Arrived"
    if cur["awarded"] != prev["awarded"]:
        return f"+{cur['awarded'] - prev['awarded']:,} aUEC"
    active = sum(1 for s, *_ in cur["missions"].values() if s == "active")
    return f"{active} active contract{'' if active == 1 else 's'}"


def _build(key: str, path: str) -> tuple[list, list]:
    """Replay one log file, capturing a checkpoint each time the target session's
    visible state changes. Earlier/other sessions in the file are fed (so state stays
    correct) but only the target session's lines are snapshotted."""
    cmap = load_commodities()
    st = State()  # no on_session_end hook -> session resets just clear, no archiving
    points: list[dict] = []
    snapshots: list[dict] = []
    prev_facts: dict | None = None
    prev_sig: tuple | None = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                st.feed(line)
                if _session_key(st) != key:
                    prev_facts, prev_sig = None, None  # outside the target session
                    continue
                facts = _facts(st, cmap)
                sig = _sig(facts)
                if sig == prev_sig:
                    continue
                label = _label(prev_facts, facts, st)
                snap = build_snapshot(st)
                # how "populated" the cargo-ops dashboard is at this checkpoint — the UI
                # defaults the scrubber here (its busiest moment) so replay lands on a
                # visibly-filled dashboard rather than the usually-empty session end.
                fill = (len(snap.get("missions") or []) + len(snap.get("loading") or [])
                        + len(snap.get("unloading") or []))
                # Collapse a burst of same-ts, same-label checkpoints into one (the
                # login establisher flaps the session in/out, re-emitting "Session
                # start"): keep the latest snapshot for that instant.
                if points and points[-1]["ts"] == st.last_event_ts and points[-1]["label"] == label:
                    snapshots[-1] = snap
                    points[-1]["fill"] = fill
                else:
                    points.append({"i": len(points), "ts": st.last_event_ts,
                                   "label": label, "fill": fill})
                    snapshots.append(snap)
                prev_facts, prev_sig = facts, sig
    except OSError:
        return [], []
    return points, snapshots


def _entry(key: str, log_path: str | None) -> dict | None:
    """The cached (or freshly built) timeline for a session key, or None if no
    present log file contains it."""
    with _lock:
        c = _cache.get(key)
    if c and os.path.isfile(c["path"]) and _mtime(c["path"]) == c["mtime"]:
        return c
    for path in _candidate_order(key, log_path):
        points, snapshots = _build(key, path)
        if points:
            entry = {"path": path, "mtime": _mtime(path),
                     "points": points, "snapshots": snapshots}
            with _lock:
                _cache[key] = entry
                while len(_cache) > _CACHE_MAX:
                    _cache.pop(next(iter(_cache)))  # evict oldest insertion
            return entry
    return None


def build_timeline(key: str, log_path: str | None) -> dict | None:
    """Lightweight timeline for the scrub UI: ordered checkpoints (index, ts, label).
    None when the session's source log is no longer available."""
    e = _entry(key, log_path)
    if not e:
        return None
    return {"points": e["points"], "count": len(e["points"])}


def snapshot_at(key: str, log_path: str | None, i: int) -> dict | None:
    """The full dashboard snapshot for checkpoint ``i`` (clamped). None when the
    session's source log is no longer available."""
    e = _entry(key, log_path)
    if not e or not e["snapshots"]:
        return None
    i = max(0, min(int(i), len(e["snapshots"]) - 1))
    return e["snapshots"][i]
