"""Launch orchestration: the tracker drives the game.

The dashboard process is the *parent* of the stock LUG ``sc-launch.sh`` (which in turn
launches the RSI Launcher, which launches the game). That inversion is why this module
exists: as the launcher's parent we observe its death as a plain child exit, so the old
``setpriv --pdeathsig USR1`` sibling trick (see the former ``lib/sc-run.sh``) is gone.

It also folds in the StarStrings ``global.ini`` refresh that used to live in shell, kicked
off at startup -- before the game reads the file -- so the localization is current.

Linux-only in practice: on native Windows there is no LUG launcher and the tracker runs
standalone (see ``run-tracker.bat``); ``launch_game`` is simply never called there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

from starlogger import config, settings

# The default StarStrings global.ini source. Re-exported here (the canonical value lives in
# config) so existing callers/tests can still reach it as ignition.STARSTRINGS_URL; the
# effective URL is resolved per-launch from the user setting, falling back to this.
STARSTRINGS_URL = config.STARSTRINGS_URL
# Where we remember the last-fetched ETag, so a conditional GET can stay quiet when the
# upstream file is unchanged. A sidecar in DATA_DIR replaces the old xattr-on-the-file dance.
_ETAG_PATH = os.path.join(config.DATA_DIR, "starstrings.etag")


def notify(app: str, urgency: str, summary: str) -> None:
    """Best-effort desktop notification. A missing notify-send (or any failure) is silent --
    a launch must never break because the notifier isn't there."""
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.run(
            ["notify-send", f"--app-name={app}", f"--urgency={urgency}", summary],
            check=False,
        )
    except OSError:
        pass


def update_starstrings(log_path: str) -> None:
    """Fetch the community global.ini if it changed upstream, and install it into the game's
    LIVE localization folder. Conditional on a cached ETag: an unchanged file (HTTP 304) is a
    quiet no-op; a real update is written atomically and announced; any failure keeps whatever
    is already on disk. Best-effort throughout -- never raises into the caller."""
    live = os.path.dirname(log_path)                       # <prefix>/.../StarCitizen/LIVE
    dest_dir = os.path.join(live, "Data", "Localization", "english")
    dest = os.path.join(dest_dir, "global.ini")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return

    headers = {"User-Agent": config.USER_AGENT}
    # Send If-None-Match only when BOTH the file and a stored ETag exist -- otherwise we want a
    # full fetch (a stored ETag with no file would wrongly 304 us into never writing it).
    stored = _load_etag() if os.path.isfile(dest) else None
    if stored:
        headers["If-None-Match"] = stored

    # The user can point at a different global.ini; an empty/unset setting uses the default.
    url = settings.resolve_str("starstrings_url") or config.STARSTRINGS_URL
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            new_etag = resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:                                  # unchanged upstream -> stay quiet
            return
        notify("StarStrings", "critical", "Update failed — using existing global.ini")
        return
    except (urllib.error.URLError, OSError, TimeoutError):
        notify("StarStrings", "critical", "Update failed — using existing global.ini")
        return

    if not data:
        return
    try:
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)                              # atomic swap into place
    except OSError:
        notify("StarStrings", "critical", "Update failed — using existing global.ini")
        return
    if new_etag:
        _save_etag(new_etag)
    notify("StarStrings", "normal", "global.ini updated")


def _load_etag() -> str | None:
    try:
        with open(_ETAG_PATH, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_etag(etag: str) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(_ETAG_PATH, "w", encoding="utf-8") as f:
            f.write(etag)
    except OSError:
        pass


def resolve_sc_launch(log_path: str) -> str | None:
    """Locate the stock LUG ``sc-launch.sh``. It lives at the Wine prefix root: prefer an
    explicit $WINEPREFIX, else derive the prefix from the Game.log path (the part before
    ``/drive_c/``). Returns the path only if it exists, else None."""
    wp = os.environ.get("WINEPREFIX")
    if wp:
        cand = os.path.join(os.path.expanduser(wp), "sc-launch.sh")
        return cand if os.path.isfile(cand) else None
    marker = os.sep + "drive_c" + os.sep
    idx = log_path.find(marker)
    if idx == -1:
        return None
    cand = os.path.join(log_path[:idx], "sc-launch.sh")
    return cand if os.path.isfile(cand) else None


def launch_game(log_path: str, on_exit) -> int | None:
    """Start (or re-adopt) the game and tie its lifetime to ours. Returns the launcher PID,
    or None if no launcher could be started (the tracker then just serves without a game).

    The PID is published in $STARLOGGER_GAME_PID. On a mid-session self-update the tracker
    re-execs (os.execv keeps our PID -> the launcher is still our child); the replacement sees
    that env var and ADOPTS the running launcher -- watching it instead of starting a second
    game -- so launcher-death detection survives the update.
    """
    adopted = os.environ.get("STARLOGGER_GAME_PID")
    if adopted:
        # A previous instance of us already launched it. Watch the existing PID; never spawn a
        # second game. If it's already gone, the watcher resolves immediately (correct).
        try:
            pid = int(adopted)
        except ValueError:
            pid = None
        if pid is not None:
            _watch_until_exit(lambda: os.waitpid(pid, 0), on_exit)
            return pid
        os.environ.pop("STARLOGGER_GAME_PID", None)        # malformed -> fall through to spawn

    sc_launch = resolve_sc_launch(log_path)
    if not sc_launch or not os.access(sc_launch, os.X_OK):
        print("[ignition] no executable sc-launch.sh found — serving without a game launch")
        return None

    # Refresh StarStrings concurrently with the launch (not before it): the RSI Launcher takes
    # seconds to come up and only then is the game started, so the fetch comfortably lands
    # before global.ini is read, without ever delaying the launch on a slow network. Skipped
    # entirely when the user has turned the global.ini download off.
    if settings.resolve_bool("starstrings_enabled"):
        threading.Thread(target=update_starstrings, args=(log_path,), daemon=True).start()

    try:
        proc = subprocess.Popen([sc_launch])
    except OSError as e:
        notify("Starlogger", "critical", "could not launch the game")
        print(f"[ignition] launch failed: {e}")
        return None
    os.environ["STARLOGGER_GAME_PID"] = str(proc.pid)
    _watch_until_exit(proc.wait, on_exit)
    return proc.pid


def _watch_until_exit(waiter, on_exit) -> None:
    """Run ``waiter`` (which blocks until the launcher exits) on a daemon thread, then fire
    ``on_exit``. A missing/already-reaped child just resolves to 'exited' -- the safe default,
    since we only use this to mark the launcher gone."""
    def run() -> None:
        try:
            waiter()
        except (ChildProcessError, OSError):
            pass
        on_exit()
    threading.Thread(target=run, daemon=True).start()
