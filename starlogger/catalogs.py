"""Background refresh of every ``Data.p4k``-derived catalog (ship cargo, commodity /
station reference, mineables, blueprints, contracts) behind one version-gated loop.

The heavy StarBreaker extraction lives in ``scdata``; each catalog owns its own
save/load module (``ships``, ``reference``, ``mineables``, ``blueprints``,
``contracts``). This module is the catalog-agnostic engine that decides *when* to
rebuild (only on a MAJOR game-version move, or if a cache is missing), locates the
p4k once per pass, and isolates each rebuild from the others' failures."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .config import SHIP_CARGO_PATH
from .patterns import major_version
from . import scdata
from .ships import build_ship_cargo, load_ship_cargo, save_ship_cargo


@dataclass
class _Catalog:
    """One rebuildable cache. ``rebuild(p4k, ver, reason)`` does the build + atomic save +
    logging and raises on failure; the orchestrator gates it on ``_reason`` and isolates it."""
    label: str
    has_cache: Callable[[], bool]            # a usable cache already exists
    cached_version: Callable[[], "str | None"]
    rebuild: Callable[[str, "str | None", str], None]


def _reason(cat: _Catalog, ver: str | None) -> str | None:
    """Why ``cat`` needs rebuilding: missing cache, or a MAJOR game-version move; else None."""
    if not cat.has_cache():
        return "no cache"
    if ver and major_version(ver) != major_version(cat.cached_version()):
        return f"version {cat.cached_version() or '?'} -> {ver}"
    return None


def _build_catalogs(path: str) -> list:
    """The catalogs the background loop keeps fresh, each gated/rebuilt the same way. The
    reference/mineables/blueprints modules are imported lazily (only the loop needs them)."""
    from . import blueprints, contracts, mineables, reference

    def _ship(p4k, ver, reason):
        print(f"[ship cargo] rebuilding from local install ({reason}) -- niced, ~minutes")
        ships = build_ship_cargo(p4k)
        if ships:
            save_ship_cargo(ships, game_version=ver)
            print(f"[ship cargo] rebuilt {len(ships)} ships ({reason})")

    def _reference(p4k, ver, reason):
        ref = scdata.build_reference_data(p4k)
        reference.save_reference(
            ref["commodities"], ref["location_codes"],
            commodity_names=ref["commodity_names"],
            station_names=ref["station_names"],
            commodity_types=ref["commodity_types"], game_version=ver)
        print(f"[reference] built {len(ref['commodity_names'])} commodities "
              f"({len(ref['categories'])} categories) + "
              f"{len(ref['station_names'])} stations ({reason})")

    def _mineables(p4k, ver, reason):
        print(f"[mineables] rebuilding from local install ({reason}) -- niced, ~minutes")
        rocks = scdata.build_mineables_from_p4k(p4k)
        if rocks:
            mineables.save_mineables(rocks, game_version=ver)
            print(f"[mineables] built {len(rocks)} mineable rocks ({reason})")

    def _blueprints(p4k, ver, reason):
        print(f"[blueprints] rebuilding from local install ({reason}) -- niced, ~minutes")
        bps = scdata.build_blueprints_from_p4k(p4k)
        if bps:
            blueprints.save_blueprints(bps, game_version=ver)
            print(f"[blueprints] built {len(bps)} blueprints ({reason})")

    def _contracts(p4k, ver, reason):
        print(f"[contracts] rebuilding from local install ({reason}) -- niced, ~minutes")
        data = scdata.build_contracts_from_p4k(p4k)
        if data["templates"]:
            contracts.save_contracts(data["templates"], data["cargo_manifests"],
                                     game_version=ver, icons=data.get("icons"))
            print(f"[contracts] built {len(data['templates'])} templates + "
                  f"{len(data['cargo_manifests'])} cargo manifests + "
                  f"{len(data.get('icons') or {})} type icons ({reason})")

    return [
        _Catalog("ship cargo",
                 lambda: bool(load_ship_cargo(path).get("ships")),
                 lambda: load_ship_cargo(path).get("game_version"), _ship),
        # Commodity + station reference data; cheap to build, gated like the rest.
        _Catalog("reference",
                 lambda: bool(reference.load_commodities()) and bool(reference.location_codes()),
                 reference.commodities_version, _reference),
        # Mineable-rock RS + composition (full DataCore extract; own file/trigger).
        _Catalog("mineables",
                 lambda: bool(mineables.load_mineables().get("rocks")),
                 mineables.mineables_version, _mineables),
        # Crafting blueprints + requirements (same full-extract source as mineables).
        _Catalog("blueprints",
                 lambda: bool(blueprints.load_blueprints().get("blueprints")),
                 blueprints.blueprints_version, _blueprints),
        # Contract taxonomy + cargo manifests (same full-extract source as mineables).
        _Catalog("contracts",
                 lambda: bool(contracts.load_contracts().get("templates")),
                 contracts.contracts_version, _contracts),
    ]


def _refresh_once(catalogs: list, ver: str | None, log_path: str | None) -> None:
    """One pass: find the stale catalogs, locate Data.p4k once, rebuild each (a failure in
    one doesn't stop the others). Callable on its own, which is what the tests drive."""
    stale = [(c, r) for c in catalogs if (r := _reason(c, ver))]
    if not stale:
        return
    p4k = scdata.find_p4k(log_path)
    if not p4k:
        print("[ship cargo] skip refresh: Data.p4k not found next to Game.log")
        return
    for cat, reason in stale:
        try:
            cat.rebuild(p4k, ver, reason)
        except Exception as e:  # keep the old cache, retry next check
            print(f"[{cat.label}] rebuild failed: {e}")


def refresh_loop(state, stop: threading.Event, log_path: str | None = None,
                 path: str = SHIP_CARGO_PATH) -> None:
    """Rebuild the local caches only on a MAJOR game-version change (or if missing), reading
    the local install. Runs the heavy StarBreaker extraction niced in the background (see
    scdata); the tracker keeps serving the old files until each atomic replace."""
    for _ in range(20):  # ~10s for the tailer to parse the version header
        if state.game_version or stop.is_set():
            break
        stop.wait(0.5)

    catalogs = _build_catalogs(path)
    while not stop.is_set():
        _refresh_once(catalogs, state.game_version, log_path)
        stop.wait(300)  # re-check for a version bump (e.g. after a patch + relaunch)
