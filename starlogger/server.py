"""Flask app: serves the dashboard and the state/override API."""

from __future__ import annotations

import logging

from flask import Flask, jsonify, request, send_from_directory

from .archive import filter_sessions, load_sessions
from .config import WEB_DIR
from .overrides import get_overrides, set_leg_field, set_leg_states, write_override
from .replay import build_timeline, snapshot_at
from .settings import set_setting
from .mineables import load_mineables, lookup_rs
from .shipcargo import load_ship_cargo
from .tradeflags import set_lost
from .snapshot import PENDING_DEST, PENDING_ORIGIN, build_snapshot
from .state import State
from .stations import get_station_names, set_station_name


def _resolve_zone(zone_names, z) -> str:
    if z and z in zone_names:
        return zone_names[z]
    if z:
        return f"Unknown station (zone {z})"
    return "Unknown station"


def _origin_of(mis, ov, zone_names) -> str:
    """A mission's displayed origin, matching snapshot.build_snapshot: an explicit
    override origin wins, else 'Origin pending' when the only pickup is a host
    artifact, else the pickup zone resolved through zone_names, else 'Unknown station'."""
    o = (ov or {}).get("origin")
    if o:
        return o
    if mis.has_pending_origin:
        return PENDING_ORIGIN
    return _resolve_zone(zone_names, mis.origin_zone)


def _dleg_loc(mis, leg, zone_names) -> str:
    """A dropoff leg's destination label, matching snapshot.dleg_loc: deliver text
    wins; an acceptance-host zone (shared with the pickup) is not a real destination
    and shows as pending until the game reveals it; else resolve the zone."""
    if leg.location:
        return leg.location
    if leg.zone_host_id in mis.host_artifact_zones:
        return PENDING_DEST
    return _resolve_zone(zone_names, leg.zone_host_id)


def _dests_of(mis, zone_names) -> tuple:
    """A mission's destination signature (sorted dropoff station labels), matching
    snapshot's `destinations`. Used with the origin to identify same-route siblings."""
    return tuple(sorted({_dleg_loc(mis, l, zone_names)
                         for l in mis.legs.values() if l.kind == "dropoff"}))


def create_app(state: State, log_path: str | None = None) -> Flask:
    # static_url_path="" serves web/ assets at the root (/styles.css, /app.js).
    # log_path (when known) backs the Archive's session-replay feature, which
    # reconstructs a past session's dashboard by re-feeding its source log.
    app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/api/state")
    def api_state():
        return jsonify(build_snapshot(state, trade_only=request.args.get("trade") == "1"))

    @app.get("/api/ships")
    def api_ships():
        # The whole cargo-grid database (name → {scu, manufacturer, groups}) plus
        # its metadata — backs the all-ships debug page at /grids.html.
        return jsonify(load_ship_cargo())

    @app.get("/api/rock-lookup")
    def api_rock_lookup():
        # Reverse-map an observed radar RS reading to candidate mineable rock
        # class(es), the inferred cluster size (HUD value ≈ base_rs × count), and each
        # class's probabilistic mineral makeup. ?rs=<number> required. With no rs, just
        # returns the full mineable catalog (count + game_version) for browsing.
        raw = request.args.get("rs")
        if raw is None:
            data = load_mineables()
            return jsonify({"count": data.get("count", len(data.get("rocks", []))),
                            "game_version": data.get("game_version"),
                            "rocks": data.get("rocks", [])})
        try:
            rs = float(raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "rs must be a number"}), 400
        if rs <= 0:
            return jsonify({"ok": False, "error": "rs must be positive"}), 400
        return jsonify({"rs": rs, "candidates": lookup_rs(rs)})

    @app.post("/api/select-ship")
    def api_select_ship():
        # Manually pick the ship for the gauge/grid when the log hasn't detected
        # one. Empty/null clears it. A detected ship always overrides this.
        payload = request.get_json(force=True, silent=True) or {}
        ship = payload.get("ship")
        if ship is not None and not isinstance(ship, str):
            return jsonify({"ok": False, "error": "ship must be a string or null"}), 400
        try:
            set_setting("selected_ship", (ship or "").strip() or None)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    @app.get("/api/sessions")
    def api_sessions():
        return jsonify(filter_sessions(
            load_sessions(),
            trade_only=request.args.get("trade") == "1",
            show_unfinished=request.args.get("unfinished") == "1",
        ))

    @app.get("/api/replay/timeline")
    def api_replay_timeline():
        # Ordered scrub checkpoints (index, ts, label) for a session, reconstructed
        # from its source log. {available:false} when that log is no longer present.
        key = request.args.get("key") or ""
        if not key:
            return jsonify({"ok": False, "error": "key required"}), 400
        tl = build_timeline(key, log_path)
        if tl is None:
            return jsonify({"available": False})
        return jsonify({"available": True, **tl})

    @app.get("/api/replay/state")
    def api_replay_state():
        # The full dashboard snapshot at one checkpoint — drives the whole UI in
        # replay mode, same shape as /api/state.
        key = request.args.get("key") or ""
        try:
            at = int(request.args.get("at", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "at must be an integer"}), 400
        if not key:
            return jsonify({"ok": False, "error": "key required"}), 400
        snap = snapshot_at(key, log_path, at)
        if snap is None:
            return jsonify({"available": False}), 404
        return jsonify(snap)

    @app.post("/api/override")
    def api_override():
        payload = request.get_json(force=True, silent=True) or {}
        mid = payload.get("mission_id")
        if not isinstance(mid, str) or not mid:
            return jsonify({"ok": False, "error": "mission_id required"}), 400
        override = payload.get("override")
        if override is not None and not isinstance(override, dict):
            return jsonify({"ok": False, "error": "override must be an object or null"}), 400
        origin = (override or {}).get("origin")
        origin = origin.strip() if isinstance(origin, str) else None
        try:
            # Correcting an origin propagates only to *same-route* siblings: other
            # active missions that share BOTH the edited mission's displayed origin
            # AND its destination(s). Origin alone is too coarse — many missions
            # share an "Unknown station" origin while running different routes, so
            # we'd otherwise rewrite all of them (including a reverse-direction
            # haul). Keyed on what's displayed; only writes per-mission origin
            # overrides, never touches zone names.
            before, prev_ov = {}, {}
            if origin:
                with state.lock:
                    zone_names = {**get_station_names(), **state.zone_names}
                    prev_ov = get_overrides()
                    before = {oid: (_origin_of(m, prev_ov.get(oid), zone_names),
                                    _dests_of(m, zone_names))
                              for oid, m in state.missions.items() if m.status == "active"}

            write_override(mid, override)

            key = before.get(mid)
            if origin and key:
                for oid, sib_key in before.items():
                    if oid == mid or sib_key != key:
                        continue
                    sib = dict(prev_ov.get(oid) or {})
                    sib["origin"] = origin
                    write_override(oid, sib)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    @app.post("/api/trade-lost")
    def api_trade_lost():
        # Flag (or unflag) a manual-trade buy whose cargo was lost (destroyed/stolen),
        # so its unsold remainder realises as a loss in the trade-load view. Keyed by
        # the trade's stable id (ts|action|guid|shop), which the frontend reconstructs.
        payload = request.get_json(force=True, silent=True) or {}
        tid = payload.get("trade_id")
        if not isinstance(tid, str) or not tid:
            return jsonify({"ok": False, "error": "trade_id required"}), 400
        try:
            set_lost(tid, bool(payload.get("lost", True)))
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    @app.post("/api/station-name")
    def api_station_name():
        # Name (or rename) a station by its zoneHostId. Persists to
        # station_names.json and back-fills every mission that uses the zone.
        payload = request.get_json(force=True, silent=True) or {}
        zone = payload.get("zone")
        if not isinstance(zone, (str, int)) or str(zone) == "":
            return jsonify({"ok": False, "error": "zone required"}), 400
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            return jsonify({"ok": False, "error": "name must be a string or null"}), 400
        try:
            set_station_name(str(zone), (name or "").strip() or None)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    @app.post("/api/leg-state")
    def api_leg_state():
        # Mark one or more delivery legs delivered/undelivered. Accepts a single
        # {mission_id, oid} or {legs: [{mission_id, oid}, …]} plus done:bool.
        payload = request.get_json(force=True, silent=True) or {}
        legs = payload.get("legs")
        if legs is None:
            legs = [{"mission_id": payload.get("mission_id"), "oid": payload.get("oid")}]
        if not isinstance(legs, list) or not legs:
            return jsonify({"ok": False, "error": "legs required"}), 400
        for it in legs:
            if not isinstance(it, dict) or not it.get("mission_id") or not it.get("oid"):
                return jsonify({"ok": False, "error": "each leg needs mission_id and oid"}), 400
        done = bool(payload.get("done", True))
        try:
            set_leg_states(legs, done)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    @app.post("/api/leg-field")
    def api_leg_field():
        # Inline-edit one leg's commodity or quantity (the unified editor on the
        # cargo-ops screens). Keyed by mission_id + objective id; stored as a leg_fields
        # override overlaid by oid, so a single unknown is fixed without rebuilding legs.
        payload = request.get_json(force=True, silent=True) or {}
        mid, oid = payload.get("mission_id"), payload.get("oid")
        field = payload.get("field")
        if not isinstance(mid, str) or not mid or not isinstance(oid, str) or not oid:
            return jsonify({"ok": False, "error": "mission_id and oid required"}), 400
        if field not in ("cargo", "qty"):
            return jsonify({"ok": False, "error": "field must be 'cargo' or 'qty'"}), 400
        value = payload.get("value")
        if field == "qty":
            if value in (None, ""):
                value = None
            else:
                try:
                    value = int(float(value))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "qty must be a number"}), 400
                if value < 0:
                    value = None
        else:
            value = value.strip() if isinstance(value, str) else None
            value = value or None
        try:
            set_leg_field(mid, oid, field, value)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})

    return app
