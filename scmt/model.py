"""Mission / leg data model."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import patterns


@dataclass
class Leg:
    objective_id: str
    kind: str  # "pickup" | "dropoff"
    cargo: str | None = None
    qty: int | None = None  # required SCU
    have: int = 0
    location: str | None = None  # resolved name when known
    zone_host_id: str | None = None
    pos: tuple[float, float, float] | None = None
    state: str = "pending"  # "pending" | "completed"


@dataclass
class Trade:
    """A manual commodity-terminal transaction (buy/sell), parsed from the game log.

    Distinct from mission cargo: the player bought or sold a commodity at a trade
    kiosk. `commodity` is resolved from `commodity_guid` at display/archive time
    (the log only carries the GUID). `scu` is derived from the box data, `auec` is
    the total transaction value. `trade_id` is a stable composite so re-feeding the
    same log (restart / rotation replay) upserts rather than duplicates."""
    trade_id: str
    action: str  # "buy" | "sell"
    commodity_guid: str
    scu: int
    auec: int
    shop: str  # raw shop entity code
    shop_label: str  # readable best-effort label
    ts: str | None = None
    commodity: str | None = None  # resolved name, filled lazily

    @property
    def unit_price(self) -> int:
        return round(self.auec / self.scu) if self.scu else 0


@dataclass
class Mission:
    mission_id: str
    title: str = ""
    org: str = ""
    contract: str = ""
    contract_def_id: str = ""
    status: str = "active"  # active | completed | failed | abandoned | expired
    origin_name: str | None = None  # set by a manual override
    accepted_at: str | None = None
    ended_at: str | None = None
    completion_type: str | None = None
    reason: str | None = None
    reward: int | None = None
    legs: dict[str, Leg] = field(default_factory=dict)

    @property
    def decoded(self) -> dict:
        return patterns.decode_contract(self.contract)

    @property
    def cargo_types(self) -> list[str]:
        named: list[str] = []
        for leg in self.legs.values():
            if leg.cargo and leg.cargo not in named:
                named.append(leg.cargo)
        return named or patterns.decode_cargo_from_contract(self.contract)

    @property
    def origin_zone(self) -> str | None:
        # The game sometimes drops a pickup marker on the *delivery* zone for
        # deliver-only missions (you source the cargo yourself, no "Collect"
        # objective). That isn't a real origin — it would render as
        # "Station X -> Station X" — so skip a pickup that shares a dropoff's zone.
        drop_zones = {l.zone_host_id for l in self.legs.values()
                      if l.kind == "dropoff" and l.zone_host_id}
        for leg in self.legs.values():
            if leg.kind == "pickup" and leg.zone_host_id not in drop_zones:
                return leg.zone_host_id
        return None

    @property
    def host_artifact_zones(self) -> set[str]:
        """zoneHostIds that tag BOTH a pickup and a dropoff leg of this mission.

        The game hosts a freshly-accepted contract's markers on the *acceptance*
        station's zone, so a zone shared by collect and deliver is that host
        artifact, not a real endpoint — naming a leg from it would mislabel every
        contract whose true destination differs from where it was accepted. The
        deliver-objective text (Leg.location) is the only trustworthy source until
        then. Mirrors origin_zone's same-zone skip, on the dropoff side."""
        pick = {l.zone_host_id for l in self.legs.values()
                if l.kind == "pickup" and l.zone_host_id}
        drop = {l.zone_host_id for l in self.legs.values()
                if l.kind == "dropoff" and l.zone_host_id}
        return pick & drop

    @property
    def has_pending_origin(self) -> bool:
        """A pickup leg exists but its only zone is a host artifact (shared with a
        dropoff), so origin_zone skipped it — the real origin is unknown until the
        game logs a Collect/objective line. The origin-side mirror of a host-artifact
        dropoff. False when origin_zone resolves a real pickup, or there's no pickup."""
        if self.origin_zone:
            return False
        return any(l.kind == "pickup" and l.zone_host_id in self.host_artifact_zones
                   for l in self.legs.values())

    @property
    def is_trade(self) -> bool:
        """Cargo-hauling/trade mission (vs combat, etc.)."""
        return (
            self.contract.lower().startswith("haulcargo")
            or "hauling" in self.org.lower()
            or "cargo haul" in self.title.lower()
        )
