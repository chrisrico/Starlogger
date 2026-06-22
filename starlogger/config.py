"""Filesystem paths and constants shared across the package."""

from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform == "win32"

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(PKG_DIR)

# Generated data files live in DATA_DIR. STARLOGGER_DATA_DIR is the explicit override
# (the containerized service points it at a persistent volume so code, baked into the
# image, and data, on the host, stay separate). Otherwise it follows the XDG Base
# Directory spec on Linux ($XDG_DATA_HOME/starlogger, default ~/.local/share/starlogger)
# and %LOCALAPPDATA%\starlogger on Windows.
def _default_data_dir() -> str:
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "starlogger")
    # XDG spec: honor $XDG_DATA_HOME only if set and absolute, else ~/.local/share.
    xdg = os.environ.get("XDG_DATA_HOME")
    if not (xdg and os.path.isabs(xdg)):
        xdg = os.path.expanduser("~/.local/share")
    return os.path.join(xdg, "starlogger")


DATA_DIR = os.environ.get("STARLOGGER_DATA_DIR") or _default_data_dir()

WEB_DIR = os.path.join(BASE_DIR, "web")  # static assets always ship with the code
OVERRIDES_PATH = os.path.join(DATA_DIR, "overrides.json")
SHIP_CARGO_PATH = os.path.join(DATA_DIR, "ships.json")
# Commodity GUID->name and location code->name, built together from Data.p4k and
# kept in one file (see reference.py). Was commodities.json + locations.json.
REFERENCE_PATH = os.path.join(DATA_DIR, "reference.json")
# Mineable-rock RS (radar signature) + composition, built from the full DataCore
# extract (its own file/trigger, like ships.json -- the data can't be pulled
# via the cheap query path reference.json uses). See mineables.py.
MINEABLES_PATH = os.path.join(DATA_DIR, "mineables.json")
# Mining equipment (laser heads + consumable modules), from the same full DataCore
# extract as mineables -- the gear side of the rock-feasibility calc. See mining_gear.py.
MINING_GEAR_PATH = os.path.join(DATA_DIR, "mining_gear.json")
# Ship radar components, from the same full DataCore extract -- carries the resource-signature
# (RS) detection stat that ranks a radar for mining (deposit identification range). Folded into
# the per-ship loadout the mining UI tracks (head + modules + radar). See radar.py.
RADAR_PATH = os.path.join(DATA_DIR, "radar.json")
# Salvageable wrecks (ship-debris hulls + panels) and their radar signature, from the same
# full DataCore extract as mineables. Salvage has no mineral composition -- only a signature
# that identifies a target (whole-ship hull or n debris panels). Feeds the rock-lookup
# Identify tab as a separate "salvage targets" section. See salvageables.py.
SALVAGEABLES_PATH = os.path.join(DATA_DIR, "salvageables.json")
# Salvageable wreck ships (every ship with an *_Unmanned_Salvage variant) -> the removable
# components the salvage beam can strip, built from the same full DataCore extract + per-ship
# loadouts. Feeds the Salvage mode's Ship-ID panel. See salvage_ships.py.
SALVAGE_SHIPS_PATH = os.path.join(DATA_DIR, "salvage_ships.json")
# Crafting blueprints + their material requirements, from the same full DataCore
# extract (links a blueprint -> the minerals it needs -> the rocks that yield them).
# See blueprints.py.
BLUEPRINTS_PATH = os.path.join(DATA_DIR, "blueprints.json")
# Contract template taxonomy + cargo manifests, from the same full DataCore extract
# (the authoritative grade/route/SCU-cap/rep/legal table behind contract classification).
# See contracts.py.
CONTRACTS_PATH = os.path.join(DATA_DIR, "contracts.json")
# The game's own mobiGlas mission-type icons (one SVG per type slug), extracted from the
# p4k alongside contracts.json and served at /mission-icons/<slug>.svg. p4k-derived =>
# gitignored, never committed (same rule as the .json caches).
MISSION_ICONS_DIR = os.path.join(DATA_DIR, "mission_icons")
# Decoded game music (one Ogg Vorbis per Wwise WEM id), extracted on demand from the p4k
# for the jukebox tab and served at /music/<id>.ogg, with a manifest in music.json. Heavy
# (~2.6 GB) and p4k-derived => gitignored, never committed (same rule as the .json caches).
MUSIC_DIR = os.path.join(DATA_DIR, "music")
MUSIC_PATH = os.path.join(DATA_DIR, "music.json")
# Per-user jukebox curation: playlist order, hidden tracks, and custom names (no real names
# exist in the data -- see music.py). Keyed by stable WEM id; this LOCAL sidecar overlays the
# shipped default below, so user edits survive a re-extract. Local => gitignored.
MUSIC_CURATION_PATH = os.path.join(DATA_DIR, "music_curation.json")
# Shipped default curation (same shape), hand-authored and committed so a fresh install starts
# named/ordered. Pure metadata (ids + names + order), NOT p4k-derived audio -> safe to track;
# must never carry the StarBreaker source marker the pre-commit hook scans for.
DEFAULT_MUSIC_CURATION_PATH = os.path.join(PKG_DIR, "default_music_curation.json")
TRADE_FLAGS_PATH = os.path.join(DATA_DIR, "trade_flags.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
STATION_NAMES_PATH = os.path.join(DATA_DIR, "station_names.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
# Per-install bearer token gating the mutating API (see server._enforce_guard). Generated
# on first run, persisted 0600, reused across restarts/re-exec. Local secret => gitignored.
API_TOKEN_PATH = os.path.join(DATA_DIR, "api_token")
SESSIONS_KEEP = 300  # cap archived session history

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) starlogger" if IS_WINDOWS
    else "Mozilla/5.0 (X11; Linux x86_64) starlogger"
)

# Community localization fork: a richer global.ini that names many otherwise-blank
# entities. The default source for the launch-time refresh (see ignition.update_starstrings);
# the Settings panel can point at a different global.ini, and an empty setting falls back here.
STARSTRINGS_URL = (
    "https://raw.githubusercontent.com/MrKraken/StarStrings/refs/heads/master/"
    "Data/Localization/english/global.ini"
)

# `Game.log` (and `Data.p4k` beside it) live in the game's LIVE folder. On native
# Windows that's under %PROGRAMFILES%; on Linux it's inside the Wine/Proton prefix.
# We probe every platform's candidates (a stray isfile() on the wrong OS is cheap and
# the path just won't exist) -- STARLOGGER_LOG, when set, wins and is the escape hatch for
# non-default install drives/folders. When $WINEPREFIX is set we derive its LIVE/PTU log
# (next-best after STARLOGGER_LOG, ahead of the hardcoded prefix guesses below).
_PROGRAM_FILES = os.environ.get("PROGRAMFILES", r"C:\Program Files")
_RSI = ("Roberts Space Industries", "StarCitizen")
_WINEPREFIX = os.path.expanduser(os.environ["WINEPREFIX"]) if os.environ.get("WINEPREFIX") else None
DEFAULT_LOG_CANDIDATES = [
    p for p in (
        os.path.expanduser(os.environ["STARLOGGER_LOG"]) if os.environ.get("STARLOGGER_LOG") else None,
        # explicit Wine/Proton prefix via $WINEPREFIX (LIVE, then PTU)
        os.path.join(_WINEPREFIX, "drive_c", "Program Files", *_RSI, "LIVE", "Game.log") if _WINEPREFIX else None,
        os.path.join(_WINEPREFIX, "drive_c", "Program Files", *_RSI, "PTU", "Game.log") if _WINEPREFIX else None,
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
