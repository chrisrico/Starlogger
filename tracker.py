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
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from starlogger import catalogs, contracts, ignition, scdata, settings
from starlogger.archive import (
    ARCHIVE_SCHEMA,
    archive_session,
    load_backfill_index,
    load_sessions,
    save_backfill_index,
)
from starlogger.config import BASE_DIR, IS_WINDOWS, find_log, find_log_backups
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
    # env STARLOGGER_IDLE_TIMEOUT > settings.json > IDLE_TIMEOUT_DEFAULT (clamped >=1).
    return settings.resolve_number("idle_timeout")


def _close_timeout() -> float:
    # env STARLOGGER_CLOSE_TIMEOUT > settings.json > CLOSE_TIMEOUT_DEFAULT (clamped >=0.5).
    return settings.resolve_number("close_timeout")


class Presence:
    """Shared liveness state between the SSE endpoint (which connects/disconnects streams)
    and the shutdown watchdog. `streams` is the count of open dashboard connections;
    `launcher_dead` is flagged via mark_launcher_dead() when the launcher exits (Linux) --
    by the game-exit watcher under the parent model, or the legacy SIGUSR1 handler."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.streams = 0
        self.last_empty: float | None = None   # monotonic when streams last hit 0
        self.launcher_dead = False
        self.launcher_dead_at: float | None = None  # monotonic when the launcher exit was seen
        self.closing = False                   # a tab beaconed a deliberate close

    def mark_launcher_dead(self) -> None:
        """Flag that the launcher has exited (Linux). Routed to by both the game-exit watcher
        (parent model: ignition.launch_game) and the legacy SIGUSR1 handler. Doesn't shut
        anything down -- the watchdog lets the dashboard linger for post-session review and
        releases us once the last tab closes."""
        if not self.launcher_dead:                  # first-writer wins; keep the original time
            self.launcher_dead = True
            self.launcher_dead_at = time.monotonic()

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
    # A schema bump re-archives history, but the per-mission `type` is only as good as the
    # contract taxonomy on disk -- and that taxonomy rebuilds (minutes) in a sibling thread
    # on the same update. Wait for it to reach the current extract version first, so history
    # picks up newly-added mission types instead of re-stamping against the stale cache and
    # never retrying. Only wait when a rebuild is actually possible (Data.p4k present); bail
    # after a grace period so an offline install still backfills with the keyword heuristic.
    if scdata.find_p4k(log_path):
        deadline = time.monotonic() + 900
        while (contracts.contracts_extract_version() < contracts.EXTRACT_VERSION
               and not stop.is_set() and time.monotonic() < deadline):
            stop.wait(2)

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


# ---- update lifecycle: detect upstream, prompt the dashboard, apply on request ---- #
# The tracker owns ALL updating now (lib/sc-run.sh no longer fetches/prompts). A background
# loop fetches the configured remote/branch; when it moves past HEAD it either applies
# (update_mode=auto) or records an UpdateState + bumps the snapshot version so every open
# dashboard shows an "update available" banner (update_mode=prompt). Applying does
# fetch -> reset --hard -> optional pip -> re-exec; the port handoff + the dashboard's
# asset-hash reload (server._assets_version) finish the swap.


class UpdateState:
    """Shared 'is a new build available?' state between the poller, the SSE snapshot (which
    surfaces it to the dashboard banner), the apply endpoint, and a settings-driven apply.
    Thread-safe."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.available = False
        self.current = ""             # short hash we're running
        self.latest = ""              # short hash upstream offers
        self.latest_full = ""         # full hash, for dismiss bookkeeping
        self.compare_url: str | None = None
        self.dismissed = ""           # full hash the user dismissed (don't re-prompt for it)
        self.applying = False

    def offer(self, current: str, latest: str, latest_full: str,
              compare_url: str | None) -> bool:
        """Record an available update unless the user already dismissed this exact commit.
        Returns True when the banner state changed, so the caller bumps the snapshot version."""
        with self.lock:
            if latest_full and latest_full == self.dismissed:
                return False
            changed = not (self.available and self.latest_full == latest_full)
            self.available = True
            self.current, self.latest, self.latest_full = current, latest, latest_full
            self.compare_url = compare_url
            return changed

    def clear(self) -> None:
        with self.lock:
            self.available = False

    def dismiss(self) -> None:
        with self.lock:
            self.dismissed = self.latest_full
            self.available = False

    def as_dict(self) -> dict:
        with self.lock:
            d = {"available": self.available, "current": self.current,
                 "latest": self.latest, "compare_url": self.compare_url}
        d["mode"] = settings.resolve_str("update_mode")   # cheap read, outside the lock
        return d


def _live_update_secs() -> int:
    """Poll interval for the update loop; <= 0 disables periodic checks. Default 900s (15m).
    env STARLOGGER_LIVE_UPDATE_SECS > settings.json > default."""
    return settings.resolve_int("live_update_secs")


def _git(repo: str, *args: str, check: bool = True) -> str | None:
    """Run `git -C repo args`, returning stdout. None on failure when check=False
    (e.g. an offline fetch); raises CalledProcessError when check=True."""
    out = subprocess.run(["git", "-C", repo, *args],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        if check:
            raise subprocess.CalledProcessError(out.returncode, out.args, out.stdout, out.stderr)
        return None
    return out.stdout


def _repo_ready() -> str | None:
    """BASE_DIR if updates may run there: a git clone with a CLEAN tree that is NOT its own
    update source.

    The dirty-tree guard keeps reset --hard from clobbering a dev checkout mid-edit. It looks at
    TRACKED changes only (--untracked-files=no): reset --hard never touches untracked files, so
    stray runtime artifacts in a managed install (e.g. *.bak backups) must not block updates --
    only uncommitted edits to tracked files (which a reset would wipe) should.

    The self-source guard keeps a tracker run straight from the dev tree -- with update_remote
    pointing back at that same tree (e.g. a managed install and a dev-folder run sharing one
    data dir, where update_remote is the dev path) -- from fetching itself and reset --hard'ing
    the dev checkout onto its own FETCH_HEAD, which would wipe in-progress branches/commits.
    The managed install (a different dir that pulls FROM the dev tree) compares unequal here, so
    it still updates normally."""
    repo = BASE_DIR
    if not os.path.isdir(os.path.join(repo, ".git")):
        return None
    if (_git(repo, "status", "--porcelain", "--untracked-files=no", check=False) or "").strip():
        return None       # tracked changes a reset would clobber; untracked files are safe
    src = os.path.expanduser(settings.resolve_str("update_remote"))
    if os.path.isdir(src) and os.path.realpath(src) == os.path.realpath(repo):
        return None                       # update source IS this checkout -> never git-op it
    return repo


def _remote_compare_url(repo: str, remote: str, have: str, want: str) -> str | None:
    """A GitHub compare URL (current...latest) for the banner's 'View changes' link, or None
    for a non-GitHub / local remote (a filesystem path, or a fork with no web diff)."""
    url = (_git(repo, "remote", "get-url", remote, check=False) or "").strip() or remote
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return f"https://github.com/{m.group(1)}/compare/{have}...{want}" if m else None


def _is_shallow(repo: str) -> bool:
    """True if ``repo`` is a shallow clone (a managed install cloned --depth 1)."""
    return (_git(repo, "rev-parse", "--is-shallow-repository", check=False) or "").strip() == "true"


def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    """True if ``ancestor`` is an ancestor of (or equal to) ``descendant`` -- i.e. ``descendant``
    already contains it. ``git merge-base --is-ancestor`` exits 0 when so (1 = not, !=0/1 = error,
    both -> False via _git's check=False)."""
    return _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False) is not None


def _upstream_current(repo: str, have: str, want: str) -> bool:
    """Whether ``want`` (upstream) is NOT something to update to: identical, or already contained
    in ``have`` (we're ahead -- e.g. the tracker is run from a dev checkout that's ahead of origin,
    where blindly resetting to upstream would DELETE local commits). Only a genuinely-ahead
    upstream counts as an update."""
    return have == want or _is_ancestor(repo, want, have)


def _fetch_target(repo: str, remote: str, branch: str) -> tuple[str, str] | None:
    """git fetch the remote/branch and return (have, want) = HEAD vs FETCH_HEAD full hashes,
    or None when offline / the fetch failed. Detection only -- never resets.

    ``--depth 1`` is used ONLY when the repo is ALREADY shallow (a managed install), to keep it
    small. A shallow fetch against a FULL clone writes .git/shallow and grafts its history short
    -- which silently turned a dev checkout (the tracker is run straight from its source tree)
    into a 1-commit-deep shallow repo. So a full clone always gets a normal fetch, which can
    never introduce shallowness; an already-shallow install stays shallow."""
    fetch = ["fetch", "--quiet", remote, branch]
    if _is_shallow(repo):
        fetch[1:1] = ["--depth", "1"]
    if _git(repo, *fetch, check=False) is None:
        return None
    have = (_git(repo, "rev-parse", "HEAD") or "").strip()
    want = (_git(repo, "rev-parse", "FETCH_HEAD") or "").strip()
    return (have, want) if have and want else None


def _apply(ustate: "UpdateState", trigger_restart) -> bool:
    """Fetch + reset --hard to upstream, pip-install if requirements.txt changed, then
    trigger a restart (re-exec into the new code). Lock-guarded so the poller, the apply
    endpoint, and a settings-change can't apply at once. False = nothing to do."""
    with ustate.lock:
        if ustate.applying:
            return False
        ustate.applying = True
    try:
        repo = _repo_ready()
        if not repo:
            return False
        remote = settings.resolve_str("update_remote")
        branch = settings.resolve_str("update_branch")
        target = _fetch_target(repo, remote, branch)
        if not target or _upstream_current(repo, *target):
            return False                      # offline, already current, or we're ahead of upstream
        have, want = target
        changed = (_git(repo, "diff", "--name-only", "HEAD", "FETCH_HEAD", check=False) or "")
        _git(repo, "reset", "--hard", "FETCH_HEAD")
        if "requirements.txt" in changed.split():
            try:                              # deps moved -> install before re-exec (sc-run.sh used to)
                subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                                "--disable-pip-version-check", "-r",
                                os.path.join(repo, "requirements.txt")], timeout=300)
            except Exception as e:
                print(f"[update] pip install failed (continuing): {e}", flush=True)
        print(f"[update] {have[:9]} -> {want[:9]}; restarting to apply", flush=True)
        trigger_restart()                     # restart Event + httpd.shutdown() -> main finally -> _reexec
        return True
    finally:
        with ustate.lock:
            ustate.applying = False


def _check_update(ustate: "UpdateState", state, trigger_restart) -> None:
    """One poll: skip when off / not a clean clone / offline. A new upstream commit either
    applies immediately (update_mode=auto) or is recorded + pushed to the dashboard banner
    (update_mode=prompt)."""
    mode = settings.resolve_str("update_mode")
    if mode == "off":
        return
    repo = _repo_ready()
    if not repo:
        return
    remote = settings.resolve_str("update_remote")
    branch = settings.resolve_str("update_branch")
    target = _fetch_target(repo, remote, branch)
    if not target:
        return                                # offline / fetch failed
    have, want = target
    if _upstream_current(repo, have, want):
        ustate.clear()                        # in sync, or we're ahead of upstream (no downgrade)
        return
    if mode == "auto":
        _apply(ustate, trigger_restart)
        return
    if ustate.offer(have[:9], want[:9], want, _remote_compare_url(repo, remote, have, want)):
        state.bump_version()                  # push the banner to every open dashboard


def _manual_check(ustate: "UpdateState", state, trigger_restart) -> dict:
    """The dashboard's explicit 'Check for updates' button: fetch now and, if a new build
    exists, apply it immediately -- the click is the approval, so no banner/prompt and the
    Updates mode is bypassed entirely. Returns a status the panel surfaces inline."""
    repo = _repo_ready()
    if not repo:
        return {"ok": False, "status": "blocked"}    # dirty tree / not a clone
    remote = settings.resolve_str("update_remote")
    branch = settings.resolve_str("update_branch")
    target = _fetch_target(repo, remote, branch)
    if not target:
        return {"ok": False, "status": "offline"}     # fetch failed / no network
    have, want = target
    if _upstream_current(repo, have, want):           # in sync, or we're ahead of upstream
        if ustate.available:                          # drop a now-stale banner, push the clear
            ustate.clear()
            state.bump_version()
        return {"ok": True, "status": "current", "build": have[:9]}
    # New build -> apply now, off the request thread (httpd.shutdown blocks); the asset-hash
    # reload swaps this tab into it and the completion toast fires post-reload.
    threading.Thread(target=lambda: _apply(ustate, trigger_restart), daemon=True).start()
    return {"ok": True, "status": "updating", "current": have[:9], "latest": want[:9]}


def _check_due(last_check: float | None, now: float, secs: int) -> bool:
    """Whether update_loop should run a check now. Never when disabled (secs <= 0);
    immediately when it has never checked (the initial post-launch check); otherwise once
    `secs` have elapsed since the last check. Crucially this is evaluated against the
    *current* interval every tick, so SHORTENING the interval in the Settings panel takes
    effect within one loop tick instead of waiting out the old interval."""
    if secs <= 0:
        return False
    if last_check is None:
        return True
    return (now - last_check) >= secs


def update_loop(stop: threading.Event, ustate: "UpdateState", state, trigger_restart) -> None:
    """Daemon: an initial check ~20s after start (so a launch-time update is offered promptly,
    like the old dialog), then every live_update_secs. The interval is re-read on a short tick
    (not just once per check), so changing it in the Settings panel takes effect within a few
    seconds rather than after the old interval elapses. interval <= 0 genuinely disables checks
    but keeps the loop alive so re-enabling resumes them."""
    if stop.wait(20):                         # interruptible initial delay; True => shutting down
        return
    last_check: float | None = None           # None => the initial check is due now
    while True:
        if _check_due(last_check, time.monotonic(), _live_update_secs()):
            try:
                _check_update(ustate, state, trigger_restart)
            except Exception as e:            # transient git/network error -> retry next tick
                print(f"[update] check failed: {e}", flush=True)
            last_check = time.monotonic()
        # Re-read the interval every few seconds so a settings change applies promptly; `stop`
        # still interrupts instantly for shutdown. Re-reads are cheap (settings.json is mtime-cached).
        if stop.wait(5.0):                    # True => shutting down
            return


def _reexec() -> None:
    """Replace this process with a fresh tracker using the same args/env. The server is
    already stopped and the port released (main's finally), so the replacement binds
    directly. POSIX: os.execv keeps the PID and inherits the environment, so launcher-death
    detection survives the swap -- under --launch the game is still our child and the
    replacement re-adopts it via $STARLOGGER_GAME_PID (see ignition.launch_game); a legacy
    setpriv --pdeathsig USR1 link likewise survives (main re-arms the SIGUSR1 handler).
    Windows has neither, so spawn-and-exit and let the existing port handoff take over."""
    sys.stdout.flush()
    sys.stderr.flush()
    # Preserve interpreter flags (-u, -O, ...) too: they live in orig_argv, not argv.
    # (orig_argv is 3.10+; fall back to a plain rebuild on older runtimes.)
    orig = getattr(sys, "orig_argv", None)
    argv = [sys.executable, *orig[1:]] if orig else [sys.executable, *sys.argv]
    if IS_WINDOWS:
        subprocess.Popen(argv)                # new process; we then return -> main exits
    else:
        os.execv(sys.executable, argv)        # never returns


# ---- jukebox: the background music build's progress holder ---- #
# The soundtrack is extracted automatically by catalogs.refresh_loop (once on first run, then on a
# major game-version move), like the DataCore catalogs -- no button. MusicState carries that
# build's decode progress to the dashboard the same way UpdateState carries the update banner:
# merged into the SSE snapshot, pushed by state.bump_version() from the refresh loop.


class MusicState:
    """Shared progress for the background music build, surfaced to the dashboard via the SSE
    snapshot. Thread-safe."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phase = "idle"          # idle | extracting | done | error
        self.done = 0                # full songs decoded so far
        self.total = 0              # full songs the bank yields
        self.error = ""

    def set(self, *, phase: str | None = None, done: int | None = None,
            total: int | None = None, error: str | None = None) -> None:
        with self.lock:
            if phase is not None:
                self.phase = phase
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total
            if error is not None:
                self.error = error

    def as_dict(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "done": self.done,
                    "total": self.total, "error": self.error}


def main() -> None:
    ap = argparse.ArgumentParser(description="Starlogger -- Star Citizen cargo/flight logger + dashboard")
    ap.add_argument("--log", help="path to Game.log (auto-detected if omitted)")
    ap.add_argument("--host", default=None,
                    help="address to bind the dashboard server on "
                         "(default: the 'Bind address' setting, else 127.0.0.1)")
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
    ap.add_argument("--launch", action="store_true",
                    help="spawn the LUG sc-launch.sh as a child and tie its lifetime to ours "
                         "(the dashboard drives the game; Linux only)")
    args = ap.parse_args()

    # Bind address: an explicit --host wins; otherwise fall to the configurable setting
    # (env STARLOGGER_HOST > settings.json > 127.0.0.1). 0.0.0.0 exposes the dashboard to
    # the local network.
    if args.host is None:
        args.host = settings.resolve_str("bind_host")

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

    # Lifecycle: live while a dashboard is open OR (on Linux) the launcher runs. The SSE
    # stream count is the dashboard-presence signal; launcher death (flagged via
    # presence.mark_launcher_dead) doesn't kill us, so we linger for post-session review and
    # the watchdog releases us once the last tab closes. Death is observed two ways, both
    # routing to mark_launcher_dead:
    #   - parent model (--launch): the game is OUR child; its exit fires the ignition watcher;
    #   - legacy: SIGUSR1 from a run-tracker.sh started with setpriv --pdeathsig USR1.
    presence = Presence()
    has_launcher_detection = hasattr(signal, "SIGUSR1") and not IS_WINDOWS
    if has_launcher_detection:
        # Register before serving: the default SIGUSR1 disposition terminates the process.
        def _on_launcher_death(signum, frame):  # main thread; keep trivial
            presence.mark_launcher_dead()
        signal.signal(signal.SIGUSR1, _on_launcher_death)

    # Parent model: spawn the LUG launcher as our child (and refresh StarStrings) so its exit
    # ties the tracker's lifetime to the game's -- no parent-death signal needed. Done BEFORE
    # the port-takeover dance so the game always launches promptly (as the old shell launcher
    # did): a wedged old instance must never prevent the game from starting, and the new game's
    # `wineserver -k` helps the old session release :8765 sooner.
    if args.launch and not IS_WINDOWS:
        ignition.launch_game(log_path, on_exit=presence.mark_launcher_dead)

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
    restart = threading.Event()   # set by an apply -> re-exec in the finally below
    ustate = UpdateState()        # 'new build available?' state shared with the dashboard banner
    mstate = MusicState()         # jukebox extraction progress, shared with the dashboard

    def on_epoch_change(prev: int, new: int) -> None:  # runs under state.lock -> stay cheap
        print(f"[epoch] server build {prev} -> {new}; scheduling cleanup")
        epoch_trigger.set()

    state.on_epoch_change = on_epoch_change
    threading.Thread(target=tail_loop, args=(log_path, state, stop), daemon=True).start()
    # backfill the session archive from logbackups in the background (skips ones
    # already archived) so a fresh data dir self-populates without a manual --rebuild.
    threading.Thread(target=backfill_archive, args=(log_path, stop), daemon=True).start()
    threading.Thread(target=catalogs.refresh_loop, args=(state, stop, log_path),
                     kwargs={"music_state": mstate}, daemon=True).start()
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
    elif not args.no_browser and settings.resolve_bool("open_browser"):
        threading.Thread(target=_open_browser_when_ready,
                         args=(args.host, args.port, url), daemon=True).start()
    # make_server (not app.run) so the watchdog can stop us via httpd.shutdown() -- a
    # thread-safe call that returns serve_forever() cleanly, without relying on a signal
    # (a self-SIGINT is dropped when we're backgrounded; see shutdown_watchdog).
    app = create_app(state, log_path, presence=presence, update_state=ustate,
                     music_state=mstate)
    httpd = make_server(args.host, args.port, app, threaded=True)
    # Let a newer launch replace us via POST /api/quit (see _ask_existing_to_quit).
    app.config["QUIT_FN"] = httpd.shutdown
    # Restart = re-exec into freshly-applied code: flag it, then stop the server cleanly
    # (shutdown returns serve_forever -> the finally below -> _reexec). The apply endpoint
    # and the settings-driven apply run on_apply OFF-thread so their HTTP response returns
    # before the server goes down.
    def trigger_restart() -> None:
        restart.set()
        httpd.shutdown()
    def on_apply() -> None:
        threading.Thread(target=lambda: _apply(ustate, trigger_restart), daemon=True).start()
    app.config["ON_APPLY"] = on_apply
    # A plain re-exec (no git update): the bind address is only read at startup, so changing
    # it in the Settings panel re-execs us to rebind. Off-thread for the same reason as
    # on_apply -- httpd.shutdown() blocks, so let the HTTP response return first.
    def on_restart() -> None:
        threading.Thread(target=trigger_restart, daemon=True).start()
    app.config["ON_RESTART"] = on_restart
    # The 'Check for updates' button: check synchronously + apply immediately if there's a build.
    app.config["ON_CHECK_NOW"] = lambda: _manual_check(ustate, state, trigger_restart)
    threading.Thread(target=shutdown_watchdog,
                     args=(presence, stop, has_launcher_detection, _idle_timeout(),
                           time.monotonic(), httpd.shutdown, 2.0, _close_timeout()),
                     daemon=True).start()
    # The tracker owns updating: poll upstream, prompt the dashboard (or auto-apply).
    threading.Thread(target=update_loop, args=(stop, ustate, state, trigger_restart),
                     daemon=True).start()
    try:
        httpd.serve_forever()        # Ctrl-C (foreground) raises KeyboardInterrupt here
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()         # release :8765 promptly for a relaunch to take over
        if restart.is_set():
            _reexec()                # replace this process with the updated code (POSIX: no return)


if __name__ == "__main__":
    main()
