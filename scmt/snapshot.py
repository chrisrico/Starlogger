"""Build the JSON snapshot the dashboard polls: mission list plus the
loading/unloading/route views, grouped to help load and unload cargo.

`trade_only=True` restricts everything to cargo-hauling/trade missions."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import asdict

from . import patterns
from .model import Leg, Mission
from .planner import BODY_ORDER, SYSTEM_ORDER, classify_station, plan_trip
from .overrides import apply_override, get_overrides
from .settings import get_settings
from .shipcargo import load_ship_cargo, ship_capacity, ship_grid, ship_layout
from .state import State
from .stations import get_station_names, learn_station_names


# Shown for a leg whose only location signal is the acceptance-host zone (see
# Mission.host_artifact_zones / has_pending_origin): we know an endpoint exists, just
# not which yet — the game reveals it via the objective text and the label self-heals.
PENDING_DEST = "Destination pending"
PENDING_ORIGIN = "Origin pending"
_PENDING = {PENDING_DEST, PENDING_ORIGIN}


def _unresolved(loc: str) -> bool:
    """A placeholder destination/origin that should sort after real stations."""
    return loc.startswith("Unknown station") or loc in _PENDING


def _resolve(zone_names: dict, zone: str | None) -> str:
    if zone and zone in zone_names:
        return zone_names[zone]
    if zone:
        return f"Unknown station (zone {zone})"
    return "Unknown station"


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


def build_test_snapshot(live_state: State, scenario_missions: list) -> dict:
    """Build a FULL snapshot from synthetic scenario missions, reusing the live
    state's ship/location context, so a test scenario previews the *entire* dashboard
    (loading, unloading, routes, missions, counts) and not just the cargo grid.

    Each scenario mission is ``{title, origin, cargo, qty, dest}`` (or with a ``drops``
    list of ``{cargo, qty, dest}`` for multi-drop). Missions become real ``Mission``
    objects with a pickup leg at ``origin`` and a dropoff leg per drop, run through the
    same pipeline as live data."""
    temp = State()
    with live_state.lock:
        for attr in ("player", "location", "game_version", "game_build", "ship",
                     "ship_internal", "ship_ts", "in_seat", "session_started_at",
                     "session_gamerules", "last_event_ts"):
            setattr(temp, attr, getattr(live_state, attr, None))
        temp.zone_names = dict(live_state.zone_names)
    temp.logged_in = True   # a preview behaves as "in verse" so the whole UI lights up

    for i, sm in enumerate(scenario_missions or []):
        mid = f"tc{i}"
        m = Mission(
            mission_id=mid, title=sm.get("title") or "Direct Cargo Haul",
            contract="HaulCargo", status="active", origin_name=sm.get("origin"),
            reward=sm.get("reward") or (i + 1) * 1000,  # unique → distinct group labels
            accepted_at=f"{i:04d}",
        )
        if sm.get("origin"):
            m.legs["p"] = Leg(objective_id="p", kind="pickup", cargo=sm.get("cargo"),
                              qty=sm.get("qty"), location=sm.get("origin"))
        drops = sm.get("drops") or [{"cargo": sm.get("cargo"), "qty": sm.get("qty"),
                                     "dest": sm.get("dest")}]
        for j, dp in enumerate(drops):
            m.legs[f"d{j}"] = Leg(objective_id=f"d{j}", kind="dropoff",
                                  cargo=dp.get("cargo"), qty=dp.get("qty"),
                                  location=dp.get("dest"))
        temp.missions[mid] = m
    return build_snapshot(temp)


def build_snapshot(state: State, trade_only: bool = False) -> dict:
    overrides = get_overrides()
    cargo_db = load_ship_cargo()
    selected_ship = get_settings().get("selected_ship")

    with state.lock:
        # Persist names this session learned, then resolve against the union of
        # the persistent store (manual edits + everything learned before) and
        # this session's freshly-learned names (the live truth wins on conflict).
        learn_station_names(state.zone_names)
        zone_names = {**get_station_names(), **state.zone_names}
        missions: list[Mission] = []
        hidden_ids: set[str] = set()
        for m in state.missions.values():
            ov = overrides.get(m.mission_id)
            eff = apply_override(m, ov) if ov else m
            # The live dashboard is cargo-ops only: non-trade missions (couriers,
            # combat, etc.) never appear in loading/unloading/routes/manifest or
            # the header counts. They're recorded in the Archive instead, where the
            # Trade-only toggle decides whether to include them.
            if not eff.is_trade:
                continue
            if ov and ov.get("hidden"):
                hidden_ids.add(eff.mission_id)
            missions.append(eff)
        # hidden (manually deleted) missions stay listed but are excluded from the
        # active/loading/unloading/route views and counts.
        visible = [m for m in missions if m.mission_id not in hidden_ids]

        # Dropoff legs whose only location signal is the acceptance-host zone (shared
        # with the mission's pickup) — not a real destination. Keyed by objective id
        # (per-instance UUIDs, globally unique) so dleg_loc can suppress without a
        # mission handle. Drops out the moment deliver text sets leg.location.
        pending_drops = {
            leg.objective_id
            for mis in missions
            for leg in mis.legs.values()
            if leg.kind == "dropoff" and not leg.location
            and leg.zone_host_id in mis.host_artifact_zones
        }

        def dleg_loc(leg: Leg) -> str:
            if leg.location:
                return leg.location
            if leg.objective_id in pending_drops:
                return PENDING_DEST
            return _resolve(zone_names, leg.zone_host_id)

        def origin_of(mis: Mission) -> str:
            if mis.origin_name:
                return mis.origin_name
            if mis.has_pending_origin:
                return PENDING_ORIGIN
            return _resolve(zone_names, mis.origin_zone)

        def mlabel(mis: Mission) -> str:
            # include the reward so same-titled contracts are distinguishable
            base = mis.title or mis.contract
            return f"{base} ({mis.reward:,} aUEC)" if mis.reward else base

        # ---- mission list (all missions, incl. hidden) ---- #
        mission_dicts = []
        for mis in sorted(missions, key=lambda x: x.accepted_at or ""):
            d = asdict(mis)
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
            # resolved station name per leg (known names only) so the editor can
            # pre-fill its best guess instead of showing blank rows
            for ld in d["legs"].values():
                z = ld.get("zone_host_id")
                ld["name"] = ld.get("location") or (zone_names.get(z) if z else None) or ""
            mission_dicts.append(d)

        active = [m for m in visible if m.status == "active"]

        load = _build_loading(active, origin_of, dleg_loc, mlabel)
        unload, routes = _build_unloading_routes(active, origin_of, dleg_loc, mlabel)
        plan = plan_trip(active, origin_of, dleg_loc, current=state.location)

        counts = {
            "active": len(active),
            "partial": sum(
                1 for d in mission_dicts
                if d["status"] == "active" and d["partial"] and not d["hidden"]
            ),
            "completed": sum(1 for m in visible if m.status == "completed"),
            "abandoned": sum(1 for m in visible if m.status == "abandoned"),
            "failed": sum(1 for m in visible if m.status in ("failed", "expired")),
            "hidden": len(hidden_ids),
            "total": len(visible),
        }
        # Committed cargo (not merely loaded): the SCU each active mission still
        # owes. Prefer the delivery objectives' quantities (authoritative, and
        # they shrink as you deliver); but a mission accepted yet not loaded often
        # has no Deliver quantity logged, so fall back to its Collect (pickup)
        # quantities, which carry the committed amount from the moment it's taken.
        def _committed(m: Mission) -> int:
            dp = sum(l.qty for l in m.legs.values()
                     if l.kind == "dropoff" and l.qty and l.state != "completed")
            if dp:
                return dp
            return sum(l.qty for l in m.legs.values()
                       if l.kind == "pickup" and l.qty and l.state != "completed")
        active_scu = sum(_committed(m) for m in active)
        # Peak simultaneous hold usage (what actually has to fit at once). Cargo is
        # loaded at each mission's origin and dropped at its destination, so a
        # back-haul (A->B plus B->A) peaks at the larger leg, not their sum.
        peak_scu = _peak_load(active, origin_of, dleg_loc, _committed, anchor=state.location)
        # session income; when filtering to trade, sum the trade missions' rewards
        earned = sum(m.reward for m in visible if m.reward) if trade_only else state.total_awarded

        # autocomplete catalog for the editor: known stations (persisted map +
        # anything seen this session) and cargo names (canonical list + live).
        stations = set(zone_names.values())
        cargo_names = set(patterns.COMMODITY_NAMES)
        for mis in missions:
            for leg in mis.legs.values():
                if leg.location:
                    stations.add(leg.location)
                if leg.cargo:
                    cargo_names.add(leg.cargo)

        # Effective ship: the game-detected ship always wins; otherwise fall back
        # to the user's manual pick (settings.json). This drives the capacity
        # gauge and the cargo-grid view even at the main menu / for planning.
        effective_ship = state.ship or selected_ship

        return {
            "player": state.player,
            "location": state.location,
            "ship": effective_ship,
            "ship_detected": bool(state.ship),
            "selected_ship": selected_ship,
            "ship_internal": state.ship_internal,
            "ship_ts": state.ship_ts,
            "ship_scu": ship_capacity(effective_ship, cargo_db),
            "ship_grid": ship_grid(effective_ship, cargo_db),
            "ship_layout": ship_layout(effective_ship, cargo_db),
            "ship_cargo_updated": cargo_db.get("fetched_at"),
            "ship_cargo_version": cargo_db.get("game_version"),
            "game_version": state.game_version,
            "game_build": state.game_build,
            "in_seat": state.in_seat,
            "session_started_at": state.session_started_at,
            "logged_in": state.logged_in,
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
            "catalog": {"stations": sorted(stations), "cargo": sorted(cargo_names)},
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
                    "mission_id": mis.mission_id, "done": False,
                    "partial": not (leg.cargo and leg.qty),
                })
            continue

        g = grp(origin_of(mis), mis.origin_zone)
        detailed = [l for l in drops if l.cargo and l.qty]
        if detailed:
            for leg in detailed:
                g["total_scu"] += leg.qty
                g["items"].append({
                    "cargo": leg.cargo, "qty": leg.qty, "to": dleg_loc(leg),
                    "mission": mlabel(mis), "mission_id": mis.mission_id,
                    "done": leg.state == "completed", "partial": False,
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
            cargo = leg.cargo if detailed else mis_cargo
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
                 "cargo": {}, "missions": set(), "has_partial": False},
            )
            r["total_scu"] += qty or 0
            r["has_partial"] = r["has_partial"] or not detailed
            c = r["cargo"].setdefault(cargo, {"qty": 0, "legs": []})
            c["qty"] += qty or 0
            c["legs"].append({"mission_id": mis.mission_id, "oid": leg.objective_id})
            r["missions"].add(mis.mission_id)

    route_list = [
        {
            "origin": r["origin"], "destination": r["destination"],
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
