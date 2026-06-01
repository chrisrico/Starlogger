"""Filesystem paths and constants shared across the package."""

from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform == "win32"

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PKG_DIR)

# Generated data files live in DATA_DIR; defaults to the repo root for bare-metal
# runs, but the containerized service points SCMT_DATA_DIR at a persistent volume
# so code (baked into the image) and data (on the host) stay separate.
DATA_DIR = os.environ.get("SCMT_DATA_DIR", BASE_DIR)

WEB_DIR = os.path.join(BASE_DIR, "web")  # static assets always ship with the code
OVERRIDES_PATH = os.path.join(DATA_DIR, "overrides.json")
SHIP_CARGO_PATH = os.path.join(DATA_DIR, "ships_cargo.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
STATION_NAMES_PATH = os.path.join(DATA_DIR, "station_names.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
SESSIONS_KEEP = 300  # cap archived session history

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) sc-mission-tracker" if IS_WINDOWS
    else "Mozilla/5.0 (X11; Linux x86_64) sc-mission-tracker"
)

# `Game.log` (and `Data.p4k` beside it) live in the game's LIVE folder. On native
# Windows that's under %PROGRAMFILES%; on Linux it's inside the Wine/Proton prefix.
# We probe every platform's candidates (a stray isfile() on the wrong OS is cheap and
# the path just won't exist) -- SCMT_LOG, when set, wins and is the escape hatch for
# non-default install drives/folders.
_PROGRAM_FILES = os.environ.get("PROGRAMFILES", r"C:\Program Files")
_RSI = ("Roberts Space Industries", "StarCitizen")
DEFAULT_LOG_CANDIDATES = [
    p for p in (
        os.path.expanduser(os.environ["SCMT_LOG"]) if os.environ.get("SCMT_LOG") else None,
        # native Windows install (LIVE, then PTU)
        os.path.join(_PROGRAM_FILES, *_RSI, "LIVE", "Game.log"),
        os.path.join(_PROGRAM_FILES, *_RSI, "PTU", "Game.log"),
        # Linux Wine/Proton prefixes
        os.path.expanduser(
            "~/Games/star-citizen/drive_c/Program Files/Roberts Space Industries/"
            "StarCitizen/LIVE/Game.log"
        ),
        os.path.expanduser(
            "~/.wine/drive_c/Program Files/Roberts Space Industries/StarCitizen/LIVE/Game.log"
        ),
    ) if p
]


def find_log() -> str | None:
    for p in DEFAULT_LOG_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def find_log_backups(log_path: str) -> list[str]:
    """Past-session logs SC keeps in a sibling `logbackups/` folder, oldest first."""
    d = os.path.join(os.path.dirname(log_path), "logbackups")
    if not os.path.isdir(d):
        return []
    files = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".log")]
    files.sort(key=lambda p: os.path.getmtime(p))
    return files
