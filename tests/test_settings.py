"""Config-knob resolver: precedence (env > settings.json > default), coercion/
clamping, env_toggle semantics, and update() validation/persistence.

Run: .venv/bin/python -m pytest tests/test_settings.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import settings


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the settings store at a throwaway file and clear the mtime cache, so each
    test sees a clean settings.json and no env knobs leak in from the runner."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(path))
    monkeypatch.setattr(settings, "_cache", {"mtime": None, "data": {}})
    # Strip any STARLOGGER_* knobs the outer environment might set.
    for f in settings.CONFIG_SCHEMA:
        monkeypatch.delenv(f["env"], raising=False)
    return str(path)


def test_defaults_when_unset(store):
    assert settings.resolve_int("live_update_secs") == 900
    assert settings.resolve_bool("open_browser") is True
    assert settings.resolve_number("idle_timeout") == 30.0
    assert settings.resolve_str("update_remote") == "origin"
    assert settings.env_override("live_update_secs") is False


def test_settings_file_over_default(store):
    settings.update({"live_update_secs": 120, "update_branch": "dev"})
    assert settings.resolve_int("live_update_secs") == 120
    assert settings.resolve_str("update_branch") == "dev"


def test_env_over_settings_file(store, monkeypatch):
    settings.update({"live_update_secs": 120})
    monkeypatch.setenv("STARLOGGER_LIVE_UPDATE_SECS", "300")
    assert settings.resolve_int("live_update_secs") == 300
    assert settings.env_override("live_update_secs") is True


def test_env_toggle_forces_off(store, monkeypatch):
    assert settings.resolve_bool("open_browser") is True
    monkeypatch.setenv("STARLOGGER_NO_BROWSER", "1")
    assert settings.resolve_bool("open_browser") is False
    assert settings.env_override("open_browser") is True
    # auto_update toggles the same way.
    monkeypatch.setenv("STARLOGGER_NO_UPDATE", "1")
    assert settings.resolve_bool("auto_update") is False


def test_numeric_clamp(store):
    settings.update({"idle_timeout": 0.1, "close_timeout": 0.0})
    assert settings.resolve_number("idle_timeout") == 1.0     # min 1.0
    assert settings.resolve_number("close_timeout") == 0.5    # min 0.5


def test_int_coercion(store):
    settings.update({"live_update_secs": 5.9})
    assert settings.resolve_int("live_update_secs") == 5      # truncates to int


def test_update_rejects_unknown_key(store):
    with pytest.raises(ValueError):
        settings.update({"bogus": 1})


def test_update_rejects_bad_value(store):
    with pytest.raises(ValueError):
        settings.update({"idle_timeout": "not-a-number"})


def test_update_drops_default_value(store):
    import json
    settings.update({"live_update_secs": 120})
    settings.update({"live_update_secs": 900})  # back to default
    with open(store) as f:
        on_disk = json.load(f)
    assert "live_update_secs" not in on_disk      # only departures are recorded


def test_update_preserves_unrelated_keys(store):
    import json
    settings.set_setting("selected_ship", "Caterpillar")
    settings.update({"live_update_secs": 120})
    with open(store) as f:
        on_disk = json.load(f)
    assert on_disk["selected_ship"] == "Caterpillar"   # batch update doesn't clobber it
    assert on_disk["live_update_secs"] == 120


def test_describe_shape(store, monkeypatch):
    monkeypatch.setenv("STARLOGGER_UPDATE_BRANCH", "release")
    rows = {d["key"]: d for d in settings.describe()}
    assert set(rows) == {f["key"] for f in settings.CONFIG_SCHEMA}
    assert rows["update_branch"]["value"] == "release"
    assert rows["update_branch"]["env_override"] is True
    assert rows["live_update_secs"]["env_override"] is False
    # every row carries what the UI renders from
    for row in rows.values():
        assert {"key", "type", "group", "label", "help", "default", "value",
                "env_override"} <= set(row)
