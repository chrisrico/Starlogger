"""Trip planner for the Routes tab.

Turns the current outstanding cargo into an ordered delivery itinerary that
minimizes quantum jumps. Routing is **topological**, not geometric: the log's
marker positions live in a shifting reference frame (the same station shows wildly
different coordinates across markers), so distance math is unreliable. Instead we
group stops by celestial body and order the groups by a fixed travel-cost model.

Travel-cost tiers the ordering encodes:
  1. intra-body  — a planet's Lagrange stations / its orbital + surface (cheapest)
  2. inter-body  — a quantum jump to another planet/moon in the same system
  3. cross-system — via a jump-point gateway (most expensive)

Station -> (system, body, moon) is a hand-curated table (STATION_BODY below),
compiled from in-game geography. Lagrange stations resolve by their NNN-Ln prefix;
named stations use STATION_BODY; a body name resolves to itself. Unknowns fall into
a "?" bucket that sorts last and is never hidden.
"""

from __future__ import annotations

import re

# --- celestial hierarchy ---------------------------------------------------- #

# Lagrange-station prefix -> parent planet (all Stanton).
LAGRANGE_PREFIX = {
    "HUR": "Hurston",
    "CRU": "Crusader",
    "ARC": "ArcCorp",
    "MIC": "microTech",
}

# Named station -> (system, body, moon-or-None). Bodies are planets; a moon is
# called out separately so it can be its own sub-group (a separate quantum hop).
STATION_BODY = {
    # Stanton — Hurston
    "Everus Harbor": ("Stanton", "Hurston", None),
    "Teasa Spaceport": ("Stanton", "Hurston", None),       # Lorville, on Hurston
    "Lorville": ("Stanton", "Hurston", None),
    # Stanton — Crusader
    "Seraphim Station": ("Stanton", "Crusader", None),
    "Orison": ("Stanton", "Crusader", None),
    "Covalex Hub Gundo": ("Stanton", "Crusader", None),
    "Grim HEX": ("Stanton", "Crusader", "Yela"),
    # Stanton — ArcCorp
    "Baijini Point": ("Stanton", "ArcCorp", None),
    "Area18": ("Stanton", "ArcCorp", None),
    "August Dunlow Spaceport": ("Stanton", "ArcCorp", "Lyria"),
    "Shubin Mining Facility SAL-2": ("Stanton", "ArcCorp", "Lyria"),
    "Shubin Mining Facility SAL-5": ("Stanton", "ArcCorp", "Lyria"),
    # Stanton — microTech
    "Port Tressler": ("Stanton", "microTech", None),
    "New Babbage": ("Stanton", "microTech", None),
    # Stanton — Delamar
    "Levski": ("Stanton", "Delamar", None),
    # Pyro — station->body attach is approximate; system-level groups are fine
    # until Pyro hauls actually appear. Listed bodies keep ordering sane.
    "Ruin Station": ("Pyro", "Pyro", None),
    "Checkmate Station": ("Pyro", "Pyro", None),
    "Patch City": ("Pyro", "Pyro", None),
    "Megumi Refueling": ("Pyro", "Pyro", None),
    "Starlight Service Station": ("Pyro", "Pyro", None),
    "Rod's Fuel 'N Supplies": ("Pyro", "Pyro", None),
    "Dudley & Daughters": ("Pyro", "Pyro", None),
    # Nyx
    "Delamar": ("Nyx", "Delamar", None),
}

# Across-system order: home system first.
SYSTEM_ORDER = {"Stanton": 0, "Pyro": 1, "Nyx": 2}

# Within a system, the orbital ring — used as a stable tie-breaker when two bodies
# carry equal outstanding cargo. Lower = visited earlier.
BODY_ORDER = {
    "Hurston": 0, "Crusader": 1, "ArcCorp": 2, "microTech": 3, "Delamar": 4,
    "Pyro": 0,
}

_LAGRANGE = re.compile(r"^([A-Z]{3})-L\d", re.I)
_UNKNOWN = "?"
# Stanton bodies, so a body-level live location (e.g. "Crusader") still anchors.
_BODIES = {"Hurston": ("Stanton", "Hurston", None), "Crusader": ("Stanton", "Crusader", None),
           "ArcCorp": ("Stanton", "ArcCorp", None), "microTech": ("Stanton", "microTech", None),
           "Delamar": ("Stanton", "Delamar", None)}


def classify_station(name: str | None):
    """Return (system, body, moon) for a station name. Unknowns -> ('?','?',None)."""
    if not name:
        return (_UNKNOWN, _UNKNOWN, None)
    m = _LAGRANGE.match(name.strip())
    if m:
        planet = LAGRANGE_PREFIX.get(m.group(1).upper())
        if planet:
            return ("Stanton", planet, None)
    hit = STATION_BODY.get(name.strip())
    if hit:
        return hit
    return _BODIES.get(name.strip(), (_UNKNOWN, _UNKNOWN, None))


def _group_key(system, body, moon):
    """A stable identity for one stop-cluster (planet, or a moon of a planet)."""
    return (system, body, moon or "")


def plan_trip(active, origin_of, dleg_loc, current=None):
    """Build an ordered delivery itinerary from the active trade missions.

    active     : list[Mission] (already trade-filtered, status == active)
    origin_of  : Mission -> origin station name (snapshot helper)
    dleg_loc   : Leg -> destination station name (snapshot helper)
    current    : the player's current station (live location), used as the route
                 anchor so the tour starts from where you actually are; falls back
                 to the most common mission origin when unknown.

    Returns {load, legs_total, scu_total, stops[]} or {stops: []} when nothing is
    outstanding. Each stop is one station with its cargo lines; stops are ordered
    anchor-body-first, then by outstanding SCU desc (orbital-ring tie-break), with
    a moon placed immediately after its parent planet.
    """
    # most common origin across the active hauls = where you load
    origins: dict[str, int] = {}
    for mis in active:
        origins[origin_of(mis)] = origins.get(origin_of(mis), 0) + 1
    origin = max(origins, key=origins.get) if origins else None
    # anchor the tour at the player's current location when known, else the origin
    anchor = current or origin
    origin_cluster = _group_key(*classify_station(anchor)) if anchor else None

    from .snapshot import PENDING_DEST  # lazy: snapshot imports this module at load

    # collect outstanding (not delivered) dropoff legs, grouped by station
    by_station: dict[str, dict] = {}
    load_items: list[dict] = []
    for mis in active:
        for leg in mis.legs.values():
            if leg.kind != "dropoff" or leg.state == "completed":
                continue
            dest = dleg_loc(leg)
            cargo = leg.cargo or ", ".join(mis.cargo_types) or "Unknown cargo"
            item = {
                "cargo": cargo, "qty": leg.qty,
                "mission_id": mis.mission_id, "oid": leg.objective_id,
            }
            st = by_station.setdefault(dest, {
                "station": dest, "items": [], "scu": 0,
                "cls": classify_station(dest), "zone": None,
            })
            # zone backs the inline station editor; skip the pending-dropoff placeholder
            if leg.zone_host_id and not st["zone"] and dest != PENDING_DEST:
                st["zone"] = leg.zone_host_id
            st["items"].append(item)
            st["scu"] += leg.qty or 0
            load_items.append(item)

    if not by_station:
        return {"stops": []}

    # roll stations up into clusters (planet, or moon-of-planet) for ordering
    clusters: dict[tuple, dict] = {}
    for st in by_station.values():
        system, body, moon = st["cls"]
        key = _group_key(system, body, moon)
        c = clusters.setdefault(key, {
            "system": system, "body": body, "moon": moon,
            "stations": [], "scu": 0,
        })
        c["stations"].append(st)
        c["scu"] += st["scu"]

    # planet-level SCU (planet + its moons) so a moon rides with its parent and a
    # heavier planet-system as a whole is visited earlier
    planet_scu: dict[tuple, int] = {}
    for c in clusters.values():
        pk = (c["system"], c["body"])
        planet_scu[pk] = planet_scu.get(pk, 0) + c["scu"]

    def sort_key(c):
        pk = (c["system"], c["body"])
        is_origin = 0 if origin_cluster == _group_key(c["system"], c["body"], c["moon"]) else 1
        unknown = 1 if c["body"] == _UNKNOWN else 0
        return (
            is_origin,                              # origin's own cluster first
            unknown,                                # unknown bodies last
            SYSTEM_ORDER.get(c["system"], 9),       # home system first
            -planet_scu[pk],                        # heavier planet-system earlier
            BODY_ORDER.get(c["body"], 9),           # orbital-ring tie-break
            c["body"],                              # stable
            0 if c["moon"] is None else 1,          # planet before its moons
            c["moon"] or "",
        )

    ordered = sorted(clusters.values(), key=sort_key)

    stops = []
    for c in ordered:
        for st in sorted(c["stations"], key=lambda s: s["station"]):
            st["items"].sort(key=lambda i: (-(i["qty"] or 0), i["cargo"]))
            stops.append({
                "system": c["system"], "body": c["body"], "moon": c["moon"],
                "station": st["station"], "scu": st["scu"], "items": st["items"],
                "zone": st["zone"],
            })

    load_items.sort(key=lambda i: (-(i["qty"] or 0), i["cargo"]))
    scu_total = sum(i["qty"] or 0 for i in load_items)
    return {
        "load": {"station": origin, "items": load_items},
        "legs_total": len(load_items),
        "scu_total": scu_total,
        "stops": stops,
    }
