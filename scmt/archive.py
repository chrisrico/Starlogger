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

from .commodities import load_commodities, resolve_commodity
from .config import SESSIONS_KEEP, SESSIONS_PATH
from .overrides import apply_override, get_overrides
from .patterns import canonical_ship_name, decode_qt_dest, friendly_ship, qt_system
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
            "accepted_at": m.accepted_at,
            "ended_at": m.ended_at,
        })
    # sort by when it happened (ended, else accepted) so the archive's mission order
    # is chronological; the frontend re-sorts the pooled contract log the same way.
    missions.sort(key=lambda x: x.get("ended_at") or x.get("accepted_at") or "")
    trades, trade_totals = build_session_trades(state)
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
        "trades": trades,
        "trade_totals": trade_totals,
        "travels": build_session_travels(state),
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_session_travels(state: State) -> list:
    """Pair quantum route calcs with their arrivals into completed jumps. Collapses
    recalculations (same ship+from+to in a row) and matches each route to the first
    arrival of that ship before the next route — so it survives the game logging a
    route several times. `arrived` is None for a jump with no logged arrival yet."""
    routes = sorted(state.travel_routes, key=lambda r: r["ts"] or "")
    arrivals = sorted(state.travel_arrivals, key=lambda a: a["ts"] or "")
    deduped: list[dict] = []
    for r in routes:
        prev = deduped[-1] if deduped else None
        if prev and (prev["ship"], prev["frm"], prev["to"]) == (r["ship"], r["frm"], r["to"]):
            continue
        deduped.append(r)
    out = []
    for i, r in enumerate(deduped):
        nxt = next((x["ts"] for x in deduped[i + 1:] if x["ship"] == r["ship"]), None)
        arr = next((a["ts"] for a in arrivals
                    if a["ship"] == r["ship"] and (a["ts"] or "") > (r["ts"] or "")
                    and (nxt is None or (a["ts"] or "") < nxt)), None)
        out.append({
            "ts": r["ts"],
            "ship": friendly_ship(r["ship"]),
            "from": r["frm"],
            "to": decode_qt_dest(r["to"]),
            "to_code": r["to"],
            "system": qt_system(r["to"]),
            "fuel": r.get("fuel"),
            "arrived": arr,
        })
    return out


def build_session_trades(state: State) -> tuple[list, dict]:
    """Serialize a session's manual terminal trades + a spent/earned/net rollup,
    resolving each commodity GUID to a name via the local commodities map."""
    cmap = load_commodities()
    trades = []
    totals = {"spent": 0, "earned": 0, "net": 0, "buy_scu": 0, "sell_scu": 0, "count": 0}
    for t in sorted(state.trades.values(), key=lambda t: t.ts or ""):
        trades.append({
            "action": t.action,
            "commodity": resolve_commodity(t.commodity_guid, cmap),
            "commodity_guid": t.commodity_guid,
            "scu": t.scu,
            "auec": t.auec,
            "unit_price": t.unit_price,
            "shop": t.place,          # station-preferred ("Cordys" over "Admin")
            "shop_label": t.shop_label,
            "station": t.station,
            "shop_raw": t.shop,
            "ts": t.ts,
        })
        totals["count"] += 1
        if t.action == "buy":
            totals["spent"] += t.auec
            totals["buy_scu"] += t.scu
        else:
            totals["earned"] += t.auec
            totals["sell_scu"] += t.scu
    totals["net"] = totals["earned"] - totals["spent"]
    return trades, totals


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
        if not ms and not s.get("trades"):
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
            cmap = load_commodities()
            for s in data:
                if s.get("ships"):
                    s["ships"] = sorted({canonical_ship_name(x) for x in s["ships"]})
                # Re-resolve trade commodity names from their stored GUID against the
                # current map, so trades archived before commodities.json existed (or
                # built later) self-heal from "Commodity xxxxxxxx" to a real name on
                # read -- no archive rebuild needed. Only overrides on a known GUID.
                for t in s.get("trades") or []:
                    name = cmap.get((t.get("commodity_guid") or "").lower())
                    if name:
                        t["commodity"] = name
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
