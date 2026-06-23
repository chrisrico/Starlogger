"""In-memory mission store fed line-by-line from the game log."""

from __future__ import annotations

import re
import threading
from collections import deque

from . import patterns
from .model import Leg, Mission, OreReq, SalvageTarget, Trade
from .planner import classify_station

# A mission in one of these states is finished -- the trigger for a live archive
# upsert (see _update_archive_dirty / maybe_archive).
_TERMINAL_STATUSES = ("completed", "failed", "abandoned", "expired")


class State:
    """Thread-safe mission store. `feed(line)` ingests one log line."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        # Monotonic snapshot version + a condition (sharing self.lock) used to wake SSE
        # streamers the instant new log lines land. bump_version() advances it; the
        # tailer calls that once per non-empty read batch (and after a reset).
        self.version = 0
        self.version_cv = threading.Condition(self.lock)
        self.missions: dict[str, Mission] = {}
        self.trades: dict[str, Trade] = {}  # manual terminal trades this session
        # Crafting blueprints acquired this session, from "Received Blueprint:" HUD
        # notifications. Keyed by normalized name -> {name, acquired_at}; folded into the
        # cumulative acquired_blueprints.json at session end (see acquired.merge_acquired).
        self.blueprints: dict[str, dict] = {}
        self.kiosk_names: dict[str, str] = {}  # kioskId -> place name (from kiosk entity)
        self.travel_routes: list[dict] = []  # quantum route calcs (ts, ship, frm, to)
        self.travel_arrivals: list[dict] = []  # quantum drive arrivals (ts, ship)
        self.zone_names: dict[str, str] = {}  # zoneHostId -> station name
        # Salvageable wrecks detected at the salvage site this session (entity-spawn lines;
        # see patterns.SALVAGE_SPAWN). Keyed by base ship class, deduped by entity id. A
        # session sighting, NOT tied to a contract; cleared on reset like the other sightings.
        self.salvage_targets: dict[str, SalvageTarget] = {}
        self.player: str | None = None
        self.location: str | None = None  # current station (where the client last requested inventory)
        self.session_started_at: str | None = None
        self.session_gamerules: str | None = None
        self.session_boundary_ts: str | None = None
        self.logged_in: bool = False
        # True while the game process is up (any session boundary -- menu OR PU -- means the
        # game is running; cleared on FastShutdown). A log-derived fallback for "is the game
        # running": the launcher process is authoritative when the tracker launched the game
        # (see server `_snap`), but this covers externally-launched/standalone sessions. Drives
        # the jukebox auto-pause-while-playing. Unlike logged_in it stays True at the main menu.
        self.game_running: bool = False
        self.screen_locked: bool = False  # desktop screensaver/lock engaged (screenlock watcher)
        self.game_version: str | None = None  # e.g. "4.8.0"
        self.game_build: str | None = None    # changelist
        self.last_event_ts: str | None = None
        self.total_awarded: int = 0
        # current ship
        self.ship: str | None = None
        self.ship_internal: str | None = None
        self.ship_ts: str | None = None
        self.in_seat: bool = False
        # multicrew: a ship boarded as crew on ANOTHER player's vessel (the displayed
        # ship while aboard; pilot detection only ever names your own ship)
        self.boarded_ship: str | None = None
        self.boarded_owner: str | None = None
        self.ships_used: set[str] = set()
        # called with `self` right before a session is cleared (login/logout/relaunch),
        # so the ending session can be archived. Set by the entry point.
        self.on_session_end = None
        # live archive upsert: on_archive(self) re-snapshots the CURRENT (still-running)
        # session whenever something finishes, so a completed contract / trade lands in
        # the Archive immediately rather than only at session end. Coalesced: a dirty
        # flag is set as terminal-mission/trade/award counts change, and the tailer
        # flushes it at most once per read batch (see maybe_archive). Set by the entry point.
        self.on_archive = None
        self._archive_dirty = False
        self._archive_sig = (0, 0, 0)  # (#trades, awarded, #finished missions)
        # most recent zoneHostId epoch (high bits) seen; deliberately survives
        # reset() since the server build spans sessions. on_epoch_change(prev, new)
        # fires when it changes (a new server build) so the service can prune
        # now-stale station/override rows. Set by the entry point.
        self.current_epoch: int | None = None
        self.on_epoch_change = None
        # missions that ended recently and still await an award line (FIFO)
        self._pending_award: deque[str] = deque()

    # -- helpers --------------------------------------------------------- #
    def _m(self, mid: str) -> Mission:
        m = self.missions.get(mid)
        if m is None:
            m = Mission(mission_id=mid)
            self.missions[mid] = m
        return m

    def reset(self, *, full: bool = False) -> None:
        """Clear per-session state. `full=True` (new log file) wipes everything;
        otherwise the account/player name is kept."""
        with self.lock:
            # archive the session about to be cleared, if it had any activity
            if self.on_session_end and (self.missions or self.total_awarded
                                        or self.trades or self.blueprints):
                try:
                    self.on_session_end(self)
                except Exception as e:
                    print(f"[archive] session-end hook failed: {e}")
            self.missions.clear()
            self.trades.clear()
            self.blueprints.clear()
            self.kiosk_names.clear()
            self.travel_routes.clear()
            self.travel_arrivals.clear()
            self.zone_names.clear()
            self.salvage_targets.clear()
            self.total_awarded = 0
            self._pending_award.clear()
            self.ship = None
            self.ship_internal = None
            self.ship_ts = None
            self.location = None
            self.in_seat = False
            self.boarded_ship = None
            self.boarded_owner = None
            self.ships_used.clear()
            # the ending session was just archived (above); the cleared session has no
            # finished work, so reset the dirty tracking to match -- otherwise the next
            # fed line would see the signature "change" back to zero and re-flush an
            # empty session.
            self._archive_dirty = False
            self._archive_sig = (0, 0, 0)
            if full:
                self.player = None
                self.session_started_at = None
                self.session_gamerules = None
                self.session_boundary_ts = None
                self.logged_in = False
                self.game_running = False

    # -- ingest ---------------------------------------------------------- #
    # Real Game.log lines are a single event, far under this; a longer line is corrupt or
    # hostile. Skip it so no pattern runs over an attacker-sized string (DoS backstop).
    _MAX_LINE = 64 * 1024

    def feed(self, line: str) -> None:
        if len(line) > self._MAX_LINE:
            return
        ts_m = patterns.TS.search(line)
        ts = ts_m.group("ts") if ts_m else None

        with self.lock:
            if ts:
                self.last_event_ts = ts
            self._dispatch(line, ts)
            self._update_archive_dirty()

    def _dispatch(self, line: str, ts: str | None) -> None:
        if self._session_boundary(line, ts):
            return
        if self._shutdown(line, ts):
            return
        if self.session_started_at is None and ts:
            self.session_started_at = ts
        if self._version(line):
            return
        if self._ship(line, ts):
            return
        if self._salvage_spawn(line, ts):
            return
        if self._trade(line, ts):
            return
        if self._travel(line, ts):
            return
        if self._blueprint(line, ts):
            return
        self._mission(line, ts)

    def _update_archive_dirty(self) -> None:
        """Flag the session for a live archive upsert when its finished work changes --
        a mission reaching a terminal state, a new trade, or an award. Mere acceptance or
        in-progress objective updates don't move the signature, so we don't write for them.
        Called under self.lock at the end of every fed line.
        Locked by tests/test_live_archive.py (accept doesn't flush; completions coalesce)."""
        sig = (len(self.trades), self.total_awarded,
               sum(1 for m in self.missions.values() if m.status in _TERMINAL_STATUSES))
        if sig != self._archive_sig:
            self._archive_sig = sig
            self._archive_dirty = True

    def maybe_archive(self) -> None:
        """Flush a pending live archive upsert (called once per tailer read batch, so a
        burst of completions in one batch is a single write). No-op unless something
        finished. Runs the write outside the lock -- the tailer thread that calls this is
        the only writer, so there's no concurrent mutation to guard against."""
        with self.lock:
            ready = (self._archive_dirty and self.on_archive
                     and (self.missions or self.total_awarded or self.trades))
            self._archive_dirty = False
            cb = self.on_archive if ready else None
        if cb:
            try:
                cb(self)
            except Exception as e:
                print(f"[archive] live upsert failed: {e}")

    def bump_version(self) -> None:
        """Advance the snapshot version and wake any SSE streamers waiting on it."""
        with self.version_cv:
            self.version += 1
            self.version_cv.notify_all()

    def set_screen_locked(self, locked: bool) -> None:
        """Record the desktop screen-lock state (from the screenlock watcher thread) and wake
        streamers so the dashboard can auto-pause/resume the jukebox. Cross-thread -> guarded."""
        locked = bool(locked)
        with self.lock:
            if self.screen_locked == locked:
                return
            self.screen_locked = locked
        self.bump_version()

    # -- handlers -------------------------------------------------------- #
    def _session_boundary(self, line: str, ts: str | None) -> bool:
        m = patterns.SESSION.search(line)
        if not m:
            return False
        gr = m.group("gr")
        self.session_gamerules = gr
        # Any session boundary -- main menu (SC_Frontend) or PU (SC_Default) -- means the game
        # process is up. Stays True across a quit-to-menu; only FastShutdown clears it.
        self.game_running = True
        if gr == "SC_Frontend":
            # back to main menu == logged out == all missions abandoned. Reset
            # ONLY here (dedup by ts so an establisher burst counts once). The
            # frontend-dedup and SC_Default-relaunch-keeps-missions rules are locked
            # by tests/test_state.py (test_frontend_boundary_resets_once_per_timestamp,
            # test_relaunch_into_pu_does_not_reset).
            if ts != self.session_boundary_ts:
                self.reset()  # keeps player name
                self.session_boundary_ts = ts
            self.logged_in = False
            self.session_started_at = None
        else:
            # logged into the PU. Do NOT reset -- on a relaunch the game restores
            # missions right around this establisher; resetting would wipe them.
            self.logged_in = True
            if self.session_started_at is None:
                self.session_started_at = ts
        return True

    def _shutdown(self, line: str, ts: str | None) -> bool:
        """Clean quit-to-desktop: archive + reset like a logout. The game writes no
        SC_Frontend boundary on this path, so without it the session would linger
        until the next launch rotated the log. Guarded on logged_in (and dedup'd by
        that flag) so a stray re-match can't double-archive or wipe a fresh login.
        Locked by tests/test_state.py::test_fastshutdown_archives_once_not_twice."""
        if not patterns.SHUTDOWN.search(line):
            return False
        if (self.logged_in or self.missions or self.total_awarded
                or self.trades or self.blueprints):
            self.reset()  # keeps player name; fires the session-end (archive) hook
        self.logged_in = False
        self.game_running = False  # clean quit-to-desktop: the game process is gone
        self.session_started_at = None
        return True

    def _version(self, line: str) -> bool:
        if self.game_version is None:
            m = patterns.VERSION.search(line)
            if m:
                self.game_version = m.group(1)
                return True
        if self.game_build is None:
            m = patterns.CHANGELIST.search(line)
            if m:
                self.game_build = m.group(1)
                return True
        return False

    def _ship(self, line: str, ts: str | None) -> bool:
        # pilot-seat control is authoritative ("Local client node" == this player).
        # It only ever names YOUR OWN ship, so any such event also means you're back on
        # your own ship -> stop showing a boarded one.
        m = patterns.VEHICLE_CTRL.search(line)
        if m:
            self.ship_internal = m.group("ent")
            self.ship = patterns.friendly_ship(m.group("ent"))
            self.ship_ts = ts
            self.in_seat = m.group("act") == "SetDriver"
            self.ships_used.add(self.ship)
            self._clear_boarded()
            return True
        # multicrew: boarding another player's ship as crew joins its comms channel,
        # named '<Ship> : <Owner>'. Owner != you AND a recognizable ship -> show that
        # ship; rejoining your OWN ship's channel reverts. (Pilot detection never names
        # a ship you only crew on.) Falls through to the SHIP_CHANNEL fallback below.
        m = patterns.CHANNEL_JOIN.search(line)
        if m and self.player:
            owner = m.group("owner").strip()
            if owner == self.player:
                self._clear_boarded()
            else:
                ship = patterns.resolve_ship_name(m.group("ship"))
                if ship:
                    self.boarded_ship = ship
                    self.boarded_owner = owner
                    self.ship_ts = ts
        # left a channel -> if it's the ship we're aboard, we've disembarked
        m = patterns.CHANNEL_LEAVE.search(line)
        if m and self.boarded_ship and self.boarded_ship == patterns.resolve_ship_name(m.group("ship")):
            self._clear_boarded()
            return True
        # comms channel join names the ship in plain text -- fallback only, before
        # any vehicle-control event (channel entries can be stale / non-ship).
        m = patterns.SHIP_CHANNEL.search(line)
        if m:
            self.player = self.player or m.group("player").strip()
            if self.ship_internal is None:
                # the channel carries the manufacturer-prefixed marketing name;
                # normalize so it doesn't duplicate the seat-detected canonical name
                self.ship = patterns.canonical_ship_name(m.group("ship").strip())
                self.ship_ts = ts
                self.in_seat = True
                self.ships_used.add(self.ship)
            return True
        return False

    def _clear_boarded(self) -> None:
        self.boarded_ship = None
        self.boarded_owner = None

    def _salvage_spawn(self, line: str, ts: str | None) -> bool:
        """Record a salvageable wreck from its resource-host spawn line. A single wreck logs
        several host lines (one per child tank) at the same entity id, so dedupe by id; the
        line names no MissionId, so this is just a session sighting (see model.SalvageTarget)."""
        m = patterns.SALVAGE_SPAWN.search(line)
        if not m:
            return False
        base, eid = m.group("base"), m.group("eid")
        t = self.salvage_targets.get(base)
        if t is None:
            t = SalvageTarget(ship_class=base, first_seen=ts)
            self.salvage_targets[base] = t
        t.entity_ids.add(eid)
        t.last_seen = ts
        return True

    def _trade(self, line: str, ts: str | None) -> bool:
        """Record a manual commodity-terminal buy/sell. The request line is the only
        record (no settle/confirm follow-up), so it's treated as effective. SCU comes
        from the box data (boxSize x unitAmount), authoritative for both directions.
        Keyed by ts|action|guid|shop so re-feeding the log upserts, not duplicates.
        Locked by tests/test_trades.py (SCU derivation + idempotent refeed).

        Also learns kioskId -> place from the kiosk-binding line (logged when the kiosk
        is opened, before the transaction), which names the station ("Cordys") the
        shop name ("Admin") doesn't."""
        mk = patterns.KIOSK_BIND.search(line)
        if mk:
            place = patterns.friendly_kiosk(mk.group("ent"))
            if place:
                self.kiosk_names[mk.group("kid")] = place
            return True
        for action, pat in (("buy", patterns.TRADE_BUY), ("sell", patterns.TRADE_SELL)):
            m = pat.search(line)
            if not m:
                continue
            guid = m.group("guid").lower()
            shop = m.group("shop")
            scu = round(float(m.group("box")) * int(m.group("units")))
            tid = f"{ts}|{action}|{guid}|{shop}"
            self.trades[tid] = Trade(
                trade_id=tid, action=action, commodity_guid=guid, scu=scu,
                auec=round(float(m.group("auec"))), shop=shop,
                shop_label=patterns.friendly_shop(shop), ts=ts,
                station=self.kiosk_names.get(m.group("kiosk")),
            )
            return True
        return False

    def _travel(self, line: str, ts: str | None) -> bool:
        """Collect quantum-travel route calcs (named start -> destination code) and
        arrivals; build_session_travels pairs them into completed jumps."""
        m = patterns.QT_ROUTE.search(line)
        if m:
            self.travel_routes.append({"ts": ts, "ship": m.group("ship"),
                                       "frm": m.group("frm").strip(), "to": m.group("to"),
                                       "fuel": None})
            return True
        m = patterns.QT_FUEL.search(line)
        if m:
            # the fuel-estimate line follows its route line (same ts); attach to it
            for r in reversed(self.travel_routes):
                if r["ship"] == m.group("ship") and r["to"] == m.group("to") and r.get("fuel") is None:
                    r["fuel"] = round(float(m.group("fuel")))
                    break
            return True
        m = patterns.QT_ARRIVED.search(line)
        if m:
            self.travel_arrivals.append({"ts": ts, "ship": m.group("ship")})
            return True
        return False

    def _blueprint(self, line: str, ts: str | None) -> bool:
        """Record a crafting blueprint the player acquired, from its "Received Blueprint:"
        HUD notification. Anchored (in the pattern) on the SHUDEvent "Added notification"
        form so the UI-lifecycle echoes and the bare re-print don't double-count -- one
        match per acquisition. Deduped within the session by normalized name (earliest ts
        kept); the cumulative cross-session union lives in acquired.merge_acquired."""
        m = patterns.BLUEPRINT_RECEIVED.search(line)
        if not m:
            return False
        name = m.group("name").strip()
        if not name:
            return True
        key = patterns.norm_bp_name(name)
        rec = self.blueprints.get(key)
        if rec is None:
            self.blueprints[key] = {"name": name, "acquired_at": ts}
        elif ts and (not rec["acquired_at"] or ts < rec["acquired_at"]):
            rec["acquired_at"] = ts
        return True

    def _mission(self, line: str, ts: str | None) -> None:
        m = patterns.MARKER.search(line)
        if m:
            self._marker(m, ts)
            return

        m = patterns.DELIVER.search(line)
        if m:
            self._objective_text(m, "dropoff")
            return

        m = patterns.COLLECT.search(line)
        if m:
            self._objective_text(m, "pickup")
            return

        # Mining-contract objectives (Shubin purchase orders) -- a set of ore counts, an
        # "any one of" marker, and a "Go to <place>" start step, instead of pickup/dropoff legs.
        m = patterns.MINING_ORE.search(line)
        if m:
            mis = self._m(m.group("mid"))
            ore = m.group("ore").strip()
            mis.ores[ore] = OreReq(ore=ore, have=int(m.group("have")), need=int(m.group("need")))
            return

        m = patterns.MINING_ANY.search(line)
        if m:
            self._m(m.group("mid")).ore_any = True
            return

        m = patterns.MINING_GOTO.search(line)
        if m:
            mis = self._m(m.group("mid"))
            if not mis.mining_goto:
                mis.mining_goto = m.group("loc").strip()
            return

        m = patterns.PLAYER_LOCATION.search(line)
        if m:
            # the client only requests its own inventory, but guard on the player
            # name when we know it. A precise station fix always wins; a body-level
            # fix only updates when it's a *different* body than where we are (so a
            # vague orbital code doesn't downgrade a known station).
            if not self.player or m.group("player").strip() == self.player:
                name, is_station = patterns.decode_location(m.group("loc"))
                if name and (is_station or classify_station(self.location)[1] != name):
                    self.location = name
            return

        m = patterns.ACCEPTED.search(line)
        if m:
            mis = self._m(m.group("mid"))
            mis.title = patterns.clean_title(m.group("title"))
            mis.accepted_at = ts
            if mis.status not in ("completed", "failed", "abandoned", "expired"):
                mis.status = "active"
            return

        m = patterns.OBJ_UPSERT.search(line)
        if m:
            mis = self.missions.get(m.group("mid"))
            if mis and m.group("oid") in mis.legs and m.group("state") == "COMPLETED":
                mis.legs[m.group("oid")].state = "completed"
            return

        m = patterns.END_MISSION.search(line)
        if m:
            mis = self._m(m.group("mid"))
            self.player = self.player or m.group("player")
            mis.ended_at = ts
            mis.completion_type = m.group("ctype")
            mis.reason = m.group("reason")
            mis.status = patterns.classify_end(m.group("ctype"), m.group("reason"))
            if mis.status == "completed":  # only completed missions pay out (test_state.py)
                self._pending_award.append(mis.mission_id)
            return

        m = patterns.MISSION_ENDED.search(line)
        if m:
            mis = self._m(m.group("mid"))
            if mis.status == "active":
                mis.status = patterns.classify_end(m.group("state"))
            mis.ended_at = mis.ended_at or ts
            return

        m = patterns.COMPLETE_NOTE.search(line)
        if m:
            mis = self._m(m.group("mid"))
            if not mis.title:
                mis.title = patterns.clean_title(m.group("title"))
            mis.status = "completed"
            return

        m = patterns.FAILED_NOTE.search(line)
        if m:
            mis = self._m(m.group("mid"))
            if not mis.title:
                mis.title = patterns.clean_title(m.group("title"))
            mis.status = "failed"
            mis.ended_at = mis.ended_at or ts
            return

        m = patterns.ABANDONED_NOTE.search(line)
        if m:
            mis = self._m(m.group("mid"))
            if not mis.title:
                mis.title = patterns.clean_title(m.group("title"))
            mis.status = "abandoned"
            mis.ended_at = mis.ended_at or ts
            return

        m = patterns.AWARD.search(line)
        if m:
            amt = int(m.group("amt"))
            self.total_awarded += amt
            if self._pending_award:
                tgt = self.missions.get(self._pending_award.popleft())
                if tgt:
                    tgt.reward = (tgt.reward or 0) + amt

    def _marker(self, m: re.Match, ts: str | None) -> None:
        mis = self._m(m.group("mid"))
        mis.org = m.group("gen")
        mis.contract = m.group("contract")
        mis.contract_def_id = m.group("cdef")
        if mis.accepted_at is None:
            mis.accepted_at = ts
        oid = m.group("oid")
        kind = "pickup" if oid.startswith("pickup") else "dropoff"
        zone = m.group("zone")
        pos = (float(m.group("x")), float(m.group("y")), float(m.group("z")))
        self._note_epoch(zone)

        # A single-destination contract carrying several commodities (e.g.
        # HaulCargo_AToB_RefinedOre_Mixed_AluminiumTungstenCorundum_...) delivers each
        # commodity as its own objective -- dropoff_<phase>_0 / _1 / _2 -- to the SAME
        # station. When the game logs the per-commodity "Deliver" text those become
        # distinct legs naturally (each line carries its own ObjectiveId). But it often
        # drops that text (notification-queue overflow) and logs a CreateMarker for only
        # _0, leaving one combined leg. Expand that lone dropoff marker into one leg per
        # decoded commodity, keyed by the real per-commodity objective ids (so completion
        # events and any later Deliver text map straight in), all sharing this zone.
        # Multi-DESTINATION contracts (SingleToMulti*) have a distinct zone per drop, so
        # they're excluded -- their per-oid markers/text already split correctly.
        # Locked by tests/test_state.py (test_multicommodity_marker_expands_to_one_leg_per_commodity,
        # test_singletomulti_marker_is_not_expanded).
        cargos = patterns.decode_cargo_from_contract(mis.contract)
        if kind == "dropoff" and len(cargos) > 1 and "SingleToMulti" not in mis.contract:
            base = oid.rsplit("_", 1)[0]
            for i, cargo in enumerate(cargos):
                key = f"{base}_{i}"
                leg = mis.legs.get(key) or Leg(objective_id=key, kind="dropoff")
                leg.kind = "dropoff"
                leg.zone_host_id = zone
                leg.pos = pos
                if not leg.cargo:  # placeholder until/unless Deliver text confirms it
                    leg.cargo = cargo
                mis.legs[key] = leg
                if zone and leg.location:
                    self.zone_names[zone] = leg.location
            return

        leg = mis.legs.get(oid) or Leg(objective_id=oid, kind=kind)
        leg.kind = kind
        leg.zone_host_id = zone
        leg.pos = pos
        mis.legs[oid] = leg
        if zone and leg.location:  # text already seen -> learn now
            self.zone_names[zone] = leg.location

    def _note_epoch(self, zone_id: str | None) -> None:
        """Track the server-build epoch (zoneHostId high bits) and fire
        on_epoch_change when it shifts. The callback runs under self.lock, so it
        must be cheap (the entry point just signals a worker thread)."""
        if not zone_id:
            return
        try:
            ep = int(zone_id) >> 32
        except ValueError:
            return
        if ep == self.current_epoch:
            return
        prev, self.current_epoch = self.current_epoch, ep
        if prev is not None and self.on_epoch_change:
            self.on_epoch_change(prev, ep)

    def _objective_text(self, m: re.Match, kind: str) -> None:
        mis = self._m(m.group("mid"))
        oid = m.group("oid")
        leg = mis.legs.get(oid) or Leg(objective_id=oid, kind=kind)
        leg.kind = kind
        leg.cargo = m.group("cargo").strip()
        leg.qty = int(m.group("need"))
        leg.have = int(m.group("have"))
        leg.location = m.group("loc").strip()
        mis.legs[oid] = leg
        if leg.zone_host_id and leg.location:  # learn zone -> name
            self.zone_names[leg.zone_host_id] = leg.location
