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
import signal
import socket
import threading
import time
import urllib.request
import webbrowser

from starlogger import catalogs
from starlogger.archive import (
    ARCHIVE_SCHEMA,
    archive_session,
    load_backfill_index,
    load_sessions,
    save_backfill_index,
)
from starlogger.config import IS_WINDOWS, find_log, find_log_backups
from starlogger.maintenance import run_cleanup
from starlogger.server import create_app
from starlogger.snapshot import build_snapshot
from starlogger.state import State
from starlogger.stations import seed_station_names, zone_epoch
from starlogger.tailer import parse_whole_file, tail_loop
from werkzeug.serving import make_server


# Seconds an unattended tracker lingers before exiting (default; STARLOGGER_IDLE_TIMEOUT
# overrides). "Unattended" = no dashboard SSE stream is connected AND, on Linux, the
# launcher is gone. Comfortably covers a page reload (the stream drops, then reconnects
# in ~1s) so a reload never trips a shutdown.
IDLE_TIMEOUT_DEFAULT = 30.0

# Once a tab beacons /api/closing on pagehide it withdraws its "keep me alive" claim, so a
# tracker whose launcher is already gone needn't wait out the full idle timeout -- it may
# exit after only this short grace (STARLOGGER_CLOSE_TIMEOUT overrides). The grace exists
# purely to outlast a reload: a reload also fires pagehide, but its EventSource reconnects
# in ~1s and re-asserts presence, cancelling the shutdown before this elapses.
CLOSE_TIMEOUT_DEFAULT = 2.0


def _idle_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("STARLOGGER_IDLE_TIMEOUT", IDLE_TIMEOUT_DEFAULT)))
    except (TypeError, ValueError):
        return IDLE_TIMEOUT_DEFAULT


def _close_timeout() -> float:
    try:
        return max(0.5, float(os.environ.get("STARLOGGER_CLOSE_TIMEOUT", CLOSE_TIMEOUT_DEFAULT)))
    except (TypeError, ValueError):
        return CLOSE_TIMEOUT_DEFAULT


class Presence:
    """Shared liveness state between the SSE endpoint (which connects/disconnects streams)
    and the shutdown watchdog. `streams` is the count of open dashboard connections;
    `launcher_dead` is flagged by the SIGUSR1 handler when the launcher exits (Linux)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.streams = 0
        self.last_empty: float | None = None   # monotonic when streams last hit 0
        self.launcher_dead = False
        self.launcher_dead_at: float | None = None  # monotonic when SIGUSR1 arrived
        self.closing = False                   # a tab beaconed a deliberate close

    def stream_connect(self) -> None:
        with self.lock:
            self.streams += 1
            self.last_empty = None
            self.closing = False               # a (re)connect re-asserts the keep-alive claim

    def stream_disconnect(self) -> None:
        with self.lock:
            self.streams = max(0, self.streams - 1)
            if self.streams == 0:
                self.last_empty = time.monotonic()

    def mark_closing(self) -> None:
        """A tab signalled (via /api/closing on pagehide) that it is deliberately leaving,
        withdrawing its keep-alive claim. Doesn't shut anything down -- just lets the
        watchdog use the short close grace instead of the full idle timeout once the
        launcher is also gone. Cleared by the next stream_connect (e.g. a reload)."""
        with self.lock:
            self.closing = True

    def snapshot(self) -> tuple[int, float | None, bool, float | None, bool]:
        with self.lock:
            return (self.streams, self.last_empty, self.launcher_dead,
                    self.launcher_dead_at, self.closing)


def should_shutdown(*, streams: int, launcher_dead: bool, has_launcher_detection: bool,
                    last_empty: float | None, launcher_dead_at: float | None,
                    start_ts: float, now: float, timeout: float,
                    closing: bool = False, close_timeout: float = CLOSE_TIMEOUT_DEFAULT) -> bool:
    """Pure decision: should the tracker exit now?

    Stay up while a dashboard stream is open, or (on Linux) while the launcher runs.
    Once neither holds, exit after `timeout` seconds idle -- or, when the last stream
    departed via a deliberate beaconed close (`closing`), after only `close_timeout`. The
    reference time is when the last stream closed; if no stream was ever opened, fall back
    to when the launcher died (Linux) or process start (Windows, undetectable)."""
    if streams > 0:
        return False
    if has_launcher_detection and not launcher_dead:
        return False
    if last_empty is not None:
        ref = last_empty
    elif has_launcher_detection:        # never connected; launcher has died (checked above)
        ref = launcher_dead_at if launcher_dead_at is not None else start_ts
    else:                               # Windows: no launcher signal -> start grace from boot
        ref = start_ts
    return (now - ref) > (close_timeout if closing else timeout)


def shutdown_watchdog(presence: Presence, stop: threading.Event,
                      has_launcher_detection: bool, timeout: float,
                      start_ts: float, shutdown_cb, poll: float = 2.0,
                      close_timeout: float = CLOSE_TIMEOUT_DEFAULT) -> None:
    """Stop the server once no dashboard is attached and the launcher is gone (or, on
    Windows, just no dashboard). shutdown_cb is the WSGI server's thread-safe .shutdown(),
    which makes serve_forever() return so main()'s `finally: stop.set()` runs.

    NOT a signal: a self-SIGINT is silently dropped here. run-tracker.sh backgrounds us
    from a non-interactive shell, which sets SIGINT/SIGQUIT to SIG_IGN, and Python keeps an
    inherited SIG_IGN -- so os.kill(getpid(), SIGINT) would be a no-op and we'd never die."""
    while not stop.wait(poll):
        streams, last_empty, launcher_dead, launcher_dead_at, closing = presence.snapshot()
        if should_shutdown(streams=streams, launcher_dead=launcher_dead,
                           has_launcher_detection=has_launcher_detection,
                           last_empty=last_empty, launcher_dead_at=launcher_dead_at,
                           start_ts=start_ts, now=time.monotonic(), timeout=timeout,
                           closing=closing, close_timeout=close_timeout):
            grace = close_timeout if closing else timeout
            why = "launcher gone + no dashboard" if has_launcher_detection else "no dashboard"
            print(f"[watchdog] shutting down ({why} for >{grace:.0f}s)")
            shutdown_cb()
            return


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
    """Wait a bounded window for host:port to free, then report whether we may bind.
    Used after asking an existing instance to quit (see _ask_existing_to_quit): a new
    launch replaces the old tracker rather than deferring to it, so the latest code runs
    and the new game session owns the tracker. Returns True once free, False if it
    stayed busy (the old instance wedged / a non-starlogger squatter holds the port)."""
    deadline = time.monotonic() + timeout
    while _port_in_use(host, port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


def _ask_existing_to_quit(host: str, port: int) -> None:
    """Tell an instance already serving host:port to shut down, so this newer launch can
    take over the port. Best-effort POST /api/quit -- if it's a starlogger it exits
    cleanly (and its dashboard tab's SSE stream reconnects to us); if it's something else
    or already gone, the call just fails and _wait_to_bind decides whether we may bind."""
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"http://{_probe_host(host)}:{port}/api/quit",
                                   data=b"", method="POST"),
            timeout=2.0).close()
    except OSError:
        pass


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

    # Only the live-serving path takes over + auto-opens; the one-shot/maintenance modes
    # above have already returned. A new launch REPLACES any instance already on the port:
    # ask it to quit, then wait for the port to free. This guarantees the latest code runs
    # (sc-run.sh updates on each launch) and the new game session owns the tracker. If it
    # won't release the port (wedged, or a non-starlogger squatter), bail rather than fight.
    took_over = _port_in_use(args.host, args.port)
    if took_over:
        _ask_existing_to_quit(args.host, args.port)
        if not _wait_to_bind(args.host, args.port):
            print(f"Another server is holding http://{args.host}:{args.port} and won't "
                  f"release it -- not starting.")
            return

    stop = threading.Event()
    epoch_trigger = threading.Event()

    # Lifecycle: live while a dashboard is open OR (on Linux) the launcher runs. The SSE
    # stream count is the dashboard-presence signal; SIGUSR1 (sent by run-tracker.sh's
    # setpriv --pdeathsig USR1) flags launcher death without killing us, so we linger for
    # post-session review and the watchdog releases us once the last tab closes.
    presence = Presence()
    has_launcher_detection = hasattr(signal, "SIGUSR1") and not IS_WINDOWS
    if has_launcher_detection:
        # Register before serving: the default SIGUSR1 disposition terminates the process.
        def _on_launcher_death(signum, frame):  # main thread; keep trivial
            presence.launcher_dead = True
            presence.launcher_dead_at = time.monotonic()
        signal.signal(signal.SIGUSR1, _on_launcher_death)

    def on_epoch_change(prev: int, new: int) -> None:  # runs under state.lock -> stay cheap
        print(f"[epoch] server build {prev} -> {new}; scheduling cleanup")
        epoch_trigger.set()

    state.on_epoch_change = on_epoch_change
    threading.Thread(target=tail_loop, args=(log_path, state, stop), daemon=True).start()
    # backfill the session archive from logbackups in the background (skips ones
    # already archived) so a fresh data dir self-populates without a manual --rebuild.
    threading.Thread(target=backfill_archive, args=(log_path, stop), daemon=True).start()
    threading.Thread(target=catalogs.refresh_loop, args=(state, stop, log_path), daemon=True).start()
    threading.Thread(target=cleanup_loop, args=(log_path, epoch_trigger, stop), daemon=True).start()

    url = f"http://{args.host}:{args.port}"
    print("Starlogger -- Star Citizen cargo/flight logger")
    print(f"  log:       {log_path}")
    print(f"  dashboard: {url}")
    print("  Ctrl-C to stop")

    # Auto-open the dashboard only on a fresh start. On a relaunch we replaced the previous
    # instance (above), so its tab's SSE stream auto-reconnects to the same URL -- opening
    # another would pile up a duplicate tab every relaunch.
    if took_over:
        print("  (replaced a running instance -- its dashboard tab will reconnect)")
    elif not (args.no_browser or os.environ.get("STARLOGGER_NO_BROWSER")):
        threading.Thread(target=_open_browser_when_ready,
                         args=(args.host, args.port, url), daemon=True).start()
    # make_server (not app.run) so the watchdog can stop us via httpd.shutdown() -- a
    # thread-safe call that returns serve_forever() cleanly, without relying on a signal
    # (a self-SIGINT is dropped when we're backgrounded; see shutdown_watchdog).
    app = create_app(state, log_path, presence=presence)
    httpd = make_server(args.host, args.port, app, threaded=True)
    # Let a newer launch replace us via POST /api/quit (see _ask_existing_to_quit).
    app.config["QUIT_FN"] = httpd.shutdown
    threading.Thread(target=shutdown_watchdog,
                     args=(presence, stop, has_launcher_detection, _idle_timeout(),
                           time.monotonic(), httpd.shutdown, 2.0, _close_timeout()),
                     daemon=True).start()
    try:
        httpd.serve_forever()        # Ctrl-C (foreground) raises KeyboardInterrupt here
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()         # release :8765 promptly for a relaunch to take over


if __name__ == "__main__":
    main()
