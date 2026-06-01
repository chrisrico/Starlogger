"""In-memory mission store fed line-by-line from the game log."""

from __future__ import annotations

import re
import threading

from . import patterns
from .model import Leg, Mission
from .planner import classify_station


class State:
    """Thread-safe mission store. `feed(line)` ingests one log line."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.missions: dict[str, Mission] = {}
        self.zone_names: dict[str, str] = {}  # zoneHostId -> station name
        self.player: str | None = None
        self.location: str | None = None  # current station (where the client last requested inventory)
        self.session_started_at: str | None = None
        self.session_gamerules: str | None = None
        self.session_boundary_ts: str | None = None
        self.logged_in: bool = False
        self.game_version: str | None = None  # e.g. "4.8.0"
        self.game_build: str | None = None    # changelist
        self.last_event_ts: str | None = None
        self.total_awarded: int = 0
        # current ship
        self.ship: str | None = None
        self.ship_internal: str | None = None
        self.ship_ts: str | None = None
        self.in_seat: bool = False
        self.ships_used: set[str] = set()
        # called with `self` right before a session is cleared (login/logout/relaunch),
        # so the ending session can be archived. Set by the entry point.
        self.on_session_end = None
        # most recent zoneHostId epoch (high bits) seen; deliberately survives
        # reset() since the server build spans sessions. on_epoch_change(prev, new)
        # fires when it changes (a new server build) so the service can prune
        # now-stale station/override rows. Set by the entry point.
        self.current_epoch: int | None = None
        self.on_epoch_change = None
        # missions that ended recently and still await an award line
        self._pending_award: list[str] = []

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
            if self.on_session_end and (self.missions or self.total_awarded):
                try:
                    self.on_session_end(self)
                except Exception as e:
                    print(f"[archive] session-end hook failed: {e}")
            self.missions.clear()
            self.zone_names.clear()
            self.total_awarded = 0
            self._pending_award.clear()
            self.ship = None
            self.ship_internal = None
            self.ship_ts = None
            self.location = None
            self.in_seat = False
            self.ships_used.clear()
            if full:
                self.player = None
                self.session_started_at = None
                self.session_gamerules = None
                self.session_boundary_ts = None
                self.logged_in = False

    # -- ingest ---------------------------------------------------------- #
    def feed(self, line: str) -> None:
        ts_m = patterns.TS.search(line)
        ts = ts_m.group("ts") if ts_m else None

        with self.lock:
            if ts:
                self.last_event_ts = ts

            if self._session_boundary(line, ts):
                return
            if self.session_started_at is None and ts:
                self.session_started_at = ts
            if self._version(line):
                return
            if self._ship(line, ts):
                return
            self._mission(line, ts)

    # -- handlers -------------------------------------------------------- #
    def _session_boundary(self, line: str, ts: str | None) -> bool:
        m = patterns.SESSION.search(line)
        if not m:
            return False
        gr = m.group("gr")
        self.session_gamerules = gr
        if gr == "SC_Frontend":
            # back to main menu == logged out == all missions abandoned. Reset
            # ONLY here (dedup by ts so an establisher burst counts once).
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
        # pilot-seat control is authoritative ("Local client node" == this player)
        m = patterns.VEHICLE_CTRL.search(line)
        if m:
            self.ship_internal = m.group("ent")
            self.ship = patterns.friendly_ship(m.group("ent"))
            self.ship_ts = ts
            self.in_seat = m.group("act") == "SetDriver"
            self.ships_used.add(self.ship)
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
            if mis.status == "completed":  # only completed missions pay out
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
                tgt = self.missions.get(self._pending_award.pop(0))
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
        leg = mis.legs.get(oid) or Leg(objective_id=oid, kind=kind)
        leg.kind = kind
        leg.zone_host_id = m.group("zone")
        leg.pos = (float(m.group("x")), float(m.group("y")), float(m.group("z")))
        mis.legs[oid] = leg
        self._note_epoch(leg.zone_host_id)
        if leg.zone_host_id and leg.location:  # text already seen -> learn now
            self.zone_names[leg.zone_host_id] = leg.location

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
