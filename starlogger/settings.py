"""Persistent key/value settings store (settings.json in DATA_DIR) plus the
user-configurable schema behind the dashboard's Settings panel.

Two kinds of keys live in the same file:

  * `selected_ship` -- the ship the user picked by hand, used to drive the capacity
    gauge / cargo grid when the game log hasn't detected one (a detected ship always
    wins; see snapshot.build_snapshot). Read/written via get_settings / set_setting.

  * the CONFIG_SCHEMA knobs below -- runtime + self-update settings that used to be
    environment variables only. They resolve with precedence **env var > settings.json
    > built-in default**, so an env var stays an escape hatch (and every existing
    test/dev workflow that sets one keeps working), while the UI manages the persistent
    layer. `describe()` feeds the panel; `update()` validates + writes what it saves;
    `resolve_*` is how tracker.py (and lib/sc-run.sh, which reads the same file) read
    the effective value.

Mirrors the mtime-cached read / atomic write conventions in stations.py and
overrides.py, so a saved change is picked up live with no restart.
"""

from __future__ import annotations

import os

from .config import SETTINGS_PATH
from .jsonstore import atomic_write, load_cached, read_json

_cache: dict = {"mtime": None, "data": {}}


# The user-configurable knobs, in display order. Each descriptor carries everything
# both the resolver (env/coerce/clamp) and the UI (label/help/group/options) need, so
# there's a single source of truth. `env` is the escape-hatch variable; `env_toggle`
# marks a presence-style var (NO_BROWSER) whose *mere presence* forces False; `legacy_env`
# maps an old presence var to a fixed value (NO_UPDATE -> off, AUTO_UPDATE -> auto).
# `min` clamps numerics; `options` lists an enum's allowed values.
CONFIG_SCHEMA = [
    {
        "key": "open_browser", "type": "bool", "default": True,
        "env": "STARLOGGER_NO_BROWSER", "env_toggle": True,
        "group": "General", "label": "Auto-open dashboard",
        "help": "Open the dashboard in a browser when the tracker starts. "
                "Applies on the next launch.",
    },
    {
        "key": "music_autoplay", "type": "bool", "default": False,
        "env": "STARLOGGER_MUSIC_AUTOPLAY",
        "group": "General", "label": "Auto-play music",
        "help": "Start the jukebox automatically when the dashboard loads — resuming the "
                "last track, or starting the playlist. Your browser may keep it silent "
                "until you click the page.",
    },
    {
        "key": "idle_timeout", "type": "number", "default": 30.0, "min": 1.0,
        "env": "STARLOGGER_IDLE_TIMEOUT",
        "group": "Lifecycle", "label": "Idle shutdown (seconds)",
        "help": "How long the tracker lingers with no dashboard open (and, on Linux, "
                "the launcher gone) before exiting. Applies on the next launch.",
    },
    {
        "key": "close_timeout", "type": "number", "default": 2.0, "min": 0.5,
        "env": "STARLOGGER_CLOSE_TIMEOUT",
        "group": "Lifecycle", "label": "Close grace (seconds)",
        "help": "Shorter grace used after a tab is deliberately closed. Applies on the "
                "next launch.",
    },
    {
        "key": "update_mode", "type": "enum", "default": "prompt",
        "options": ["prompt", "auto", "off"],
        "env": "STARLOGGER_UPDATE_MODE",
        "legacy_env": {"STARLOGGER_NO_UPDATE": "off", "STARLOGGER_AUTO_UPDATE": "auto"},
        "group": "Updates", "label": "Updates",
        "help": "Prompt = ask in the dashboard before applying a new build · "
                "Automatic = apply silently · Off = don't check.",
    },
    {
        "key": "live_update_secs", "type": "int", "default": 900, "min": 0,
        "env": "STARLOGGER_LIVE_UPDATE_SECS",
        "group": "Updates", "label": "Update check interval (seconds)",
        "help": "How often a running tracker checks for a new build. 0 disables the "
                "mid-session check. Takes effect immediately.",
    },
    {
        "key": "update_remote", "type": "string", "default": "origin",
        "env": "STARLOGGER_UPDATE_REMOTE",
        "group": "Updates", "label": "Update remote",
        "help": "Git remote (or path/URL) updates are pulled from. Advanced.",
    },
    {
        "key": "update_branch", "type": "string", "default": "main",
        "env": "STARLOGGER_UPDATE_BRANCH",
        "group": "Updates", "label": "Update branch",
        "help": "Git branch updates are pulled from. Advanced.",
    },
]

_BY_KEY = {f["key"]: f for f in CONFIG_SCHEMA}


# ---- selected_ship + raw store ------------------------------------------- #

# `path` defaults to None (not SETTINGS_PATH) so the module global is read at CALL time,
# not bound once at import -- tests monkeypatch settings.SETTINGS_PATH and rely on that.

def get_settings(path: str | None = None) -> dict:
    return load_cached(path or SETTINGS_PATH, _cache)


def set_setting(key: str, value, path: str | None = None) -> None:
    """Set one key (or remove it when value is None/empty). Atomic write."""
    path = path or SETTINGS_PATH
    data = read_json(path, dict)
    if value is None or value == "":
        data.pop(key, None)
    else:
        data[key] = value
    atomic_write(path, data)
    _cache["mtime"] = None  # force a fresh read on next get_settings()


# ---- config knobs: coercion, resolution, description, update ------------- #

def _coerce(field: dict, value):
    """Coerce a stored/incoming value to the field's type, clamping numerics to `min`.
    Raises ValueError on a value that can't be coerced (drives the API's 400)."""
    t = field["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if t in ("number", "int"):
        num = float(value)  # raises ValueError/TypeError on junk
        if "min" in field:
            num = max(field["min"], num)
        return int(num) if t == "int" else num
    if t == "enum":
        v = str(value).strip().lower()
        if v not in field["options"]:
            raise ValueError(f"{value!r} not one of {field['options']}")
        return v
    # string
    return str(value).strip()


def env_override(key: str) -> bool:
    """True when an environment variable is currently forcing this key (so the UI shows
    it read-only). For a toggle var, any non-empty value counts; for a value var, being
    set at all counts; a `legacy_env` presence var counts too."""
    field = _BY_KEY[key]
    if os.environ.get(field["env"]):
        return True
    return any(os.environ.get(e) for e in field.get("legacy_env", {}))


def resolve(key: str):
    """Effective value: env var > settings.json > built-in default.

    A `legacy_env` presence var (e.g. STARLOGGER_NO_UPDATE) wins first, mapping to a fixed
    value -- back-compat for the old toggle env vars. For a toggle var (env_toggle),
    presence forces the inverted default (`STARLOGGER_NO_BROWSER` set => open_browser=False).
    For a value var, the env string is coerced to the field's type. Otherwise the stored
    setting (if any) is coerced, falling back to the default."""
    field = _BY_KEY[key]
    for env_name, mapped in field.get("legacy_env", {}).items():
        if os.environ.get(env_name):       # first match wins (NO_UPDATE=off before AUTO=auto)
            return mapped
    raw = os.environ.get(field["env"])
    if raw is not None and raw != "":
        if field.get("env_toggle"):
            return False  # the presence-style vars only ever turn the feature OFF
        try:
            return _coerce(field, raw)
        except (ValueError, TypeError):
            pass  # malformed env -> fall through to stored/default
    stored = get_settings().get(key)
    if stored is not None:
        try:
            return _coerce(field, stored)
        except (ValueError, TypeError):
            pass
    return field["default"]


def resolve_bool(key: str) -> bool:
    return bool(resolve(key))


def resolve_number(key: str) -> float:
    return float(resolve(key))


def resolve_int(key: str) -> int:
    return int(resolve(key))


def resolve_str(key: str) -> str:
    return str(resolve(key))


def describe() -> list[dict]:
    """The schema with each knob's current effective value + whether an env var is
    shadowing it -- everything the Settings panel needs to render and annotate."""
    out = []
    for f in CONFIG_SCHEMA:
        row = {
            "key": f["key"], "type": f["type"], "group": f["group"],
            "label": f["label"], "help": f["help"], "default": f["default"],
            "value": resolve(f["key"]), "env_override": env_override(f["key"]),
            "env": f["env"],
        }
        if "options" in f:
            row["options"] = f["options"]   # enum: the UI renders a <select>
        out.append(row)
    return out


def update(payload: dict, path: str | None = None) -> None:
    """Validate + persist a batch of config knobs. Unknown keys or values that can't be
    coerced raise ValueError (-> 400). A value equal to its built-in default is dropped
    from the file, so settings.json only ever records genuine departures. Atomic write."""
    path = path or SETTINGS_PATH
    if not isinstance(payload, dict):
        raise ValueError("settings payload must be an object")
    cleaned: dict = {}
    for key, value in payload.items():
        field = _BY_KEY.get(key)
        if field is None:
            raise ValueError(f"unknown setting: {key}")
        try:
            cleaned[key] = _coerce(field, value)
        except (ValueError, TypeError):
            raise ValueError(f"invalid value for {key}: {value!r}")
    data = read_json(path, dict)
    for key, value in cleaned.items():
        if value == _BY_KEY[key]["default"]:
            data.pop(key, None)        # back to default -> stop recording it
        else:
            data[key] = value
    atomic_write(path, data)
    _cache["mtime"] = None  # force a fresh read on next get_settings()
