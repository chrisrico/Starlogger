"""Flask app: serves the dashboard and the state/override API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
from html import escape as _html_escape
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from .archive import filter_sessions, load_sessions
from .config import API_TOKEN_PATH, MISSION_ICONS_DIR, MUSIC_DIR, OVERRIDES_PATH, WEB_DIR
from .jsonstore import atomic_write, read_json
from .overrides import set_leg_field, set_leg_states
from .replay import build_timeline, snapshot_with_overlay, state_at
from .replay_edit import apply_override_with_siblings, apply_replay_op, seed_overlay
from .settings import describe as describe_settings, set_setting
from .settings import get_ship_equipment, set_ship_equipment
from .settings import resolve_str as settings_str
from .settings import update as update_settings
from .blueprints import aggregate_blueprints, blueprint_catalog, lookup_blueprint
from .shipbuild import ship_build_plan
from .contracts import load_contracts
from .music import load_curation, load_music, set_curation
from .mineables import (all_minerals, decompose_rs, load_mineables, lookup_mineral,
                        lookup_rs, mineral_index, mining_plan, rock_signatures)
from .mining_gear import head_by_class, load_mining_gear, modules as gear_modules
from .body_mineables import load_body_mineables
from .space_mineables import load_space_mineables
from .mine_locations import mine_locations
from .radar import load_radar, radar_by_class
from .salvageables import salvage_lookup
from . import salvage_ships
from .ships import load_ship_cargo, mining_head, mining_hardpoints, radar_slot
from .tradeflags import set_lost
from .snapshot import build_snapshot
from .state import State
from .stations import set_station_name


# How long an idle SSE stream waits before emitting a keepalive comment. Bounds how
# fast we notice a dead socket (the next write fails -> the generator's finally runs).
SSE_KEEPALIVE_SECS = 15.0


def _asset_files() -> list[str]:
    """Every frontend file this process serves: the page, the stylesheet, and ALL JS in
    web/ (app.js is split into ES modules, so glob rather than hardcode — a new module is
    picked up automatically). Sorted for a deterministic hash."""
    return ["index.html", "styles.css"] + sorted(
        f for f in os.listdir(WEB_DIR) if f.endswith(".js"))


# (stat-signature, hash) of the last _assets_version() computation. Re-hashing only when a
# file's mtime/size moves keeps the per-tick liveness check (see the SSE loop) cheap: a few
# stats, the sha256 only on an actual change.
_assets_cache: "tuple[tuple, str] | None" = None


def _assets_version() -> str:
    """Content hash of the served frontend assets. An open dashboard reloads whenever this
    hash changes -- whether it sees the change across an SSE reconnect (a relaunch dropped
    the socket) or mid-stream (an in-place asset swap re-pushed by the live connection).
    Stat-gated rather than cached for process life: the tracker mostly changes these files
    by re-exec'ing into a new build, but they can also move under a still-running process (a
    manual edit, a `git pull`, a dev sync), and that must still be noticed."""
    global _assets_cache
    names = _asset_files()
    sig = []
    for name in names:
        try:
            st = os.stat(os.path.join(WEB_DIR, name))
            sig.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((name, 0, -1))   # missing file -> a stable, distinct signature entry
    sig = tuple(sig)
    if _assets_cache is not None and _assets_cache[0] == sig:
        return _assets_cache[1]
    h = hashlib.sha256()
    for name in names:
        try:
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"\0")  # missing file still contributes a stable token
        h.update(b"\x00")  # delimiter so concatenation can't alias
    digest = h.hexdigest()[:16]
    _assets_cache = (sig, digest)
    return digest


def _load_or_create_token() -> str:
    """The per-install API token, generated once and persisted 0600 in the data dir so it
    survives restarts/re-exec. Gates the mutating API: same-origin dashboard JS reads it
    from the served page, so a cross-origin attacker can't lift it to forge writes, and a
    blind LAN client (when bound non-loopback) doesn't have it at all."""
    try:
        with open(API_TOKEN_PATH, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        os.makedirs(os.path.dirname(API_TOKEN_PATH), exist_ok=True)
        # O_CREAT mode only applies when creating; chmod afterwards so a pre-existing file
        # with looser perms is tightened too. Either way the secret ends up 0600.
        fd = os.open(API_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
        try:
            os.chmod(API_TOKEN_PATH, 0o600)
        except OSError:  # pragma: no cover - e.g. Windows / unusual filesystems
            pass
    except OSError:  # pragma: no cover - read-only data dir; token still works in-memory
        pass
    return tok


# Mutating routes that intentionally skip the token check. /api/closing is fired by
# navigator.sendBeacon on tab close, which CANNOT set request headers; forging it only
# shortens the watchdog's idle grace, so it's harmless to leave unauthenticated. The
# cross-origin (Origin) check below still applies to it.
_TOKEN_EXEMPT = frozenset({"/api/closing"})

# Loopback Host values a browser would send when the dashboard is reached the normal way.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_allowed(host_header: str) -> bool:
    """Anti DNS-rebinding: accept a request only if its Host is loopback -- UNLESS the server was
    deliberately bound to a non-loopback address (the user opted into LAN access, where an
    arbitrary Host is expected and rebinding doesn't apply). A rebinding page at evil.com -> 127.0.0.1
    sends Host: evil.com, so without this its Origin (evil.com) matches request.host (evil.com),
    the same-origin gate passes, and a GET of the shell would hand it the embedded api-token. Host
    is a browser-forbidden header, so the attacker can't forge it to `localhost`."""
    hostname = (host_header or "").rsplit(":", 1)[0].strip("[]").lower()
    if hostname in _LOOPBACK_HOSTS:
        return True
    bind = (settings_str("bind_host") or "").strip().lower()
    return bind not in ("", "localhost", "127.0.0.1", "::1")


def _mine_locations(name: str) -> list:
    """Unified inline "where to mine this" list for a mineral across ALL methods (ship + hand +
    ROC/ground surface bodies, plus ship-only asteroid fields), each location tagged with its
    method -- so the Find / Plan tools surface hand-only gems and ROC spots too, not just ship.
    Thin wrapper over the shared ``mine_locations`` join (contracts pass their own method)."""
    return mine_locations(name, method="any")


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
    app.config["API_TOKEN"] = _load_or_create_token()
    # Cap request bodies so a local process / same-origin client can't OOM the server with an
    # unbounded POST (every mutating route does request.get_json(force=True)). Flask 413s past this.
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

    @app.before_request
    def _enforce_guard():
        # DNS-rebinding gate first, and on EVERY method: the token is served on GET /, so a
        # rebinding page reading it must be blocked before it ever reaches the shell.
        if not _host_allowed(request.host):
            return jsonify({"ok": False, "error": "host not allowed"}), 403
        # Read-only methods are otherwise unguarded: the only secret the server emits (the page
        # token) is same-origin readable, so it can't be lifted cross-origin. State-changing
        # methods must clear two more gates that together kill drive-by CSRF and blind LAN abuse.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        # (1) Same-origin: a browser always stamps Origin on a cross-site write, so a mismatch
        # is a forged request -> reject. This alone defeats the text/plain simple-request CSRF
        # bypass. A matching Origin, or none at all (curl, sendBeacon), falls through to (2).
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify({"ok": False, "error": "cross-origin request rejected"}), 403
        # (2) Bearer token: same-origin dashboard JS attaches it; a cross-origin attacker can't
        # read it, and a blind client (non-loopback bind) doesn't have it.
        if request.path in _TOKEN_EXEMPT:
            return None
        token = app.config.get("API_TOKEN")
        if not token:  # fail closed: a write must never slip through an unconfigured token
            return jsonify({"ok": False, "error": "server token unavailable"}), 503
        sent = request.headers.get("X-Starlogger-Token", "")
        if not (sent and secrets.compare_digest(sent, token)):
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        return None

    def _serve_shell():
        # index.html with the per-install API token injected as a <meta> the dashboard JS reads
        # (see web/net.js). Injected at serve time, not on disk, so the asset-version hash (which
        # reads the disk file) is unaffected. Falls back to the raw file if it can't be read.
        try:
            with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
                html = f.read()
        except OSError:  # pragma: no cover
            return send_from_directory(WEB_DIR, "index.html")
        tag = f'<meta name="api-token" content="{_html_escape(app.config.get("API_TOKEN") or "")}">'
        return Response(html.replace("<head>", "<head>\n" + tag, 1), mimetype="text/html")

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
        # When the tracker launched the game (parent model), the launcher PROCESS is the
        # authoritative "is the game running" signal: it flips at the literal launch (before any
        # log line, so it covers launcher/menu music), survives quit-to-menu, and catches a crash
        # (no FastShutdown). Otherwise fall back to the log-derived flag from build_snapshot.
        # Bare bool reads on purpose (no presence.lock): game_launched is set once at startup and
        # launcher_dead flips once (first-writer-wins) -- there's no multi-field invariant to read
        # atomically, and mark_launcher_dead runs in a SIGUSR1 handler that must stay lock-free,
        # so locking these would add a deadlock risk for no consistency gain.
        if presence is not None and presence.game_launched:
            snap["game_running"] = not presence.launcher_dead
        return snap

    @app.get("/")
    def index():
        return _serve_shell()

    @app.errorhandler(404)
    def spa_fallback(err):
        # The dashboard is path-routed (/contracts, /cargo, /plan, /archive, /mining) via the
        # History API, so a direct hit / reload / new-tab on one of those paths has no file on
        # disk and the static handler 404s — serve the SPA shell and let app.js route from the
        # URL. Real misses stay 404s: only GETs, never /api/* (a missing endpoint is a bug),
        # and only extension-less paths (a missing /styles.css or /foo.js must surface, not
        # silently return HTML).
        path = request.path
        if (request.method == "GET" and not path.startswith("/api/")
                and "." not in path.rsplit("/", 1)[-1]):
            return _serve_shell()
        return err

    @app.get("/mission-icons/<path:name>")
    def mission_icon(name):
        # The game's own mobiGlas mission-type icons, extracted from the p4k into the
        # gitignored data dir (absent until contracts are built -> 404, frontend falls
        # back to a glyph). send_from_directory confines `name` to the icons dir.
        return send_from_directory(MISSION_ICONS_DIR, name)

    @app.get("/music/<path:name>")
    def music_file(name):
        # A decoded music track (Ogg Vorbis), extracted from the p4k into the gitignored
        # data dir by the background music build (absent until that has run once -> 404).
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
                sent_assets = _assets_version()
                yield f"event: meta\ndata: {json.dumps({'assets': sent_assets})}\n\n"
                seen_assets = sent_assets   # one-tick debounce for the re-push below
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
                    # Catch a frontend update that swapped these files under us WITHOUT a
                    # connection-dropping relaunch (manual edit / git pull / dev sync): re-push
                    # `meta` so the tab reloads, instead of waiting for a reconnect that may
                    # never come. Announce only once the new hash has held for a full tick, so
                    # a mid-write copy can't reload the tab onto a half-written asset.
                    now_assets = _assets_version()
                    if now_assets != sent_assets and now_assets == seen_assets:
                        sent_assets = now_assets
                        yield f"event: meta\ndata: {json.dumps({'assets': now_assets})}\n\n"
                    seen_assets = now_assets
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

    @app.post("/api/restart")
    def api_restart():
        # Restart in place: re-exec the tracker with the same args/env (ON_RESTART, wired by
        # tracker.py, stops the server cleanly off-thread so this response returns first, then
        # main's finally re-execs). The dashboard's SSE stream reconnects to the same URL once
        # the replacement is up. 503 if not wired (e.g. tests / a host without restart support).
        fn = app.config.get("ON_RESTART")
        if fn is None:
            return jsonify({"ok": False, "error": "restart unavailable"}), 503
        fn()
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
        # Salvage targets share the radar; the same reading can also be a wreck (ship hull
        # or n debris panels), surfaced as a separate Identify section (no composition).
        return jsonify({"rs": rs, "candidates": lookup_rs(rs), "salvage": salvage_lookup(rs)})

    @app.get("/api/salvage-ship")
    def api_salvage_ship():
        # Removable (salvage-beam-strippable) components of a wreck ship, for the Salvage
        # mode's Ship-ID panel. ?class=<base class> or ?name=<display name> resolves one ship
        # (used when a manual RS-lookup candidate is clicked); with neither, returns the whole
        # catalog. Auto-detected wrecks already carry their components in the snapshot.
        ident = request.args.get("class") or request.args.get("name")
        if ident:
            hit = salvage_ships.lookup(ident)
            if not hit:
                return jsonify({"ok": False, "error": "unknown salvage ship"}), 404
            return jsonify(hit)
        return jsonify(salvage_ships.load_salvage_ships())

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

    @app.get("/api/body-mineables")
    def api_body_mineables():
        # Per-celestial-body mineables (which planet/moon yields which mineral), parsed from
        # the starmap descriptions. The catalog behind the inline "where to mine this" hints.
        return jsonify(load_body_mineables())

    @app.get("/api/space-mineables")
    def api_space_mineables():
        # Space mining locations (asteroid fields / belts / Lagrange fields -> ship mineables +
        # rarity), from the HarvestableProviderPreset records. The space half of body-mineables.
        return jsonify(load_space_mineables())

    @app.get("/api/mineral-lookup")
    def api_mineral_lookup():
        # Forward lookup: a mineral → the RS value(s) to scan for and ranked source rocks, plus
        # where it's ship-mineable (locations: surface bodies + space fields, each kind-tagged).
        name = request.args.get("name", "")
        if not name.strip():
            return jsonify({"ok": False, "error": "name is required"}), 400
        r = lookup_mineral(name)
        r["locations"] = _mine_locations(name)
        return jsonify(r)

    @app.get("/api/mineral-index")
    def api_mineral_index():
        # The full mineral → rocks reverse map.
        return jsonify({"minerals": mineral_index()})

    @app.post("/api/mining-plan")
    def api_mining_plan():
        # Blueprint plan: wanted minerals → per-mineral sourcing + deposit coverage ranking.
        # Each per-mineral entry also carries where it's ship-mineable (bodies + space fields).
        payload = request.get_json(force=True, silent=True) or {}
        minerals = payload.get("minerals")
        if not isinstance(minerals, list):
            return jsonify({"ok": False, "error": "minerals must be a list"}), 400
        plan = mining_plan([str(m) for m in minerals])
        for entry in plan.get("per_mineral", []):
            entry["locations"] = _mine_locations(entry["mineral"])
        return jsonify(plan)

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

    @app.post("/api/blueprints-plan")
    def api_blueprints_plan():
        # A build-list of {name, qty} -> one material shopping list (every recipe summed by
        # resource) + the distinct minerals to feed /api/mining-plan. Like /api/blueprint but
        # for many blueprints at once; the Plan tab's crafting list posts here.
        payload = request.get_json(force=True, silent=True) or {}
        items = payload.get("items")
        if not isinstance(items, list):
            return jsonify({"ok": False, "error": "items must be a list"}), 400
        return jsonify(aggregate_blueprints(
            [{"name": str((it or {}).get("name", "")), "qty": (it or {}).get("qty", 1)}
             for it in items]))

    @app.get("/api/ship-build")
    def api_ship_build():
        # Shipbuilder: the Grade-A blueprints that outfit a ship's components to a chosen class
        # (?ship=<name>&cls=<class>) -- the chosen class where it makes the part, else the closest
        # class. One build per slot at the stock count; feeds the planner's "outfit a ship" control.
        ship = request.args.get("ship", "")
        if not ship.strip():
            return jsonify({"ok": False, "error": "ship is required"}), 400
        return jsonify(ship_build_plan(ship.strip(), request.args.get("cls", "Military"),
                                       load_ship_cargo()))

    @app.get("/api/mining-gear")
    def api_mining_gear():
        # The mining-equipment catalog (laser heads + consumable modules + radar) + the user's
        # saved per-ship loadout. With ?ship=<name>, heads/radars are filtered to that ship's
        # hardpoint sizes and the response carries just that ship's saved selection -- this is
        # what the equipment popup renders. Without it, the full catalog (for browsing).
        cat = load_mining_gear()
        all_heads = cat.get("heads") or []
        all_modules = cat.get("modules") or []
        all_radars = load_radar().get("radars") or []
        saved = get_ship_equipment()
        ship = request.args.get("ship")
        if ship:
            db = load_ship_cargo()
            hardpoints = mining_hardpoints(ship, None, db)
            sizes = set(hardpoints)
            heads = [h for h in all_heads if h.get("size") in sizes]
            # Restrict to heads sharing the factory head's mount tag: a bespoke head (the Golem's
            # Pitman, "DRAK_miningMount") fits ONLY its ship, and a generic ship ("miningMount")
            # must not be offered that bespoke head. `fixed_head` flags the bespoke case so the UI
            # can note the head isn't a free choice.
            factory_cls = (mining_head(ship, None, db) or "").lower()
            factory = next((h for h in all_heads if h["class"].lower() == factory_cls), None)
            fixed_head = False
            if factory and factory.get("mount"):
                heads = [h for h in heads if h.get("mount") == factory["mount"]]
                fixed_head = factory["mount"] != "miningMount"   # non-generic mount = bespoke
            rslot = radar_slot(ship, None, db)
            rsize = (rslot or {}).get("size")
            radars = [r for r in all_radars if r.get("size") == rsize] if rsize is not None else []
            return jsonify({"ship": ship, "hardpoints": hardpoints, "heads": heads,
                            "fixed_head": fixed_head, "modules": all_modules, "radars": radars,
                            "radar_slot": rslot, "selected": saved.get(ship)})
        return jsonify({"heads": all_heads, "modules": all_modules, "radars": all_radars,
                        "selected": saved, "game_version": cat.get("game_version")})

    @app.post("/api/mining-gear")
    def api_mining_gear_set():
        # Persist one ship's mining loadout {ship, head, modules, radar}. Validates the
        # head/module/radar classes against the catalog, caps modules at the head's slot count,
        # and checks the radar fits the ship's radar slot size. Empty head + modules + radar
        # clears the ship's entry.
        payload = request.get_json(force=True, silent=True) or {}
        ship = payload.get("ship")
        if not isinstance(ship, str) or not ship.strip():
            return jsonify({"ok": False, "error": "ship is required"}), 400
        head = payload.get("head") or None
        mods = payload.get("modules") or []
        radar = payload.get("radar") or None
        if head is not None and not isinstance(head, str):
            return jsonify({"ok": False, "error": "head must be a class string or null"}), 400
        if not isinstance(mods, list) or any(not isinstance(m, str) for m in mods):
            return jsonify({"ok": False, "error": "modules must be a list of class strings"}), 400
        if radar is not None and not isinstance(radar, str):
            return jsonify({"ok": False, "error": "radar must be a class string or null"}), 400
        known_mods = {m["class"] for m in gear_modules()}
        head_rec = head_by_class(head) if head else None
        if head and not head_rec:
            return jsonify({"ok": False, "error": "unknown head"}), 400
        if any(m not in known_mods for m in mods):
            return jsonify({"ok": False, "error": "unknown module"}), 400
        slots = (head_rec or {}).get("module_slots", 0)
        if mods and not head:
            return jsonify({"ok": False, "error": "modules require a head"}), 400
        if len(mods) > slots:
            return jsonify({"ok": False, "error": f"head has only {slots} module slot(s)"}), 400
        if radar:
            radar_rec = radar_by_class(radar)
            if not radar_rec:
                return jsonify({"ok": False, "error": "unknown radar"}), 400
            rslot = radar_slot(ship.strip(), None, load_ship_cargo())
            if rslot and radar_rec.get("size") != rslot.get("size"):
                return jsonify({"ok": False,
                                "error": f"radar must be size {rslot.get('size')}"}), 400
        try:
            set_ship_equipment(ship.strip(), {"head": head, "modules": mods, "radar": radar})
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "error": str(e)}), 500
        return _ok()

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
        before_host = settings_str("bind_host")
        # Validate a new update source BEFORE persisting: a typo'd remote or a branch that
        # doesn't exist upstream would otherwise silently break auto-update. The validator
        # (wired by the tracker, which owns git) checks the prospective remote+branch pair.
        if "update_remote" in payload or "update_branch" in payload:
            validate = app.config.get("ON_VALIDATE_SOURCE")
            if validate:
                err = validate(payload.get("update_remote", before[0]),
                               payload.get("update_branch", before[1]))
                if err:
                    return jsonify({"ok": False, "error": err}), 400
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
        # The bind address is only read when the server binds at startup, so a change can't
        # take effect in-place -> re-exec to rebind. Skip when an update-apply already
        # triggered a restart above (the re-exec re-reads bind_host anyway).
        elif settings_str("bind_host") != before_host:
            fn = app.config.get("ON_RESTART")
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
        # Explicit "Check for updates" button: fetch now and, if a new build exists, honour the
        # Updates mode -- apply immediately ("updating") only in Automatic; in Prompt/Off raise
        # the banner instead ("available"). The status comes straight back; "updating" means the
        # server is restarting.
        fn = app.config.get("ON_CHECK_NOW")
        if fn is None:
            return jsonify({"ok": False, "status": "unavailable"}), 503
        try:
            return jsonify(fn())
        except Exception as e:  # pragma: no cover
            return jsonify({"ok": False, "status": "error", "error": str(e)}), 500

    @app.get("/api/music")
    def api_music():
        # The jukebox full-song manifest ({tracks, count, ...}) plus the effective curation
        # (playlist order, skipped ids, custom names = shipped default overlaid by local edits).
        # Empty until the background extract has run once.
        d = dict(load_music())
        d["curation"] = load_curation()
        return jsonify(d)

    @app.post("/api/music/curate")
    def api_music_curate():
        # Persist a jukebox curation edit (reorder / skip / rename) to the local sidecar. Accepts
        # any of {order:[id...], skipped:[id...], names:{id:name}}; merges partials. _ok() bumps the
        # version so the change rides the SSE push to every open dashboard.
        body = request.get_json(silent=True) or {}
        order = body.get("order")
        skipped = body.get("skipped")
        names = body.get("names")
        if order is not None and not isinstance(order, list):
            return jsonify({"ok": False, "error": "order must be a list"}), 400
        if skipped is not None and not isinstance(skipped, list):
            return jsonify({"ok": False, "error": "skipped must be a list"}), 400
        if names is not None and not isinstance(names, dict):
            return jsonify({"ok": False, "error": "names must be an object"}), 400
        if order is None and skipped is None and names is None:
            return jsonify({"ok": False, "error": "nothing to update"}), 400
        set_curation(order=order, skipped=skipped, names=names)
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
