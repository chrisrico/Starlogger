"""End-to-end: a MINING contract renders in the Contracts section with its ore requirements
and where-to-mine chips.

The unit/integration suites cover the parser, the location join, and the snapshot; this is the
RUNTIME guard for app.js's miningLegs() -- the contract-card branch that only runs when a
mission carries ores. Boots the real dashboard over a State holding one hand-mining contract and
a seeded body-mineables catalog, then asserts the Contracts table shows the ore × qty and the
"Mined on" chips.

Marked `browser`; run with `pytest -m browser`, skipped gracefully without Chromium.
"""
from __future__ import annotations

import os
import threading

import pytest
from werkzeug.serving import make_server

import starlogger.server as server
from starlogger import body_mineables, config
from starlogger.model import Mission, OreReq
from starlogger.state import State

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _need_browser(require_browser):
    """Skip if Chromium isn't available."""


def _seeded_state() -> State:
    st = State()
    st.missions = {"m1": Mission(
        mission_id="m1", title="Small Purchase Order: Hand Mined Materials", status="active",
        accepted_at="t", ore_any=True, mining_goto="HDMS-Perlman",
        ores={"Aphorite": OreReq("Aphorite", 0, 15), "Hadanite": OreReq("Hadanite", 0, 5)})}
    return st


@pytest.fixture(scope="module")
def mining_server():
    """Real app over a State holding a hand-mining contract + a seeded body-mineables catalog
    (Aphorite/Hadanite on two bodies). Cleans the seeded catalog on teardown."""
    body_mineables.save_body_mineables([
        {"name": "Hurston", "system": "Stanton", "ship_mineables": [],
         "hand_mineables": ["Aphorite", "Hadanite"], "ground_mineables": [],
         "harvestables": [], "creatures": [], "description": ""},
        {"name": "Daymar", "system": "Stanton", "ship_mineables": [],
         "hand_mineables": ["Aphorite"], "ground_mineables": [],
         "harvestables": [], "creatures": [], "description": ""},
    ], path=config.BODY_MINEABLES_PATH)
    body_mineables._cache["mtime"] = None

    app = server.create_app(_seeded_state(), log_path=os.environ["STARLOGGER_LOG"])
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        try:
            os.remove(config.BODY_MINEABLES_PATH)
        except OSError:
            pass


def test_mining_contract_card_shows_ores_and_locations(page, mining_server):
    page.set_default_timeout(8000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(mining_server)
    page.wait_for_selector("#contracts")
    page.wait_for_selector("#contracts .ore-row")                  # the mining card rendered

    text = page.inner_text("#contracts")
    assert "Aphorite" in text and "×15" in text                    # ore + quantity
    assert "Hadanite" in text and "×5" in text
    assert "collect any" in text.lower()                           # the any-one-of header
    assert "HDMS-Perlman" in text                                  # the "Go to" marker (origin cell)
    # the where-to-mine chips (reused locChips): bodies show up as cyan chips
    chips = page.locator("#contracts .mloc-chip")
    assert chips.count() >= 2
    chip_text = " ".join(chips.all_inner_texts()).lower()       # CSS uppercases the chip labels
    assert "hurston" in chip_text and "daymar" in chip_text
    assert errors == [], errors
