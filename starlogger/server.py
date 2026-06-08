"""Flask app: serves the dashboard and the state/override API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from functools import lru_cache

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from .archive import filter_sessions, load_sessions
from .config import MISSION_ICONS_DIR, MUSIC_DIR, OVERRIDES_PATH, WEB_DIR
from .jsonstore import atomic_write, read_json
from .overrides import set_leg_field, set_leg_states
from .replay import build_timeline, snapshot_with_overlay, state_at
from .replay_edit import apply_override_with_siblings, apply_replay_op, seed_overlay
from .settings import describe as describe_settings, set_setting
from .settings import resolve_str as settings_str
from .settings import update as update_settings
from .blueprints import blueprint_catalog, lookup_blueprint
from .contracts import load_contracts
from .music import load_music
from .mineables import (all_minerals, decompose_rs, load_mineables, lookup_mineral,
                        lookup_rs, mineral_index, mining_plan, rock_signatures)
from .ships import load_ship_cargo
from .tradeflags import set_lost
from .snapshot import build_snapshot
from .state import State
from .stations import set_station_name


# How long an idle SSE stream waits before emitting a keepalive comment. Bounds how
# fast we notice a dead socket (the next write fails -> the generator's finally runs).
SSE_KEEPALIVE_SECS = 15.0


# The four frontend files this process serves. A relaunch with a new build changes
# their bytes; a server-only relaunch leaves them identical.
_ASSET_FILES = ("index.html", "app.js", "styles.css", "cargogrid.js")


@lru_cache(maxsize=1)
def _assets_version() -> str:
    """Content hash of the served frontend assets. An open dashboard compares this
    across SSE reconnects: a relaunch that changed any of these files -> the tab
    reloads to pick up the new code; a server-only relaunch -> same hash, no reload.
    Cached for process life -- the tracker is re-exec'd per launch, so it can't go
    stale (same reasoning as snapshot._app_version)."""
    h = hashlib.sha256()
    for name in _ASSET_FILES:
        try:
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"\0")  # missing file still contributes a stable token
        h.update(b"\x00")  # delimiter so concatenation can't alias
    return h.hexdigest()[:16]


def create_app(state: State, log_path: str | None = None, presence=None,
               update_state=None, music_state=None) -> Flask:
    # static_url_path="" serves web/ assets at the root (/styles.css, /app.js).
    # log_path (when known) backs the Archive's session-replay feature, which
    # reconstructs a past session's dashboard by re-feeding its source log.
    # presence (when given) tracks open SSE streams so the entry point's watchdog
    # knows whether a dashboard is still attached (see tracker.py).
    # update_state (when given) carries the tracker's 'new build available?' status,
    # merged into the snapshot so the dashboard's update banner rides the SSE push.
    app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    def _ok():
        # A manual edit (override/leg/ship/etc.) changed what build_snapshot returns but
        # didn't touch the log, so bump the version to push the new snapshot to every open
        # dashboard stream -- not just the tab that made the edit.
        state.bump_version()
        return jsonify({"ok": True})

    def _snap(trade_only=False):
        # The dashboard snapshot + the tracker's update status (when wired), so the
        # "update available" banner is pushed by the same version-bump as everything else.
        snap = build_snapshot(state, trade_only=trade_only)
        if update_state is not None:
            snap["update"] = update_state.as_dict()
        if music_state is not None:
            snap["music"] = music_state.as_dict()
        return snap

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/mission-icons/<path:name>")
    def mission_icon(name):
        # The game's own mobiGlas mission-type icons, extracted from the p4k into the
        # gitignored data dir (absent until contracts are built -> 404, frontend falls
        # back to a glyph). send_from_directory confines `name` to the icons dir.
        return send_from_directory(MISSION_ICONS_DIR, name)

    @app.get("/music/<path:name>")
    def music_file(name):
        # A decoded music track (Ogg Vorbis), extracted from the p4k into the gitignored
        # data dir on demand (absent until the jukebox's Extract is run -> 404).
        # send_from_directory confines `name` to the music dir. conditional=True so the
        # <audio> element gets Range support (seek/scrub) for free.
        return send_from_directory(MUSIC_DIR, name, conditional=True)

    @app.get("/api/state")
    def api_state():
        return jsonify(_snap(trade_only=request.args.get("trade") == "1"))

    @app.get("/api/stream")
    def api_stream():
        # Server-Sent Events: push the full snapshot on connect, then again whenever the
        # tailer bumps state.version (real-time, no client polling). The open connection
        # also doubles as the dashboard's presence signal for the lifecycle watchdog.
        trade_only = request.args.get("trade") == "1"

        @stream_with_context
        def gen():
            if presence is not None:
                presence.stream_connect()
            try:
                # First frame: the asset version, so a reconnecting dashboard can tell
                # a new-build relaunch (reload to get new code) from a server-only one.
                yield f"event: meta\ndata: {json.dumps({'assets': _assets_version()})}\n\n"
                last = None
                while True:
                    with state.version_cv:
                        if last is not None:
                            # Wait for a new version, or time out to send a keepalive.
                            state.version_cv.wait_for(
                                lambda: state.version != last, timeout=SSE_KEEPALIVE_SECS)
                        cur = state.version
                    if cur != last:
                        # Build the snapshot OUTSIDE version_cv (build_snapshot re-acquires
                        # state.lock, which is reentrant -- no deadlock).
                        last = cur
                        snap = _snap(trade_only=trade_only)
                        yield f"data: {json.dumps(snap)}\n\n"
                    else:
                        yield ": keepalive\n\n"  # a failed write here -> finally -> disconnect
            finally:
                if presence is not None:
                    presence.stream_disconnect()

        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        })

    @app.post("/api/quit")
    def api_quit():
        # A newer launch is taking over the port -> shut this instance down cleanly. QUIT_FN
        # (the WSGI server's thread-safe .shutdown(), wired in by tracker.py) runs in a
        # daemon thread so this response returns first. No-op if not wired (e.g. in tests
        # without a real server). Localhost-only tool, so no auth -- worst case is stopping
        # your own dev server.
        fn = app.config.get("QUIT_FN")
        if fn is not None:
            threading.Thread(target=fn, daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/api/closing")
    def api_closing():
        # The dashboard beacons this on pagehide (a deliberate tab close). It does NOT shut
        # us down -- it just withdraws that tab's keep-alive claim so the watchdog may use a
        # short grace instead of the full idle timeout once the launcher is also gone. A
        # reload also fires pagehide, but its stream reconnects (clearing the flag) within
        # the grace. No-op when no presence tracker is wired (e.g. tests).
        if presence is not None:
            presence.mark_closing()
        return jsonify({"ok": True})

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

    @app.get("/api/rock-decompose")
    def api_rock_decompose():
        # Break an RS reading into plausible homogeneous + 2-class mixed clusters.
        try:
            rs = float(request.args.get("rs", ""))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "rs must be a number"}), 400
        if rs <= 0:
            return jsonify({"ok": False, "error": "rs must be positive"}), 400
        return jsonify({"rs": rs, "combos": decompose_rs(rs)})

    @app.get("/api/rock-signatures")
    def api_rock_signatures():
        # Distinct base RS values, seeding the Identify input's inline prediction.
        return jsonify({"signatures": rock_signatures()})

    @app.get("/api/minerals")
    def api_minerals():
        # Distinct mineral names (autocomplete for the forward lookup + blueprint plan).
        return jsonify({"minerals": all_minerals()})

    @app.get("/api/mineral-lookup")
    def api_mineral_lookup():
        # Forward lookup: a mineral → the RS value(s) to scan for and ranked source rocks.
        name = request.args.get("name", "")
        if not name.strip():
            return jsonify({"ok": False, "error": "name is required"}), 400
        return jsonify(lookup_mineral(name))

    @app.get("/api/mineral-index")
    def api_mineral_index():
        # The full mineral → rocks reverse map.
        return jsonify({"minerals": mineral_index()})

    @app.post("/api/mining-plan")
    def api_mining_plan():
        # Blueprint plan: wanted minerals → per-mineral sourcing + deposit coverage ranking.
        payload = request.get_json(force=True, silent=True) or {}
        minerals = payload.get("minerals")
        if not isinstance(minerals, list):
            return jsonify({"ok": False, "error": "minerals must be a list"}), 400
        return jsonify(mining_plan([str(m) for m in minerals]))

    @app.get("/api/contracts")
    def api_contracts():
        # The contract-template taxonomy (grade × route × SCU cap × rep × legal) +
        # cargo-manifest composition, mined from the p4k. Empty until first built.
        return jsonify(load_contracts())

    @app.get("/api/blueprints")
    def api_blueprints():
        # {name, category} rows for the planner's grouped (type/size) picker.
        return jsonify({"blueprints": blueprint_catalog()})

    @app.get("/api/blueprint")
    def api_blueprint():
        # One blueprint's recipe (requirements + minerals) by name; 404 if unknown.
        name = request.args.get("name", "")
        if not name.strip():
            return jsonify({"ok": False, "error": "name is required"}), 400
        bp = lookup_blueprint(name)
        if not bp:
            return jsonify({"ok": False, "error": "no such blueprint"}), 404
        return jsonify(bp)

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
        return _ok()

    @app.get("/api/settings")
    def api_settings_get():
        # The configurable knobs with their effective values + env-shadow flags; the
        # dashboard's Settings panel renders straight from this schema.
        return jsonify({"schema": describe_settings()})

    @app.post("/api/settings")
    def api_settings_set():
        # Persist a batch of knobs to settings.json. update() validates against the
        # schema (unknown key / bad value -> ValueError -> 400). env vars still win at
        # read time, so a saved value may be shadowed -- the GET reports that.
        payload = request.get_json(force=True, silent=True) or {}
        before = (settings_str("update_remote"), settings_str("update_branch"))
        try:
            update_settings(payload)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        # Changing the update source here is a deliberate "switch me to that build" -> the
        # save itself is the approval, so apply it now without a second prompt (unless updates
        # are off). This is distinct from merely *having* a remote configured, which never
        # bypasses the Updates mode -- the background poll still prompts there. Restarts only
        # if the new source actually differs.
        after = (settings_str("update_remote"), settings_str("update_branch"))
        if after != before and settings_str("update_mode") != "off":
            fn = app.config.get("ON_APPLY")
            if fn:
                fn()
        return _ok()

    @app.post("/api/update/apply")
    def api_update_apply():
        # User clicked "Update now" (prompt mode). on_apply fetches + resets + restarts off
        # the request thread, so this returns first; the dashboard's asset-hash reload then
        # swaps in the new build. 503 if the tracker didn't wire updating (e.g. --once host).
        fn = app.config.get("ON_APPLY")
        if fn is None:
            return jsonify({"ok": False, "error": "updates unavailable"}), 503
        fn()
        return _ok()

    @app.post("/api/update/check")
    def api_update_check():
        # Explicit "Check for updates" button: fetch now and apply immediately if there's a
        # new build (no prompt -- the click is the approval, bypassing the Updates mode). The
        # status comes straight back; an "updating" result means the server is restarting.
        fn = app.config.get("ON_CHECK_NOW")
        if fn is None:
            return jsonify({"ok": False, "status": "unavailable"}), 503
        try:
            return jsonify(fn())
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "status": "error", "error": str(e)}), 500

    @app.get("/api/music")
    def api_music():
        # The jukebox track manifest ({tracks, count, ...}); empty until extracted.
        return jsonify(load_music())

    @app.post("/api/music/extract")
    def api_music_extract():
        # User clicked "Extract music" in the jukebox: decode the soundtrack off the request
        # thread (it's ~2.6 GB / minutes), reporting progress via the SSE snapshot's "music"
        # field. 503 if extraction wasn't wired (e.g. --once host / no p4k path known).
        fn = app.config.get("ON_EXTRACT_MUSIC")
        if fn is None:
            return jsonify({"ok": False, "error": "music extraction unavailable"}), 503
        fn()
        return _ok()

    @app.post("/api/update/dismiss")
    def api_update_dismiss():
        # Hide the banner for this commit (re-offered only when a newer one lands).
        if update_state is not None:
            update_state.dismiss()
        return _ok()

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

    @app.route("/api/replay/state", methods=["GET", "POST"])
    def api_replay_state():
        # The full dashboard snapshot at one checkpoint — drives the whole UI in replay
        # mode, same shape as /api/state. GET (no overlay) returns the cached disk-state
        # snapshot; POST {key, at, overlay} re-renders that checkpoint with an ephemeral
        # edit overlay applied (archive editing scrubs with edits kept), persisting nothing.
        body = request.get_json(force=True, silent=True) or {}
        key = request.args.get("key") or body.get("key") or ""
        try:
            at = int(request.args.get("at", body.get("at", 0)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "at must be an integer"}), 400
        if not key:
            return jsonify({"ok": False, "error": "key required"}), 400
        snap = snapshot_with_overlay(key, log_path, at, body.get("overlay"))
        if snap is None:
            return jsonify({"available": False}), 404
        return jsonify(snap)

    @app.post("/api/replay/edit")
    def api_replay_edit():
        # Apply ONE edit op to the ephemeral archive overlay and return the recomputed
        # snapshot + the updated overlay (which the client echoes back on the next edit
        # /scrub). Mirrors the live edit endpoints exactly but writes nothing to disk.
        body = request.get_json(force=True, silent=True) or {}
        key, op = body.get("key") or "", body.get("op")
        try:
            at = int(body.get("at", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "at must be an integer"}), 400
        if not key or not isinstance(op, dict) or not op.get("kind"):
            return jsonify({"ok": False, "error": "key and op required"}), 400
        st = state_at(key, log_path, at)
        if st is None:
            return jsonify({"available": False}), 404
        overlay = body.get("overlay") or seed_overlay()
        try:
            apply_replay_op(overlay, op, st)
        except (KeyError, TypeError, ValueError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "snapshot": build_snapshot(st, overlay=overlay),
                        "overlay": overlay})

    @app.post("/api/override")
    def api_override():
        payload = request.get_json(force=True, silent=True) or {}
        mid = payload.get("mission_id")
        if not isinstance(mid, str) or not mid:
            return jsonify({"ok": False, "error": "mission_id required"}), 400
        override = payload.get("override")
        if override is not None and not isinstance(override, dict):
            return jsonify({"ok": False, "error": "override must be an object or null"}), 400
        try:
            # Correcting an origin propagates only to *same-route* siblings: other
            # active missions that share BOTH the edited mission's displayed origin
            # AND its destination(s). Origin alone is too coarse — many missions
            # share an "Unknown station" origin while running different routes, so
            # we'd otherwise rewrite all of them (including a reverse-direction haul).
            # The merge logic is shared with the ephemeral replay overlay.
            data = apply_override_with_siblings(read_json(OVERRIDES_PATH, dict), state, mid, override)
            atomic_write(OVERRIDES_PATH, data)
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        return _ok()

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
        return _ok()

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
        return _ok()

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
        return _ok()

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
        return _ok()

    return app
