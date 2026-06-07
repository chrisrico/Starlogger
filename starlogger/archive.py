"""Archive of past play sessions.

When a session ends (logout / relaunch), `archive_session` snapshots a compact
summary to sessions.json. Entries are keyed by start time + player so replaying
the same log (e.g. on tracker restart) updates rather than duplicates them.
"""

from __future__ import annotations

import threading
import time

from .config import SESSIONS_KEEP, SESSIONS_PATH
from .jsonstore import atomic_write, load_cached, read_json
from .overrides import apply_override, get_overrides
from .patterns import (
    canonical_ship_name,
    classify_contract,
    decode_qt_dest,
    friendly_ship,
    qt_system,
)
from .reference import load_commodities, resolve_commodity
from .state import State

_cache = {"mtime": None, "data": []}
_write_lock = threading.Lock()  # serialize sessions.json writers (live tailer + backfill)

# Bump when build_summary() gains a field that existing archives should acquire. The
# backfill records this version per processed logbackup (the `backfill` map in
# sessions.json) and re-parses any backup stamped with an older schema, refreshing its
# session(s) — so a deploy that adds a summary field still self-heals history.
# 2: per-mission `type` now prefers the authoritative ContractTemplate class (+ `icon`),
#    replacing the keyword heuristic where a template matches.
# 3: `type`/`icon` now also resolve named/scripted ContractGenerator missions (guild & story
#    contracts) via their debugName -- see contracts.decode / scdata.build_contract_generators.
ARCHIVE_SCHEMA = 3


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
            # Authoritative mission class from the matched ContractTemplate (p4k); the
            # keyword heuristic is the offline/unmatched fallback. `icon` is its icon slug.
            "type": m.decoded.get("type") or classify_contract(m.contract, m.org, m.title,
                                                                m.is_trade),
            "icon": m.decoded.get("icon"),
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


# sessions.json holds an object: {"backfill": {basename: {size, schema}}, "sessions": [...]}.
# The backfill index lives here rather than in its own file so wiping sessions.json resets it
# atomically (they can never desync). A legacy bare-list file (just the sessions) is read
# transparently and rewritten into the object form on the next archive write.
def _read_archive(path: str) -> dict:
    data = read_json(path)
    if data is None:
        return {"backfill": {}, "sessions": []}
    if isinstance(data, list):  # legacy format: a bare sessions list
        return {"backfill": {}, "sessions": data}
    return {"backfill": data.get("backfill") or {}, "sessions": data.get("sessions") or []}


def _write_archive(path: str, sessions: list, backfill: dict) -> None:
    # sort_keys=False: keep the session list in its (chronological) write order and
    # the top-level keys as authored, rather than alphabetising the whole archive.
    atomic_write(path, {"backfill": backfill, "sessions": sessions}, sort_keys=False)


def _normalize_sessions(raw) -> list:
    data = raw if isinstance(raw, list) else (raw.get("sessions") or [])
    # normalize ship names so sessions archived before the canonical-name fix don't
    # show both "Crusader Mercury Star Runner" and "Mercury Star Runner" (display-only;
    # the stored file is left untouched).
    cmap = load_commodities()
    for s in data:
        if s.get("ships"):
            s["ships"] = sorted({canonical_ship_name(x) for x in s["ships"]})
        # Re-resolve trade commodity names from their stored GUID against the current
        # map, so trades archived before the reference map existed (or built later)
        # self-heal from "Commodity xxxxxxxx" to a real name on read -- no archive
        # rebuild needed. Only overrides on a known GUID.
        for t in s.get("trades") or []:
            name = cmap.get((t.get("commodity_guid") or "").lower())
            if name:
                t["commodity"] = name
    return data


def load_sessions(path: str = SESSIONS_PATH) -> list:
    return load_cached(path, _cache, _normalize_sessions)


def archive_session(state: State, path: str = SESSIONS_PATH) -> None:
    summary = build_summary(state)
    with _write_lock:  # read-modify-write is atomic vs. other archiving threads
        arch = _read_archive(path)
        # replace an existing entry with the same key, else append
        sessions = [s for s in arch["sessions"] if s.get("key") != summary["key"]]
        sessions.append(summary)
        sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
        sessions = sessions[:SESSIONS_KEEP]
        _write_archive(path, sessions, arch["backfill"])  # preserve the backfill index
    _cache["mtime"] = None  # force reload on next read


# --- backfill index (which immutable logbackups have been processed) ----------------- #
# Stored alongside the sessions in the same file so the two stay in lockstep. See
# tracker.backfill_archive for how it's used.
def load_backfill_index(path: str = SESSIONS_PATH) -> dict:
    return _read_archive(path)["backfill"]


def save_backfill_index(index: dict, path: str = SESSIONS_PATH) -> None:
    with _write_lock:
        arch = _read_archive(path)
        _write_archive(path, arch["sessions"], index)  # preserve the sessions
    _cache["mtime"] = None
