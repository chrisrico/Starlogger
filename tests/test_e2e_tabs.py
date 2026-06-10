"""End-to-end coverage of the data-driven tabs (Contracts / Cargo / Plan / Archive /
Mining) and their inline handlers — the surface the jukebox/settings e2e in test_e2e.py
doesn't touch.

Why this exists: app.js is being split into ES modules. Under <script type="module">
top-level functions are module-scoped, so every inline-handler name must be re-exposed on
window (see test_window_bridge.py for the static guard). This suite is the RUNTIME guard:
it boots the real dashboard over a State seeded with a few missions (built directly from
the model, never parsed from a log — same approach as test_snapshot.py), visits every tab,
asserts no console/page errors, confirms every statically-referenced handler actually
resolved onto window, and drives a representative handler per area.

Marked `browser`; run with `pytest -m browser`, skipped gracefully without Chromium.
"""
from __future__ import annotations

import os
import threading

import pytest
from werkzeug.serving import make_server

import starlogger.server as server
from starlogger.model import Leg, Mission
from starlogger.state import State

# Reuse the static bridge parser so the runtime check tracks the same handler set.
from test_window_bridge import _all_js, _referenced_names

pytestmark = pytest.mark.browser

ZONES = {"Z1": "Port Olisar", "Z2": "Everus Harbor", "Z3": "Port Tressler"}


def _haul(mid, legs, status="active"):
    return Mission(mission_id=mid, contract="HaulCargo_AToB", accepted_at="t",
                   status=status, legs=legs)


def _seeded_state() -> State:
    """A small but representative haul set: two full pickup→dropoff missions (Loading +
    Unloading + a multi-stop route) plus a deliver-only and a completed one (Archive/counts)."""
    st = State()
    st.zone_names = dict(ZONES)
    st.missions = {
        "mA": _haul("mA", {"mAp": Leg("mAp", "pickup", cargo="Gold", qty=100, zone_host_id="Z1"),
                           "mAd": Leg("mAd", "dropoff", cargo="Gold", qty=100, zone_host_id="Z2")}),
        "mB": _haul("mB", {"mBp": Leg("mBp", "pickup", cargo="Tin", qty=30, zone_host_id="Z1"),
                           "mBd": Leg("mBd", "dropoff", cargo="Tin", qty=30, zone_host_id="Z3")}),
        "mC": _haul("mC", {"mCd": Leg("mCd", "dropoff", cargo="Agricium", qty=50, zone_host_id="Z2")}),
        "mD": _haul("mD", {"mDd": Leg("mDd", "dropoff", cargo="Gold", qty=80, zone_host_id="Z2")},
                    status="completed"),
    }
    return st


@pytest.fixture(autouse=True)
def _need_browser(require_browser):
    """Skip every test here if Chromium isn't available."""


@pytest.fixture(scope="module")
def populated_server():
    """The real Flask app over a State pre-seeded with missions, on its own ephemeral port."""
    app = server.create_app(_seeded_state(), log_path=os.environ["STARLOGGER_LOG"])
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


@pytest.fixture(autouse=True)
def _fast_timeouts(page):
    page.set_default_timeout(7000)
    page.set_default_navigation_timeout(15000)


def _boot(page, url):
    """Navigate, capture page errors, wait until the first snapshot has rendered missions."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector("#contracts")
    page.wait_for_function("() => document.querySelector('#contracts') "
                           "&& document.querySelector('#contracts').children.length > 0")
    return errors


def test_every_inline_handler_resolves_on_window(page, populated_server):
    """The whole point of the window bridge: after the module loads, every name reachable
    from an inline handler must actually be a function on window."""
    errors = _boot(page, populated_server)
    names = sorted(_referenced_names(_all_js()))
    assert names, "parser found no handler names — guard is mis-wired"
    missing = page.evaluate(
        "(names) => names.filter(n => typeof window[n] !== 'function')", names)
    assert missing == [], f"handlers not on window at runtime: {missing}"
    assert errors == [], errors


def test_each_tab_renders_without_errors(page, populated_server):
    errors = _boot(page, populated_server)
    # Mining is intentionally excluded: its nav button stays hidden unless a mining ship is
    # active (this seeded state is cargo-hauling), so it isn't clickable here by design.
    for tab in ("contracts", "cargo", "plan", "archive"):
        page.click(f'#nav a[data-tab="{tab}"]')
        page.wait_for_selector(f"#{tab}:not(.hide)")
    assert errors == [], errors


def test_mining_tab_renders_in_mining_mode(page, populated_server):
    """The Mining tab is hidden in cargo mode, so force mining via setMode (a bridged
    handler) — effectiveMining() honours the override without needing a mining ship — then
    confirm the tab and its tools shell render with no errors."""
    errors = _boot(page, populated_server)
    page.evaluate("window.setMode('mining')")
    page.wait_for_selector('#nav a[data-tab="mining"]:not(.hide)')
    page.click('#nav a[data-tab="mining"]')
    page.wait_for_function("() => document.querySelector('#mining:not(.hide)') "
                           "&& document.querySelector('#mining').children.length > 0")
    assert errors == [], errors


def test_contracts_editor_opens_via_inline_handler(page, populated_server):
    """Drives editMission (a bridged inline handler) and confirms the editor renders."""
    errors = _boot(page, populated_server)
    page.click('#nav a[data-tab="contracts"]')
    page.evaluate("window.editMission('mA')")
    page.wait_for_selector("#contracts input")   # the inline editor exposes input fields
    assert errors == [], errors


def test_cargo_subtab_toggle_via_interpolated_handler(page, populated_server):
    """cargoSub is referenced only through tabBar's interpolated onclick=\"${fn}(…)\" —
    the case static analysis is most likely to miss. Exercise it for real."""
    errors = _boot(page, populated_server)
    page.click('#nav a[data-tab="cargo"]')
    page.wait_for_selector("#cargo .arch-tabs")
    page.evaluate("window.cargoSub('dropoff')")
    page.wait_for_function(
        "() => document.querySelector('#cargo .arch-tab.active') "
        "&& /unload/i.test(document.querySelector('#cargo .arch-tab.active').textContent)")
    assert errors == [], errors
