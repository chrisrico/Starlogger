"""Shared pytest fixtures, incl. the headless-browser (Playwright) e2e harness.

HARD ISOLATION (see tests/test_e2e.py): browser tests must NEVER touch the user's live
install (~/.local/share/starlogger) or the source tree. config.DATA_DIR and every derived
path (settings.json, music/, music_curation.json, …) are computed at import time from
STARLOGGER_DATA_DIR, so we point that at a throwaway temp dir BEFORE importing any starlogger
module. A fail-fast guard in `live_server` refuses to run if isolation didn't take.

The live server is the REAL app (create_app) over that empty temp data dir — every loader
tolerates missing data — so nothing is stubbed; we only SEED a few synthetic music tracks
(a generated silent ogg, not p4k data) so the jukebox has content.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading

# --- isolation: redirect ALL data paths to a temp dir BEFORE importing starlogger ---------
_TMP_DATA = tempfile.mkdtemp(prefix="starlogger-e2e-")
os.environ["STARLOGGER_DATA_DIR"] = _TMP_DATA
os.environ["STARLOGGER_LOG"] = os.path.join(_TMP_DATA, "Game.log")   # fake; never tailed for real
os.environ.pop("STARLOGGER_MUSIC_AUTOPLAY", None)                    # tests own this knob
atexit.register(lambda: shutil.rmtree(_TMP_DATA, ignore_errors=True))

import pytest
from werkzeug.serving import make_server

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import config, settings
import starlogger.server as server
from starlogger.state import State

# A few synthetic tracks, all backed by one tiny generated SILENT ogg (so playback/seek/metadata
# work for real, deterministically, with no copyrighted p4k audio and nothing outside the temp dir).
SEED_TRACKS = [
    {"id": "t1", "file": "t1.ogg", "duration": 2.0, "size": 4096},
    {"id": "t2", "file": "t2.ogg", "duration": 2.0, "size": 4096},
    {"id": "t3", "file": "t3.ogg", "duration": 2.0, "size": 4096},
]


def pytest_configure(config):  # noqa: A002 - pluggy matches this hook arg by name
    config.addinivalue_line(
        "markers", "browser: end-to-end tests needing a real headless browser "
                   "(skip with -m 'not browser')")


def _assert_isolated():
    """Refuse to run if anything would touch the real install or escape the temp dir."""
    real = os.path.realpath(os.path.expanduser("~/.local/share/starlogger"))
    assert os.path.realpath(config.DATA_DIR) == os.path.realpath(_TMP_DATA), \
        f"DATA_DIR not isolated: {config.DATA_DIR}"
    for p in (config.SETTINGS_PATH, config.MUSIC_DIR, config.MUSIC_CURATION_PATH):
        assert os.path.realpath(p).startswith(os.path.realpath(_TMP_DATA)), f"escapes temp: {p}"
        assert not os.path.realpath(p).startswith(real), f"points at live install: {p}"


def _seed_music():
    os.makedirs(config.MUSIC_DIR, exist_ok=True)
    silence = os.path.join(config.MUSIC_DIR, "_silence.ogg")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "2", "-c:a", "libvorbis", silence],
        check=True, capture_output=True)
    for t in SEED_TRACKS:
        shutil.copyfile(silence, os.path.join(config.MUSIC_DIR, t["file"]))
    with open(config.MUSIC_PATH, "w") as f:
        json.dump({"tracks": SEED_TRACKS, "count": len(SEED_TRACKS), "game_version": "test"}, f)


@pytest.fixture(scope="session")
def live_server():
    """The real Flask app on an ephemeral port, in a daemon thread (Playwright needs a real
    HTTP server, not test_client). Yields the base URL."""
    _assert_isolated()
    _seed_music()
    app = server.create_app(State(), log_path=os.environ["STARLOGGER_LOG"])
    # threaded=True is REQUIRED: the dashboard holds a long-lived SSE stream (/api/stream) open,
    # which would monopolize a single-threaded server and hang every later request.
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@pytest.fixture(autouse=True)
def _reset_settings():
    """Keep per-test isolation of the (temp) settings.json — a setting changed by one test
    must not leak into the next. Runs for every test; cheap and harmless for non-browser ones."""
    try:
        with open(config.SETTINGS_PATH, "w") as f:
            json.dump({}, f)
        settings._cache["mtime"] = None
    except OSError:
        pass
    yield


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # Let <audio>.play() start without a prior gesture (autoplay test) and stay silent in CI.
    return {**browser_type_launch_args,
            "args": ["--autoplay-policy=no-user-gesture-required", "--mute-audio"]}


@pytest.fixture(scope="session")
def require_browser():
    """Skip the e2e suite gracefully when Chromium isn't installed (so plain `pytest` still
    passes for contributors without it). The check runs in a SUBPROCESS — calling the Playwright
    sync API in-process clashes with pytest-playwright's own event loop. On this machine the
    cached Chromium launches."""
    import subprocess
    import sys
    probe = ("from playwright.sync_api import sync_playwright\n"
             "with sync_playwright() as p:\n"
             "    p.chromium.launch().close()\n")
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    if r.returncode != 0:                                   # pragma: no cover
        pytest.skip("Chromium unavailable (run `playwright install chromium`): "
                    + (r.stderr or "").strip()[-300:])
