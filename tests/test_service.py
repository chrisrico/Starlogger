"""The systemd user-unit renderer (starlogger.service) + the --service CLI surface.

install.sh bakes the output of `tracker.py --print-systemd-unit` straight into
~/.config/systemd/user/starlogger.service, so the path-templating and quoting here are
load-bearing -- a split on the space-containing Game.log path, or a missing --service in
ExecStart, breaks the live unit. These pin every branch without standing up systemd.

Run: python -m pytest tests/test_service.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tracker
from starlogger import service

PY = "/opt/sl/.venv/bin/python"
SCRIPT = "/opt/sl/tracker.py"
DATA = "/home/u/.local/share/starlogger"
LOG = "/home/u/Games/sc/drive_c/Program Files/Roberts Space Industries/StarCitizen/LIVE/Game.log"


def _env_lines(unit: str) -> list[str]:
    return [ln for ln in unit.splitlines() if ln.startswith("Environment=")]


# --- structure ------------------------------------------------------------ #

def test_has_all_sections():
    unit = service.systemd_unit_text(PY, SCRIPT, DATA, LOG)
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in unit


def test_execstart_runs_the_service_flag():
    unit = service.systemd_unit_text(PY, SCRIPT, DATA, LOG)
    # the abs interpreter + abs script + --service, verbatim (this is what makes it a *service*)
    assert f"ExecStart={PY} {SCRIPT} --service" in unit


def test_service_directives():
    unit = service.systemd_unit_text(PY, SCRIPT, DATA, LOG)
    assert "Type=simple" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert f"WorkingDirectory={DATA}" in unit


# --- environment: quoting + the DBUS bus + data dir ----------------------- #

def test_every_environment_value_is_double_quoted():
    # systemd splits unquoted Environment= on whitespace; the log path has spaces, so each
    # value MUST be wrapped in double quotes.
    unit = service.systemd_unit_text(PY, SCRIPT, DATA, LOG)
    lines = _env_lines(unit)
    assert lines  # there is at least one
    for ln in lines:
        assert ln.startswith('Environment="') and ln.endswith('"'), ln


def test_data_dir_and_dbus_bus_present():
    unit = service.systemd_unit_text(PY, SCRIPT, DATA, LOG)
    assert f'Environment="STARLOGGER_DATA_DIR={DATA}"' in unit
    # %t (XDG_RUNTIME_DIR) -> the user session bus, for the screen-lock watcher.
    assert 'Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus"' in unit


def test_log_path_with_spaces_stays_one_quoted_value():
    unit = service.systemd_unit_text(PY, SCRIPT, DATA, LOG)
    # the whole spaced path lands in a single quoted assignment, not split across tokens
    assert f'Environment="STARLOGGER_LOG={LOG}"' in unit


def test_log_omitted_when_unknown():
    unit = service.systemd_unit_text(PY, SCRIPT, DATA, log_path=None)
    assert "STARLOGGER_LOG" not in unit          # runtime find_log() takes over instead
    # but the unit is otherwise complete and runnable
    assert f"ExecStart={PY} {SCRIPT} --service" in unit


# --- install destination -------------------------------------------------- #

def test_unit_dest_path_defaults_to_xdg_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    p = service.unit_dest_path()
    assert p.endswith("/.config/systemd/user/starlogger.service")


def test_unit_dest_path_honors_absolute_xdg_config(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    assert service.unit_dest_path() == "/cfg/systemd/user/starlogger.service"


def test_unit_dest_path_ignores_relative_xdg_config(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")  # spec: ignore non-absolute
    assert service.unit_dest_path().endswith("/.config/systemd/user/starlogger.service")


# --- the --service / --print-systemd-unit CLI surface --------------------- #

def test_cli_exposes_service_and_print_unit_flags():
    ap = tracker.build_arg_parser()
    args = ap.parse_args(["--service"])
    assert args.service is True
    args = ap.parse_args(["--print-systemd-unit"])
    assert args.print_systemd_unit is True


def test_cli_defaults_are_off():
    args = tracker.build_arg_parser().parse_args([])
    assert args.service is False
    assert args.print_systemd_unit is False
