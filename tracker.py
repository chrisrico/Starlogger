#!/usr/bin/env python3
"""Star Citizen mission tracker -- CLI entry point.

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
import threading

from scmt import shipcargo
from scmt.archive import archive_session, load_sessions
from scmt.config import find_log, find_log_backups
from scmt.maintenance import run_cleanup
from scmt.server import create_app
from scmt.snapshot import build_snapshot
from scmt.state import State
from scmt.stations import seed_station_names, zone_epoch
from scmt.tailer import parse_whole_file, tail_loop


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


def _first_session_key(path: str, max_lines: int = 8000) -> str | None:
    """Cheaply read just the head of a backup to get its first session's archive
    key (started_at|player) without parsing the whole (large) file. Requires
    `logged_in` so session_started_at is the post-login establisher value the
    archive actually keys on (an early pre-login timestamp gets reset). Capped so
    a login-less crash log doesn't read end-to-end."""
    st = State()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                st.feed(line)
                if st.logged_in and st.session_started_at and st.player:
                    return f"{st.session_started_at}|{st.player}"
    except OSError:
        return None
    return None


def backfill_archive(log_path: str, stop: threading.Event) -> None:
    """Incrementally archive any logbackup sessions missing from sessions.json,
    in the background once the tailer is up. A backup is SKIPPED without a full
    parse when its first session is already archived -- backups are immutable and
    parsed atomically, so one archived session means the file was fully processed.
    Self-healing: if sessions.json is wiped, the keys are gone so everything
    re-archives; nothing to keep in sync. Also schema-aware -- a backup whose
    archived session predates a summary field (e.g. `trades`, added later) is
    re-parsed once to backfill it, so a deploy that adds a field heals existing
    history without a manual --rebuild."""
    # only treat a session as "done" once its archived entry carries the current
    # schema: the `travels` key AND a per-mission `type` (added later). Older entries
    # lack one or the other and re-parse once to backfill it.
    def archived_keys() -> set:
        return {s.get("key") for s in load_sessions()
                if "travels" in s and all("type" in m for m in s.get("missions", []))}

    archived = archived_keys()
    before = len(archived)
    for f in find_log_backups(log_path):
        if stop.is_set():
            return
        key = _first_session_key(f)
        if key and key in archived:
            continue  # this backup's session(s) already archived at current schema
        st = State()
        st.on_session_end = archive_session
        try:
            parse_whole_file(f, st)
        except OSError:
            continue
        st.reset()  # closed log -> flush its final (ended) session
        archived = archived_keys()
    added = len(archived) - before
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Star Citizen mission tracker + dashboard")
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

    if args.once:
        parse_whole_file(log_path, state)
        print(json.dumps(build_snapshot(state), indent=2))
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

    print("Star Citizen mission tracker")
    print(f"  log:       {log_path}")
    print(f"  dashboard: http://{args.host}:{args.port}")
    print("  Ctrl-C to stop")
    try:
        create_app(state, log_path).run(host=args.host, port=args.port, threaded=True)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
