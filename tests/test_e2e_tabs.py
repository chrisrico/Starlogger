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
    # active (this seeded state is cargo-hauling), so it isn't clickable here by design. Signal ID
    # is included — it's a top-level page visible in every mode.
    for tab in ("contracts", "signal", "cargo", "plan", "archive"):
        page.click(f'#nav a[data-tab="{tab}"]')
        page.wait_for_selector(f"#{tab}:not(.hide)")
    assert errors == [], errors


def test_signal_tab_renders_top_level(page, populated_server):
    """Signal ID is a top-level page in every mode (here cargo, the seeded state) — no setMode
    needed. Clicking it builds the RS-reading tool; deeper identify behaviour needs the p4k
    mineables catalog (absent from the isolated test data dir), so this confirms it mounts clean."""
    errors = _boot(page, populated_server)
    page.click('#nav a[data-tab="signal"]')
    page.wait_for_selector("#signal:not(.hide)")
    page.wait_for_selector("#signal input#signal-rs")   # the RS-reading tool rendered
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


def test_salvage_tab_dropdown_lists_ships(page, populated_server):
    """The Salvage tab is hidden in cargo mode, so force salvage via setMode (a bridged handler),
    then drive the ship picker: a searchable combobox (mirroring the blueprint picker) over a
    manufacturer-grouped listbox. Focusing opens the list (salvageDdOpen); clicking a hull option
    (salvagePick) must render its removable-component breakdown, greying the non-pullable
    (size>2 non-weapon) rows. A tiny catalog is seeded into the isolated data dir so
    /api/salvage-ship has content; deeper resolution is unit-tested."""
    from starlogger import config, salvage_ships
    salvage_ships.save_salvage_ships({
        "aegs_gladius": {"class": "AEGS_Gladius", "name": "Gladius", "manufacturer": "Aegis",
                         "components": [
                             {"category": "weapon", "name": "Test Gun", "size": 3, "grade": "A",
                              "count": 1, "pullable": True},
                             {"category": "shield", "name": "Big Shield", "size": 3, "grade": "B",
                              "count": 1, "pullable": False}]},
        "anvl_carrack": {"class": "ANVL_Carrack", "name": "Carrack", "manufacturer": "Anvil",
                         "components": []},
    }, path=config.SALVAGE_SHIPS_PATH)

    errors = _boot(page, populated_server)
    page.evaluate("window.setMode('salvage')")
    page.wait_for_selector('#nav a[data-tab="salvage"]:not(.hide)')
    page.click('#nav a[data-tab="salvage"]')
    page.wait_for_selector("#salvage #salv-auto")                       # auto-detected wreck container
    page.wait_for_selector("#salvage input#salv-pick")                  # combobox (after catalog fetch)
    page.click("#salvage #salv-pick")                                   # focus -> opens the listbox
    page.wait_for_selector("#salvage .salv-dd-list.open")
    # The list is structured: ships are grouped under manufacturer section headers.
    assert page.locator("#salvage .salv-dd-grp").count() >= 2           # Aegis + Anvil sections
    page.click("#salvage .salv-dd-item[data-key='aegs_gladius']")       # pick the hull
    page.wait_for_selector("#salvage .salv-comps")                      # components rendered
    page.wait_for_selector("#salvage .salv-comp.greyed")                # the size-3 shield is greyed
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


def test_mining_plan_table_populates_when_catalog_loads_late(page, populated_server):
    """Regression: deep-linking to the Plan sub built the blueprint table before /api/blueprints
    resolved (applySub runs miningSub synchronously; the fetch can't resolve mid-stack), and the
    catalog-load path then skipped the rebuild — leaving a permanently empty table. Delay the
    catalog so the shell is built first, then assert it fills once the catalog arrives."""
    import json
    held = []
    page.route("**/api/blueprints", lambda r: held.append(r))   # hold the catalog response open
    errors = _boot(page, populated_server)
    page.evaluate("() => window.setMode('mining')")
    page.click('#nav a[data-tab="mining"]')              # initMining() fires the (held) catalog fetch
    page.evaluate("() => window.miningSub('plan')")      # builds the shell while the catalog is held
    page.wait_for_selector("#mining .bp-table")          # table built...
    assert page.locator("#mining .bp-prow").count() == 0  # ...but empty: exactly the regression's state
    for _ in range(60):                                  # let the intercept fire, then release it
        if held:
            break
        page.wait_for_timeout(50)
    assert held, "catalog request was never intercepted"
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps({"blueprints": [
        {"name": "Stub A", "type": "FPS Weapons", "subtype": "Rifle", "cls": "", "quality": "A", "size": 1},
        {"name": "Stub B", "type": "FPS Weapons", "subtype": "Pistol", "cls": "", "quality": "A", "size": 1},
    ]}))
    page.wait_for_function("() => document.querySelectorAll('#mining .bp-prow').length > 0")
    assert errors == []


def test_mining_plan_table_columns_and_filter(page, populated_server):
    """The Plan table exposes Name/Type/Subtype/Class/Quality/Size columns, each with a
    spreadsheet-style multi-select filter; unchecking a value hides its rows."""
    import json
    rows = [
        {"name": "Mil Shield", "type": "Vehicle Component", "subtype": "Shield",
         "cls": "Military", "quality": "A", "size": 2},
        {"name": "Civ Shield", "type": "Vehicle Component", "subtype": "Shield",
         "cls": "Civilian", "quality": "B", "size": 2},
    ]
    # stub the catalog API so the assertion doesn't depend on the shared on-disk cache
    page.route("**/api/blueprints", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"blueprints": rows})))
    errors = _boot(page, populated_server)
    page.evaluate("() => window.setMode('mining')")
    page.click('#nav a[data-tab="mining"]')
    page.evaluate("() => window.miningSub('plan')")
    page.wait_for_function("() => document.querySelectorAll('#mining .bp-prow').length === 2")
    heads = page.eval_on_selector_all(
        "#mining .bp-table thead th",
        "ths => ths.map(t => t.textContent.replace(/[\u25be\u25b2\u25bc]/g, '').trim())")
    assert heads == ["Name", "Type", "Subtype", "Class", "Quality", "Size", "Qty"], heads
    assert page.locator("#mining .bp-prow").count() == 2
    # multi-select filter on Class: uncheck "Military" -> only the Civilian row remains
    page.click('#mining th[data-col="cls"] .bp-fbtn')
    page.wait_for_selector("#bp-fpop.open")
    page.click('#bp-fpop .bp-fopt input[value="Military"]')
    page.wait_for_function("() => [...document.querySelectorAll('#mining .bp-prow')]"
                           ".filter(r => r.style.display !== 'none').length === 1")
    shown = page.eval_on_selector_all(
        "#mining .bp-prow",
        "rows => rows.filter(r => r.style.display !== 'none').map(r => r.querySelector('td b').textContent)")
    assert shown == ["Civ Shield"], shown
    assert errors == []


def test_reward_contracts_card_renders_per_blueprint(page, populated_server):
    """Selecting a blueprint with known reward sources renders the per-blueprint 'Reward
    contracts' card: the granting faction's label + its contract-title chips, capped at 6 per
    faction with a '+N more' tail. The plan endpoints are stubbed so the assertion doesn't
    depend on the on-disk catalog (which may predate the structured-`sources` rebuild)."""
    import json
    titles = [f"Contract {i}" for i in range(1, 9)]   # 8 -> shown capped to 6 with "+2 more"
    page.route("**/api/blueprints", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"blueprints": [
            {"name": "Test Cannon", "type": "Vehicle Weapons", "subtype": "Cannon",
             "cls": "Military", "quality": "A", "size": 3}]})))
    page.route("**/api/blueprints-plan", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "items": [{"name": "Test Cannon", "qty": 1, "found": True,
                       "sources": [{"faction": "Eckhart Security", "contracts": titles}]}],
            "requirements": [], "minerals": [], "craft_seconds": 0, "total_scu": 0})))
    page.route("**/api/mining-plan", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"targets": [], "per_mineral": [], "coverage": []})))
    errors = _boot(page, populated_server)
    page.evaluate("() => window.setMode('mining')")
    page.click('#nav a[data-tab="mining"]')
    page.evaluate("() => window.miningSub('plan')")
    page.wait_for_function("() => document.querySelectorAll('#mining .bp-prow').length === 1")
    page.evaluate("() => window.bpStep(0, 1)")            # qty 1 -> schedules renderBpPlan
    page.wait_for_function(
        "() => [...document.querySelectorAll('#mres-plan .card h3 span')]"
        ".some(s => /Reward contracts/i.test(s.textContent))")
    page.wait_for_selector("#mres-plan .bp-fac")
    assert "Eckhart Security" in page.locator("#mres-plan .bp-fac").first.text_content()
    assert page.locator("#mres-plan .bp-src .lt-tag").count() == 6           # capped
    assert page.locator("#mres-plan .bp-src", has_text="+2 more").count() == 1
    assert errors == [], errors
