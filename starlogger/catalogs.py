"""Background refresh of every ``Data.p4k``-derived catalog (ship cargo, commodity /
station reference, mineables, blueprints, contracts) behind one version-gated loop.

The heavy StarBreaker extraction lives in ``scdata``; each catalog owns its own
save/load module (``ships``, ``reference``, ``mineables``, ``blueprints``,
``contracts``). This module is the catalog-agnostic engine that decides *when* to
rebuild (a cache is missing, a MAJOR game-version move, or an extract-schema bump --
each module's ``EXTRACT_VERSION``, raised when its extraction grows/changes fields so
installs rebuild on a code update too), locates the p4k once per pass, and isolates
each rebuild from the others' failures."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .config import SHIP_CARGO_PATH
from .patterns import major_version
from . import scdata
from .ships import (build_ship_cargo, load_ship_cargo, save_ship_cargo,
                    ships_extract_version, EXTRACT_VERSION as SHIPS_EXTRACT_VERSION)


@dataclass
class _Catalog:
    """One rebuildable cache. ``rebuild(p4k, ver, reason)`` does the build + atomic save +
    logging and raises on failure; the orchestrator gates it on ``_reason`` and isolates it."""
    label: str
    has_cache: Callable[[], bool]            # a usable cache already exists
    cached_version: Callable[[], "str | None"]
    rebuild: Callable[[str, "str | None", str], None]
    extract_version: int                     # this code's current extract schema (bump on shape change)
    cached_extract_version: Callable[[], int]  # schema the on-disk cache was built with (0 == absent)


def _reason(cat: _Catalog, ver: str | None) -> str | None:
    """Why ``cat`` needs rebuilding: missing cache, a MAJOR game-version move, or an
    extract-schema bump (the generating code grew/changed fields); else None."""
    if not cat.has_cache():
        return "no cache"
    if ver and major_version(ver) != major_version(cat.cached_version()):
        return f"version {cat.cached_version() or '?'} -> {ver}"
    if cat.extract_version != cat.cached_extract_version():
        return f"extract schema v{cat.cached_extract_version()} -> v{cat.extract_version}"
    return None


def _build_catalogs(path: str, state=None, music_state=None) -> list:
    """The catalogs the background loop keeps fresh, each gated/rebuilt the same way. The
    reference/mineables/blueprints modules are imported lazily (only the loop needs them).
    ``state``/``music_state`` (when given) let the music build push decode progress to the
    dashboard via the SSE snapshot -- everything else builds silently."""
    from . import (blueprints, body_mineables, contracts, mineables, mining_gear, music,
                   radar, reference, salvageables, salvage_ships)
    from .config import MUSIC_DIR

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

    def _mining_gear(p4k, ver, reason):
        print(f"[mining gear] rebuilding from local install ({reason}) -- niced, ~minutes")
        gear = scdata.build_mining_gear_from_p4k(p4k)
        if gear["heads"]:
            mining_gear.save_mining_gear(gear["heads"], gear["modules"], game_version=ver)
            print(f"[mining gear] built {len(gear['heads'])} heads + "
                  f"{len(gear['modules'])} modules ({reason})")

    def _body_mineables(p4k, ver, reason):
        print(f"[body mineables] rebuilding from local install ({reason}) -- niced, ~minutes")
        bms = scdata.build_body_mineables_from_p4k(p4k)
        if bms:
            body_mineables.save_body_mineables(bms, game_version=ver)
            print(f"[body mineables] built {len(bms)} bodies ({reason})")

    def _radar(p4k, ver, reason):
        print(f"[radar] rebuilding from local install ({reason}) -- niced, ~minutes")
        rads = scdata.build_radar_from_p4k(p4k)
        if rads:
            radar.save_radar(rads, game_version=ver)
            print(f"[radar] built {len(rads)} radars ({reason})")

    def _salvageables(p4k, ver, reason):
        print(f"[salvageables] rebuilding from local install ({reason}) -- niced, ~minutes")
        wrecks = scdata.build_salvageables_from_p4k(p4k)
        if wrecks:
            salvageables.save_salvageables(wrecks, game_version=ver)
            print(f"[salvageables] built {len(wrecks)} salvage targets ({reason})")

    def _salvage_ships(p4k, ver, reason):
        print(f"[salvage ships] rebuilding from local install ({reason}) -- niced, ~minutes")
        ships = scdata.build_salvage_ships_from_p4k(p4k)
        if ships:
            salvage_ships.save_salvage_ships(ships, game_version=ver)
            print(f"[salvage ships] built {len(ships)} salvage ships ({reason})")

    def _contracts(p4k, ver, reason):
        print(f"[contracts] rebuilding from local install ({reason}) -- niced, ~minutes")
        data = scdata.build_contracts_from_p4k(p4k)
        if data["templates"]:
            contracts.save_contracts(data["templates"], data["cargo_manifests"],
                                     game_version=ver, icons=data.get("icons"),
                                     generators=data.get("generators"))
            print(f"[contracts] built {len(data['templates'])} templates + "
                  f"{len(data.get('generators') or [])} named generators + "
                  f"{len(data['cargo_manifests'])} cargo manifests + "
                  f"{len(data.get('icons') or {})} type icons ({reason})")

    def _music(p4k, ver, reason):
        # Jukebox best-track set. Builds once on first run, then refreshes on a major version move.
        # Scan first (no decode, ~seconds): if the keep-set is unchanged AND the manifest is already
        # at the current schema, just re-stamp its version. A changed set OR a schema bump (cached
        # extract_version < this code's) pays the full re-decode -- the schema case so a new row
        # field reaches existing installs even when the song set didn't change (else the restamp
        # shortcut would mark the manifest current with stale rows, and the version gate would never
        # fire again). StarBreaker decodes the whole bank, niced; we prune to the pinned allowlist +
        # heuristic keepers as it goes. Decode progress is pushed to the dashboard via music_state.
        scanned = scdata.scan_songs(p4k)
        schema_current = music.music_extract_version() == music.EXTRACT_VERSION
        if schema_current and scanned and scanned == music.track_ids():
            music.restamp_version(ver)
            print(f"[music] scan: no new songs ({reason}); marked current for {ver}")
            return
        new = len(scanned - music.track_ids())
        print(f"[music] scan: {new} new song(s) ({reason}) -- extracting, niced, ~minutes")

        def progress(done, total):
            if music_state is not None:
                music_state.set(phase="extracting", done=done, total=total)
                if state is not None:
                    state.bump_version()

        if music_state is not None:
            music_state.set(phase="extracting", done=0, total=len(scanned))
            if state is not None:
                state.bump_version()
        tracks = scdata.build_music_from_p4k(p4k, MUSIC_DIR, progress=progress)
        if tracks:
            music.save_music(tracks, game_version=ver, min_duration=0.0)
            print(f"[music] extracted {len(tracks)} best tracks ({reason})")
        if music_state is not None:
            music_state.set(phase="done", done=len(tracks), total=len(tracks))
            if state is not None:
                state.bump_version()

    cats = [
        _Catalog("ship cargo",
                 lambda: bool(load_ship_cargo(path).get("ships")),
                 lambda: load_ship_cargo(path).get("game_version"), _ship,
                 SHIPS_EXTRACT_VERSION, lambda: ships_extract_version(path)),
        # Commodity + station reference data; cheap to build, gated like the rest.
        _Catalog("reference",
                 lambda: bool(reference.load_commodities()) and bool(reference.location_codes()),
                 reference.commodities_version, _reference,
                 reference.EXTRACT_VERSION, reference.reference_extract_version),
        # Mineable-rock RS + composition (full DataCore extract; own file/trigger).
        _Catalog("mineables",
                 lambda: bool(mineables.load_mineables().get("rocks")),
                 mineables.mineables_version, _mineables,
                 mineables.EXTRACT_VERSION, mineables.mineables_extract_version),
        # Crafting blueprints + requirements (same full-extract source as mineables).
        _Catalog("blueprints",
                 lambda: bool(blueprints.load_blueprints().get("blueprints")),
                 blueprints.blueprints_version, _blueprints,
                 blueprints.EXTRACT_VERSION, blueprints.blueprints_extract_version),
        # Mining equipment (heads + modules; same full-extract source as mineables).
        _Catalog("mining gear",
                 lambda: bool(mining_gear.load_mining_gear().get("heads")),
                 mining_gear.mining_gear_version, _mining_gear,
                 mining_gear.EXTRACT_VERSION, mining_gear.mining_gear_extract_version),
        # Per-body mineables (which planet/moon yields which mineral; same full-extract source).
        # Parsed from the starmap descriptions -- the location side of the mining picture.
        _Catalog("body mineables",
                 lambda: bool(body_mineables.load_body_mineables().get("bodies")),
                 body_mineables.body_mineables_version, _body_mineables,
                 body_mineables.EXTRACT_VERSION, body_mineables.body_mineables_extract_version),
        # Ship radar components (same full-extract source as mining gear) -- the radar slot of
        # the per-ship mining loadout, carrying the resource-signature (RS) detection stat.
        _Catalog("radar",
                 lambda: bool(radar.load_radar().get("radars")),
                 radar.radar_version, _radar,
                 radar.EXTRACT_VERSION, radar.radar_extract_version),
        # Salvageable wrecks (ship-debris hulls + panels; signature-only, no composition).
        _Catalog("salvageables",
                 lambda: bool(salvageables.load_salvageables().get("wrecks")),
                 salvageables.salvageables_version, _salvageables,
                 salvageables.EXTRACT_VERSION, salvageables.salvageables_extract_version),
        # Salvage wreck ships -> removable (salvage-beam-strippable) components, with per-ship
        # loadouts (full-extract source; gated like the ship catalog). Feeds Salvage mode.
        _Catalog("salvage ships",
                 lambda: bool(salvage_ships.load_salvage_ships().get("ships")),
                 salvage_ships.salvage_ships_version, _salvage_ships,
                 salvage_ships.EXTRACT_VERSION, salvage_ships.salvage_ships_extract_version),
        # Contract taxonomy + cargo manifests (same full-extract source as mineables).
        _Catalog("contracts",
                 lambda: bool(contracts.load_contracts().get("templates")),
                 contracts.contracts_version, _contracts,
                 contracts.EXTRACT_VERSION, contracts.contracts_extract_version),
        # Jukebox soundtrack -- the full-song set, distilled to ~0.4 GB. Builds automatically on
        # first run (has_cache False -> "no cache" -> build) and refreshes on a major version move,
        # niced like the rest. The decode is one-shot, pruned to the long standalone pieces.
        _Catalog("music",
                 music.is_extracted, music.music_version, _music,
                 music.EXTRACT_VERSION, music.music_extract_version),
    ]
    return cats


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
                 path: str = SHIP_CARGO_PATH, music_state=None) -> None:
    """Rebuild the local caches only on a MAJOR game-version change (or if missing), reading
    the local install. Runs the heavy StarBreaker extraction niced in the background (see
    scdata); the tracker keeps serving the old files until each atomic replace. ``music_state``
    (when given) surfaces the music build's decode progress on the dashboard SSE snapshot."""
    for _ in range(20):  # ~10s for the tailer to parse the version header
        if state.game_version or stop.is_set():
            break
        stop.wait(0.5)

    catalogs = _build_catalogs(path, state=state, music_state=music_state)
    while not stop.is_set():
        _refresh_once(catalogs, state.game_version, log_path)
        stop.wait(300)  # re-check for a version bump (e.g. after a patch + relaunch)
