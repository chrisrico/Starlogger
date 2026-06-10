"""Security contract for the mutating-API guard (server._enforce_guard).

Locks the CSRF + token gate that protects the state-changing endpoints: a cross-origin
write, or one without the per-install token, must be refused BEFORE the handler runs. The
self-update chain (POST /api/settings -> git reset --hard + pip install + re-exec) made an
unauthenticated write a remote-code-execution vector, so this is load-bearing.

Run: python -m pytest tests/test_csrf_guard.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import config, server
from starlogger.state import State


@pytest.fixture
def app():
    return server.create_app(State(), log_path="/fake/Game.log")


def test_token_is_generated_and_persisted(app):
    token = app.config["API_TOKEN"]
    assert token and len(token) >= 32
    with open(config.API_TOKEN_PATH, encoding="utf-8") as f:
        assert f.read().strip() == token


def test_token_injected_into_served_shell(app):
    html = app.test_client().get("/").data.decode()
    assert f'<meta name="api-token" content="{app.config["API_TOKEN"]}">' in html


def test_write_without_token_is_rejected(app):
    # No token at all -> 403, and the handler must not run (no side effect to assert here;
    # the point is the request never reaches set_station_name).
    r = app.test_client().post("/api/station-name", json={"zone": "1", "name": "X"})
    assert r.status_code == 403


def test_cross_origin_write_is_rejected_even_with_token(app):
    # A forged cross-site POST carries a foreign Origin; reject regardless of token. This is
    # the gate that defeats the text/plain simple-request CSRF bypass.
    r = app.test_client().post(
        "/api/station-name", json={"zone": "1", "name": "X"},
        headers={"X-Starlogger-Token": app.config["API_TOKEN"], "Origin": "http://evil.example"})
    assert r.status_code == 403


def test_same_origin_write_with_token_succeeds(app, monkeypatch):
    monkeypatch.setattr(server, "set_station_name", lambda zone, name: None)
    r = app.test_client().post(
        "/api/station-name", json={"zone": "1", "name": "X"},
        headers={"X-Starlogger-Token": app.config["API_TOKEN"], "Origin": "http://localhost"})
    assert r.status_code == 200


def test_write_with_token_and_no_origin_succeeds(app, monkeypatch):
    # A non-browser client (curl) sends no Origin; the token alone is sufficient.
    monkeypatch.setattr(server, "set_station_name", lambda zone, name: None)
    r = app.test_client().post(
        "/api/station-name", json={"zone": "1", "name": "X"},
        headers={"X-Starlogger-Token": app.config["API_TOKEN"]})
    assert r.status_code == 200


def test_reads_are_not_guarded(app):
    # GETs expose no secret an attacker could lift cross-origin, so they stay open.
    assert app.test_client().get("/").status_code == 200


def test_closing_beacon_is_exempt_from_token(app):
    # navigator.sendBeacon can't set headers; forging /api/closing only shortens the idle
    # grace, so it's intentionally token-exempt.
    assert app.test_client().post("/api/closing").status_code == 200
