"""End-to-end: the ⚙ modal's recommended MODULE is head-aware -- a tight-window head gets a
window module, a wide-window head gets a power module.

Guards shipequip.js's head-aware `_moduleScore` (reuses window.feasibility): the bug was that
module recommendations were identical for every head. Boots the real dashboard with a tiny
two-head / two-module catalog and a mining ship, opens the modal, and checks the ★-marked module
flips when you switch heads.

Marked `browser`; run with `pytest -m browser`, skipped gracefully without Chromium.
"""
from __future__ import annotations

import json
import os
import threading

import pytest
from werkzeug.serving import make_server

import starlogger.server as server
from starlogger import config, mining_gear
from starlogger.state import State

pytestmark = pytest.mark.browser


def _head(cls, name, window):
    return {"class": cls, "name": name, "manufacturer": "Test", "manufacturer_code": "TST",
            "size": 1, "mount": "miningMount", "power": 3000.0, "module_slots": 1,
            "modifiers": {"window_size": window}}


def _mod(cls, name, mods):
    return {"class": cls, "name": name, "manufacturer": "Test", "manufacturer_code": "TST",
            "tier": None, "charges": 1, "active": False, "modifiers": mods}


@pytest.fixture(autouse=True)
def _need_browser(require_browser):
    """Skip if Chromium isn't available."""


@pytest.fixture(scope="module")
def mining_server():
    mining_gear.save_mining_gear(
        [_head("H_WIDE", "Widewindow", 40), _head("H_NARROW", "Narrowindow", -40)],
        [_mod("M_POWER", "PowerMod", {"power": 25.0}),
         _mod("M_WINDOW", "WindowMod", {"window_size": 40.0})],
        game_version="4.8", path=config.MINING_GEAR_PATH)
    with open(config.SHIP_CARGO_PATH, "w") as f:
        json.dump({"game_version": "4.8", "ships": {"Prospector": {
            "class": "MISC_Prospector", "scu": 0, "name": "Prospector", "role": "Light Mining",
            "mining": {"hardpoints": [1], "head": "h_wide"}}}}, f)

    st = State()
    st.ship = "Prospector"
    app = server.create_app(st, log_path=os.environ["STARLOGGER_LOG"])
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        for p in (config.MINING_GEAR_PATH, config.SHIP_CARGO_PATH):
            try:
                os.remove(p)
            except OSError:
                pass


def _starred_module(page):
    """The text of the ★-marked option in the first module slot."""
    texts = page.eval_on_selector_all(
        "#se-mod-0 option", "els => els.map(e => e.textContent.trim())")
    return next((t for t in texts if t.startswith("★")), None)


def test_recommended_module_is_head_aware(page, mining_server):
    page.set_default_timeout(8000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(mining_server)
    page.wait_for_selector("#shipEquipBtn")
    page.click("#shipEquipBtn")
    page.wait_for_selector("#se-head")

    # Tight-window head -> the window module is recommended.
    page.select_option("#se-head", "H_NARROW")
    page.wait_for_selector("#se-mod-0")
    narrow_star = _starred_module(page)
    assert narrow_star and "WindowMod" in narrow_star, narrow_star

    # Wide-window head -> the power module is recommended instead.
    page.select_option("#se-head", "H_WIDE")
    page.wait_for_selector("#se-mod-0")
    wide_star = _starred_module(page)
    assert wide_star and "PowerMod" in wide_star, wide_star

    assert narrow_star != wide_star            # the recommendation actually changed with the head
    assert errors == [], errors
