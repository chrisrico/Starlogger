"""Build the JSON snapshot the dashboard polls: mission list plus the
loading/unloading/route views, grouped to help load and unload cargo.

`trade_only=True` restricts everything to cargo-hauling/trade missions."""

from __future__ import annotations

import subprocess
import time
from collections import defaultdict
from dataclasses import asdict
from functools import lru_cache

from . import patterns
from .config import BASE_DIR
from .archive import build_session_trades, build_session_travels
from .mine_locations import mine_locations
from .reference import commodity_names, commodity_types, load_commodities, station_names
from .model import Leg, Mission
from .planner import BODY_ORDER, SYSTEM_ORDER, classify_station, plan_trip
from .overrides import apply_override, get_overrides
from .settings import get_settings
from .ships import (
    is_mining_ship, is_salvage_ship, load_ship_cargo, mining_hardpoints,
    ship_capacity, ship_grid, ship_layout,
)
from . import salvage_ships
from .tradeflags import lost_trade_ids
from .state import State
from .stations import get_station_names, learn_station_names


@lru_cache(maxsize=1)
def _app_version() -> str | None:
    """Short git commit hash of the running code, shown as the app version in the
    footer. Cached for the process lifetime — fine because the tracker is restarted
    on each game launch (sc-run.sh re-execs after self-updating), so it can't go
    stale within a session. None when BASE_DIR isn't a git checkout or git is absent."""
    try:
        out = subprocess.run(
            ["git", "-C", BASE_DIR, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


# Shown for a leg whose only location signal is the acceptance-host zone (see
# Mission.host_artifact_zones / has_pending_origin): we know an endpoint exists, just
# not which yet — the game reveals it via the objective text and the label self-heals.
PENDING_DEST = "Destination pending"
PENDING_ORIGIN = "Origin pending"
_PENDING = {PENDING_DEST, PENDING_ORIGIN}


def _unresolved(loc: str) -> bool:
    """A placeholder destination/origin that should sort after real stations."""
    return loc.startswith("Unknown station") or loc in _PENDING


# ---- shared origin / destination / zone resolution -------------------------------- #
# The host-artifact / pending / override rules below back BOTH the live snapshot and the
# server's same-route sibling detection (server.py). They operate on an *effective*
# (override-applied) Mission so the two call sites can't drift. See model.py for the
# host_artifact_zones / has_pending_origin / origin_zone properties they lean on.

def resolve_zone(zone_names: dict, zone: str | None) -> str:
    """A zoneHostId's display name: the learned/overridden name, else a placeholder that
    still shows the raw id when one exists, else a generic unknown."""
    if zone and zone in zone_names:
        return zone_names[zone]
    if zone:
        return f"Unknown station (zone {zone})"
    return "Unknown station"


def origin_label(mis: Mission, zone_names: dict) -> str:
    """A mission's displayed origin: an override origin (origin_name) wins, else
    'Origin pending' when the only pickup is a host artifact, else the pickup zone
    resolved through zone_names."""
    if mis.origin_name:
        return mis.origin_name
    if mis.has_pending_origin:
        return PENDING_ORIGIN
    return resolve_zone(zone_names, mis.origin_zone)


def dleg_label(mis: Mission, leg: Leg, zone_names: dict) -> str:
    """A dropoff leg's displayed destination: deliver text (leg.location) wins; an
    acceptance-host zone shared with the pickup isn't a real destination and shows as
    pending until the game reveals it; else resolve the zone."""
    if leg.location:
        return leg.location
    if leg.zone_host_id in mis.host_artifact_zones:
        return PENDING_DEST
    return resolve_zone(zone_names, leg.zone_host_id)


def dest_signature(mis: Mission, zone_names: dict) -> tuple:
    """A mission's destination signature: sorted, de-duplicated dropoff labels. Paired
    with origin_label to identify same-route siblings."""
    return tuple(sorted({dleg_label(mis, l, zone_names)
                         for l in mis.legs.values() if l.kind == "dropoff"}))


def _peak_load(active, origin_of, dleg_loc, committed_of, anchor=None) -> int:
    """Estimated peak simultaneous hold usage along a single-pass route.

    Each mission loads its committed SCU at its origin and drops it at its
    destination(s); we walk the stations in celestial order (system, body, moon)
    delivering before loading at each. The peak running load is what has to fit at
    once — so a back-haul (A->B plus B->A) peaks at the larger leg, not the sum.

    It's an estimate (the true peak depends on the order you actually fly), but the
    back-haul result is order-independent: max(A->B, B->A) whichever end you start.
    """
    load_at: dict[str, int] = defaultdict(int)
    drop_at: dict[str, int] = defaultdict(int)
    for mis in active:
        c = committed_of(mis)
        if c <= 0:
            continue
        load_at[origin_of(mis)] += c
        drops = [l for l in mis.legs.values()
                 if l.kind == "dropoff" and l.state != "completed"]
        assigned = 0
        for l in drops:
            if l.qty:
                drop_at[dleg_loc(l)] += l.qty
                assigned += l.qty
        leftover = c - assigned  # cargo whose Deliver qty isn't logged yet
        if leftover > 0:
            drop_at[dleg_loc(drops[0]) if drops else origin_of(mis)] += leftover

    # start the walk from the player's current body when known, so the first leg
    # flown is from where you actually are (matters for 3+ stop routes).
    anchor_body = classify_station(anchor)[1] if anchor else None

    def order_key(s: str):
        system, body, moon = classify_station(s)
        return (0 if body == anchor_body and anchor_body != "?" else 1,
                SYSTEM_ORDER.get(system, 9), BODY_ORDER.get(body, 9), body, moon or "", s)

    load = peak = 0
    for s in sorted(set(load_at) | set(drop_at), key=order_key):
        load = max(0, load - drop_at[s])  # deliver here, then pick up
        load += load_at[s]
        peak = max(peak, load)
    return peak


def _sorted_groups(groups: dict) -> list:
    # Loading is keyed by origin, unloading by destination; either way `location`
    # is the station to sort on. Alphabetical, case-insensitive, with unresolved
    # "Unknown station …" groups sorted to the end.
    out = [g for g in groups.values() if g["items"]]
    for g in out:
        # delivered (done) lines sink to the bottom of each card
        g["items"].sort(key=lambda i: (i.get("done", False), -(i["qty"] or 0), i["cargo"]))
    out.sort(key=lambda g: (_unresolved(g["location"]), g["location"].lower()))
    return out


# ---- build_snapshot pieces (pure helpers; the orchestrator below wires them together) ---- #

def _split_missions(state_missions: dict, overrides: dict):
    """Apply each mission's override, keep the cargo-ops missions the live dashboard shows --
    trade/hauling AND mining (Shubin purchase orders, which carry ore requirements instead of
    legs); couriers/combat still go to the Archive instead. Partition into the full list, the
    hidden (manually-deleted) ids, and the visible (non-hidden) subset."""
    missions: list[Mission] = []
    hidden_ids: set[str] = set()
    for m in state_missions.values():
        ov = overrides.get(m.mission_id)
        eff = apply_override(m, ov) if ov else m
        if not (eff.is_trade or eff.is_mining):
            continue
        if ov and ov.get("hidden"):
            hidden_ids.add(eff.mission_id)
        missions.append(eff)
    visible = [m for m in missions if m.mission_id not in hidden_ids]
    return missions, hidden_ids, visible


def _mission_label(mis: Mission) -> str:
    """Mission display label; the reward disambiguates same-titled contracts."""
    base = mis.title or mis.contract
    return f"{base} ({mis.reward:,} aUEC)" if mis.reward else base


def committed_scu(m: Mission) -> int:
    """SCU an active mission still owes (not merely what's loaded). Prefer the uncompleted
    Deliver quantities (authoritative, and they shrink as you deliver); fall back to the
    Collect quantities for a mission accepted-but-not-yet-loaded with no Deliver qty logged."""
    dp = sum(l.qty for l in m.legs.values()
             if l.kind == "dropoff" and l.qty and l.state != "completed")
    if dp:
        return dp
    return sum(l.qty for l in m.legs.values()
               if l.kind == "pickup" and l.qty and l.state != "completed")


# Cap the where-to-mine chips shown per ore on a contract card (a common gem is on every body).
_LOC_CAP = 8


# Which mining method a contract's title implies -> which body-mineables list its ores are
# found on (hand cave gems vs ship-mined ore vs ROC ground). Drives the where-to-mine join.
def _mining_method(title: str) -> str:
    t = (title or "").lower()
    if "hand mined" in t or "hand mining" in t:
        return "hand"
    if "ground vehicle" in t or "roc" in t:
        return "ground"
    return "ship"


def _mission_dict(mis: Mission, origin_of, dleg_loc, zone_names: dict,
                  hidden_ids: set, overrides: dict) -> dict:
    """Serialize one mission for the client: the dataclass plus derived origin/destinations,
    flags, and a best-guess resolved name per leg (so the editor pre-fills its rows). Mining
    contracts also get their ore requirements joined to where-to-mine locations."""
    d = asdict(mis)
    if mis.ores:
        method = _mining_method(mis.title)
        d["mining_method"] = method
        # Replace the asdict ore map with an ordered list, each ore joined to its mine locations.
        # Cap the chip list (a common gem like Aphorite is on every body -> 25 chips); keep the
        # full count so the card can show "+N more".
        ores = []
        for o in mis.ores.values():
            locs = mine_locations(o.ore, method)
            ores.append({"ore": o.ore, "have": o.have, "need": o.need,
                         "locations": locs[:_LOC_CAP], "loc_count": len(locs)})
        d["ores"] = ores
    d["decoded"] = mis.decoded
    d["origin"] = origin_of(mis)
    d["cargo_types"] = mis.cargo_types
    d["is_trade"] = mis.is_trade
    d["hidden"] = mis.mission_id in hidden_ids
    d["overridden"] = mis.mission_id in overrides
    d["raw_override"] = overrides.get(mis.mission_id)
    drops = [l for l in mis.legs.values() if l.kind == "dropoff"]
    d["partial"] = bool(drops) and any(not (l.cargo and l.qty) for l in drops)
    d["destinations"] = sorted({dleg_loc(l) for l in drops if dleg_loc(l)})
    for ld in d["legs"].values():
        z = ld.get("zone_host_id")
        ld["name"] = ld.get("location") or (zone_names.get(z) if z else None) or ""
    return d


def _counts(visible: list, active: list, mission_dicts: list, hidden_ids: set) -> dict:
    return {
        "active": len(active),
        "partial": sum(1 for d in mission_dicts
                       if d["status"] == "active" and d["partial"] and not d["hidden"]),
        "completed": sum(1 for m in visible if m.status == "completed"),
        "abandoned": sum(1 for m in visible if m.status == "abandoned"),
        "failed": sum(1 for m in visible if m.status in ("failed", "expired")),
        "hidden": len(hidden_ids),
        "total": len(visible),
    }


def _autocomplete_catalog(missions: list, zone_names: dict) -> dict:
    """Editor autocomplete: known station names (zone map + p4k catalog + anything seen
    this session), cargo names (p4k commodity list + canonical fallback + live), and the
    commodity-name -> category taxonomy (T1) so the UI can group/colour cargo by type."""
    stations = set(zone_names.values()) | set(station_names())
    cargo_names = set(patterns.COMMODITY_NAMES) | set(commodity_names())
    for mis in missions:
        for leg in mis.legs.values():
            if leg.location:
                stations.add(leg.location)
            if leg.cargo:
                cargo_names.add(leg.cargo)
    # commodity_types is keyed by GUID; the UI works in names, so join via the GUID->name map.
    guid_name = load_commodities()
    cargo_types = {guid_name[g]: c for g, c in commodity_types().items() if g in guid_name}
    return {"stations": sorted(stations), "cargo": sorted(cargo_names),
            "cargo_types": cargo_types}


def _detected_salvage(state: State, salvage_db: dict) -> list:
    """Salvageable wrecks sighted this session (state.salvage_targets) -> a list for the
    Salvage mode's Ship-ID panel, each decorated with its removable components from the
    salvage-ship catalog. ``resolved`` is False (and ``components`` empty) when the catalog
    has no entry for that hull yet (not built, or a brand-new ship). Newest sighting first."""
    out = []
    for t in state.salvage_targets.values():
        entry = salvage_db.get(t.ship_class.lower())
        out.append({
            "ship_class": t.ship_class,
            "name": (entry or {}).get("name") or t.ship_class,
            "manufacturer": (entry or {}).get("manufacturer"),
            "count": t.count,
            "first_seen": t.first_seen,
            "last_seen": t.last_seen,
            "resolved": entry is not None,
            "components": (entry or {}).get("components") or [],
        })
    out.sort(key=lambda d: (d["last_seen"] or ""), reverse=True)
    return out


def build_snapshot(state: State, trade_only: bool = False, overlay: dict | None = None) -> dict:
    # `overlay` (replay/archive editing) supplies an EPHEMERAL edit set —
    # {overrides, station_names, lost, selected_ship} — used instead of the on-disk
    # stores, and suppresses every persistence side effect, so a past session can be
    # edited exactly like the live one without writing anything to disk.
    ephemeral = overlay is not None
    overrides = overlay.get("overrides") or {} if ephemeral else get_overrides()
    cargo_db = load_ship_cargo()
    selected_ship = overlay.get("selected_ship") if ephemeral else get_settings().get("selected_ship")
    ov_station = overlay.get("station_names") or {} if ephemeral else {}
    ov_lost = overlay.get("lost") if ephemeral else None

    with state.lock:
        # Persist names this session learned, then resolve against the union of
        # the persistent store (manual edits + everything learned before) and
        # this session's freshly-learned names (the live truth wins on conflict).
        # An overlay edit (archive rename) wins over all of it; ephemeral runs never persist.
        if not ephemeral:
            learn_station_names(state.zone_names)
        zone_names = {**get_station_names(), **state.zone_names, **ov_station}
        # apply overrides, drop non-trade, split into hidden vs visible
        missions, hidden_ids, visible = _split_missions(state.missions, overrides)

        # The route helpers below call dleg_loc(leg) with a leg only, but the shared
        # dleg_label needs the owning mission (for host_artifact_zones). objective_ids are
        # per-instance UUIDs (globally unique), so a leg->mission map lets the thin closure
        # stay leg-only while delegating the host-artifact/pending rules to dleg_label.
        leg_mis = {leg.objective_id: mis for mis in missions
                   for leg in mis.legs.values()}

        def dleg_loc(leg: Leg) -> str:
            return dleg_label(leg_mis[leg.objective_id], leg, zone_names)

        def origin_of(mis: Mission) -> str:
            return origin_label(mis, zone_names)

        # mission list (all missions, incl. hidden), sorted by acceptance time
        mission_dicts = [_mission_dict(mis, origin_of, dleg_loc, zone_names, hidden_ids, overrides)
                         for mis in sorted(missions, key=lambda x: x.accepted_at or "")]

        active = [m for m in visible if m.status == "active"]

        load = _build_loading(active, origin_of, dleg_loc, _mission_label)
        unload, routes = _build_unloading_routes(active, origin_of, dleg_loc, _mission_label)
        plan = plan_trip(active, origin_of, dleg_loc, current=state.location)

        counts = _counts(visible, active, mission_dicts, hidden_ids)
        active_scu = sum(committed_scu(m) for m in active)
        # Peak simultaneous hold usage (what actually has to fit at once). Cargo is
        # loaded at each mission's origin and dropped at its destination, so a
        # back-haul (A->B plus B->A) peaks at the larger leg, not their sum.
        peak_scu = _peak_load(active, origin_of, dleg_loc, committed_scu, anchor=state.location)
        # session income; when filtering to trade, sum the trade missions' rewards
        earned = sum(m.reward for m in visible if m.reward) if trade_only else state.total_awarded

        # manual commodity-terminal trades this session (buy/sell), with a rollup
        trades, trade_summary = build_session_trades(state)

        # Effective ship: a ship boarded as crew on another player's vessel wins while
        # aboard (the dashboard then shows that ship's hold for the shared haul); else
        # the game-detected pilot ship; else the user's manual pick (settings.json).
        # Drives the capacity gauge and cargo-grid view even at the main menu.
        effective_ship = state.boarded_ship or state.ship or selected_ship

        return {
            "player": state.player,
            "location": state.location,
            "ship": effective_ship,
            "ship_detected": bool(state.ship),
            "selected_ship": selected_ship,
            # crewing another player's ship: effective_ship is theirs, badge it as such
            "boarded": bool(state.boarded_ship),
            "boarded_owner": state.boarded_owner,
            "ship_internal": state.ship_internal,
            "ship_ts": state.ship_ts,
            "ship_scu": ship_capacity(effective_ship, cargo_db),
            "ship_grid": ship_grid(effective_ship, cargo_db),
            "ship_layout": ship_layout(effective_ship, cargo_db),
            # True in a mining vehicle (Prospector/MOLE/ROC…): the dashboard then swaps
            # the cargo-ops tabs (loading/manifest/unloading/routes) for the Mining tab.
            "mining_ship": is_mining_ship(effective_ship, state.ship_internal, cargo_db),
            # The ship's mining-laser hardpoint sizes (Prospector [1], MOLE [2,2,2]); drives the
            # Identify tab's "can't crack → try this gear" suggester (it can only fit heads that
            # match a hardpoint, and flags when a rock needs a bigger mining ship entirely).
            "mining_hardpoints": mining_hardpoints(effective_ship, state.ship_internal, cargo_db),
            # True in a salvage vessel (Vulture/Reclaimer…): one trigger for the Salvage mode.
            "salvage_ship": is_salvage_ship(effective_ship, state.ship_internal, cargo_db),
            # Wrecks detected from the log this session, each with its removable components --
            # feeds the Salvage mode's Ship-ID panel (and, non-empty, also triggers the mode).
            "detected_salvage": _detected_salvage(state, salvage_ships.catalog()),
            "ship_cargo_updated": cargo_db.get("fetched_at"),
            "ship_cargo_version": cargo_db.get("game_version"),
            "game_version": state.game_version,
            "game_build": state.game_build,
            "app_version": _app_version(),
            "in_seat": state.in_seat,
            "session_started_at": state.session_started_at,
            "logged_in": state.logged_in,
            # Is the SC game process up? Log-derived fallback; the server overrides it with the
            # authoritative launcher-process state when the tracker launched the game. Drives the
            # jukebox auto-pause (web/jukebox.js jukeOnGameRunning).
            "game_running": state.game_running,
            "screen_locked": state.screen_locked,   # desktop locked -> jukebox auto-pause
            "session_gamerules": state.session_gamerules,
            "last_event_ts": state.last_event_ts,
            "total_awarded": earned,
            "trade_only": trade_only,
            "counts": counts,
            "active_scu": active_scu,
            "peak_scu": peak_scu,
            "loading": load,
            "unloading": unload,
            "routes": routes,
            "plan": plan,
            "missions": mission_dicts,
            "trades": trades,
            "trade_summary": trade_summary,
            "travels": build_session_travels(state),
            "lost_trades": ov_lost if ov_lost is not None else lost_trade_ids(),

            "catalog": _autocomplete_catalog(missions, zone_names),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


def _build_loading(active, origin_of, dleg_loc, mlabel) -> list:
    """Per origin station: what to load. Collect-style missions (named pickups)
    group by each pickup station; single-origin missions group by their origin."""
    load: dict[str, dict] = {}

    def grp(loc: str, zone: str | None = None) -> dict:
        g = load.setdefault(
            loc, {"location": loc, "zone": None, "total_scu": 0, "items": [], "has_partial": False}
        )
        if zone and not g["zone"]:
            g["zone"] = zone
        return g

    for mis in active:
        pickups = [l for l in mis.legs.values() if l.kind == "pickup"]
        drops = [l for l in mis.legs.values() if l.kind == "dropoff"]
        if pickups and all(l.state == "completed" for l in pickups):
            continue  # already loaded
        # Collect-style only when pickups carry their own cargo (genuine multi-
        # pickup "Collect N SCU of X from Y" objectives). A normal haul's pickup
        # leg may have a location (resolved zone / manual origin) but no cargo —
        # that must NOT hijack loading; its drops drive what to load.
        located_pickups = [l for l in pickups if l.location and l.cargo]

        if located_pickups:
            dests = ", ".join(sorted({dleg_loc(l) for l in drops if dleg_loc(l)})) or "—"
            for leg in located_pickups:
                if leg.state == "completed":
                    continue
                g = grp(dleg_loc(leg), leg.zone_host_id)
                g["total_scu"] += leg.qty or 0
                g["has_partial"] = g["has_partial"] or not leg.qty
                g["items"].append({
                    "cargo": leg.cargo or ", ".join(mis.cargo_types) or "Unknown cargo",
                    "qty": leg.qty, "to": dests, "mission": mlabel(mis),
                    "mission_id": mis.mission_id, "oid": leg.objective_id, "done": False,
                    "partial": not (leg.cargo and leg.qty),
                })
            continue

        g = grp(origin_of(mis), mis.origin_zone)
        # one row per cargo-bearing drop leg (so each commodity + its qty is editable
        # in place, even when the qty wasn't logged); fall back to the decoded cargo
        # list only when no leg carries a commodity at all.
        cargo_legs = [l for l in drops if l.cargo]
        if cargo_legs:
            for leg in cargo_legs:
                g["total_scu"] += leg.qty or 0
                g["has_partial"] = g["has_partial"] or not leg.qty
                g["items"].append({
                    "cargo": leg.cargo, "qty": leg.qty, "to": dleg_loc(leg),
                    "mission": mlabel(mis), "mission_id": mis.mission_id,
                    "oid": leg.objective_id,
                    "done": leg.state == "completed", "partial": not leg.qty,
                })
        else:
            g["has_partial"] = True
            dests = ", ".join(sorted({dleg_loc(l) for l in drops if dleg_loc(l)})) or "—"
            for cargo in (mis.cargo_types or ["Unknown cargo"]):
                g["items"].append({
                    "cargo": cargo, "qty": None, "to": dests, "mission": mlabel(mis),
                    "mission_id": mis.mission_id, "done": False, "partial": True,
                })

    return _sorted_groups(load)


def _build_unloading_routes(active, origin_of, dleg_loc, mlabel):
    """Per destination: what to drop. Plus the origin->destination route rollup."""
    unload: dict[str, dict] = {}
    routes: dict[tuple, dict] = {}

    for mis in active:
        origin = origin_of(mis)
        mis_cargo = ", ".join(mis.cargo_types) if mis.cargo_types else "Unknown cargo"
        for leg in mis.legs.values():
            if leg.kind != "dropoff":
                continue
            # completed drops stay listed in unloading (struck-through, so a
            # mistaken mark can be undone) but are excluded from to-deliver totals.
            done = leg.state == "completed"
            dest = dleg_loc(leg)
            detailed = bool(leg.cargo and leg.qty)
            # Prefer the leg's own commodity (multi-commodity contracts split into one
            # leg per commodity, some with qty still unknown); only fall back to the
            # whole-mission cargo list for a truly bare leg with no commodity at all.
            cargo = leg.cargo or mis_cargo
            qty = leg.qty if detailed else None

            g = unload.setdefault(
                dest, {"location": dest, "zone": None, "total_scu": 0, "items": [], "has_partial": False}
            )
            # a pending dropoff carries the acceptance-host zone (e.g. Baijini); don't
            # expose it for naming or it'd let the user mis-rename that real station.
            if leg.zone_host_id and not g["zone"] and dest != PENDING_DEST:
                g["zone"] = leg.zone_host_id
            if not done:
                g["total_scu"] += qty or 0
                g["has_partial"] = g["has_partial"] or not detailed
            g["items"].append({
                "cargo": cargo, "qty": qty, "from": origin, "mission": mlabel(mis),
                "mission_id": mis.mission_id, "oid": leg.objective_id,
                "partial": not detailed, "done": done,
            })

            # routes are the to-do rollup: skip delivered legs entirely.
            if done:
                continue
            r = routes.setdefault(
                (origin, dest),
                {"origin": origin, "destination": dest, "total_scu": 0,
                 "cargo": {}, "missions": set(), "has_partial": False,
                 "origin_zone": None, "dest_zone": None},
            )
            # zones back the inline station editor; suppress the acceptance-host zone
            # of a still-pending dropoff so it can't be mis-named.
            if mis.origin_zone and not r["origin_zone"]:
                r["origin_zone"] = mis.origin_zone
            if leg.zone_host_id and not r["dest_zone"] and dest != PENDING_DEST:
                r["dest_zone"] = leg.zone_host_id
            r["total_scu"] += qty or 0
            r["has_partial"] = r["has_partial"] or not detailed
            c = r["cargo"].setdefault(cargo, {"qty": 0, "legs": []})
            c["qty"] += qty or 0
            c["legs"].append({"mission_id": mis.mission_id, "oid": leg.objective_id})
            r["missions"].add(mis.mission_id)

    route_list = [
        {
            "origin": r["origin"], "destination": r["destination"],
            "origin_zone": r["origin_zone"], "dest_zone": r["dest_zone"],
            "total_scu": r["total_scu"], "has_partial": r["has_partial"],
            "cargo": [{"cargo": c, "qty": v["qty"], "legs": v["legs"]}
                      for c, v in sorted(r["cargo"].items())],
            "mission_count": len(r["missions"]),
        }
        for r in routes.values()
    ]
    # routes sorted by origin then destination (unresolved stations last)
    route_list.sort(key=lambda r: (
        _unresolved(r["origin"]), r["origin"].lower(),
        _unresolved(r["destination"]), r["destination"].lower(),
    ))
    return _sorted_groups(unload), route_list
