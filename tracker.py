#!/usr/bin/env python3
"""Starlogger -- Star Citizen cargo/flight logger, CLI entry point.

Tails the game's Game.log, models accepted missions (cargo, quantity, origin,
destination, progress), and serves a web dashboard that groups the work by route.

    python3 tracker.py                 # auto-detect Game.log, serve on :8765
    python3 tracker.py --log /path/to/Game.log
    python3 tracker.py --port 9000
    python3 tracker.py --once          # parse current log, print JSON, exit

Run inside the project venv (see README): .venv/bin/python tracker.py
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
import webbrowser

from starlogger import shipcargo
from starlogger.archive import (
    ARCHIVE_SCHEMA,
    archive_session,
    load_backfill_index,
    load_sessions,
    save_backfill_index,
)
from starlogger.config import find_log, find_log_backups
from starlogger.maintenance import run_cleanup
from starlogger.server import create_app
from starlogger.snapshot import build_snapshot
from starlogger.state import State
from starlogger.stations import seed_station_names, zone_epoch
from starlogger.tailer import parse_whole_file, tail_loop


def rebuild_history(log_path: str) -> int:
    """Backfill the session archive from SC's logbackups/ plus the current log."""
    before = len(load_sessions())
    backups = find_log_backups(log_path)
    print(f"rebuilding history from {len(backups)} backup log(s) + current log…")
    for f in backups:
        st = State()
        st.on_session_end = archive_session
        try:
            parse_whole_file(f, st)
        except OSError:
            continue
        st.reset()  # the backup is a closed log -> flush its final (ended) session
    # current log: archive any sessions that already ended within it; leave the
    # ongoing session for the live tracker to archive when it actually ends.
    st = State()
    st.on_session_end = archive_session
    parse_whole_file(log_path, st)
    return len(load_sessions()) - before


def backfill_archive(log_path: str, stop: threading.Event) -> None:
    """Archive any logbackup sessions missing from sessions.json, in the background
    once the tailer is up. Logbackups are immutable, so an index recorded inside
    sessions.json (the `backfill` map: {basename: {size, schema}}) tracks which ones
    have already been processed -- a relaunch then skips them WITHOUT reading the file
    at all, instead of re-parsing all of them every startup. A backup is processed (full
    parse + archive) when it's new, its size changed, or its recorded `schema` is older
    than ARCHIVE_SCHEMA (a deploy that adds a summary field bumps the version, so history
    self-heals on the next run). The index shares sessions.json, so wiping that file
    resets both together."""
    index = load_backfill_index()
    before = len(load_sessions())
    dirty = False
    for f in find_log_backups(log_path):
        if stop.is_set():
            break
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        bn = os.path.basename(f)
        rec = index.get(bn)
        if rec and rec.get("size") == size and rec.get("schema") == ARCHIVE_SCHEMA:
            continue  # immutable backup already processed at the current schema
        st = State()
        st.on_session_end = archive_session
        try:
            parse_whole_file(f, st)
        except OSError:
            continue
        st.reset()  # closed log -> flush its final (ended) session
        index[bn] = {"size": size, "schema": ARCHIVE_SCHEMA}
        dirty = True
    if dirty:
        save_backfill_index(index)
    added = len(load_sessions()) - before
    if added:
        print(f"[archive] backfilled {added} session(s) from logbackups")


def recover_stations(log_path: str) -> None:
    """Mine the current log + every logbackup for zoneHostId -> station-name
    pairs and seed station_names.json. The live tracker keeps learning new ones
    as you play; this just backfills what history already knows."""
    all_logs = find_log_backups(log_path) + [log_path]
    print(f"scanning {len(all_logs)} log file(s) for station names…")
    res = seed_station_names(all_logs)
    added = res["added"]
    print(f"recovered {res['total_recovered']} zone(s); added {len(added)} new name(s):")
    for zone, name in sorted(added.items(), key=lambda kv: kv[1]):
        print(f"  {zone} -> {name}")
    if res["ambiguous"]:
        print(f"\n{len(res['ambiguous'])} zone(s) had conflicting names "
              f"(best guess kept — correct any in the dashboard):")
        for zone, names in sorted(res["ambiguous"].items()):
            joined = ", ".join(f"{n} ×{c}" for n, c in sorted(names.items(), key=lambda x: -x[1]))
            print(f"  {zone}: {joined}")
    print("\ndone — names persist in station_names.json and resolve live.")


def cleanup(log_path: str, dry_run: bool = False) -> None:
    """CLI wrapper over maintenance.run_cleanup: epoch-aware file hygiene for
    station_names.json + overrides.json. Stale rows are inert, not wrong, so this
    is hygiene, not a fix -- the same prune the live service runs on an epoch
    change, here on demand. Off-epoch zone names and overrides for missions no
    longer in the current log can never match again, so they're dropped."""
    res = run_cleanup(log_path, dry_run=dry_run)
    if res["skipped"]:
        # No markers -> we can't identify the current epoch or which missions are
        # live, so nothing is touched.
        print("current log has no accepted-mission activity yet -- nothing to do.")
        print("run --cleanup after you've been in a play session this launch.")
        return

    verb = "would remove" if dry_run else "removed"
    st, ov = res["stations"], res["overrides"]
    print(f"{'DRY RUN -- no files written' + chr(10) if dry_run else ''}"
          f"current server epoch(s): {', '.join(map(str, sorted(res['epochs'])))}")
    print(f"\nstation_names.json: {verb} {len(st['removed'])} off-epoch zone(s), kept {st['kept']}")
    for z, n in sorted(st["removed"].items(), key=lambda kv: kv[1]):
        print(f"  {z} (epoch {zone_epoch(z)})  {n}")
    print(f"\noverrides.json: {verb} {len(ov['removed'])} stale entr(ies), kept {ov['kept']}")
    for mid, label in sorted(ov["removed"].items(), key=lambda kv: kv[1]):
        print(f"  {mid}  {label}")
    print("\ndry run -- re-run without --dry-run to apply." if dry_run else "\ndone.")


def cleanup_loop(log_path: str, trigger: threading.Event, stop: threading.Event,
                 debounce: int = 30) -> None:
    """Run the epoch-aware cleanup shortly after the parser reports a new server
    epoch. Debounced so a relaunch's burst of restored missions has landed in the
    log before we decide which override rows are still live."""
    while not stop.is_set():
        if not trigger.wait(timeout=60):
            continue
        trigger.clear()
        if stop.wait(debounce):  # settle window; also exits promptly on shutdown
            break
        try:
            res = run_cleanup(log_path)
            if res and not res["skipped"]:
                st, ov = res["stations"], res["overrides"]
                if st["removed"] or ov["removed"]:
                    print(f"[cleanup] pruned {len(st['removed'])} station(s), "
                          f"{len(ov['removed'])} override(s) after epoch change")
        except Exception as e:
            print(f"[cleanup] failed: {e}")


def _probe_host(host: str) -> str:
    """Map wildcard bind addresses to a loopback address we can actually connect to."""
    if host in ("0.0.0.0", "", "::"):
        return "127.0.0.1"
    return host


def _port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """True if something already accepts TCP on host:port. A plain TCP check, matching
    run-tracker.sh's /dev/tcp guard -- a non-starlogger squatter counts as in-use on
    purpose (we won't start a second server on an occupied port either way)."""
    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_to_bind(host: str, port: int, timeout: float = 20.0) -> bool:
    """Something already serves host:port. Wait a bounded window for it to free,
    then report whether we may bind. This disambiguates the two reasons the port is
    busy at startup:
      - a relaunch is tearing the *previous* session's server down (sc-launch's
        `wineserver -k` kills the old game -> its sc-launch exits -> pdeathsig
        releases :8765 within a second or two) -> the port frees -> take over;
      - a healthy other instance is serving and isn't going anywhere -> the port
        stays busy the whole window -> leave it alone.
    Returns True once the port is free (bind/take over), False if it stayed busy."""
    deadline = time.monotonic() + timeout
    while _port_in_use(host, port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


def _open_browser_when_ready(host: str, port: int, url: str) -> None:
    """Daemon thread: wait for our own server to start accepting, then open it once.
    Best-effort -- on a headless box webbrowser.open returns False or raises; swallow it
    (the URL is already printed, so the user can still click it)."""
    probe = _probe_host(host)
    for _ in range(50):                       # ~10s max (50 * 0.2s)
        if _port_in_use(probe, port, timeout=0.2):
            break
        time.sleep(0.2)
    try:
        webbrowser.open(url, new=2)           # new=2 -> new tab if possible
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Starlogger -- Star Citizen cargo/flight logger + dashboard")
    ap.add_argument("--log", help="path to Game.log (auto-detected if omitted)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--once", action="store_true", help="parse current log, print JSON, exit")
    ap.add_argument("--rebuild", action="store_true",
                    help="backfill session history from logbackups/ + current log, then exit")
    ap.add_argument("--recover-stations", action="store_true",
                    help="backfill station_names.json (zoneHostId -> name) from all logs, then exit")
    ap.add_argument("--cleanup", action="store_true",
                    help="epoch-aware prune of stale station_names.json + overrides.json rows, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --cleanup: report what would be removed without writing")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't auto-open the dashboard in a browser on launch")
    args = ap.parse_args()

    log_path = args.log or find_log()
    if not log_path or not os.path.isfile(log_path):
        raise SystemExit("Could not find Game.log. Pass it with --log /path/to/Game.log")

    if args.rebuild:
        n = rebuild_history(log_path)
        print(f"done — {n} new session(s) archived ({len(load_sessions())} total)")
        return

    if args.recover_stations:
        recover_stations(log_path)
        return

    if args.cleanup:
        cleanup(log_path, dry_run=args.dry_run)
        return

    state = State()
    state.on_session_end = archive_session  # snapshot a session before it's cleared
    state.on_archive = archive_session      # live upsert as contracts/trades finish

    if args.once:
        parse_whole_file(log_path, state)
        print(json.dumps(build_snapshot(state), indent=2))
        return

    # Only the live-serving path dedups + auto-opens; the one-shot/maintenance modes
    # above have already returned. If the port looks busy, wait briefly: a relaunch's
    # wineserver -k is tearing the previous server down and will free it, whereas a
    # healthy other instance holds it through the window (and we leave it be).
    if _port_in_use(args.host, args.port) and not _wait_to_bind(args.host, args.port):
        print(f"Starlogger already running at http://{args.host}:{args.port} -- "
              f"not starting a second instance.")
        return

    stop = threading.Event()
    epoch_trigger = threading.Event()

    def on_epoch_change(prev: int, new: int) -> None:  # runs under state.lock -> stay cheap
        print(f"[epoch] server build {prev} -> {new}; scheduling cleanup")
        epoch_trigger.set()

    state.on_epoch_change = on_epoch_change
    threading.Thread(target=tail_loop, args=(log_path, state, stop), daemon=True).start()
    # backfill the session archive from logbackups in the background (skips ones
    # already archived) so a fresh data dir self-populates without a manual --rebuild.
    threading.Thread(target=backfill_archive, args=(log_path, stop), daemon=True).start()
    threading.Thread(target=shipcargo.refresh_loop, args=(state, stop, log_path), daemon=True).start()
    threading.Thread(target=cleanup_loop, args=(log_path, epoch_trigger, stop), daemon=True).start()

    url = f"http://{args.host}:{args.port}"
    print("Starlogger -- Star Citizen cargo/flight logger")
    print(f"  log:       {log_path}")
    print(f"  dashboard: {url}")
    print("  Ctrl-C to stop")

    if not (args.no_browser or os.environ.get("STARLOGGER_NO_BROWSER")):
        threading.Thread(target=_open_browser_when_ready,
                         args=(args.host, args.port, url), daemon=True).start()
    try:
        create_app(state, log_path).run(host=args.host, port=args.port, threaded=True)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
