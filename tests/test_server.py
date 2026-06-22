"""Flask route contract for starlogger/server.py.

The server layer (arg parsing, validation, status codes, JSON shape, pass-through)
had no tests. The downstream functions each have their own tests, so here they're
stubbed at the server-module boundary and we assert the HTTP contract only.

Run: python -m pytest tests/test_server.py
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import server
from starlogger.state import State


@pytest.fixture
def client(monkeypatch):
    """A test client with every downstream dependency stubbed to a harmless default,
    so each route is exercised in isolation. Individual tests re-stub where they need
    to capture arguments or force a branch."""
    stubs = {
        "build_snapshot": lambda st, **kw: {"missions": [], "kw": kw},
        "load_ship_cargo": lambda: {"ships": {}, "game_version": "4.8"},
        "load_mineables": lambda: {"count": 2, "game_version": "4.8", "rocks": [1, 2]},
        "lookup_rs": lambda rs: [{"class": "x"}],
        "salvage_lookup": lambda rs: [{"kind": "ship", "label": "z"}],
        "decompose_rs": lambda rs: [{"combo": "y"}],
        "rock_signatures": lambda: [100.0, 200.0],
        "all_minerals": lambda: ["Quartz", "Titanium"],
        "lookup_mineral": lambda name: {"name": name, "rocks": []},
        "mineral_index": lambda: {"Quartz": ["RockA"]},
        "mining_plan": lambda mins: {"plan": mins},
        "load_contracts": lambda: {"templates": []},
        "blueprint_catalog": lambda: [{"name": "BP", "category": "C"}],
        "lookup_blueprint": lambda name: {"name": name} if name == "Known" else None,
        "set_setting": lambda k, v: None,
        "load_mining_gear": lambda: {"heads": [
            {"class": "H_S1", "size": 1, "module_slots": 1},
            {"class": "H_S2", "size": 2, "module_slots": 2}],
            "modules": [{"class": "M_A"}, {"class": "M_B"}], "game_version": "4.8"},
        "get_ship_equipment": lambda: {"MOLE": {"head": "H_S2", "modules": ["M_A"]}},
        "set_ship_equipment": lambda ship, eq: None,
        "mining_hardpoints": lambda name, internal, db: [2, 2, 2] if name == "MOLE" else [1],
        "head_by_class": lambda cls: {"class": "H_S2", "module_slots": 2} if cls == "H_S2" else None,
        "gear_modules": lambda: [{"class": "M_A"}, {"class": "M_B"}],
        "load_radar": lambda: {"radars": []},
        "radar_by_class": lambda cls: None,
        "radar_slot": lambda name, internal, db: None,
        "filter_sessions": lambda s, **kw: {"sessions": [], "kw": kw},
        "load_sessions": lambda: [],
        "build_timeline": lambda key, lp: {"checkpoints": []} if key == "good" else None,
        "snapshot_with_overlay": lambda key, lp, at, ov: {"missions": []} if key == "good" else None,
        "state_at": lambda key, lp, at: State() if key == "good" else None,
        "seed_overlay": lambda: {"edits": []},
        "apply_replay_op": lambda overlay, op, st: None,
        "apply_override_with_siblings": lambda data, st, mid, ov: {},
        "read_json": lambda path, typ: {},
        "atomic_write": lambda path, data: None,
        "set_lost": lambda tid, lost: None,
        "set_station_name": lambda zone, name: None,
        "set_leg_states": lambda legs, done: None,
        "set_leg_field": lambda mid, oid, field, value: None,
    }
    for name, fn in stubs.items():
        monkeypatch.setattr(server, name, fn)
    app = server.create_app(State(), log_path="/fake/Game.log")
    app.testing = True
    return _token_client(app)


def _token_client(app):
    """A test client that attaches the per-install API token to every request. The mutating
    API is token-gated (see server._enforce_guard); these tests exercise the route contract,
    not the guard, which is covered separately in test_csrf_guard.py."""
    c = app.test_client()
    token = app.config["API_TOKEN"]
    _open = c.open
    def _open_with_token(*a, **kw):
        headers = dict(kw.get("headers") or {})
        headers.setdefault("X-Starlogger-Token", token)
        kw["headers"] = headers
        return _open(*a, **kw)
    c.open = _open_with_token
    return c


# --- simple GETs ----------------------------------------------------------- #

def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200


def test_spa_fallback_serves_shell_for_path_routed_pages(client):
    # The dashboard is path-routed via the History API; a direct hit / reload on one of the
    # primary screens has no file on disk, so the server must serve index.html and let app.js
    # route from the URL. (The shell is the same bytes the "/" route serves.)
    shell = client.get("/").data
    for path in ("/contracts", "/cargo", "/plan", "/archive", "/mining"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.data == shell, path


def test_spa_fallback_leaves_real_misses_as_404(client):
    # The fallback must NOT mask genuine misses: a missing asset (has an extension) and a
    # missing API endpoint (under /api/) both stay 404 rather than silently returning HTML.
    assert client.get("/does-not-exist.js").status_code == 404
    assert client.get("/api/nope").status_code == 404


def test_state_passes_trade_flag(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(server, "build_snapshot", lambda st, **kw: seen.update(kw) or {"ok": 1})
    assert client.get("/api/state").status_code == 200
    assert seen == {"trade_only": False}
    client.get("/api/state?trade=1")
    assert seen == {"trade_only": True}


def test_ships(client):
    assert client.get("/api/ships").get_json()["game_version"] == "4.8"


# --- mining-gear catalog + per-ship loadout -------------------------------- #

def test_mining_gear_full_catalog(client):
    j = client.get("/api/mining-gear").get_json()
    assert len(j["heads"]) == 2 and len(j["modules"]) == 2
    assert j["selected"] == {"MOLE": {"head": "H_S2", "modules": ["M_A"]}}


def test_mining_gear_filtered_by_ship_hardpoints(client):
    # MOLE has size-2 hardpoints -> only the S2 head; its saved selection is returned.
    j = client.get("/api/mining-gear?ship=MOLE").get_json()
    assert j["hardpoints"] == [2, 2, 2]
    assert [h["class"] for h in j["heads"]] == ["H_S2"]
    assert j["selected"] == {"head": "H_S2", "modules": ["M_A"]}


def test_mining_gear_set_validates(client):
    assert client.post("/api/mining-gear", json={}).status_code == 400          # no ship
    assert client.post("/api/mining-gear",
                       json={"ship": "MOLE", "head": "NOPE"}).status_code == 400  # unknown head
    assert client.post("/api/mining-gear",
                       json={"ship": "MOLE", "head": "H_S2",
                             "modules": ["M_A", "M_B", "M_A"]}).status_code == 400  # > slots
    assert client.post("/api/mining-gear",
                       json={"ship": "MOLE", "modules": ["M_A"]}).status_code == 400  # mods w/o head


def test_mining_gear_set_ok(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(server, "set_ship_equipment", lambda s, eq: seen.update(ship=s, eq=eq))
    r = client.post("/api/mining-gear", json={"ship": "MOLE", "head": "H_S2", "modules": ["M_A"]})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert seen == {"ship": "MOLE", "eq": {"head": "H_S2", "modules": ["M_A"], "radar": None}}


def test_mining_gear_filtered_includes_radars(client, monkeypatch):
    # radars are filtered to the ship's radar slot size, and the slot {size, stock} is returned.
    monkeypatch.setattr(server, "load_radar", lambda: {"radars": [
        {"class": "R_S1", "size": 1, "rs": 1.0}, {"class": "R_S2", "size": 2, "rs": 1.0}]})
    monkeypatch.setattr(server, "radar_slot",
                        lambda name, internal, db: {"size": 1, "stock": "r_stock"})
    monkeypatch.setattr(server, "mining_hardpoints", lambda name, internal, db: [1])
    j = client.get("/api/mining-gear?ship=Prospector").get_json()
    assert [r["class"] for r in j["radars"]] == ["R_S1"]          # only the size-1 radar
    assert j["radar_slot"] == {"size": 1, "stock": "r_stock"}


def test_mining_gear_set_radar_validates(client, monkeypatch):
    monkeypatch.setattr(server, "radar_slot", lambda name, internal, db: {"size": 1})
    # unknown radar class -> 400
    monkeypatch.setattr(server, "radar_by_class", lambda cls: None)
    assert client.post("/api/mining-gear",
                       json={"ship": "Prospector", "radar": "NOPE"}).status_code == 400
    # radar size mismatch vs the ship's slot -> 400
    monkeypatch.setattr(server, "radar_by_class", lambda cls: {"class": cls, "size": 2})
    assert client.post("/api/mining-gear",
                       json={"ship": "Prospector", "radar": "R_S2"}).status_code == 400
    # a correctly-sized known radar -> 200
    monkeypatch.setattr(server, "radar_by_class", lambda cls: {"class": cls, "size": 1})
    r = client.post("/api/mining-gear", json={"ship": "Prospector", "radar": "R_S1"})
    assert r.status_code == 200 and r.get_json()["ok"] is True


# --- mining GETs with arg validation --------------------------------------- #

def test_rock_lookup_no_rs_returns_catalog(client):
    j = client.get("/api/rock-lookup").get_json()
    assert j["count"] == 2 and j["rocks"] == [1, 2]


def test_rock_lookup_validates_rs(client):
    assert client.get("/api/rock-lookup?rs=abc").status_code == 400
    assert client.get("/api/rock-lookup?rs=-5").status_code == 400
    j = client.get("/api/rock-lookup?rs=42").get_json()
    assert j["rs"] == 42 and j["candidates"] == [{"class": "x"}]
    # salvage targets ride the same reading as a separate section
    assert j["salvage"] == [{"kind": "ship", "label": "z"}]


def test_rock_decompose_validates_rs(client):
    assert client.get("/api/rock-decompose").status_code == 400        # missing
    assert client.get("/api/rock-decompose?rs=0").status_code == 400   # not positive
    assert client.get("/api/rock-decompose?rs=10").get_json()["combos"] == [{"combo": "y"}]


def test_rock_signatures_and_minerals(client):
    assert client.get("/api/rock-signatures").get_json()["signatures"] == [100.0, 200.0]
    assert client.get("/api/minerals").get_json()["minerals"] == ["Quartz", "Titanium"]
    assert client.get("/api/mineral-index").get_json()["minerals"] == {"Quartz": ["RockA"]}


def test_mineral_lookup_requires_name(client):
    assert client.get("/api/mineral-lookup?name=%20").status_code == 400
    assert client.get("/api/mineral-lookup?name=Quartz").get_json()["name"] == "Quartz"


def test_mining_plan_requires_list(client):
    assert client.post("/api/mining-plan", json={"minerals": "nope"}).status_code == 400
    assert client.post("/api/mining-plan", json={}).status_code == 400
    r = client.post("/api/mining-plan", json={"minerals": ["Quartz"]})
    assert r.get_json()["plan"] == ["Quartz"]


# --- contracts / blueprints ------------------------------------------------ #

def test_contracts_and_blueprints(client):
    assert client.get("/api/contracts").get_json() == {"templates": []}
    assert client.get("/api/blueprints").get_json()["blueprints"][0]["name"] == "BP"


def test_blueprint_lookup(client):
    assert client.get("/api/blueprint?name=%20").status_code == 400
    assert client.get("/api/blueprint?name=Unknown").status_code == 404
    assert client.get("/api/blueprint?name=Known").get_json()["name"] == "Known"


# --- select-ship validation ------------------------------------------------ #

def test_select_ship_validates_type(client):
    assert client.post("/api/select-ship", json={"ship": 123}).status_code == 400
    assert client.post("/api/select-ship", json={"ship": "Hull C"}).get_json()["ok"] is True
    assert client.post("/api/select-ship", json={"ship": None}).get_json()["ok"] is True


# --- sessions + replay ----------------------------------------------------- #

def test_sessions_passes_filters(client):
    j = client.get("/api/sessions?trade=1&unfinished=1").get_json()
    assert j["kw"] == {"trade_only": True, "show_unfinished": True}


def test_replay_timeline(client):
    assert client.get("/api/replay/timeline").status_code == 400          # key required
    assert client.get("/api/replay/timeline?key=gone").get_json() == {"available": False}
    j = client.get("/api/replay/timeline?key=good").get_json()
    assert j["available"] is True and "checkpoints" in j


def test_replay_state(client):
    assert client.get("/api/replay/state").status_code == 400              # key required
    assert client.get("/api/replay/state?key=good&at=x").status_code == 400  # bad at
    assert client.get("/api/replay/state?key=gone&at=0").status_code == 404
    assert client.get("/api/replay/state?key=good&at=3").get_json() == {"missions": []}


def test_replay_edit(client, monkeypatch):
    assert client.post("/api/replay/edit", json={"at": "x"}).status_code == 400
    assert client.post("/api/replay/edit", json={"key": "good"}).status_code == 400   # op missing
    assert client.post("/api/replay/edit",
                       json={"key": "gone", "op": {"kind": "x"}}).status_code == 404
    ok = client.post("/api/replay/edit", json={"key": "good", "op": {"kind": "hide"}, "at": 1})
    assert ok.get_json()["ok"] is True and "snapshot" in ok.get_json()

    def boom(overlay, op, st):
        raise ValueError("bad op")
    monkeypatch.setattr(server, "apply_replay_op", boom)
    r = client.post("/api/replay/edit", json={"key": "good", "op": {"kind": "x"}})
    assert r.status_code == 400 and "bad op" in r.get_json()["error"]


# --- live edit / persistence endpoints ------------------------------------- #

def test_override_validation(client):
    assert client.post("/api/override", json={}).status_code == 400               # mission_id
    assert client.post("/api/override",
                       json={"mission_id": "m1", "override": "nope"}).status_code == 400
    assert client.post("/api/override",
                       json={"mission_id": "m1", "override": {"hidden": True}}).get_json()["ok"] is True


def test_trade_lost_validation(client):
    assert client.post("/api/trade-lost", json={}).status_code == 400
    assert client.post("/api/trade-lost", json={"trade_id": "t1"}).get_json()["ok"] is True


def test_station_name_validation(client):
    assert client.post("/api/station-name", json={}).status_code == 400            # zone
    assert client.post("/api/station-name", json={"zone": 123, "name": 5}).status_code == 400
    assert client.post("/api/station-name",
                       json={"zone": "67890", "name": "Cordys"}).get_json()["ok"] is True


def test_leg_state_validation(client):
    assert client.post("/api/leg-state", json={"legs": []}).status_code == 400
    assert client.post("/api/leg-state",
                       json={"legs": [{"mission_id": "m1"}]}).status_code == 400   # no oid
    # single-leg shorthand
    assert client.post("/api/leg-state",
                       json={"mission_id": "m1", "oid": "o1", "done": True}).get_json()["ok"] is True


def test_leg_field_validation(client):
    assert client.post("/api/leg-field", json={"mission_id": "m1"}).status_code == 400  # oid
    assert client.post("/api/leg-field",
                       json={"mission_id": "m1", "oid": "o1", "field": "bogus"}).status_code == 400
    assert client.post("/api/leg-field",
                       json={"mission_id": "m1", "oid": "o1", "field": "qty",
                             "value": "abc"}).status_code == 400
    assert client.post("/api/leg-field",
                       json={"mission_id": "m1", "oid": "o1", "field": "cargo",
                             "value": "Quartz"}).get_json()["ok"] is True


# --- SSE stream presence + push ------------------------------------------- #

class _Presence:
    """Minimal stand-in for tracker.Presence: just counts connect/disconnect."""
    def __init__(self):
        self.streams = 0
        self.max = 0
        self.closing = False

    def stream_connect(self):
        self.streams += 1
        self.max = max(self.max, self.streams)

    def stream_disconnect(self):
        self.streams -= 1

    def mark_closing(self):
        self.closing = True


def _stream_client(monkeypatch, presence):
    monkeypatch.setattr(server, "build_snapshot", lambda st, **kw: {"missions": [], "v": st.version})
    app = server.create_app(State(), log_path="/fake/Game.log", presence=presence)
    app.testing = True
    return app.test_client()


def test_stream_pushes_initial_snapshot_and_counts_presence(monkeypatch):
    p = _Presence()
    c = _stream_client(monkeypatch, p)
    # buffered=False keeps the (infinite) generator lazy; pull exactly one frame.
    r = c.get("/api/stream", buffered=False)
    assert r.mimetype == "text/event-stream"
    first = next(r.response)
    assert b"data:" in first             # initial snapshot pushed on connect
    assert p.streams == 1                 # the open stream is counted as present
    r.close()                            # client gone -> generator finally runs
    assert p.streams == 0                 # ...and presence is released
    assert p.max == 1


def test_stream_optional_without_presence(client):
    # create_app(presence=None) (the default fixture) must still serve the stream.
    r = client.get("/api/stream", buffered=False)
    assert r.mimetype == "text/event-stream"
    assert b"data:" in next(r.response)
    r.close()


# --- live asset-version / auto-reload -------------------------------------- #

def _fake_web(tmp_path):
    (tmp_path / "index.html").write_text("<html>")
    (tmp_path / "styles.css").write_text("body{}")
    (tmp_path / "app.js").write_text("v1")
    return str(tmp_path)


def test_assets_version_tracks_file_changes(monkeypatch, tmp_path):
    # The hash must follow the served files (so a long-running process notices an in-place
    # swap), but stay stable -- and reuse the stat-gated cache -- while they don't change.
    monkeypatch.setattr(server, "WEB_DIR", _fake_web(tmp_path))
    monkeypatch.setattr(server, "_assets_cache", None)
    v1 = server._assets_version()
    assert v1 == server._assets_version()                 # unchanged files -> same hash
    (tmp_path / "app.js").write_text("v2___longer")        # different size -> new signature
    assert server._assets_version() != v1                  # ...and a new hash


def test_stream_first_frame_is_asset_meta(monkeypatch):
    c = _stream_client(monkeypatch, _Presence())
    r = c.get("/api/stream", buffered=False)
    first = next(r.response)
    assert first.startswith(b"event: meta")               # asset version leads the stream
    assert b'"assets"' in first
    r.close()


def test_stream_repushes_meta_on_asset_change(monkeypatch, tmp_path):
    # An in-place frontend swap under a still-running server must re-push `meta` (the tab
    # reloads) WITHOUT needing a reconnect. The new hash is announced only once it has held
    # for a tick, so the bump after the change drives the debounce past its one-tick guard.
    monkeypatch.setattr(server, "WEB_DIR", _fake_web(tmp_path))
    monkeypatch.setattr(server, "_assets_cache", None)
    monkeypatch.setattr(server, "build_snapshot", lambda st, **kw: {"v": st.version})
    st = State()
    app = server.create_app(st, log_path="/fake/Game.log")
    app.testing = True
    r = app.test_client().get("/api/stream", buffered=False)

    f1 = next(r.response)                                  # meta (baseline hash)
    assert f1.startswith(b"event: meta")
    next(r.response)                                       # initial snapshot

    (tmp_path / "app.js").write_text("v2___changed")       # swap an asset mid-stream
    st.bump_version()                                      # wake the loop past the debounce
    next(r.response)                                       # snapshot (debounce settles)
    f4 = next(r.response)                                  # ...then the re-pushed meta
    assert f4.startswith(b"event: meta")
    assert f4 != f1                                        # carries the NEW hash
    r.close()


# --- /api/quit (a newer launch replacing this instance) ------------------- #

def test_quit_invokes_shutdown(monkeypatch):
    monkeypatch.setattr(server, "build_snapshot", lambda st, **kw: {})
    app = server.create_app(State(), log_path="/fake/Game.log")
    called = threading.Event()
    app.config["QUIT_FN"] = called.set    # stand in for httpd.shutdown
    app.testing = True
    r = _token_client(app).post("/api/quit")
    assert r.get_json()["ok"] is True
    assert called.wait(2)                  # the daemon thread invoked QUIT_FN


def test_quit_noop_without_fn(client):
    # No QUIT_FN wired (default fixture) -> still returns ok, just does nothing.
    assert client.post("/api/quit").get_json()["ok"] is True


# --- /api/restart (re-exec in place) -------------------------------------- #

def test_restart_invokes_on_restart(monkeypatch):
    monkeypatch.setattr(server, "build_snapshot", lambda st, **kw: {})
    app = server.create_app(State(), log_path="/fake/Game.log")
    called = threading.Event()
    app.config["ON_RESTART"] = called.set   # stand in for the tracker's re-exec trigger
    app.testing = True
    r = _token_client(app).post("/api/restart")
    assert r.get_json()["ok"] is True
    assert called.is_set()                   # ON_RESTART fired (it returns immediately)


def test_restart_503_without_fn(client):
    # No ON_RESTART wired (e.g. a --once host) -> 503, restart unavailable.
    r = client.post("/api/restart")
    assert r.status_code == 503
    assert r.get_json()["ok"] is False


# --- /api/update/check (explicit "Check for updates" button) -------------- #

def test_update_check_unavailable_without_fn(client):
    # No ON_CHECK_NOW wired (e.g. a --once host) -> 503, status "unavailable".
    r = client.post("/api/update/check")
    assert r.status_code == 503
    assert r.get_json()["status"] == "unavailable"


def test_update_check_passes_through_status(monkeypatch):
    monkeypatch.setattr(server, "build_snapshot", lambda st, **kw: {})
    app = server.create_app(State(), log_path="/fake/Game.log")
    app.config["ON_CHECK_NOW"] = lambda: {"ok": True, "status": "updating", "latest": "abc1234"}
    app.testing = True
    j = _token_client(app).post("/api/update/check").get_json()
    assert j == {"ok": True, "status": "updating", "latest": "abc1234"}


# --- /api/settings (changing the bind address re-execs the server) -------- #

def _settings_app(monkeypatch, tmp_path):
    """An app whose settings store is a throwaway file with no STARLOGGER_* env
    knobs leaking in, so /api/settings reads/writes are isolated."""
    from starlogger import settings
    monkeypatch.setattr(settings, "SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", {"mtime": None, "data": {}})
    for f in settings.CONFIG_SCHEMA:
        monkeypatch.delenv(f["env"], raising=False)
        for e in f.get("legacy_env", {}):
            monkeypatch.delenv(e, raising=False)
    monkeypatch.setattr(server, "build_snapshot", lambda st, **kw: {})
    app = server.create_app(State(), log_path="/fake/Game.log")
    app.testing = True
    return app


def test_bind_host_change_restarts(monkeypatch, tmp_path):
    app = _settings_app(monkeypatch, tmp_path)
    restarted = threading.Event()
    app.config["ON_RESTART"] = restarted.set    # stand in for the off-thread re-exec
    j = _token_client(app).post("/api/settings", json={"bind_host": "0.0.0.0"}).get_json()
    assert j["ok"] is True
    assert restarted.is_set()                    # a changed bind address triggers a restart


def test_bind_host_unchanged_no_restart(monkeypatch, tmp_path):
    # Saving the current value (here the default) is not a change -> no restart.
    app = _settings_app(monkeypatch, tmp_path)
    restarted = threading.Event()
    app.config["ON_RESTART"] = restarted.set
    _token_client(app).post("/api/settings", json={"bind_host": "127.0.0.1"})
    assert not restarted.is_set()


def test_update_source_validation_rejects_bad_branch(monkeypatch, tmp_path):
    # A failing validator (here: branch doesn't exist) blocks the save with a 400 and the
    # error message, and nothing is persisted.
    app = _settings_app(monkeypatch, tmp_path)
    app.config["ON_VALIDATE_SOURCE"] = lambda remote, branch: f"Branch “{branch}” doesn't exist."
    r = _token_client(app).post("/api/settings", json={"update_branch": "nope"})
    assert r.status_code == 400
    j = r.get_json()
    assert j["ok"] is False and "nope" in j["error"]
    from starlogger import settings
    assert settings.resolve_str("update_branch") == "main"   # unchanged


def test_update_source_validation_passes_good_branch(monkeypatch, tmp_path):
    app = _settings_app(monkeypatch, tmp_path)
    seen = {}
    app.config["ON_VALIDATE_SOURCE"] = lambda remote, branch: seen.update(r=remote, b=branch) or None
    r = _token_client(app).post("/api/settings", json={"update_branch": "dev"})
    assert r.get_json()["ok"] is True
    # Validated the prospective pair: the unchanged remote + the new branch.
    assert seen == {"r": "origin", "b": "dev"}


# --- /api/closing (deliberate tab close beacon) --------------------------- #

def test_closing_marks_presence(monkeypatch):
    p = _Presence()
    c = _stream_client(monkeypatch, p)
    assert p.closing is False
    assert c.post("/api/closing").get_json()["ok"] is True
    assert p.closing is True               # withdraws the keep-alive claim (no shutdown)


def test_closing_noop_without_presence(client):
    # No presence wired (default fixture) -> still returns ok, just does nothing.
    assert client.post("/api/closing").get_json()["ok"] is True
