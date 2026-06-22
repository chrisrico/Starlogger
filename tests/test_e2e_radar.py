"""End-to-end: the ⚙ ship-equipment modal's RADAR row renders, ranked, for a mining ship.

The unit/integration suites cover the radar catalog, the endpoint, and persistence; this is the
RUNTIME guard for shipequip.js's renderEquip() radar branch — code that only executes when the
modal actually opens. Boots the real dashboard with a Prospector as the active (mining) ship and
a seeded radar catalog, opens the modal, and asserts the radar <select> lists the radars best-RS
first with a ★ on the top pick and the stock tag on the Surveyor-Lite.

Marked `browser`; run with `pytest -m browser`, skipped gracefully without Chromium.
"""
from __future__ import annotations

import json
import os
import threading

import pytest
from werkzeug.serving import make_server

import starlogger.server as server
from starlogger import config, radar
from starlogger.state import State

pytestmark = pytest.mark.browser


def _radar(cls, name, mfr_code, mfr, rs, pierce=1.0, grade=3):
    return {"class": cls, "name": name, "manufacturer": mfr, "manufacturer_code": mfr_code,
            "size": 1, "grade": grade, "sub_type": "MidRangeRadar",
            "rs": rs, "rs_piercing": pierce, "sensitivity_max": 1.0, "ping_cooldown": 2.5}


@pytest.fixture(autouse=True)
def _need_browser(require_browser):
    """Skip if Chromium isn't available."""


@pytest.fixture(scope="module")
def mining_server():
    """Real app with a Prospector as the detected ship (-> mining_ship true -> the ⚙ button
    shows) over a seeded ships + radar catalog. Cleans the seeded catalog files on teardown so
    the shared session data dir doesn't leak into other e2e modules."""
    # A Prospector with a size-1 mining hardpoint + a size-1 radar slot (what build_ships records).
    with open(config.SHIP_CARGO_PATH, "w") as f:
        json.dump({"game_version": "4.8.0", "ships": {"Prospector": {
            "class": "MISC_Prospector", "scu": 0, "name": "Prospector", "role": "Light Mining",
            "mining": {"hardpoints": [1]},
            "radar": {"size": 1, "stock": "radr_chco_s01_surveyorlite"}}}}, f)
    radar.save_radar([
        _radar("RADR_WLOP_S01_Abetti", "Abetti", "WLOP", "WillsOp", 1.0),
        _radar("RADR_CHCO_S01_SurveyorLite", "Surveyor-Lite", "CHCO", "Chimera Communications", 0.8),
        _radar("RADR_CHCO_S01_ObserverLite", "Observer-Lite", "CHCO", "Chimera Communications", 0.6),
    ], game_version="4.8.0")

    st = State()
    st.ship = "Prospector"                       # detected ship -> snapshot.mining_ship = True
    app = server.create_app(st, log_path=os.environ["STARLOGGER_LOG"])
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        for p in (config.SHIP_CARGO_PATH, config.RADAR_PATH):
            try:
                os.remove(p)
            except OSError:
                pass


def test_radar_row_renders_ranked(page, mining_server):
    page.set_default_timeout(8000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(mining_server)
    # The header shows the ⚙ ship-equipment button once the snapshot reports a mining ship.
    page.wait_for_selector("#shipEquipBtn")
    page.click("#shipEquipBtn")
    page.wait_for_selector("#se-radar")                              # the radar row rendered
    texts = page.eval_on_selector_all("#se-radar option", "els => els.map(e => e.textContent.trim())")
    # Best-RS first, ★ on the top pick, RS% shown, and the stock tag on the Surveyor-Lite.
    assert texts[0] == "— none —"
    assert texts[1].startswith("★") and "RS 100%" in texts[1]        # a 1.0-RS radar leads, starred
    surveyor = next(t for t in texts if "Surveyor-Lite" in t)
    assert "RS 80%" in surveyor and "stock" in surveyor and not surveyor.startswith("★")
    # The "Apply recommended" affordance is present too.
    assert page.locator(".se-rec-btn").count() == 1
    assert errors == [], errors
