"use strict";

import { $, num, mount, tabBarTpl } from "./dom.js";
import { html, nothing, repeat, unsafeHTML, ifDefined } from "./lit.js";
import { postJSON, postRaw, getJSON } from "./net.js";
import { initMinerals, locChips, locKey } from "./minerals.js";
import { initBlueprint } from "./blueprint.js";
import { registerCombo, comboInputHtml } from "./combobox.js";
import { initSignal, syncSignalSession } from "./signal.js";
import { initSalvage, renderSalvage } from "./salvage.js";
import { initJukebox, openJukebox, closeJukebox, jukeApplyMusicState, claimJukeboxPrimary } from "./jukebox.js";
import "./settings.js";   // side-effect: renders the Settings overlay + wires its own nav button
import { openShipEquip } from "./shipequip.js";  // also side-effect: wires the popup + self-bridges its handlers
import "./hint.js";       // side-effect: drives the floating tips for inline "?" help badges
// Shared hot state (TAB / LAST / ROUTE_ORDER / REPLAY_*) + the snapshot accessor live on the
// `S` object so the archive/stream/editor modules all mutate the same state. See state.js.
import { S, REPLAY_UNAVAILABLE, curData } from "./state.js";
// Archive tab + replay (self-wires its own inline handlers); typeMark is shared with the
// live contracts table. archive.js imports renderAll/replayEdit back from here.
import { loadSessions, activateArchiveTab, typeMark } from "./archive.js";
// Live SSE feed + update banner. connectStream boots the feed (from the bootstrap below);
// refresh is the one-shot pull the editor reaches for after a write.
import { connectStream, refresh } from "./stream.js";

let EDIT = null;      // mission_id whose editor is open (Contracts tab). NOT a render guard:
                      // the Contracts tab is lit-rendered, so renderAll repaints it on every
                      // snapshot — lit reuses the open editor's DOM (typed-but-unsaved values
                      // and focus survive). EDIT just says which row shows the form.
let EDIT_DROPS = [];  // the editor's Drop-off rows, lit-managed: {id,cargo,qty,loc,hint}. Held
let EDIT_PICKUPS = []; // here (not in the DOM) so Add/Remove and a live snapshot don't fight an
let _legSeq = 0;      // imperative innerHTML splice; a keyed repeat reuses each row's inputs.
let EDIT_CELL = null; // token of the open inline editor (unified, one at a time)


// ---- Cargo / Plan sub-tabs ---- //
// Loading+Unloading live under the Cargo tab; Routes+Manifest under the Plan tab,
// each behind an .arch-tabs segmented control. The active sub persists in localStorage.
// CARGO_SUB defaults to "" (auto): the active phase is inferred from the snapshot
// (current location / cargo aboard) until the user picks one explicitly — see cargoDefault.
let CARGO_SUB = localStorage.getItem("cargoSub") || "";       // "" = auto · "pickup" · "dropoff"

// ---- tabs (path-routed: /contracts, /cargo, … — sub-tabs ride the #hash) ---- //
// Each primary screen is a real page at /<tab>, navigated with the History API so the URL
// is shareable / reloadable (the Flask app serves index.html for these paths — see
// server.py's SPA fallback) and the sidebar items are genuine <a href> links. A section's
// sub-tab (Cargo's Loading/Unloading, Mining's Identify/Find/Plan) is view state WITHIN a
// page, not a page of its own, so it lives in the URL #hash instead of the path.
const TABS = ["contracts", "signal", "cargo", "plan", "archive", "minerals", "blueprint", "salvage"];
const DEFAULT_TAB = "contracts";
const tabFromPath = (p) => {
  const seg = (p || "/").replace(/^\/+|\/+$/g, "").split("/")[0];
  return TABS.includes(seg) ? seg : DEFAULT_TAB;
};
// push=true  → a user navigation: enter the section fresh (drop any sub-tab #hash), add a
//              history entry. push=false → boot/popstate/auto: mirror the current URL
//              (keeping its #hash so the sub-tab is restored) without a new entry.
function activateTab(name, { push = true } = {}) {
  if (!TABS.includes(name)) return;
  S.TAB = name;
  document.querySelectorAll("#nav [data-tab]").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hide", t.id !== name));
  const hash = push ? "" : location.hash;            // entering fresh drops the sub-tab hash
  const url = "/" + name + hash;
  if (location.pathname + location.hash !== url) {
    (push ? history.pushState : history.replaceState).call(history, null, "", url);
  }
  if (name === "archive") activateArchiveTab();
  if (name === "signal") initSignal();
  if (name === "minerals") initMinerals();
  if (name === "blueprint") initBlueprint();
  if (name === "salvage") initSalvage();
  applySub(name, hash.slice(1));                     // restore Cargo's Loading/Unloading sub
}
// Apply a section's sub-tab from the URL #hash. No (or an unrecognised) hash leaves the
// section's own default in place — for Cargo that's the auto/persisted phase (cargoSubActive),
// so the hash acts as an explicit deep-link override rather than the source of truth.
// (Minerals + Blueprints are single-view pages now — no sub-tabs.)
function applySub(name, sub) {
  if (name === "cargo") {
    if (sub === "loading") cargoSub("pickup");
    else if (sub === "unloading") cargoSub("dropoff");
  }
}

// ---- sidebar: wide (icon + label) ⇄ skinny (icons only) ----
// The footer toggle flips the `.collapsed` (skinny) state and persists an explicit
// choice. With no stored choice the default follows the viewport — skinny on narrow
// screens (≤880px, the mobile default), wide on desktop — and tracks the breakpoint
// live until the user picks a side.
const SB_KEY = "sidebarCollapsed";
const SB_MQ = window.matchMedia("(max-width:880px)");
function applyCollapsed(on) {
  const sb = $("sidebar"), tog = $("navtoggle");
  if (!sb) return;
  sb.classList.toggle("collapsed", on);
  if (tog) { tog.setAttribute("aria-expanded", on ? "false" : "true");
             tog.title = on ? "Expand sidebar" : "Collapse sidebar"; }
}
function storedPref() { try { return localStorage.getItem(SB_KEY); } catch (_) { return null; } }
function setCollapsed(on) {
  applyCollapsed(on);
  try { localStorage.setItem(SB_KEY, on ? "1" : "0"); } catch (_) {}
}
applyCollapsed(storedPref() === null ? SB_MQ.matches : storedPref() === "1");
// follow the viewport across the breakpoint until an explicit choice is made
SB_MQ.addEventListener?.("change", e => { if (storedPref() === null) applyCollapsed(e.matches); });
const _collapseBtn = $("navtoggle");
if (_collapseBtn) _collapseBtn.onclick = () => setCollapsed(!$("sidebar").classList.contains("collapsed"));

// The nav items are real <a href="/tab"> links: a plain left-click is intercepted for an
// in-page History-API switch, but modified clicks (Ctrl/Cmd/Shift/Alt, middle button) fall
// through to the browser so "open in new tab/window" works against the server SPA fallback.
document.querySelectorAll("#nav [data-tab]").forEach(a => {
  a.addEventListener("click", (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    activateTab(a.dataset.tab);
  });
});
// Back/forward (and the legacy-hash redirect below) replay the URL onto the dashboard.
window.addEventListener("popstate", () => activateTab(tabFromPath(location.pathname), { push: false }));

// ---- jukebox overlay (sidebar Jukebox button -> modal, same pattern as Settings) ----
// Hidden until this tab wins the primary-jukebox lock (see the boot-restore below); only
// the owning tab shows the button / builds a player, so two tabs can't both play music.
$("navjukebox") && ($("navjukebox").style.display = "none", $("navjukebox").onclick = openJukebox);
$("jukeboxClose") && ($("jukeboxClose").onclick = closeJukebox);
$("jukeboxOverlay") && ($("jukeboxOverlay").onclick = (e) => { if (e.target.id === "jukeboxOverlay") closeJukebox(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("jukeboxOverlay").classList.contains("hide")) closeJukebox();
});

// ---- mining vs cargo mode ---- //
// Mode normally follows the snapshot: a mining vehicle (mining_ship — Prospector, MOLE,
// ROC…) hides the cargo-hauling tabs and shows Mining, and the header stats/gauge swap to
// the mining-relevant set. The MODE switch in the header lets the user pin it: "auto"
// follows detection; "cargo"/"mining" force a mode (e.g. to use the mining reference tools
// on foot, or to plan a haul while still sat in a Prospector). Persisted across sessions.
let MODE_OVERRIDE = localStorage.getItem("modeOverride") || "auto";   // auto | cargo | mining | salvage
// The effective mode for this snapshot: a pinned override, else auto-detected. Mining wins (a
// mining vehicle), then salvage (a salvage vessel OR wrecks detected in the log this session),
// else cargo. Drives the tab layout and the header readouts/gauge.
function effectiveMode(d) {
  if (MODE_OVERRIDE !== "auto") return MODE_OVERRIDE;
  if (d && d.mining_ship) return "mining";
  if (d && (d.salvage_ship || (d.detected_salvage && d.detected_salvage.length))) return "salvage";
  return "cargo";
}
function effectiveMining(d) { return effectiveMode(d) === "mining"; }  // header keys off this
function setMode(m) {
  if (MODE_OVERRIDE === m) return;
  MODE_OVERRIDE = m;
  localStorage.setItem("modeOverride", m);
  const d = curData(); if (d) renderAll(d);                           // swap tabs + header at once
}
function modeSwitchHtml(d) {
  const eff = effectiveMode(d);                                       // what Auto resolved to
  return html`${[["auto", "Auto"], ["cargo", "Cargo"], ["mining", "Mining"], ["salvage", "Salvage"]]
    .map(([k, t]) => {
      const on = MODE_OVERRIDE === k;
      const title = k === "auto" ? "Follow the detected ship / wrecks" : `Always use ${t} mode`;
      return html`<button class="modesw-opt${on ? " active" : ""}" aria-pressed=${on}
        title=${title} @click=${() => setMode(k)}>${t}${k === "auto" ? html` <small>${eff}</small>` : nothing}</button>`;
    })}`;
}

// Each mode shows its own tab set: cargo keeps the hauling tabs; mining and salvage each hide
// Cargo+Plan and slot their own tool tab right after Contracts. Driven from renderAll on every
// snapshot; idempotent via LAYOUT_MODE so it only touches the DOM on an actual mode change.
const HAUL_TABS = ["contracts", "signal", "cargo", "plan", "archive"];
const MINE_TABS = ["contracts", "signal", "minerals", "blueprint", "archive"];
const SALV_TABS = ["contracts", "signal", "salvage", "archive"];
const MODE_TABS = { mining: MINE_TABS, salvage: SALV_TABS, cargo: HAUL_TABS };
let LAYOUT_MODE = null;   // null until the first snapshot picks a layout
function applyTabLayout(mode) {
  if (LAYOUT_MODE === mode) return;
  LAYOUT_MODE = mode;
  const order = MODE_TABS[mode] || HAUL_TABS;
  document.querySelectorAll("#nav [data-tab]").forEach(b => {
    const i = order.indexOf(b.dataset.tab);
    b.classList.toggle("hide", i < 0);
    if (i >= 0) b.style.order = i;   // flex order: keep the visible slots contiguous
  });
  // If the active tab just got hidden, fall back to the mode's primary tab (mining→Minerals,
  // salvage→Salvage, else Contracts). An automatic correction, not a navigation — replace the
  // URL rather than pushing a history entry the user never asked for.
  if (!order.includes(S.TAB))
    activateTab(mode === "mining" ? "minerals" : mode === "salvage" ? "salvage" : "contracts", { push: false });
}


// ---- header: status pill, ship selector, telemetry readouts, capacity gauge ---- //

// Ship catalog for the manual selector, fetched once. name -> {manufacturer,scu,groups}.
let SHIP_DB = null;
let GRID_HOVER = false;       // hovering a load-order row — guards the poll from wiping the hold highlight
async function loadShipList() {
  try {
    const db = await getJSON("/api/ships");
    SHIP_DB = db.ships || {};
    if (S.LAST) renderAll(curData());  // repaint now that we have the catalog
  } catch (e) { /* leave null; the box still shows the current ship */ }
}

// concept ships (name tagged "[…Concept…]") are kept out of the picker
const shipIsConcept = (name) => /\[[^\]]*concept[^\]]*\]/i.test(name);

// The header picker lists only ships you operate in a tracked role — cargo (has a hold), mining,
// or salvage. Combat/other ships live in the catalog purely so the shipbuilder can outfit them
// (they'd clutter this picker); the shipbuilder keeps its own, fuller list.
const shipIsPrimary = (v) => (v.scu || 0) > 0 || !!v.mining || /mining|salvage/i.test(v.role || "");

// catalog entries matching `filter` (by ship name or manufacturer), concepts
// removed, sorted by manufacturer then name.
function shipMatches(filter) {
  if (!SHIP_DB) return [];
  const f = (filter || "").trim().toLowerCase();
  return Object.entries(SHIP_DB)
    .filter(([n, v]) => !shipIsConcept(n) && shipIsPrimary(v))
    .filter(([n, v]) => !f || n.toLowerCase().includes(f) || (v.manufacturer || "").toLowerCase().includes(f))
    .map(([n, v]) => ({ name: n, mfr: v.manufacturer || "—" }))
    .sort((a, b) => a.mfr.localeCompare(b.mfr) || a.name.localeCompare(b.name));
}

// Register the header ship picker with the shared combobox (id "shipSel"): a "clear" sentinel
// (revert to the detected ship) above the manufacturer-grouped matches; picking posts the choice,
// and leaving the box without a fresh pick restores the current ship.
let _shipComboReg = false;
function _registerShipCombo() {
  if (_shipComboReg) return; _shipComboReg = true;
  registerCombo("shipSel", {
    entries: (f) => [{ clear: true, label: "— clear (use detected) —" }, ...shipMatches(f)],
    onPick: (name) => selectShip(name),
    reset: () => (S.LAST && S.LAST.ship) || "",
  });
}

async function selectShip(name) {
  if (S.REPLAY_MODE) return replayEdit({ kind: "select_ship", ship: name || null });
  try { await postJSON("/api/select-ship", { ship: name || null }); }
  catch (e) { alert("Couldn't set ship: " + e); return; }
  refresh();
}

// The connection indicator (In Verse / Main Menu). Lives in its own slot at the far left
// of the top bar (#connpill), separate from the right-aligned ship/controls.
function connPillHtml(d) {
  const online = d.logged_in;
  return html`<span class="pill ${online ? "online" : "offline"}"><span class="dot"></span>${online ? "In Verse" : "Main Menu"}</span>`;
}

function statusHtml(d) {
  // Generic ship-equipment popup trigger. The popup is equipment-agnostic; today the only
  // equipment category is mining gear, so the button shows only for a mining ship — extend the
  // gate (and the popup body) when other equipment types arrive.
  const equip = d.mining_ship
    ? html` <button id="shipEquipBtn" class="ship-equip-btn" title="Ship equipment" aria-haspopup="dialog" @click=${openShipEquip}>⚙</button>`
    : nothing;
  if (d.boarded) {
    // crewing another player's ship — show it badged as boarded, not "detected"
    const who = d.boarded_owner ? `${d.boarded_owner}'s ship` : "another ship";
    return html`<span class="ship">SHIP <b>${d.ship || "—"}</b>${equip}
      <span class="ship-auto" title=${`you're aboard ${who} as crew — the manifest shows the shared haul in this hold`}>⚑ aboard ${who}</span></span>`;
  }
  if (d.ship_detected) {
    // detected ship overrides the manual pick — show it locked, no searchable box
    return html`<span class="ship">SHIP <b>${d.ship || "—"}</b>${equip}
      <span class="ship-auto" title="detected from the game log">● detected</span></span>`;
  }
  _registerShipCombo();
  const box = comboInputHtml("shipSel", { value: d.ship || "", placeholder: "search ship…", label: "Ship" });
  return html`<span class="ship">SHIP ${box}${equip}</span>`;
}

function readoutsHtml(d, mining) {
  // v may be a lit template (for the readouts that carry <small>/<span> markup) or a plain
  // value (lit escapes it) — both render correctly as child content.
  const stat = (k, v, cls) => html`<div class="stat ${cls || ""}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  if (mining) {
    // No hauling-contract metrics here — they don't apply to a mining run. We have no
    // live mining telemetry from the log, so surface the relevant context the snapshot
    // does carry: vehicle, its refined-ore hold (if any), where you are, and who.
    const items = [["Ship", d.ship || "—", d.ship ? "accent" : ""]];
    if (d.ship_scu) items.push(["Ore Hold", html`${num(d.ship_scu)} <small>SCU</small>`, ""]);
    items.push(["Location", d.location || "—", d.location ? "accent" : ""]);
    items.push(["Player", d.player || "—", ""]);
    return html`${items.map(([k, v, cls]) => stat(k, v, cls))}`;
  }
  const c = d.counts;
  const items = [
    ["Active", c.partial ? html`${c.active} <span class="sub">${c.partial}⚠</span>` : c.active, "accent"],
    ["Completed", c.completed, "good"],
    ["Abandoned", c.abandoned, c.abandoned ? "bad" : ""],
    ["Failed", c.failed, c.failed ? "bad" : ""],
    ["To Deliver", html`${num(d.active_scu)} <small>SCU</small>`, ""],
    ["Earned", html`${num(d.total_awarded)} <small>aUEC</small>`, ""],
    ["Location", d.location || "—", d.location ? "accent" : ""],
    ["Player", d.player || "—", ""],
  ];
  if (c.hidden) items.push(["Hidden", c.hidden, ""]);
  return html`${items.map(([k, v, cls]) => stat(k, v, cls))}`;
}

function gaugeHtml(d, mining) {
  if (mining) {
    // The "Cargo Load" gauge tracks hauling cargo, which is meaningless here; the mining
    // vehicle's refined-ore hold has no fill we can read from the log, so show capacity
    // only (or null, for hopper-only craft like the Prospector/ROC at scu 0 — renderHeader
    // collapses #capacity when the gauge is falsy).
    if (!d.ship_scu) return null;
    return html`<span class="lbl">Ore Hold</span>
      <span class="remain"><b>${num(d.ship_scu)}</b> SCU capacity</span>`;
  }
  // peak simultaneous load (back-haul aware) is what must fit at once; fall back
  // to total outstanding if the server didn't send it.
  const load = (d.peak_scu != null ? d.peak_scu : d.active_scu) || 0, cap = d.ship_scu;
  if (!cap) {
    return html`<span class="lbl">Cargo Load</span>
      <span class="remain"><b>${num(load)}</b> SCU outstanding${d.ship ? "" : " · awaiting ship"}</span>
      <div class="track"><div class="fill" style="width:0"></div></div>`;
  }
  const pct = Math.round(load / cap * 100);
  const over = load > cap;
  const remain = over
    ? html`<b class="over">${num(load - cap)}</b> SCU over`
    : html`<b>${num(cap - load)}</b> SCU free`;
  return html`<span class="lbl">Cargo Load</span>
    <span class="remain">${remain}</span>
    <div class="track"><div class="fill ${over ? "over" : ""}" style="width:${Math.min(100, pct)}%"></div><span class="pct">${num(load)} / ${num(cap)} SCU</span></div>`;
}

function renderHeader(d) {
  const mining = effectiveMining(d);
  // don't repaint the status bar while the ship search box is focused or its popup
  // is open — a poll landing mid-interaction would tear it down.
  const busy = !!(document.activeElement && document.activeElement.id === "shipSel");
  mount("connpill", connPillHtml(d));   // left-aligned connection indicator; always safe to repaint
  // The ship picker is still string-rendered (combobox.js not yet converted), so a repaint while
  // it's focused would tear down the open box — keep the busy guard until combobox.js converts.
  if (!busy) mount("status", statusHtml(d));
  mount("modeswitch", modeSwitchHtml(d));
  mount("stats", readoutsHtml(d, mining));
  const gauge = gaugeHtml(d, mining);                 // a lit template, or null when there's no gauge
  mount("capacity", gauge || nothing);
  const cap = $("capacity"); if (cap) cap.classList.toggle("hide", !gauge);  // collapse when empty
}

// ---- autocomplete catalog (cargo + station names) ---- //
function datalistsHtml(cat) {
  if (!cat) return nothing;
  // Tag each cargo option with its commodity category (Metal, Gas, …) from the p4k taxonomy
  // (T1) — shown by the autocomplete where the browser supports option labels (ifDefined omits
  // the label attribute when there's no type).
  const types = cat.cargo_types || {};
  return html`<datalist id="dl_cargo">${(cat.cargo || []).map(v =>
        html`<option value=${v} label=${ifDefined(types[v])}></option>`)}</datalist><datalist id="dl_station">${(cat.stations || []).map(v => html`<option value=${v}></option>`)}</datalist>`;
}

// ---- standby / empty states ---- //
// sub is trusted markup (e.g. <b>) → unsafeHTML; title/code are auto-escaped by lit.
function standby(title, sub, code) {
  return html`<div class="standby">
    <div class="reticle"><span class="tick h"></span><span class="tick v"></span><span class="core"></span></div>
    <div class="st-title">${title}</div>
    <div class="st-sub">${unsafeHTML(sub)}</div>
    <div class="st-code">${code}</div>
  </div>`;
}

// ---- loading / unloading / routes ---- //
const QTY = (q) => q == null ? html`<span class="warn" title="quantity not logged by the game">? SCU</span>` : (num(q) + " SCU");
const SCU = (n, partial) => num(n) + (partial ? "+" : "") + " SCU";
const partialNote = (d) => d.counts.partial
  ? html`<div class="note">⚠ ${d.counts.partial} active mission(s) are missing cargo/quantity data — Star Citizen didn't log the delivery objectives (common when several missions are accepted quickly). Cargo <b>type</b> is recovered from the contract; quantities show <b>?</b>. Use <b>Edit</b> on the Contracts tab to fill them in.</div>`
  : nothing;

// ---- unified in-place editor for unknown values --------------------------- //
// One mechanism for every cargo-ops screen (loading, unloading, routes, trip plan).
// A `field` describes what to persist: {k, zone?, mid?, oid?} where k is:
//   station — keyed by zoneHostId  -> /api/station-name (resolves everywhere)
//   origin  — keyed by mission_id  -> /api/override (merged)
//   cargo|qty — keyed by mission_id+objective id -> /api/leg-field
// Click a value to edit it in place; Enter/blur commits, Escape cancels. The token
// identifies which single cell is open so the 3s poll never yanks it mid-edit.
// A station value that's a placeholder, not a real name: missing, an unresolved
// "Unknown station …", or an "Origin/Destination pending" marker. Such values must
// read as *missing*, never as the station's actual name.
const UNKNOWN_STATION = (s) => !s || /^Unknown station/.test(s) || /^(Origin|Destination) pending$/.test(s);
// Display a station as plain text when real, or muted/italic ("missing") when a
// placeholder — for the read-only cells that don't use the editable() machinery.
const stationText = (s) => UNKNOWN_STATION(s) ? html`<span class="unk">${s || "unknown"}</span>` : (s || "");
const cellTok = (f) => [f.k, f.zone || "", f.mid || "", f.oid || ""].join("|");
const editPlaceholder = (k) => k === "qty" ? "SCU" : k === "cargo" ? "commodity" : "station name";
const editList = (k) => k === "cargo" ? "dl_cargo" : (k === "station" || k === "origin") ? "dl_station" : "";

// The open cell is guarded against the live poll by the EDIT_CELL check in renderAll (only
// rerenderEdits paints it), so the input is created once and left uncontrolled — `value=${cur}`
// seeds it and lit never re-sets it. Handlers close over the field `f` directly (no more
// data-field JSON round-trip through the DOM).
function editable(value, f, opts) {
  opts = opts || {};
  const known = value != null && value !== "" && !opts.unknown;
  if (EDIT_CELL === cellTok(f)) {
    const isnum = f.k === "qty";
    const list = editList(f.k);
    const cur = (opts.unknown || value == null) ? "" : value;
    return html`<span class="edc editing"><input id="edit_input" class="edc-in"
      type=${isnum ? "number" : "text"} min=${ifDefined(isnum ? "0" : undefined)} step=${ifDefined(isnum ? "1" : undefined)}
      inputmode=${ifDefined(isnum ? "numeric" : undefined)} list=${ifDefined(list || undefined)}
      value=${cur} placeholder=${opts.ph || editPlaceholder(f.k)}
      @keydown=${(e) => edKey(e, f)} @blur=${(e) => edCommit(e.target, f)}></span>`;
  }
  const inner = known ? value
    : html`<span class="edc-unkn">${opts.label || (value != null && value !== "" ? value : editPlaceholder(f.k))}</span>`;
  // role=button + tabindex + Enter/Space (edOpenKey) make the cell operable by keyboard and
  // announced by a screen reader, not mouse-only. aria-label says which field it edits.
  const noun = f.k === "qty" ? "quantity" : f.k === "cargo" ? "commodity"
    : f.k === "origin" ? "origin" : "station name";
  return html`<span class="edc${known ? "" : " is-unknown"}"
    role="button" tabindex="0" aria-label=${(known ? "Edit " : "Set ") + noun}
    title=${known ? "Click to correct" : "Click to set"}
    @click=${() => edOpen(f)} @keydown=${(e) => edOpenKey(e, f)}>${inner}<span class="edc-pen">✎</span></span>`;
}
// convenience wrappers used across the screens; render plain text when not editable
function stationCell(name, zone) {
  return zone ? editable(name, { k: "station", zone }, { unknown: UNKNOWN_STATION(name) }) : (name || "");
}
function cargoCell(cargo, mid, oid) {
  return (mid && oid) ? editable(cargo, { k: "cargo", mid, oid }, { unknown: !cargo || cargo === "Unknown cargo" })
                      : (cargo || "Unknown cargo");
}
function qtyCell(qty, mid, oid) {
  return (mid && oid) ? editable(qty, { k: "qty", mid, oid }, { unknown: qty == null, label: "?" })
                      : QTY(qty);
}

function edOpen(f) {
  EDIT_CELL = cellTok(f);
  rerenderEdits();
  const i = $("edit_input"); if (i) { i.focus(); if (i.select) i.select(); }
}
function edCancel() { EDIT_CELL = null; rerenderEdits(); }
// Open the in-place editor from the keyboard (Enter/Space) when the cell itself is focused.
function edOpenKey(e, f) {
  if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") { e.preventDefault(); edOpen(f); }
}
function edKey(e, f) {
  if (e.key === "Enter") { e.preventDefault(); edCommit(e.target, f); }
  else if (e.key === "Escape") { e.preventDefault(); edCancel(); }
}
let EDIT_BUSY = false;
async function edCommit(el, f) {
  if (!el || EDIT_BUSY) return;
  if (EDIT_CELL !== cellTok(f)) return;   // already cancelled/committed
  const raw = (el.value || "").trim();
  EDIT_BUSY = true; EDIT_CELL = null;
  if (S.REPLAY_MODE) {
    const op = f.k === "station" ? { kind: "station_name", zone: f.zone, name: raw }
      : f.k === "origin" ? { kind: "override", mission_id: f.mid, override: { ...rawOverride(f.mid), origin: raw || null } }
        : { kind: "leg_field", mission_id: f.mid, oid: f.oid, field: f.k, value: raw };
    await replayEdit(op);
    EDIT_BUSY = false;
    return;
  }
  try {
    if (f.k === "station") await postJSON("/api/station-name", { zone: f.zone, name: raw });
    else if (f.k === "origin") await postJSON("/api/override", { mission_id: f.mid, override: { ...rawOverride(f.mid), origin: raw || null } });
    else await postJSON("/api/leg-field", { mission_id: f.mid, oid: f.oid, field: f.k, value: raw });
  } catch (e) { alert("Save failed: " + e); }
  EDIT_BUSY = false;
  refresh();
}
// Archive edit: send one edit op to the ephemeral overlay, swap in the recomputed snapshot
// + updated overlay (nothing is persisted), and repaint. The single path every editor uses
// in replay mode in place of its live /api/* write.
// Exported: archive.js's markTradeLost routes through this while replaying.
export async function replayEdit(op) {
  try {
    const j = await postRaw("/api/replay/edit",
      { key: S.REPLAY_KEY, at: S.REPLAY_I, overlay: S.REPLAY_EDITS, op });
    if (!j || !j.snapshot) throw new Error((j && j.error) || "edit failed");
    S.REPLAY_EDITS = j.overlay; S.REPLAY_SNAPSHOT = j.snapshot;
    EDIT = null; renderAll(curData());
  } catch (e) { alert("Edit failed: " + e); }
}
// Re-render only the edit-bearing containers from the current snapshot (used when
// opening/cancelling an inline editor, without a network round-trip).
function rerenderEdits() {
  if (!S.LAST) return;
  const d = curData(); if (!d) return;
  mount("cargo", cargoView(d));
  mount("plan", planView(d));
}

// ---- Cargo tab: Loading (pickup) ⇄ Unloading (dropoff) behind a segmented control ---- //
// With no explicit choice (CARGO_SUB ""), default to the phase the logs imply: at a
// drop-off station → Dropoff; at a pickup station → Pickup; otherwise by cargo aboard.
function cargoDefault(d) {
  const loc = d && d.location;
  if (loc) {
    if ((d.unloading || []).some(g => g.location === loc)) return "dropoff";
    if ((d.loading || []).some(g => g.location === loc)) return "pickup";
  }
  return (d && d.active_scu > 0) ? "dropoff" : "pickup";
}
const cargoSubActive = (d) => CARGO_SUB || cargoDefault(d);
function cargoSub(k) {
  if (CARGO_SUB === k) return;
  CARGO_SUB = k;
  localStorage.setItem("cargoSub", k);
  // Reflect the chosen phase into the URL #hash so /cargo#loading|#unloading is a shareable
  // deep link. replaceState (not push): toggling a sub-tab shouldn't pile up history entries.
  const h = k === "dropoff" ? "unloading" : "loading";
  if (location.hash.slice(1) !== h) history.replaceState(null, "", location.pathname + "#" + h);
  const d = curData(); if (d) mount("cargo", cargoView(d));
}
function cargoView(d) {
  const sub = cargoSubActive(d);
  const body = sub === "dropoff"
    ? groupCards(d.unloading, "unloading", d)
    : groupCards(d.loading, "loading", d);
  return html`${tabBarTpl([["pickup", "Loading"], ["dropoff", "Unloading"]], sub, cargoSub)}${body}`;
}

// ---- Plan tab: ONE section — the ordered itinerary IS the load order, with the 3D
// hold below it. Dragging a stop sets the visit & load order (S.ROUTE_ORDER) that both
// the list and the hold packing follow. (Was two sub-tabs, Routes ⇄ Manifest, keyed on
// different units — a stop vs. a packed elevator — which never agreed; now one unit:
// the destination stop = one list row = one hold band.) See planView below.

function groupCards(groups, kind, d) {
  if (!groups.length) return kind === "loading"
    ? standby("Loading Bay Clear",
        "No cargo is queued for pickup. Accept a hauling contract in-game and your pickups appear here, <b>grouped by station</b>.",
        "awaiting manifest")
    : standby("Holds Empty",
        "Nothing staged for delivery. As you take on cargo, drop-offs show here <b>grouped by destination</b>.",
        "awaiting manifest");
  return html`${partialNote(d)}<div class="grid">${groups.map(g => {
    const rows = g.items.map(it => {
      const tail = kind === "loading"
        ? html`<div class="sub">→ ${it.to} · ${it.mission}</div>`
        : html`<div class="sub">← ${it.from} · ${it.mission}</div>`;
      // unloading rows carry a leg oid → can be ticked off as delivered
      const check = (kind === "unloading" && it.oid)
        ? legCheck(it.mission_id, it.oid, it.done) : nothing;
      return html`<div class="row ${it.done ? "done" : ""} ${it.partial ? "partial" : ""}">
        ${check}<div class="rowmain"><span class="cargo">${cargoCell(it.cargo, it.mission_id, it.oid)}</span>${tail}</div>
        <div class="qty">${qtyCell(it.qty, it.mission_id, it.oid)}</div></div>`;
    });
    const warn = g.has_partial ? html` <span class="warn" title="some quantities not logged">⚠</span>` : nothing;
    return html`<div class="card"><h3><span>${stationCell(g.location, g.zone)}${warn}</span>
        <span class="scu">${SCU(g.total_scu, g.has_partial)}</span></h3>${rows}</div>`;
  })}</div>`;
}

// A delivered-toggle for one leg.
function legCheck(mid, oid, done) {
  return html`<button class="legchk ${done ? "on" : ""}" title=${done ? "Mark not delivered" : "Mark delivered"}
    @click=${() => markDelivered([{ mission_id: mid, oid }], !done)}>${done ? "✓" : ""}</button>`;
}

async function markDelivered(legs, done) {
  if (S.REPLAY_MODE) return replayEdit({ kind: "leg_state", legs, done });
  try { await postJSON("/api/leg-state", { legs, done }); }
  catch (e) { alert("Update failed: " + e); return; }
  refresh();
}

// ---- Routes: one ordered itinerary that doubles as the reorder control ---- //
// (Was two stacked sections, "Trip Plan" + "Route Rollup", that duplicated the same
// deliveries. Merged: the timeline IS the run list — each stop carries its origin(s)
// and mission count, and dragging a stop sets the visit & load order that the load
// sequence and the manifest packing both follow.)
function bodyLabel(s) {   // plain string → interpolated as lit text (auto-escaped at use)
  if (s.body === "?") return "Unknown location";
  const sys = s.system && s.system !== "?" && s.system !== s.body ? `${s.system} · ` : "";
  const moon = s.moon ? ` › ${s.moon}` : "";
  return sys + s.body + moon;
}

// container-size breakdown of one cargo line, as "n×size" groups (largest first),
// e.g. 24 SCU capped at 16 → "1×16 · 1×8". Reuses the mission tier's box cap.
function boxBreakdown(qty, maxBox) {
  if (!qty || typeof synthBoxes !== "function") return "";
  const by = {};
  synthBoxes(qty, maxBox).forEach(b => { by[b.scu] = (by[b.scu] || 0) + 1; });
  return Object.keys(by).map(Number).sort((a, b) => b - a).map(s => `${by[s]}×${s}`).join(" · ");
}
// one cargo chip on a stop row: material · qty · container breakdown. Name and qty stay
// inline-editable, and a leg tick lets a stop be marked delivered without leaving Plan.
function cargoChip(it, maxBox) {
  const brk = boxBreakdown(it.qty, maxBox);
  return html`<span class="cargochip">${legCheck(it.mission_id, it.oid, false)}<span class="cc-name">${cargoCell(it.cargo, it.mission_id, it.oid)}</span> <span class="cc-qty">${qtyCell(it.qty, it.mission_id, it.oid)}</span>${brk ? html` <span class="cc-box sub">${brk}</span>` : nothing}</span>`;
}

// How the hold was packed, in words — branches on the strategy packGroups chose.
function strategyCopy(packed) {
  const s = packed && packed.strategy;
  if (s === "split") return (packed.split === "width" ? "loaded left/right by stop" : "loaded fore/aft by stop")
    + (packed.spill ? " · spillover hold last" : "");
  if (s === "dualend") return "loaded from both ends inward" + (packed.spill ? " · spillover hold last" : "");
  if (s === "linear") return "loaded front-to-back" + (packed.spill ? " · spillover hold last" : "");
  return "load order doesn't matter";   // open
}

// The section header — matches the app's .arch-sub header language (was the unstyled .archbar).
function planHead(d, stops, jumps, hasGrid, access, packed, cap, placed, totalScu, empty) {
  const free = (hasGrid && cap) ? ` · ${num(Math.max(0, cap - placed))} SCU free` : "";
  const shipBit = hasGrid ? ` · ${d.ship} ${num(totalScu)}/${num(cap)} SCU`
    : (d.ship ? ` · ${d.ship}` : "");
  const title = empty ? `Trip Plan${shipBit}` : `Trip Plan · ${stops} stop(s) · ${jumps} jump(s)${shipBit}`;
  const sub = empty
    ? "no cargo staged — accept hauling contracts and your route & load plan appear here"
    : (hasGrid ? `${accessLabel(access)} · ${strategyCopy(packed)}${free}`
               : "drag a stop to set your visit & load order");
  const reset = S.ROUTE_ORDER
    ? html`<button class="route-reset" title="Forget the manual order; revert to the planner's fewest-jump order" @click=${resetRouteOrder}>↺ auto order</button>` : nothing;
  return html`<header class="plan-head"><span class="arch-title">${title}</span>
    <span class="sub">${sub}</span>${reset}</header>`;
}

// The whole Plan section: itinerary list (= load order) + the 3D hold, from one snapshot.
function planView(d) {
  const hasGrid = !!(d.ship && d.ship_grid && d.ship_grid.length);
  const access = (hasGrid && typeof accessFor === "function") ? accessFor(d.ship) : { open: true };
  const banded = !!access.axis;

  // manual drag order wins everywhere (list order, load order, hold packing)
  const planSorted = (d.plan && d.plan.stops)
    ? { ...d.plan, stops: byRouteOrder(d.plan.stops, s => s.station) } : { stops: [] };
  const hasStops = planSorted.stops && planSorted.stops.length;
  if (!hasStops && !hasGrid)
    return standby("No Routes Plotted",
      "Active contracts are bundled into <b>origin → destination</b> runs, then ordered into a fewest-jump itinerary and a hold-loading plan. Plot a haul to chart your route.",
      "no active legs");

  // one bundle per destination → one band in the hold (no destination mixing)
  const groups = cargoGroups(d);
  const order = !hasGrid ? groups
    : (banded ? [...groups].sort((a, b) => a.routeIdx - b.routeIdx) : loadOrder(groups));
  const shipPacked = hasGrid ? packGroups(d.ship_grid, order, banded ? access : null) : null;
  const cap = d.ship_scu || 0, placed = shipPacked ? shipPacked.placedScu : 0;
  const totalScu = groups.reduce((a, g) => a + g.scu, 0);
  const hold = hasGrid ? holdHtml(d, shipPacked, access) : nothing;

  // stable destination hue (matches the hold's box hues) + 1-based physical load position
  // (banded ships load deepest/last-delivered first, so the badge counts from the hatch).
  const hueOf = {}; let hi = 0;
  (d.unloading || []).forEach(g => { if (!(g.location in hueOf)) hueOf[g.location] = destHue(hi++); });
  const loadPos = {}; const gByDest = {};
  groups.forEach(g => { gByDest[g.dest] = g; });
  ((hasGrid && banded) ? [...order].reverse() : order).forEach((g, i) => { loadPos[g.dest] = i + 1; });
  const dualEnd = !!(shipPacked && shipPacked.strategy === "dualend");

  if (!hasStops)
    return html`<div class="planwrap">${planHead(d, 0, 0, hasGrid, access, shipPacked, cap, placed, totalScu, true)}
      <div class="sub" style="margin:10px 2px 14px">No cargo staged yet — accept hauling contracts and your route & load plan appear here.</div>
      ${hold}</div>`;

  // per-mission box-size cap (Medium 8-vs-16 keys off the mission's TOTAL SCU)
  const byId = {}; (d.missions || []).forEach(m => { byId[m.mission_id] = m; });
  const mTotal = {};
  planSorted.stops.forEach(s => (s.items || []).forEach(it => {
    mTotal[it.mission_id] = (mTotal[it.mission_id] || 0) + (it.qty || 0);
  }));
  const maxBoxOf = (mid) => tierMaxBox((byId[mid] || {}).title, mTotal[mid] || 0);

  // origin(s) + mission count rolled up by destination, from the route runs
  const runByDest = {};
  (d.routes || []).forEach(r => {
    const e = runByDest[r.destination] || (runByDest[r.destination] = { origins: [], missions: 0, partial: false });
    if (r.origin && !e.origins.includes(r.origin)) e.origins.push(r.origin);
    e.missions += r.mission_count || 0;
    e.partial = e.partial || r.has_partial;
  });

  // group consecutive stops under a body/moon header to show jumps
  let lastKey = null, n = 0;
  const stopRows = planSorted.stops.map(s => {
    const key = `${s.system}/${s.body}/${s.moon || ""}`;
    const header = key !== lastKey
      ? html`<li class="plan-leg"><span class="plan-jump">${++n}</span>${bodyLabel(s)}</li>` : nothing;
    lastKey = key;
    const hue = hueOf[s.station] != null ? hueOf[s.station] : destHue(0);
    const pos = loadPos[s.station];
    const run = runByDest[s.station] || { origins: [], missions: 0, partial: false };
    const grp = gByDest[s.station];
    const sharedTag = grp && grp.shared
      ? html` <span class="ls-alone" title="carries a cargo type split across stops — load this stop fully before the next, so the identical boxes don't get mixed up">⚠ shared</span>` : nothing;
    const from = run.origins.length
      ? html`<div class="ps-from sub">from ${run.origins.join(", ")}${run.missions ? " · " + run.missions + " mission(s)" : ""}</div>` : nothing;
    const chips = (s.items || []).map(it => cargoChip(it, maxBoxOf(it.mission_id)));
    // drag handle only, so clicking the station/cargo cells to edit never starts a drag
    return html`${header}<li class="card plan-stop route" data-dest=${s.station}
        @dragover=${routeDragOver} @dragleave=${routeDragLeave} @drop=${routeDrop} @dragend=${routeDragEnd}>
      <h3><span class="ends"><button type="button" class="route-grip" draggable="true"
          title="Drag, or focus and use ↑/↓, to reorder this stop" aria-label="Reorder this stop — use arrow up or down"
          @dragstart=${routeDragStart} @keydown=${routeGripKey}>⠿</button>${hasGrid ? html`<span class="ps-sw" style=${`background:hsl(${hue},64%,52%)`}></span>` : nothing}${(hasGrid && pos) ? html`<span class="ps-pos" title=${`load #${pos}${banded ? (dualEnd ? " — loaded from both ends inward" : " — loaded deepest-first") : ""}`}>${pos}</span>` : nothing}${stationCell(s.station, s.zone)}${run.partial ? html` <span class="warn">⚠</span>` : nothing}${sharedTag}</span>
        <span class="scu">${SCU(s.scu, run.partial)}</span></h3>
      ${from}<div class="ps-cargo">${chips}</div></li>`;
  });

  const overScu = shipPacked ? shipPacked.overflow.reduce((a, b) => a + b.scu, 0) : 0;
  const over = overScu
    ? html`<div class="note">⚠ ${num(overScu)} SCU won't fit this ${num(cap)} SCU hold — you'll need another run.</div>` : nothing;
  const ambig = (hasGrid && order.some(g => g.shared))
    ? html`<div class="note">⚠ A cargo type is bound for more than one destination — its boxes look identical. Load each stop marked <b>⚠ shared</b> <b>fully</b> before the next, so the twins don't get mixed up.</div>` : nothing;

  const load = planSorted.load || {};
  const loadItems = (load.items || []).map(it => html`<span class="chip">${it.cargo}${it.qty ? " " + num(it.qty) : ""}</span>`);
  const loadCard = html`<div class="plan-load">
    <div class="plan-step">LOAD</div>
    <div class="plan-body"><div class="plan-station">${load.station || "—"}</div>
      <div class="plan-chips">${loadItems.length ? loadItems : html`<span class="sub">no cargo outstanding</span>`}</div></div>
    <div class="scu">${num(planSorted.scu_total || 0)} SCU</div></div>`;

  return html`<div class="planwrap">${planHead(d, planSorted.stops.length, n, hasGrid, access, shipPacked, cap, placed, totalScu, false)}${over}${ambig}
    ${loadCard}
    <ol class="plan-stops" id="routegrid" @mouseover=${rowHover} @mouseout=${rowHover}>${stopRows}</ol>
    ${hold}</div>${partialNote(d)}`;
}

// ---- drag-reorder of the route runs (sets the manual delivery/load order) ---- //
let DRAG_DEST = null;
function routeDragStart(ev) {
  const card = ev.target.closest(".card.route");
  DRAG_DEST = card && card.dataset.dest;
  ev.dataTransfer.effectAllowed = "move";
  if (card) card.classList.add("dragging");
}
function routeDragOver(ev) {
  if (DRAG_DEST == null) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "move";
  const card = ev.target.closest(".card.route");
  if (card && card.dataset.dest !== DRAG_DEST) card.classList.add("dragover");
}
function routeDragLeave(ev) {
  const card = ev.target.closest(".card.route");
  if (card) card.classList.remove("dragover");
}
function routeDragEnd() {
  DRAG_DEST = null;
  document.querySelectorAll(".card.route").forEach(c => c.classList.remove("dragover", "dragging"));
}
function routeDrop(ev) {
  ev.preventDefault();
  const target = ev.target.closest(".card.route");
  if (!target || DRAG_DEST == null) return routeDragEnd();
  const dropDest = target.dataset.dest;
  // rebuild the full order from the current DOM, then move the dragged run before the drop target
  let order = [...document.querySelectorAll("#routegrid .card.route")].map(c => c.dataset.dest);
  if (dropDest === DRAG_DEST) return routeDragEnd();
  order = order.filter(x => x !== DRAG_DEST);
  order.splice(order.indexOf(dropDest), 0, DRAG_DEST);
  S.ROUTE_ORDER = order;
  persistRouteOrder();
  routeDragEnd();
  renderAll(curData());
}
// Persist the manual route order to localStorage — but only when live. In archive replay
// the order is ephemeral (restored on exit), so it must not bleed into the live view.
function persistRouteOrder() {
  if (S.REPLAY_MODE) return;
  if (S.ROUTE_ORDER) localStorage.setItem("routeOrder", JSON.stringify(S.ROUTE_ORDER));
  else localStorage.removeItem("routeOrder");
}
function resetRouteOrder() {
  S.ROUTE_ORDER = null;
  persistRouteOrder();
  renderAll(curData());
}
// Keyboard reorder (a no-mouse alternative to dragging the ⠿ grip): move the run
// one slot earlier/later, persist, re-render, and keep focus on the moved grip.
function moveRoute(dest, dir) {
  let order = [...document.querySelectorAll("#routegrid .card.route")].map(c => c.dataset.dest);
  const i = order.indexOf(dest), j = i + dir;
  if (i < 0 || j < 0 || j >= order.length) return;
  order.splice(i, 1); order.splice(j, 0, dest);
  S.ROUTE_ORDER = order;
  persistRouteOrder();
  renderAll(curData());
  setTimeout(() => {   // restore focus to the grip in its new position
    const card = [...document.querySelectorAll("#routegrid .card.route")].find(c => c.dataset.dest === dest);
    const grip = card && card.querySelector(".route-grip");
    if (grip) grip.focus();
  }, 0);
}
function routeGripKey(e) {
  const card = e.currentTarget.closest(".card.route");
  if (!card) return;
  if (e.key === "ArrowUp") { e.preventDefault(); moveRoute(card.dataset.dest, -1); }
  else if (e.key === "ArrowDown") { e.preventDefault(); moveRoute(card.dataset.dest, 1); }
}

// ---- all missions table + editor ---- //
// ---- editor leg rows (Drop-offs / Pickups), lit-managed ---- //
// The rows live in EDIT_DROPS / EDIT_PICKUPS, not the DOM, so Add/Remove and a live snapshot
// landing mid-edit don't fight an imperative innerHTML splice: a keyed repeat reuses each
// row's <input> across renders, and lit skips a binding whose value is unchanged — so a
// typed-but-unsaved value survives a repaint. gatherLegs still reads committed values straight
// off the DOM on Save (the bodyId <tbody> + .lc/.lq/.ll classes are unchanged).
function buildLegRows(m, kind) {
  let rows = Object.values((m && m.legs) || {}).filter(l => l.kind === kind);
  // A normal haul's pickup is just a zone marker (no cargo) — not a real collect pickup, so
  // don't show it as a blank row. Only genuine "Collect N SCU of X from Y" pickups; "+ Add
  // pickup" turns a haul into a collect mission. Drop-offs always show.
  if (kind === "pickup") rows = rows.filter(l => l.cargo);
  // guess cargo for drop-offs: one contract cargo applies to every drop; an exact count match
  // assigns them in order. Pickups stay blank.
  const types = (m && m.cargo_types) || [];
  const guess = (kind === "dropoff")
    ? (i) => types.length === 1 ? types[0] : (types.length === rows.length ? types[i] : "")
    : () => "";
  return rows.map((l, i) => ({
    id: ++_legSeq,
    cargo: l.cargo || guess(i) || "",
    qty: l.qty == null ? "" : l.qty,
    loc: l.location || l.name || "",
    hint: (!(l.location || l.name) && l.zone_host_id) ? `Unknown station (zone ${l.zone_host_id})` : "station",
  }));
}
const legRowsFor = (kind) => kind === "dropoff" ? EDIT_DROPS : EDIT_PICKUPS;
function addLeg(kind) {
  legRowsFor(kind).push({ id: ++_legSeq, cargo: "", qty: "", loc: "", hint: "station" });
  renderMissions();
}
function removeLeg(kind, id) {
  const arr = legRowsFor(kind), i = arr.findIndex(r => r.id === id);
  if (i >= 0) arr.splice(i, 1);
  renderMissions();
}

function legRowTpl(kind, r) {
  return html`<tr>
    <td><input class="lc" list="dl_cargo" aria-label="Cargo" placeholder="cargo" value=${r.cargo}></td>
    <td><span class="numf"><span class="numf-u">SCU</span><input class="lq" type="number" min="0" step="1" inputmode="numeric" aria-label="Quantity in SCU" placeholder="?" value=${r.qty}></span></td>
    <td><input class="ll" list="dl_station" aria-label="Location" placeholder=${r.hint} value=${r.loc}></td>
    <td><button type="button" class="rm" title="remove row" aria-label="Remove row" @click=${() => removeLeg(kind, r.id)}>✕</button></td>
  </tr>`;
}

function legTableTpl(kind, bodyId, locLabel) {
  const rows = legRowsFor(kind);
  return html`<table class="legtable"><thead><tr><th>Cargo</th><th>Qty</th><th>${locLabel}</th><th></th></tr></thead>
      <tbody id=${bodyId}>${repeat(rows, r => r.id, r => legRowTpl(kind, r))}</tbody></table>
      <button type="button" class="addrow" @click=${() => addLeg(kind)}>+ Add ${locLabel === "From" ? "pickup" : "drop-off"}</button>`;
}

function gatherLegs(bodyId, locKey) {
  return [...document.querySelectorAll(`#${bodyId} tr`)].map(r => {
    const cargo = r.querySelector(".lc").value.trim();
    const q = r.querySelector(".lq").value.replace(/[^0-9]/g, "");
    const loc = r.querySelector(".ll").value.trim();
    if (!cargo && !loc && !q) return null;  // skip blank rows
    return { cargo: cargo || null, qty: q ? parseInt(q, 10) : null, [locKey]: loc || null };
  }).filter(Boolean);
}

function legRow(cargo, qty, dest, opts = {}) {
  // a delivered-toggle when this row maps to a single leg (mid + oid given). cargo/qty/dest are
  // lit values or templates supplied by the caller.
  const chk = (opts.mid && opts.oid) ? legCheck(opts.mid, opts.oid, opts.done) : nothing;
  return html`<div class="legrow ${opts.done ? "legdone" : ""}">
    <span class="ml-cargo ${opts.warnCargo ? "warn" : ""}">${cargo}</span>
    <span class="ml-qty">${qty}</span>
    <span class="ml-arrow">→</span>
    <span class="ml-dest">${dest}${chk}</span>
  </div>`;
}

function missionLegs(m) {
  const drops = Object.values(m.legs || {}).filter(l => l.kind === "dropoff");
  if (drops.some(l => l.cargo)) {
    const rows = drops.filter(l => l.cargo).map(l => {
      const qty = l.qty == null ? html`<span class="warn">?</span> SCU` : `${num(l.qty)} SCU`;
      return legRow(l.cargo, qty, l.location || "?",
        { done: l.state === "completed", mid: m.mission_id, oid: l.objective_id });
    });
    return html`<div class="manilegs">${rows}</div>`;
  }
  if (drops.length) {
    const ct = (m.cargo_types || []).join(", ") || "Unknown cargo";
    const dests = (m.destinations || []).length
      ? m.destinations.map((s, i) => html`${i ? ", " : ""}${stationText(s)}`) : html`<span class="unk">?</span>`;
    const row = legRow(ct, html`<span class="warn">?</span> SCU`,
      html`${dests} <span class="sub">· qty not logged</span>`, { warnCargo: true });
    return html`<div class="manilegs">${row}</div>`;
  }
  return html`<span class="sub">—</span>`;
}

// A MINING contract's "ore × qty → where to mine" rows (Shubin purchase orders carry ore
// requirements instead of pickup/dropoff legs). Each ore lists where it's mined via the
// same `locChips` the mining tab uses (still a string helper → unsafeHTML), capped server-side
// with a "+N more" tail.
function oreRow(o) {
  const done = o.need > 0 && o.have >= o.need;
  const hidden = (o.loc_count || 0) - (o.locations || []).length;
  const more = hidden > 0 ? `<span class="lt-tag mloc-more">+${hidden} more</span>` : "";
  const where = (o.locations && o.locations.length)
    ? unsafeHTML(locChips(o.locations, more))
    : html`<div class="mloc"><span class="sub">where-to-mine unknown</span></div>`;
  return html`<div class="ore-row ${done ? "legdone" : ""}">
    <span class="ml-cargo">${o.ore}</span><span class="ml-qty">×${num(o.need)}</span>
    <span class="ore-where">${where}</span>
  </div>`;
}

function miningLegs(m) {
  const ores = m.ores || [];
  if (!ores.length) return html`<span class="sub">—</span>`;
  // locKey() is a handler-free legend string (minerals.js) → unsafeHTML.
  return html`<div class="ore-head">collect ${m.ore_any ? html`any <b>one</b> of` : "all of"}</div><div class="orelegs">${ores.map(oreRow)}</div>${unsafeHTML(locKey())}`;
}

function editorRowTpl(m) {
  const statuses = ["active", "completed", "abandoned", "failed", "expired"];
  // an unresolved origin (Unknown station / Origin pending) is a placeholder, not real
  // content: show it as the input placeholder and leave the field empty so typing overwrites.
  const unknownOrigin = UNKNOWN_STATION(m.origin);
  return html`<tr class="editrow"><td colspan="6"><div class="editor"
    @keydown=${(e) => edFormKey(e, m.mission_id)}>
    <div class="ef"><label for="ed_title">Title</label><input id="ed_title" value=${m.title || ""}></div>
    <div class="ef"><label for="ed_origin">Origin</label><input id="ed_origin" list="dl_station" value=${unknownOrigin ? "" : m.origin} placeholder=${unknownOrigin ? (m.origin || "origin") : "origin"}></div>
    <div class="ef"><label for="ed_reward">Reward <span class="sub">(aUEC · type 12k or 1.5m)</span></label><input id="ed_reward" type="text" inputmode="decimal" value=${m.reward || ""}></div>
    <div class="ef"><label for="ed_status">Status</label><select id="ed_status">
        <option value="" selected>(from log)</option>${statuses.map(s => html`<option value=${s}>${s}</option>`)}</select></div>
    <div class="ef wide"><label>Drop-offs</label>${legTableTpl("dropoff", "ed_drops", "Destination")}</div>
    <div class="ef wide"><label>Pickups <span class="sub">(collect missions only — leave empty for normal hauls)</span></label>${legTableTpl("pickup", "ed_pickups", "From")}</div>
    <div class="ef btns">
      <button class="primary" @click=${() => saveMission(m.mission_id)}>Save</button>
      <button @click=${cancelEdit}>Cancel</button>
      <button @click=${() => resetMission(m.mission_id)}>Reset to log</button>
    </div>
  </div></td></tr>`;
}

// One contract row (+ its editor row when open). Read-only display cells reuse the existing
// shared string helpers (typeMark / stationText / missionLegs / miningLegs / standby — still
// used by the not-yet-converted Cargo/Plan views) via unsafeHTML; their inline markDelivered
// handler stays window-bridged until those tabs convert. Everything interactive in THIS row
// (Edit/Delete/Restore + the editor) binds via lit @click, so it needs no window bridge.
function missionRowTpl(m) {
  const dec = m.decoded || {};
  // dec.type/icon and dec.legal are authoritative ContractTemplate data (p4k) layered in by
  // model.Mission.decoded; structure/category/grade come from the contract-id heuristic
  // (grade/SCU are runtime). The type mark leads the chip row.
  const typeChip = dec.type ? unsafeHTML(typeMark(dec.type, dec.icon)) : nothing;
  const chips = [dec.structure, dec.category, dec.grade].filter(Boolean)
    .map(t => html`<span class="chip">${t}</span>`);
  const illegal = dec.legal === false
    ? html`<span class="chip chip-illegal" title="Illegal contract">⚠ Illegal</span>` : nothing;
  const note = m.hidden ? html`<div class="sub">hidden</div>`
    : (m.partial && m.status === "active" ? html`<div class="warn" style="font-size:11px">⚠ partial</div>` : nothing);
  const action = m.hidden
    ? html`<button @click=${() => restoreMission(m.mission_id)}>Restore</button>`
    : html`<div class="rowact"><button @click=${() => editMission(m.mission_id)}>Edit</button><button class="danger" @click=${() => deleteMission(m.mission_id)}>Delete</button></div>`;
  const edited = m.overridden && !m.hidden
    ? html` <span class="chip" title="has manual edits">✎</span>` : nothing;
  const origin = (m.ores && m.ores.length && m.mining_goto)
    ? html`<span class="ore-goto" title="Go to (mission marker)">▸ ${m.mining_goto}</span>`
    : stationText(m.origin);
  const cargo = (m.ores && m.ores.length) ? miningLegs(m) : missionLegs(m);
  return html`<tr class=${m.hidden ? "hiddenrow" : ""}>
      <td><span class="badge b-${m.status}">${m.status}</span>${note}</td>
      <td>${m.title || m.contract}${edited}<div class="sub">${m.org}</div>${typeChip}${chips}${illegal}</td>
      <td>${origin}</td>
      <td>${cargo}</td>
      <td>${m.reward ? num(m.reward) + " aUEC" : html`<span class="sub">—</span>`}</td>
      <td>${action}</td>
    </tr>${(EDIT === m.mission_id && !m.hidden) ? editorRowTpl(m) : nothing}`;
}

function missionsTable(ms) {
  if (!ms.length) return standby("No Contracts",
    "No missions parsed from this session yet. Accepted contracts are <b>tracked here in real time</b>.",
    "standby");
  const order = { active: 0, completed: 1, failed: 2, abandoned: 2, expired: 2 };
  ms = ms.slice().sort((a, b) =>
    ((a.hidden ? 1 : 0) - (b.hidden ? 1 : 0)) ||
    (order[a.status] - order[b.status]) ||
    ((b.accepted_at || "").localeCompare(a.accepted_at || "")));
  return html`<div class="tscroll"><table><thead><tr><th>Status</th><th>Mission</th><th>Origin</th><th>Cargo → Destination</th><th>Reward</th><th></th></tr></thead><tbody>${repeat(ms, m => m.mission_id, missionRowTpl)}</tbody></table></div>`;
}

function renderMissions() { const d = curData(); if (d) mount("contracts", missionsTable(d.missions)); }

// ---- cargo grid (current ship) ---- //
// Distinct, evenly-spread hues per destination (golden-angle), stable as long as
// the unloading group order is stable (the server sorts it alphabetically).
const destHue = (i) => Math.round((i * 137.508) % 360);

// ---- cargo groups, elevator staging + load-order packing ---- //
// (curData — the snapshot every tab renders from — lives in state.js.)

// Render every tab from one snapshot `d` (the live snapshot).
// Exported: archive.js's replay controls re-render the whole dashboard through this.
export function renderAll(d) {
  if (!d) return;
  syncSignalSession();   // reset the Signal ID strip the moment the play session changes
  applyTabLayout(effectiveMode(d));   // detected ship / wrecks (or the MODE switch) → tab layout
  renderHeader(d);
  renderSalvage(d);                   // refresh the Salvage panel's auto-detected wreck pills
  mount("datalists", datalistsHtml(d.catalog));
  // Cargo/Plan are lit now, but two transient-interaction guards remain on purpose: EDIT_CELL
  // (the inline cell editor is deep in dynamic lists — only rerenderEdits paints it open, so a
  // poll never reconciles it away), and DRAG_DEST/GRID_HOVER (route drag + hold-highlight apply
  // classes imperatively; re-rendering mid-drag/hover would fight them). All three are state
  // flags, not innerHTML caches — the win is the bridge + EDIT going, not these.
  if (!EDIT_CELL) mount("cargo", cargoView(d));
  if (!EDIT_CELL && DRAG_DEST == null && !GRID_HOVER) mount("plan", planView(d));
  // Contracts is fully lit: no guard — lit reuses the open editor's DOM (typed values + focus
  // survive), so we repaint on every snapshot like everything else.
  mount("contracts", missionsTable(d.missions));
}

const loadOrder = (gs) => [...gs].sort((a, b) => b.routeIdx - a.routeIdx);

// Manual delivery order (S.ROUTE_ORDER, persisted — see state.js) overrides the planner's
// order everywhere (route cards, trip plan, and the load order via deliveryIndex). Unknown
// destinations (new contracts) fall through to the server order until next reordered.
const routeRank = (dest) => {
  const i = S.ROUTE_ORDER ? S.ROUTE_ORDER.indexOf(dest) : -1;
  return i >= 0 ? i : Infinity;
};
// Stable-sort items by the manual order, keeping the server order for ties/unknowns.
function byRouteOrder(arr, destOf) {
  if (!S.ROUTE_ORDER || !arr) return arr || [];
  return arr.map((x, i) => [x, i])
    .sort((a, b) => (routeRank(destOf(a[0])) - routeRank(destOf(b[0]))) || (a[1] - b[1]))
    .map(p => p[0]);
}

// delivery position of a destination from the plotted route (0 = delivered first).
function deliveryIndex(d, dest) {
  if (S.ROUTE_ORDER) {           // user's manual drag order wins when set
    const r = S.ROUTE_ORDER.indexOf(dest);
    if (r >= 0) return r;
  }
  const stops = (d.plan && d.plan.stops) || [];
  const i = stops.findIndex(s => s.station === dest);
  return i < 0 ? 1e8 : i;   // off-route → treat as earliest delivery (loads near the bottom)
}

// Hauling-contract size tier → the largest container SCU that contract uses (a
// hard cap on synthesized box size). The size keyword sits before "Cargo Haul"
// in the title (after the distance word: "… <Direct|Solar|…> <Size> Cargo Haul"):
//   Extra Small → 1   ·   Small → 4   ·   Large → 32
// Medium is RANK-dependent: up to Member rank it's ≤126 SCU in ≤8-SCU boxes; from
// Experienced rank it's 200-600 SCU in ≤16-SCU boxes. The mission's total SCU
// disambiguates cleanly (>126 ⟹ Experienced+ ⟹ 16); a rank word in the title is a
// fallback. Unrecognized size → 32 (effectively uncapped — 32 is the biggest box).
function tierMaxBox(title, totalScu) {
  title = title || "";
  const m = /\b(Extra Small|Small|Medium|Large)\s+(?:Cargo Haul|Shipment)/i.exec(title);
  switch (m ? m[1].toLowerCase() : "") {
    case "extra small": return 1;
    case "small":       return 4;
    case "large":       return 32;
    case "medium":      return (totalScu > 126 || /\b(experienced|senior|expert|master)\b/i.test(title)) ? 16 : 8;
    default:            return 32;
  }
}

// All outstanding cargo, bundled into ONE GROUP PER DESTINATION — each stop becomes its
// own hold band so cargo for different destinations is never stacked together. Boxes
// carry their destination's hue. A group is flagged `shared` when it carries a cargo
// TYPE that also goes to another destination (e.g. Aluminum→A and Aluminum→B): those
// boxes are identical, so load each such stop fully before the next to avoid mixing the
// twins. SCU is synthesized into standard containers capped by each mission's size tier;
// `routeIdx` is the earliest delivery position (loads last / on top).
function cargoGroups(d) {
  const byId = {};
  (d.missions || []).forEach(m => { byId[m.mission_id] = m; });

  // stable hue per destination, in the server's unloading order
  const hueOf = {}; let hi = 0;
  (d.unloading || []).forEach(g => { if (!(g.location in hueOf)) hueOf[g.location] = destHue(hi++); });

  // flatten outstanding drop-off items
  const items = [];
  (d.unloading || []).forEach(g => {
    for (const it of (g.items || [])) {
      if (it.done || !it.qty) continue;            // delivered or unknown-qty → skip
      items.push({ mid: it.mission_id, dest: g.location, cargo: it.cargo || null, qty: it.qty,
                   hue: hueOf[g.location], idx: deliveryIndex(d, g.location) });
    }
  });
  // a Medium contract's box cap (8 vs 16) keys off its TOTAL SCU, so sum per
  // mission before deciding each item's container size.
  const mScu = {};
  for (const it of items) mScu[it.mid] = (mScu[it.mid] || 0) + it.qty;
  for (const it of items) it.maxBox = tierMaxBox((byId[it.mid] || {}).title, mScu[it.mid]);

  // ambiguous = a cargo type bound for ≥2 distinct destinations (identical boxes that
  // must never share an elevator across those stops).
  const destsOf = {};
  for (const it of items) if (it.cargo) (destsOf[it.cargo] || (destsOf[it.cargo] = new Set())).add(it.dest);
  const isAmbiguous = (c) => c && destsOf[c] && destsOf[c].size >= 2;

  const mkBoxes = (it) => synthBoxes(it.qty, it.maxBox).map(b => ({ dims: b.dims, scu: b.scu, hue: it.hue, dest: it.dest }));

  // one bundle per destination (always safe to stage together — a single stop)
  const byDest = {};
  for (const it of items) {
    const g = byDest[it.dest] || (byDest[it.dest] = {
      dest: it.dest, types: new Set(), cargo: [], scu: 0, boxes: [], routeIdx: it.idx, hue: it.hue });
    g.boxes.push(...mkBoxes(it));
    g.scu += it.qty;
    if (it.cargo) { g.types.add(it.cargo); if (!g.cargo.includes(it.cargo)) g.cargo.push(it.cargo); }
    g.routeIdx = Math.min(g.routeIdx, it.idx);
  }

  // one group per destination — each stop gets its own hold band, so cargo for
  // different destinations is never stacked together. `shared` flags a stop carrying a
  // cargo type that also rides another stop (identical-looking boxes): load it fully
  // before the next so the twins don't get mixed up.
  return Object.values(byDest)
    .sort((a, b) => a.routeIdx - b.routeIdx)
    .map(g => ({
      shared: g.cargo.some(isAmbiguous),
      dest: g.dest, cargo: g.cargo.join(", "),
      hue: g.hue, scu: g.scu, boxes: g.boxes, routeIdx: g.routeIdx,
    }));
}

// Bidirectional, non-destructive highlight keyed by DESTINATION: brighten that stop's
// boxes in the hold AND its row in the list, without hiding the rest. dest===null clears.
// Freezes the Plan repaint while active so the SSE/poll doesn't wipe the highlight.
function hlDest(dest) {
  GRID_HOVER = dest != null;
  const wrap = $("holdwrap");
  if (wrap) wrap.querySelectorAll(".cg-box").forEach(b =>
    b.classList.toggle("hl-on", dest != null && b.dataset.dest === dest));
  document.querySelectorAll("#routegrid .card.route").forEach(c =>
    c.classList.toggle("hl-row", dest != null && c.dataset.dest === dest));
}
// Delegated hover on the stop list / the hold — read the dest off the closest row/box.
// On mouseout, only clear when the pointer truly leaves the container (not when moving
// between two children), so highlights don't flicker.
function rowHover(ev) {
  if (ev.type === "mouseout") {
    const g = $("routegrid"); if (g && g.contains(ev.relatedTarget)) return;
    return hlDest(null);
  }
  const row = ev.target.closest(".card.route");
  hlDest(row ? row.dataset.dest : null);
}
function boxHover(ev) {
  if (ev.type === "mouseout") {
    const w = $("holdwrap"); if (w && w.contains(ev.relatedTarget)) return;
    return hlDest(null);
  }
  const box = ev.target.closest(".cg-box");
  hlDest(box ? box.dataset.dest : null);
}

const NEAR = { rear: "rear", front: "front", left: "left", right: "right" };
// Human label for a ship's cargo-hatch access (banded front/rear/side, or all-access).
function accessLabel(access) {
  if (!access.axis) return "all-access · external grid / cargo lift";
  return access.axis === "width"
    ? (access.both ? "side-loading · left + right hatches" : "side-loading · " + NEAR[access.near] + " hatch")
    : (access.both ? "front + rear hatches" : NEAR[access.near] + " hatch");
}
// The 3D hold render wrapped in #holdwrap (the hover-highlight target). Delegated
// mouseover/out drive the bidirectional highlight (box ↔ list row).
function holdHtml(d, packed, access) {
  // cargoGridHtml is the classic IIFE packer (cargogrid.js) — returns an HTML string → unsafeHTML.
  return html`<div id="holdwrap" @mouseover=${boxHover} @mouseout=${boxHover}>${unsafeHTML(
    cargoGridHtml(d.ship_grid, { scale: 22, packed, layout: d.ship_layout, access }))}</div>`;
}

// ---- editor actions ---- //
// current override for a mission, from whatever data is displayed (live S.LAST or the
// replayed snapshot) so archive edits merge onto the overlay's existing override.
const rawOverride = (mid) => {
  const d = curData();
  return ((d && d.missions.find(m => m.mission_id === mid) || {}).raw_override) || {};
};

function editMission(mid) {
  const m = (curData().missions || []).find(x => x.mission_id === mid);
  EDIT = mid;
  EDIT_DROPS = buildLegRows(m, "dropoff");      // seed the lit-managed editor rows from the log
  EDIT_PICKUPS = buildLegRows(m, "pickup");
  renderMissions();
  // Critical fields a sparse mission usually needs filled: origin, plus each leg's
  // cargo / quantity / destination. The EMPTY ones get a low positive tabindex (in
  // visual order) so Tab walks through them FIRST — fill the gaps, then Tab drops into
  // the rest of the form. Also focus the first gap so you can start typing immediately.
  setTimeout(() => {
    const row = document.querySelector("tr.editrow");
    if (!row) return;
    const critical = [...row.querySelectorAll(
      "#ed_origin, #ed_drops .lc, #ed_drops .lq, #ed_drops .ll, #ed_pickups .lc, #ed_pickups .lq, #ed_pickups .ll")];
    let ti = 1;
    const empties = [];
    for (const el of critical) if (!el.value.trim()) { el.tabIndex = ti++; empties.push(el); }
    const target = empties[0] || row.querySelector("input");
    if (target) { target.focus(); if (target.select) target.select(); }
  }, 0);
}
function cancelEdit() { EDIT = null; EDIT_DROPS = []; EDIT_PICKUPS = []; renderMissions(); }
// Editor keybindings: Enter (in a text field) saves, Escape cancels — matching the
// inline cell editor so the whole form is keyboard-dismissable.
function edFormKey(e, mid) {
  if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); saveMission(mid); }
  else if (e.key === "Escape") { e.preventDefault(); cancelEdit(); }
}
function saveMission(mid) {
  // Reward is free-text shorthand (12k / 1.5m / 2,500); reject a non-empty value that doesn't
  // parse rather than silently dropping it (parseReward would return null and lose the edit).
  const reward = ($("ed_reward")?.value || "").trim();
  if (reward && parseReward(reward) == null)
    return alert("Reward isn’t a valid amount — use digits or shorthand like 12k or 1.5m.");
  postOverride(mid, buildOverride());
}
function resetMission(mid) { postOverride(mid, null); }       // clear entirely
function restoreMission(mid) {                                // un-hide, keep edits
  const ov = { ...rawOverride(mid) };
  delete ov.hidden;
  postOverride(mid, Object.keys(ov).length ? ov : null);
}
function deleteMission(mid) {                                 // hide, keep edits (Restore brings it back)
  if (!confirm("Hide this contract from the active lists? You can bring it back from the Archive tab.")) return;
  postOverride(mid, { ...rawOverride(mid), hidden: true });
}

// Parse a reward field: plain digits, or shorthand like "12k", "1.5m", "2,500".
// Returns an integer aUEC, or null if blank/unparseable.
function parseReward(s) {
  s = (s || "").trim().toLowerCase().replace(/,/g, "").replace(/\s+/g, "");
  if (!s) return null;
  const m = s.match(/^([0-9]*\.?[0-9]+)([km])?$/);
  if (!m) return null;
  const mult = m[2] === "m" ? 1e6 : m[2] === "k" ? 1e3 : 1;
  return Math.round(parseFloat(m[1]) * mult);
}

function buildOverride() {
  const g = id => $(id).value;
  const ov = {};
  const title = g("ed_title").trim(); if (title) ov.title = title;
  const origin = g("ed_origin").trim(); if (origin) ov.origin = origin;
  const reward = parseReward(g("ed_reward")); if (reward != null) ov.reward = reward;
  const status = g("ed_status"); if (status) ov.status = status;
  const drops = gatherLegs("ed_drops", "to"); if (drops.length) ov.drops = drops;
  const pickups = gatherLegs("ed_pickups", "from"); if (pickups.length) ov.pickups = pickups;
  return ov;
}

async function postOverride(mid, override) {
  if (S.REPLAY_MODE) return replayEdit({ kind: "override", mission_id: mid, override });
  try { await postJSON("/api/override", { mission_id: mid, override }); }
  catch (e) { alert("Save failed: " + e); }
  EDIT = null;
  refresh();
}



connectStream();
loadShipList();
// Restore the jukebox across reloads: reopen the modal if it was open, and resume saved
// playback on first load even if it wasn't (build skeleton, pull tracks, restore track/pos/state).
// ...but only in the tab that owns the jukebox -- claimJukeboxPrimary fires this once we
// hold the lock (immediately for the first tab; on the primary closing for a waiter).
claimJukeboxPrimary(() => {
  const b = $("navjukebox"); if (b) b.style.display = "";   // reveal: this tab owns playback
  try {
    if (localStorage.getItem("jukeOpen") === "1") openJukebox();
    else if (localStorage.getItem("jukeState") || localStorage.getItem("jukeAutoplay") === "1") initJukebox();
  } catch (_) {}
});

// On a deliberate close, tell the tracker it may stop sooner (it still waits a short grace,
// so a reload -- which also fires pagehide -- reconnects and cancels it). pagehide is the
// reliable unload hook (fires on mobile/bfcache where unload doesn't); sendBeacon survives
// the page going away. Purely an optimization: if it's missed, the normal idle timeout applies.
window.addEventListener("pagehide", () => {
  try { navigator.sendBeacon("/api/closing"); } catch (_) { /* best effort */ }
});

// Keep --header-h / --footer-h synced with the sticky header and footer so the
// Archive's two logs fill exactly the remaining viewport (heights shift as the
// readouts/gauge/footer text update) without making the page itself scroll.
(function trackChrome() {
  const header = document.querySelector("header"), footer = document.querySelector("footer");
  const set = (el, name) => el && document.documentElement.style.setProperty(name, el.offsetHeight + "px");
  const sync = () => { set(header, "--header-h"); set(footer, "--footer-h"); };
  sync();
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(sync);
    if (header) ro.observe(header);
    if (footer) ro.observe(footer);
  }
  window.addEventListener("resize", sync);
})();

// (No window bridge: the whole dashboard is lit-rendered now and every handler binds via lit
// @event, so module-scoped functions never need to be re-exposed on `window`. The old
// Object.assign(window, {…}) bridge and tests/test_window_bridge.py that enforced it are gone.)

// ---- initial route resolution (runs last, once all tab state + functions exist) ---- //
// Map the URL onto the dashboard. Must run after the whole module is initialised —
// activating archive/minerals calls loadSessions()/initMinerals(), which touch state declared
// far below the nav setup. Old #hash bookmarks (the pre-path scheme, plus the pre-Cargo/Plan-
// merge #loading/#unloading/#routes/#grid) are redirected to their new path form first so
// shared links keep working; then the path drives activateTab.
const LEGACY_HASH = { contracts: "/contracts", cargo: "/cargo", plan: "/plan",
                      archive: "/archive", mining: "/minerals",
                      loading: "/cargo#loading", unloading: "/cargo#unloading",
                      routes: "/plan", grid: "/plan" };
const _legacy = LEGACY_HASH[location.hash.slice(1)];
if (location.pathname === "/" && _legacy) history.replaceState(null, "", _legacy);
activateTab(tabFromPath(location.pathname), { push: false });
