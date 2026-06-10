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


def test_loopback_host_is_allowed(app):
    # The normal way in: Host is localhost/127.0.0.1 (with or without a port).
    for host in ("localhost", "127.0.0.1", "127.0.0.1:7384", "[::1]:7384"):
        assert app.test_client().get("/", headers={"Host": host}).status_code == 200


def test_nonloopback_host_is_rejected_when_bound_loopback(app):
    # Anti DNS-rebinding: a rebinding page at evil.example -> 127.0.0.1 sends Host: evil.example;
    # since the server is bound to loopback (default), reject it BEFORE it can read the token
    # off GET /. Without this, its Origin==Host would have sailed through the same-origin gate.
    r = app.test_client().get("/", headers={"Host": "evil.example"})
    assert r.status_code == 403
    # and it must not be able to read the shell/token
    assert b"api-token" not in r.data


def test_nonloopback_host_allowed_when_bound_to_lan(app, monkeypatch):
    # If the user deliberately binds to a non-loopback address, an arbitrary Host is expected
    # (rebinding doesn't apply), so the guard stands down.
    monkeypatch.setattr(server, "settings_str", lambda key: "0.0.0.0" if key == "bind_host" else "")
    assert app.test_client().get("/", headers={"Host": "192.168.1.50:7384"}).status_code == 200
