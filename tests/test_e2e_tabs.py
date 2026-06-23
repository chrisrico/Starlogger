"""End-to-end coverage of the data-driven tabs (Contracts / Cargo / Plan / Archive /
Mining) and their interactions — the surface the jukebox/settings e2e in test_e2e.py
doesn't touch.

The dashboard is lit-html now: every handler binds via lit @event (no window bridge), so
this suite boots the real dashboard over a State seeded with a few missions (built directly
from the model, never parsed from a log — same approach as test_snapshot.py), visits every
tab, asserts no console/page errors, and drives a representative interaction per area
(opening the contract editor, toggling a sub-tab, the ship combobox, the mining filter, …)
by clicking the real UI.

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


@pytest.fixture
def server_with_state():
    """Like populated_server, but yields (url, State) so a test can mutate state + bump the
    version to push a real live SSE snapshot to the open dashboard (function-scoped: its own
    fresh state per test)."""
    st = _seeded_state()
    app = server.create_app(st, log_path=os.environ["STARLOGGER_LOG"])
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", st
    finally:
        httpd.shutdown()


@pytest.fixture(autouse=True)
def _fast_timeouts(page):
    page.set_default_timeout(7000)
    page.set_default_navigation_timeout(15000)


def _set_mode(page, label):
    """Pin a view mode by clicking the header mode switch (was window.setMode — the switch is
    lit-rendered and binds via @click now). label is the button text: Auto/Cargo/Mining/Salvage."""
    page.click(f"#modeswitch button:has-text('{label}')")


def _boot(page, url):
    """Navigate, capture page errors, wait until the first snapshot has rendered missions."""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector("#contracts")
    page.wait_for_function("() => document.querySelector('#contracts') "
                           "&& document.querySelector('#contracts').children.length > 0")
    return errors


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


def test_minerals_tab_renders_in_mining_mode(page, populated_server):
    """The Minerals tab is hidden in cargo mode, so force mining via setMode — effectiveMining()
    honours the override without needing a mining ship — then confirm the Find tool renders. It's
    a single-view page now (Blueprints split off to its own page), so there's no sub-tab bar."""
    errors = _boot(page, populated_server)
    _set_mode(page, 'Mining')
    page.wait_for_selector('#nav a[data-tab="minerals"]:not(.hide)')
    page.click('#nav a[data-tab="minerals"]')
    page.wait_for_selector("#minerals input#mf-name")           # the Find tool rendered
    assert page.locator("#minerals .arch-tabs").count() == 0    # single-view: no sub-tab bar
    assert errors == [], errors


def test_blueprint_tab_renders_in_mining_mode(page, populated_server):
    """Blueprints is its own top-level page now (Mining mode only). Force mining, open it, and
    confirm the picker table renders — with no sub-tab bar of its own."""
    import json
    page.route("**/api/blueprints", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"blueprints": [
            {"name": "Stub A", "type": "FPS Weapons", "subtype": "Rifle", "cls": "", "quality": "A", "size": 1}]})))
    errors = _boot(page, populated_server)
    _set_mode(page, 'Mining')
    page.wait_for_selector('#nav a[data-tab="blueprint"]:not(.hide)')
    page.click('#nav a[data-tab="blueprint"]')
    page.wait_for_function("() => document.querySelectorAll('#blueprint .bp-prow').length > 0")
    assert page.locator("#blueprint .arch-tabs").count() == 0
    assert errors == [], errors


def test_share_plan_round_trip(page, populated_server):
    """A read-only blueprint plan shared via a ?code= link. The whole plan (selected blueprints +
    quantities) rides INSIDE the link, so the recipient opens it on THEIR own install and it renders
    against their catalog — the sharer's instance serves nothing. Drives the full loop: pick a qty →
    Share copies a self-contained localhost link → opening it shows the read-only snapshot → Import
    copies it into the editable plan."""
    import json
    from urllib.parse import urlsplit

    catalog = {"blueprints": [
        {"name": "Stub A", "type": "FPS Weapons", "subtype": "Rifle", "cls": "", "quality": "A", "size": 1},
        {"name": "Stub B", "type": "Vehicle Component", "subtype": "Power Plant", "cls": "Military",
         "quality": "A", "size": 2}]}
    page.route("**/api/blueprints", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(catalog)))
    # The snapshot's materials/contracts/deposits cards POST these; stub them so the read-only view
    # paints without console errors (their content is unit-tested elsewhere).
    page.route("**/api/blueprints-plan", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"items": [], "requirements": [], "minerals": []})))
    page.route("**/api/mining-plan", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"targets": [], "coverage": [], "per_mineral": []})))

    errors = _boot(page, populated_server)
    _set_mode(page, 'Mining')
    page.wait_for_selector('#nav a[data-tab="blueprint"]:not(.hide)')
    page.click('#nav a[data-tab="blueprint"]')
    page.wait_for_function("() => document.querySelectorAll('#blueprint .bp-prow').length > 0")

    # Select Stub B with a quantity of 3 (verifies the quantity travels, not just the selection).
    page.fill("#blueprint .bp-table tr[data-i='1'] .bp-qin", "3")

    # Share: the link is always surfaced in a selectable field (clipboard may be blocked headless).
    page.click("#blueprint .bp-share")
    page.wait_for_function(
        "() => { const i = document.querySelector('#blueprint .bp-share-link');"
        " return i && i.value.includes('?code=b1.'); }")
    link = page.input_value("#blueprint .bp-share-link")
    assert "localhost" in link and "?code=b1." in link, link

    # Open the link on the same server (keep its ?code=, swap the localhost:port host for the test's
    # 127.0.0.1:port). This is the recipient opening it on their OWN instance.
    page.goto(populated_server + "/?" + urlsplit(link).query)
    page.wait_for_selector("#blueprint .bp-shared-badge")          # read-only snapshot view
    rows = page.locator("#blueprint .bp-pick table tbody tr")
    assert rows.count() == 1                                       # only the shared blueprint
    assert "Stub B" in rows.first.inner_text()
    assert "(not in your catalog)" not in rows.first.inner_text()  # resolved against the catalog
    assert "Power Plant" in rows.first.inner_text()                # metadata came from THIS catalog
    assert rows.first.locator("td.lt-num").last.inner_text().strip() == "3"  # the shared quantity

    # Import into my plan (existing plan present from the share step → confirm dialog).
    page.on("dialog", lambda d: d.accept())
    page.click("#blueprint .bp-share.primary")
    page.wait_for_function("() => document.querySelectorAll('#blueprint .bp-prow').length > 0")  # editable again
    assert page.input_value("#blueprint .bp-table tr[data-i='1'] .bp-qin") == "3"
    assert errors == [], errors


def test_share_plan_bad_code_fails_gracefully(page, populated_server):
    """A corrupt/truncated ?code= must fail LOUD with a clear message, not a blank page or a
    console error — the codec throws and the view shows a 'back to my plan' escape hatch."""
    errors = _boot(page, populated_server)
    page.goto(populated_server + "/?code=b1.not-valid-base64-@@@")
    page.wait_for_selector("#blueprint .sb-warn")
    assert "invalid or corrupted" in page.inner_text("#blueprint .sb-warn")
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
    _set_mode(page, 'Salvage')
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


def test_contracts_editor_opens(page, populated_server):
    """The Contracts tab is lit-rendered: the Edit button binds via @click (no window bridge),
    so drive the real user path — click Edit — and confirm the editor renders its inputs."""
    errors = _boot(page, populated_server)
    page.click('#nav a[data-tab="contracts"]')
    page.locator("#contracts button", has_text="Edit").first.click()
    page.wait_for_selector("#contracts input")   # the inline editor exposes input fields
    assert errors == [], errors


def test_open_editor_survives_a_live_snapshot(page, server_with_state):
    """The lit-html maintainability win this POC set out to prove: the Contracts tab repaints on
    EVERY SSE snapshot (renderAll no longer guards it behind EDIT) — yet an open editor's
    typed-but-unsaved value AND focus survive the repaint, because lit reuses the editor's DOM
    nodes and skips bindings whose value is unchanged. This is the regression guard for dropping
    the EDIT render-suppression flag; if it fails, the guard was load-bearing after all."""
    url, st = server_with_state
    errors = _boot(page, url)
    page.click('#nav a[data-tab="contracts"]')
    page.locator("#contracts button", has_text="Edit").first.click()
    page.wait_for_selector("#contracts #ed_title")
    page.fill("#contracts #ed_title", "SENTINEL_UNSAVED")
    page.focus("#contracts #ed_title")
    # Push a REAL live snapshot that changes other data, forcing a Contracts repaint underneath
    # the open editor (a new mission → a new row appears).
    st.missions["mNEW"] = _haul("mNEW", {
        "mNEWd": Leg("mNEWd", "dropoff", cargo="Quartz", qty=10, zone_host_id="Z2")})
    st.bump_version()
    page.wait_for_function(
        "() => [...document.querySelectorAll('#contracts tbody tr')]"
        ".some(tr => /Quartz/.test(tr.textContent))")   # repaint happened (new row rendered)
    # ...and the open editor came through it intact: uncommitted value preserved, still focused.
    assert page.input_value("#contracts #ed_title") == "SENTINEL_UNSAVED"
    assert page.evaluate("() => document.activeElement && document.activeElement.id") == "ed_title"
    assert errors == [], errors


def test_header_ship_combobox_opens(page, populated_server):
    """The header ship picker (combobox.js — lit now, @focus/@input/@keydown/@blur, menu
    rendered via lit render()) opens its listbox on focus. The seeded state has no detected
    ship, so the searchable picker renders."""
    errors = _boot(page, populated_server)
    page.wait_for_selector("#shipSel")
    page.focus("#shipSel")
    page.wait_for_selector("#shipSel-menu.shipmenu.open")   # @focus → comboOpen → menu rendered+open
    page.wait_for_selector("#shipSel-menu .shipopt.clear")  # the "clear (use detected)" sentinel option
    assert errors == [], errors


def test_cargo_subtab_toggle(page, populated_server):
    """The Cargo Loading⇄Unloading sub-tab (lit tabBarTpl now, @click — was tabBar's
    interpolated onclick): click Unloading and confirm it becomes the active sub."""
    errors = _boot(page, populated_server)
    page.click('#nav a[data-tab="cargo"]')
    page.wait_for_selector("#cargo .arch-tabs")
    page.click("#cargo .arch-tabs button:has-text('Unloading')")
    page.wait_for_function(
        "() => document.querySelector('#cargo .arch-tab.active') "
        "&& /unload/i.test(document.querySelector('#cargo .arch-tab.active').textContent)")
    assert errors == [], errors


def test_blueprint_table_populates_when_catalog_loads_late(page, populated_server):
    """Regression: deep-linking to the Blueprints page builds the picker table before
    /api/blueprints resolves — initBlueprint() builds the shell synchronously while still awaiting
    the catalog, so it starts EMPTY — and the catalog-load path must then rebuild it (not skip).
    Reproduce via the real deep-link (/blueprint, mining mode pinned) with the catalog held, then
    assert the table fills once it arrives."""
    import json
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    held = []
    page.route("**/api/blueprints", lambda r: held.append(r))   # hold the catalog response open
    page.goto(populated_server)
    page.evaluate("() => localStorage.setItem('modeOverride', 'mining')")   # make /blueprint reachable
    page.goto(populated_server + "/blueprint")           # deep-link: activateTab→initBlueprint()
    page.wait_for_selector("#blueprint .bp-table")        # shell built synchronously (catalog still held)...
    assert page.locator("#blueprint .bp-prow").count() == 0  # ...but empty: exactly the regression's state
    for _ in range(60):                                  # let the (held) catalog request register
        if held:
            break
        page.wait_for_timeout(50)
    assert held, "catalog request was never intercepted"
    held[-1].fulfill(status=200, content_type="application/json", body=json.dumps({"blueprints": [
        {"name": "Stub A", "type": "FPS Weapons", "subtype": "Rifle", "cls": "", "quality": "A", "size": 1},
        {"name": "Stub B", "type": "FPS Weapons", "subtype": "Pistol", "cls": "", "quality": "A", "size": 1},
    ]}))
    page.wait_for_function("() => document.querySelectorAll('#blueprint .bp-prow').length > 0")
    assert errors == []


def test_blueprint_table_columns_and_filter(page, populated_server):
    """The Blueprints table exposes Name/Type/Subtype/Class/Quality/Size/Acquired columns, each
    with a spreadsheet-style multi-select filter; unchecking a value hides its rows. Acquired
    blueprints (from the game log) render a \u2713."""
    import json
    rows = [
        {"name": "Mil Shield", "type": "Vehicle Component", "subtype": "Shield",
         "cls": "Military", "quality": "A", "size": 2, "acquired": True,
         "acquired_at": "2026-06-22T06:33:18Z"},
        {"name": "Civ Shield", "type": "Vehicle Component", "subtype": "Shield",
         "cls": "Civilian", "quality": "B", "size": 2, "acquired": False},
    ]
    # stub the catalog API so the assertion doesn't depend on the shared on-disk cache
    page.route("**/api/blueprints", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"blueprints": rows})))
    errors = _boot(page, populated_server)
    _set_mode(page, 'Mining')
    page.click('#nav a[data-tab="blueprint"]')
    page.wait_for_function("() => document.querySelectorAll('#blueprint .bp-prow').length === 2")
    heads = page.eval_on_selector_all(
        "#blueprint .bp-table thead th",
        "ths => ths.map(t => t.textContent.replace(/[\u25be\u25b2\u25bc]/g, '').trim())")
    assert heads == ["Name", "Type", "Subtype", "Class", "Quality", "Size", "Acquired", "Qty"], heads
    assert page.locator("#blueprint .bp-prow").count() == 2
    assert page.locator("#blueprint .bp-table tbody .bp-acq").count() == 1   # only the acquired row
    # multi-select filter on Class: uncheck "Military" -> only the Civilian row remains
    page.click('#blueprint th[data-col="cls"] .bp-fbtn')
    page.wait_for_selector("#bp-fpop.open")
    page.click('#bp-fpop .bp-fopt input[value="Military"]')
    page.wait_for_function("() => [...document.querySelectorAll('#blueprint .bp-prow')]"
                           ".filter(r => r.style.display !== 'none').length === 1")
    shown = page.eval_on_selector_all(
        "#blueprint .bp-prow",
        "rows => rows.filter(r => r.style.display !== 'none').map(r => r.querySelector('td b').textContent)")
    assert shown == ["Civ Shield"], shown
    assert errors == []


def test_blueprint_acquired_filter_and_column_toggle(page, populated_server):
    """The Acquired column filters like any other (uncheck "Yes" → only un-owned rows remain) and
    can be toggled off entirely, which removes it from the header and clears its filter."""
    import json
    rows = [
        {"name": "Mil Shield", "type": "Vehicle Component", "subtype": "Shield",
         "cls": "Military", "quality": "A", "size": 2, "acquired": True,
         "acquired_at": "2026-06-22T06:33:18Z"},
        {"name": "Civ Shield", "type": "Vehicle Component", "subtype": "Shield",
         "cls": "Civilian", "quality": "B", "size": 2, "acquired": False},
    ]
    page.route("**/api/blueprints", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"blueprints": rows})))
    errors = _boot(page, populated_server)
    _set_mode(page, 'Mining')
    page.click('#nav a[data-tab="blueprint"]')
    page.wait_for_function("() => document.querySelectorAll('#blueprint .bp-prow').length === 2")
    # filter by Acquired: uncheck "Yes" -> only the not-acquired row remains
    page.click('#blueprint th[data-col="acquired"] .bp-fbtn')
    page.wait_for_selector("#bp-fpop.open")
    page.click('#bp-fpop .bp-fopt input[value="Yes"]')
    page.wait_for_function("() => [...document.querySelectorAll('#blueprint .bp-prow')]"
                           ".filter(r => r.style.display !== 'none').length === 1")
    shown = page.eval_on_selector_all(
        "#blueprint .bp-prow",
        "rows => rows.filter(r => r.style.display !== 'none').map(r => r.querySelector('td b').textContent)")
    assert shown == ["Civ Shield"], shown
    # toggle the column off -> header drops "Acquired" and its filter clears (both rows return)
    page.click("#blueprint button:has-text('Hide acquired')")
    page.wait_for_function("() => [...document.querySelectorAll('#blueprint .bp-table thead th')]"
                           ".every(t => t.textContent.trim() !== 'Acquired')")
    heads = page.eval_on_selector_all(
        "#blueprint .bp-table thead th",
        "ths => ths.map(t => t.textContent.replace(/[▾▲▼]/g, '').trim())")
    assert heads == ["Name", "Type", "Subtype", "Class", "Quality", "Size", "Qty"], heads
    page.wait_for_function("() => [...document.querySelectorAll('#blueprint .bp-prow')]"
                           ".filter(r => r.style.display !== 'none').length === 2")
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
    _set_mode(page, 'Mining')
    page.click('#nav a[data-tab="blueprint"]')
    page.wait_for_function("() => document.querySelectorAll('#blueprint .bp-prow').length === 1")
    page.click("#blueprint .bp-prow .bp-step[aria-label='One more']")   # qty 0->1 (lit @click bpStep)
    page.wait_for_function(
        "() => [...document.querySelectorAll('#mres-plan .card h3 span')]"
        ".some(s => /Reward contracts/i.test(s.textContent))")
    page.wait_for_selector("#mres-plan .bp-fac")
    assert "Eckhart Security" in page.locator("#mres-plan .bp-fac").first.text_content()
    assert page.locator("#mres-plan .bp-src .lt-tag").count() == 6           # capped
    assert page.locator("#mres-plan .bp-src", has_text="+2 more").count() == 1
    assert errors == [], errors


def test_reward_contracts_suppressed_for_acquired_blueprint(page, populated_server):
    """A blueprint the player already owns (acquired=True) is dropped from the 'Reward contracts'
    card — no point showing how to earn one you have. Its only source would otherwise render, so
    the whole card is suppressed when it's the lone selected blueprint."""
    import json
    page.route("**/api/blueprints", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({"blueprints": [
            {"name": "Owned Cannon", "type": "Vehicle Weapons", "subtype": "Cannon",
             "cls": "Military", "quality": "A", "size": 3, "acquired": True,
             "acquired_at": "2026-06-22T06:33:18Z"}]})))
    page.route("**/api/blueprints-plan", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "items": [{"name": "Owned Cannon", "qty": 1, "found": True,
                       "sources": [{"faction": "Eckhart Security", "contracts": ["Contract 1"]}]}],
            "requirements": [], "minerals": [], "craft_seconds": 0, "total_scu": 0})))
    page.route("**/api/mining-plan", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"targets": [], "per_mineral": [], "coverage": []})))
    errors = _boot(page, populated_server)
    _set_mode(page, 'Mining')
    page.click('#nav a[data-tab="blueprint"]')
    page.wait_for_function("() => document.querySelectorAll('#blueprint .bp-prow').length === 1")
    page.click("#blueprint .bp-prow .bp-step[aria-label='One more']")   # qty 0->1 -> recompute plan
    # the Materials card renders (the plan ran), but no Reward-contracts card for the owned blueprint
    page.wait_for_function(
        "() => [...document.querySelectorAll('#mres-plan .card h3 span')]"
        ".some(s => /Materials needed/i.test(s.textContent))")
    cards = page.eval_on_selector_all(
        "#mres-plan .card h3 span", "ss => ss.map(s => s.textContent)")
    assert not any("Reward contracts" in c for c in cards), cards
    assert errors == [], errors
