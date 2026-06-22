"use strict";

import { $, val, esc, num, setHTML, logTable, th, tag, tabBar, toast } from "./dom.js";
import { postJSON, postRaw, getJSON } from "./net.js";
import {
  initMining, miningSub, miningFind, miningIndex, locChips, locKey,
  bpSort, bpFilterOpen, bpFilterToggle, bpFilterAll, bpFilterSearch, bpRowClick, bpStep, bpQtyInput, bpClearList, bpBuildShip, bpQtyFilter,
} from "./mining.js";
import { registerCombo, comboInputHtml, comboOpen, comboFilter, comboKey, comboBlur, comboPick } from "./combobox.js";
import {
  initSignal, syncSignalSession, signalIdentify, signalAgain, signalPredict, signalKey, signalHull,
} from "./signal.js";
import {
  initSalvage, renderSalvage, salvageToggle, salvagePick,
  salvageDdOpen, salvageDdFilter, salvageDdKey,
} from "./salvage.js";
import { initJukebox, openJukebox, closeJukebox, jukeApplyMusicState, claimJukeboxPrimary } from "./jukebox.js";
import "./settings.js";   // side-effect: renders the Settings overlay + wires its own nav button
import "./shipequip.js";  // side-effect: wires the ship-equipment popup + self-bridges its handlers
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

let EDIT = null;      // mission_id whose editor is open (Contracts tab)
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
const TABS = ["contracts", "signal", "cargo", "plan", "archive", "mining", "salvage"];
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
  if (name === "mining") initMining();
  if (name === "salvage") initSalvage();
  applySub(name, hash.slice(1));                     // restore Loading/Unloading / mining sub
}
// Apply a section's sub-tab from the URL #hash. No (or an unrecognised) hash leaves the
// section's own default in place — for Cargo that's the auto/persisted phase (cargoSubActive),
// so the hash acts as an explicit deep-link override rather than the source of truth.
function applySub(name, sub) {
  if (name === "cargo") {
    if (sub === "loading") cargoSub("pickup");
    else if (sub === "unloading") cargoSub("dropoff");
  } else if (name === "mining") {
    if (["identify", "find", "plan"].includes(sub)) miningSub(sub);
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
  return [["auto", "Auto"], ["cargo", "Cargo"], ["mining", "Mining"], ["salvage", "Salvage"]]
    .map(([k, t]) => {
      const on = MODE_OVERRIDE === k;
      const hint = (k === "auto") ? ` <small>${eff}</small>` : "";
      const title = k === "auto" ? "Follow the detected ship / wrecks" : `Always use ${t} mode`;
      return `<button class="modesw-opt${on ? " active" : ""}" aria-pressed="${on}"
        title="${title}" onclick="setMode('${k}')">${t}${hint}</button>`;
    }).join("");
}

// Each mode shows its own tab set: cargo keeps the hauling tabs; mining and salvage each hide
// Cargo+Plan and slot their own tool tab right after Contracts. Driven from renderAll on every
// snapshot; idempotent via LAYOUT_MODE so it only touches the DOM on an actual mode change.
const HAUL_TABS = ["contracts", "signal", "cargo", "plan", "archive"];
const MINE_TABS = ["contracts", "signal", "mining", "archive"];
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
  // If the active tab just got hidden, fall back to the mode's primary tab (mining→Mining,
  // salvage→Salvage, else Contracts). An automatic correction, not a navigation — replace the
  // URL rather than pushing a history entry the user never asked for.
  if (!order.includes(S.TAB)) activateTab(mode === "cargo" ? "contracts" : mode, { push: false });
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
  return `<span class="pill ${online ? "online" : "offline"}"><span class="dot"></span>${online ? "In Verse" : "Main Menu"}</span>`;
}

function statusHtml(d) {
  // Generic ship-equipment popup trigger. The popup is equipment-agnostic; today the only
  // equipment category is mining gear, so the button shows only for a mining ship — extend the
  // gate (and the popup body) when other equipment types arrive.
  const equip = d.mining_ship
    ? ` <button id="shipEquipBtn" class="ship-equip-btn" title="Ship equipment" aria-haspopup="dialog" onclick="openShipEquip()">⚙</button>`
    : "";
  if (d.boarded) {
    // crewing another player's ship — show it badged as boarded, not "detected"
    const who = d.boarded_owner ? `${esc(d.boarded_owner)}'s ship` : "another ship";
    return `<span class="ship">SHIP <b>${esc(d.ship || "—")}</b>${equip}
      <span class="ship-auto" title="you're aboard ${who} as crew — the manifest shows the shared haul in this hold">⚑ aboard ${who}</span></span>`;
  }
  if (d.ship_detected) {
    // detected ship overrides the manual pick — show it locked, no searchable box
    return `<span class="ship">SHIP <b>${esc(d.ship || "—")}</b>${equip}
      <span class="ship-auto" title="detected from the game log">● detected</span></span>`;
  }
  _registerShipCombo();
  const box = comboInputHtml("shipSel", { value: d.ship || "", placeholder: "search ship…", label: "Ship" });
  return `<span class="ship">SHIP ${box}${equip}</span>`;
}

function readoutsHtml(d, mining) {
  const stat = (k, v, cls) => `<div class="stat ${cls || ""}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  if (mining) {
    // No hauling-contract metrics here — they don't apply to a mining run. We have no
    // live mining telemetry from the log, so surface the relevant context the snapshot
    // does carry: vehicle, its refined-ore hold (if any), where you are, and who.
    const items = [["Ship", esc(d.ship || "—"), d.ship ? "accent" : ""]];
    if (d.ship_scu) items.push(["Ore Hold", `${num(d.ship_scu)} <small>SCU</small>`, ""]);
    items.push(["Location", esc(d.location || "—"), d.location ? "accent" : ""]);
    items.push(["Player", esc(d.player || "—"), ""]);
    return items.map(([k, v, cls]) => stat(k, v, cls)).join("");
  }
  const c = d.counts;
  const items = [
    ["Active", c.partial ? `${c.active} <span class="sub">${c.partial}⚠</span>` : c.active, "accent"],
    ["Completed", c.completed, "good"],
    ["Abandoned", c.abandoned, c.abandoned ? "bad" : ""],
    ["Failed", c.failed, c.failed ? "bad" : ""],
    ["To Deliver", `${num(d.active_scu)} <small>SCU</small>`, ""],
    ["Earned", `${num(d.total_awarded)} <small>aUEC</small>`, ""],
    ["Location", esc(d.location || "—"), d.location ? "accent" : ""],
    ["Player", esc(d.player || "—"), ""],
  ];
  if (c.hidden) items.push(["Hidden", c.hidden, ""]);
  return items.map(([k, v, cls]) => stat(k, v, cls)).join("");
}

function gaugeHtml(d, mining) {
  if (mining) {
    // The "Cargo Load" gauge tracks hauling cargo, which is meaningless here; the mining
    // vehicle's refined-ore hold has no fill we can read from the log, so show capacity
    // only (or nothing, for hopper-only craft like the Prospector/ROC at scu 0).
    if (!d.ship_scu) return "";
    return `<span class="lbl">Ore Hold</span>
      <span class="remain"><b>${num(d.ship_scu)}</b> SCU capacity</span>`;
  }
  // peak simultaneous load (back-haul aware) is what must fit at once; fall back
  // to total outstanding if the server didn't send it.
  const load = (d.peak_scu != null ? d.peak_scu : d.active_scu) || 0, cap = d.ship_scu;
  if (!cap) {
    return `<span class="lbl">Cargo Load</span>
      <span class="remain"><b>${num(load)}</b> SCU outstanding${d.ship ? "" : " · awaiting ship"}</span>
      <div class="track"><div class="fill" style="width:0"></div></div>`;
  }
  const pct = Math.round(load / cap * 100);
  const over = load > cap;
  const remain = over
    ? `<b class="over">${num(load - cap)}</b> SCU over`
    : `<b>${num(cap - load)}</b> SCU free`;
  return `<span class="lbl">Cargo Load</span>
    <span class="remain">${remain}</span>
    <div class="track"><div class="fill ${over ? "over" : ""}" style="width:${Math.min(100, pct)}%"></div><span class="pct">${num(load)} / ${num(cap)} SCU</span></div>`;
}

function renderHeader(d) {
  const mining = effectiveMining(d);
  // don't repaint the status bar while the ship search box is focused or its popup
  // is open — a poll landing mid-interaction would tear it down.
  const busy = !!(document.activeElement && document.activeElement.id === "shipSel");
  setHTML("connpill", connPillHtml(d));   // left-aligned connection indicator; always safe to repaint
  if (!busy) setHTML("status", statusHtml(d));
  setHTML("modeswitch", modeSwitchHtml(d));
  setHTML("stats", readoutsHtml(d, mining));
  const gauge = gaugeHtml(d, mining);
  setHTML("capacity", gauge);
  const cap = $("capacity"); if (cap) cap.classList.toggle("hide", !gauge);  // collapse when empty
}

// ---- autocomplete catalog (cargo + station names) ---- //
function datalistsHtml(cat) {
  if (!cat) return "";
  const opts = (a) => (a || []).map(v => `<option value="${esc(v)}"></option>`).join("");
  // Tag each cargo option with its commodity category (Metal, Gas, …) from the p4k
  // taxonomy (T1) — shown by the autocomplete where the browser supports option labels.
  const types = cat.cargo_types || {};
  const cargoOpts = (cat.cargo || []).map(v =>
    `<option value="${esc(v)}"${types[v] ? ` label="${esc(types[v])}"` : ""}></option>`).join("");
  return `<datalist id="dl_cargo">${cargoOpts}</datalist>` +
         `<datalist id="dl_station">${opts(cat.stations)}</datalist>`;
}

// ---- standby / empty states ---- //
// sub may contain trusted markup (e.g. <b>); title/code are escaped.
function standby(title, sub, code) {
  return `<div class="standby">
    <div class="reticle"><span class="tick h"></span><span class="tick v"></span><span class="core"></span></div>
    <div class="st-title">${esc(title)}</div>
    <div class="st-sub">${sub}</div>
    <div class="st-code">${esc(code)}</div>
  </div>`;
}

// ---- loading / unloading / routes ---- //
const QTY = (q) => q == null ? `<span class="warn" title="quantity not logged by the game">? SCU</span>` : (num(q) + " SCU");
const SCU = (n, partial) => num(n) + (partial ? "+" : "") + " SCU";
const partialNote = (d) => d.counts.partial
  ? `<div class="note">⚠ ${d.counts.partial} active mission(s) are missing cargo/quantity data — Star Citizen didn't log the delivery objectives (common when several missions are accepted quickly). Cargo <b>type</b> is recovered from the contract; quantities show <b>?</b>. Use <b>Edit</b> on the Contracts tab to fill them in.</div>`
  : "";

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
const stationText = (s) => UNKNOWN_STATION(s) ? `<span class="unk">${esc(s || "unknown")}</span>` : esc(s);
const cellTok = (f) => [f.k, f.zone || "", f.mid || "", f.oid || ""].join("|");
const editPlaceholder = (k) => k === "qty" ? "SCU" : k === "cargo" ? "commodity" : "station name";
const editList = (k) => k === "cargo" ? "dl_cargo" : (k === "station" || k === "origin") ? "dl_station" : "";

function editable(value, f, opts) {
  opts = opts || {};
  const tok = cellTok(f);
  const known = value != null && value !== "" && !opts.unknown;
  if (EDIT_CELL === tok) {
    const isnum = f.k === "qty";
    const list = editList(f.k);
    const cur = (opts.unknown || value == null) ? "" : value;
    return `<span class="edc editing"><input id="edit_input" class="edc-in"
      data-field='${esc(JSON.stringify(f))}'
      ${isnum ? 'type="number" min="0" step="1" inputmode="numeric"' : (list ? `list="${list}"` : "")}
      value="${esc(cur)}" placeholder="${esc(opts.ph || editPlaceholder(f.k))}"
      onkeydown="edKey(event)" onblur="edCommit(this)"></span>`;
  }
  const inner = known ? esc(value)
    : `<span class="edc-unkn">${esc(opts.label || (value != null && value !== "" ? value : editPlaceholder(f.k)))}</span>`;
  // role=button + tabindex + Enter/Space (edOpenKey) make the cell operable by keyboard and
  // announced by a screen reader, not mouse-only. aria-label says which field it edits.
  const noun = f.k === "qty" ? "quantity" : f.k === "cargo" ? "commodity"
    : f.k === "origin" ? "origin" : "station name";
  return `<span class="edc${known ? "" : " is-unknown"}" data-field='${esc(JSON.stringify(f))}'
    role="button" tabindex="0" aria-label="${known ? "Edit" : "Set"} ${noun}"
    title="${known ? "Click to correct" : "Click to set"}" onclick="edOpen(this)" onkeydown="edOpenKey(event)">${inner}<span class="edc-pen">✎</span></span>`;
}
// convenience wrappers used across the screens; render plain text when not editable
function stationCell(name, zone) {
  return zone ? editable(name, { k: "station", zone }, { unknown: UNKNOWN_STATION(name) }) : esc(name);
}
function cargoCell(cargo, mid, oid) {
  return (mid && oid) ? editable(cargo, { k: "cargo", mid, oid }, { unknown: !cargo || cargo === "Unknown cargo" })
                      : esc(cargo || "Unknown cargo");
}
function qtyCell(qty, mid, oid) {
  return (mid && oid) ? editable(qty, { k: "qty", mid, oid }, { unknown: qty == null, label: "?" })
                      : QTY(qty);
}

function edOpen(el) {
  let f; try { f = JSON.parse(el.dataset.field); } catch (e) { return; }
  EDIT_CELL = cellTok(f);
  rerenderEdits();
  const i = $("edit_input"); if (i) { i.focus(); if (i.select) i.select(); }
}
function edCancel() { EDIT_CELL = null; rerenderEdits(); }
// Open the in-place editor from the keyboard (Enter/Space) when the cell itself is focused.
function edOpenKey(e) {
  if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") { e.preventDefault(); edOpen(e.currentTarget); }
}
function edKey(e) {
  if (e.key === "Enter") { e.preventDefault(); edCommit(e.target); }
  else if (e.key === "Escape") { e.preventDefault(); edCancel(); }
}
let EDIT_BUSY = false;
async function edCommit(el) {
  if (!el || EDIT_BUSY) return;
  let f; try { f = JSON.parse(el.dataset.field); } catch (e) { return; }
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
  setHTML("cargo", cargoView(d));
  setHTML("plan", planView(d));
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
  const d = curData(); if (d) setHTML("cargo", cargoView(d));
}
function cargoView(d) {
  const sub = cargoSubActive(d);
  const body = sub === "dropoff"
    ? groupCards(d.unloading, "unloading", d)
    : groupCards(d.loading, "loading", d);
  return tabBar([["pickup", "Loading"], ["dropoff", "Unloading"]], sub, "cargoSub") + body;
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
  return partialNote(d) + `<div class="grid">` + groups.map(g => {
    const rows = g.items.map(it => {
      const tail = kind === "loading"
        ? `<div class="sub">→ ${esc(it.to)} · ${esc(it.mission)}</div>`
        : `<div class="sub">← ${esc(it.from)} · ${esc(it.mission)}</div>`;
      // unloading rows carry a leg oid → can be ticked off as delivered
      const check = (kind === "unloading" && it.oid)
        ? legCheck(it.mission_id, it.oid, it.done) : "";
      return `<div class="row ${it.done ? "done" : ""} ${it.partial ? "partial" : ""}">
        ${check}<div class="rowmain"><span class="cargo">${cargoCell(it.cargo, it.mission_id, it.oid)}</span>${tail}</div>
        <div class="qty">${qtyCell(it.qty, it.mission_id, it.oid)}</div></div>`;
    }).join("");
    const warn = g.has_partial ? ' <span class="warn" title="some quantities not logged">⚠</span>' : "";
    return `<div class="card"><h3><span>${stationCell(g.location, g.zone)}${warn}</span>
        <span class="scu">${SCU(g.total_scu, g.has_partial)}</span></h3>${rows}</div>`;
  }).join("") + `</div>`;
}

// A delivered-toggle for one leg. `legsJson` (optional) lets one control tick a
// whole set of legs at once (used by route cargo chips).
function legCheck(mid, oid, done, legsJson) {
  const arg = legsJson ? `'${legsJson}'` : `[{mission_id:'${esc(mid)}',oid:'${esc(oid)}'}]`;
  return `<button class="legchk ${done ? "on" : ""}" title="${done ? "Mark not delivered" : "Mark delivered"}"
    onclick='markDelivered(${arg}, ${done ? "false" : "true"})'>${done ? "✓" : ""}</button>`;
}

async function markDelivered(legs, done) {
  if (typeof legs === "string") legs = JSON.parse(legs);
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
function bodyLabel(s) {
  if (s.body === "?") return "Unknown location";
  const sys = s.system && s.system !== "?" && s.system !== s.body ? `${esc(s.system)} · ` : "";
  const moon = s.moon ? ` › ${esc(s.moon)}` : "";
  return sys + esc(s.body) + moon;
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
  return `<span class="cargochip">${legCheck(it.mission_id, it.oid, false)}<span class="cc-name">${cargoCell(it.cargo, it.mission_id, it.oid)}</span> <span class="cc-qty">${qtyCell(it.qty, it.mission_id, it.oid)}</span>${brk ? ` <span class="cc-box sub">${brk}</span>` : ""}</span>`;
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
  const shipBit = hasGrid ? ` · ${esc(d.ship)} ${num(totalScu)}/${num(cap)} SCU`
    : (d.ship ? ` · ${esc(d.ship)}` : "");
  const title = empty ? `Trip Plan${shipBit}` : `Trip Plan · ${stops} stop(s) · ${jumps} jump(s)${shipBit}`;
  const sub = empty
    ? "no cargo staged — accept hauling contracts and your route &amp; load plan appear here"
    : (hasGrid ? `${accessLabel(access)} · ${strategyCopy(packed)}${free}`
               : "drag a stop to set your visit &amp; load order");
  const reset = S.ROUTE_ORDER
    ? `<button class="route-reset" title="Forget the manual order; revert to the planner's fewest-jump order" onclick="resetRouteOrder()">↺ auto order</button>` : "";
  return `<header class="plan-head"><span class="arch-title">${title}</span>
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
  const hold = hasGrid ? holdHtml(d, shipPacked, access) : "";

  // stable destination hue (matches the hold's box hues) + 1-based physical load position
  // (banded ships load deepest/last-delivered first, so the badge counts from the hatch).
  const hueOf = {}; let hi = 0;
  (d.unloading || []).forEach(g => { if (!(g.location in hueOf)) hueOf[g.location] = destHue(hi++); });
  const loadPos = {}; const gByDest = {};
  groups.forEach(g => { gByDest[g.dest] = g; });
  ((hasGrid && banded) ? [...order].reverse() : order).forEach((g, i) => { loadPos[g.dest] = i + 1; });
  const dualEnd = !!(shipPacked && shipPacked.strategy === "dualend");

  if (!hasStops)
    return `<div class="planwrap">${planHead(d, 0, 0, hasGrid, access, shipPacked, cap, placed, totalScu, true)}
      <div class="sub" style="margin:10px 2px 14px">No cargo staged yet — accept hauling contracts and your route &amp; load plan appear here.</div>
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
      ? `<li class="plan-leg"><span class="plan-jump">${++n}</span>${bodyLabel(s)}</li>` : "";
    lastKey = key;
    const hue = hueOf[s.station] != null ? hueOf[s.station] : destHue(0);
    const pos = loadPos[s.station];
    const run = runByDest[s.station] || { origins: [], missions: 0, partial: false };
    const grp = gByDest[s.station];
    const sharedTag = grp && grp.shared
      ? ' <span class="ls-alone" title="carries a cargo type split across stops — load this stop fully before the next, so the identical boxes don\'t get mixed up">⚠ shared</span>' : "";
    const from = run.origins.length
      ? `<div class="ps-from sub">from ${run.origins.map(esc).join(", ")}${run.missions ? " · " + run.missions + " mission(s)" : ""}</div>` : "";
    const chips = (s.items || []).map(it => cargoChip(it, maxBoxOf(it.mission_id))).join("");
    // drag handle only, so clicking the station/cargo cells to edit never starts a drag
    return header + `<li class="card plan-stop route" data-dest="${esc(s.station)}"
        ondragover="routeDragOver(event)" ondragleave="routeDragLeave(event)"
        ondrop="routeDrop(event)" ondragend="routeDragEnd(event)">
      <h3><span class="ends"><button type="button" class="route-grip" draggable="true"
          title="Drag, or focus and use ↑/↓, to reorder this stop" aria-label="Reorder this stop — use arrow up or down"
          ondragstart="routeDragStart(event)" onkeydown="routeGripKey(event)">⠿</button>${hasGrid ? `<span class="ps-sw" style="background:hsl(${hue},64%,52%)"></span>` : ""}${(hasGrid && pos) ? `<span class="ps-pos" title="load #${pos}${banded ? (dualEnd ? " — loaded from both ends inward" : " — loaded deepest-first") : ""}">${pos}</span>` : ""}${stationCell(s.station, s.zone)}${run.partial ? ' <span class="warn">⚠</span>' : ""}${sharedTag}</span>
        <span class="scu">${SCU(s.scu, run.partial)}</span></h3>
      ${from}<div class="ps-cargo">${chips}</div></li>`;
  }).join("");

  const overScu = shipPacked ? shipPacked.overflow.reduce((a, b) => a + b.scu, 0) : 0;
  const over = overScu
    ? `<div class="note">⚠ ${num(overScu)} SCU won't fit this ${num(cap)} SCU hold — you'll need another run.</div>` : "";
  const ambig = (hasGrid && order.some(g => g.shared))
    ? `<div class="note">⚠ A cargo type is bound for more than one destination — its boxes look identical. Load each stop marked <b>⚠ shared</b> <b>fully</b> before the next, so the twins don't get mixed up.</div>` : "";

  const load = planSorted.load || {};
  const loadItems = (load.items || [])
    .map(it => `<span class="chip">${esc(it.cargo)}${it.qty ? " " + num(it.qty) : ""}</span>`).join("");
  const loadCard = `<div class="plan-load">
    <div class="plan-step">LOAD</div>
    <div class="plan-body"><div class="plan-station">${esc(load.station || "—")}</div>
      <div class="plan-chips">${loadItems || '<span class="sub">no cargo outstanding</span>'}</div></div>
    <div class="scu">${num(planSorted.scu_total || 0)} SCU</div></div>`;

  return `<div class="planwrap">${planHead(d, planSorted.stops.length, n, hasGrid, access, shipPacked, cap, placed, totalScu, false)}${over}${ambig}
    ${loadCard}
    <ol class="plan-stops" id="routegrid" onmouseover="rowHover(event)" onmouseout="rowHover(event)">${stopRows}</ol>
    ${hold}</div>` + partialNote(d);
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
function legRowHtml(leg, guessCargo) {
  leg = leg || {};
  // pre-fill best guesses: cargo from the contract decode, station from the
  // server-resolved zone name (leg.name), so editing a sparse mission isn't blank.
  const cargo = leg.cargo || guessCargo || "";
  const loc = leg.location || leg.name || "";
  const hint = (!loc && leg.zone_host_id) ? `Unknown station (zone ${leg.zone_host_id})` : "station";
  return `<tr>
    <td><input class="lc" list="dl_cargo" aria-label="Cargo" placeholder="cargo" value="${esc(cargo)}"></td>
    <td><span class="numf"><span class="numf-u">SCU</span><input class="lq" type="number" min="0" step="1" inputmode="numeric" aria-label="Quantity in SCU" placeholder="?" value="${leg.qty == null ? "" : leg.qty}"></span></td>
    <td><input class="ll" list="dl_station" aria-label="Location" placeholder="${esc(hint)}" value="${esc(loc)}"></td>
    <td><button type="button" class="rm" title="remove row" aria-label="Remove row" onclick="this.closest('tr').remove()">✕</button></td>
  </tr>`;
}

function legTable(legs, kind, bodyId, locLabel, mission) {
  let rows = Object.values(legs || {}).filter(l => l.kind === kind);
  // A normal haul's pickup is just a zone marker (no cargo) — it isn't a real
  // collect pickup and shouldn't show as a blank row. Only surface pickups that
  // carry cargo (genuine "Collect N SCU of X from Y" objectives); use "+ Add
  // pickup" to turn a haul into a collect mission. Drop-offs always show.
  if (kind === "pickup") rows = rows.filter(l => l.cargo);
  // guess cargo for drop-offs: a single contract cargo applies to every drop;
  // an exact count match assigns them in order. Pickups stay blank.
  const types = (mission && mission.cargo_types) || [];
  const guessFor = (kind === "dropoff")
    ? (i) => types.length === 1 ? types[0] : (types.length === rows.length ? types[i] : "")
    : () => "";
  const body = rows.map((l, i) => legRowHtml(l, guessFor(i))).join("");
  return `<table class="legtable"><thead><tr><th>Cargo</th><th>Qty</th><th>${locLabel}</th><th></th></tr></thead>
      <tbody id="${bodyId}">${body}</tbody></table>
      <button type="button" class="addrow" onclick="addLeg('${bodyId}')">+ Add ${locLabel === "From" ? "pickup" : "drop-off"}</button>`;
}

function addLeg(bodyId) { $(bodyId).insertAdjacentHTML("beforeend", legRowHtml({})); }

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
  // a delivered-toggle when this row maps to a single leg (mid + oid given)
  const chk = (opts.mid && opts.oid) ? legCheck(opts.mid, opts.oid, opts.done) : "";
  return `<div class="legrow ${opts.done ? "legdone" : ""}">
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
      const qty = l.qty == null ? '<span class="warn">?</span> SCU' : `${num(l.qty)} SCU`;
      return legRow(esc(l.cargo), qty, esc(l.location || "?"),
        { done: l.state === "completed", mid: m.mission_id, oid: l.objective_id });
    }).join("");
    return `<div class="manilegs">${rows}</div>`;
  }
  if (drops.length) {
    const ct = (m.cargo_types || []).join(", ") || "Unknown cargo";
    const dests = (m.destinations || []).length
      ? m.destinations.map(stationText).join(", ") : '<span class="unk">?</span>';
    const row = legRow(esc(ct), '<span class="warn">?</span> SCU',
      `${dests} <span class="sub">· qty not logged</span>`, { warnCargo: true });
    return `<div class="manilegs">${row}</div>`;
  }
  return '<span class="sub">—</span>';
}

// A MINING contract's "ore × qty → where to mine" rows (Shubin purchase orders carry ore
// requirements instead of pickup/dropoff legs). Each ore lists where it's mined via the
// same `locChips` the mining tab uses, capped server-side with a "+N more" tail.
function oreRow(o) {
  const done = o.need > 0 && o.have >= o.need;
  const hidden = (o.loc_count || 0) - (o.locations || []).length;
  const more = hidden > 0 ? `<span class="lt-tag mloc-more">+${hidden} more</span>` : "";
  const where = (o.locations && o.locations.length)
    ? locChips(o.locations, more)
    : '<div class="mloc"><span class="sub">where-to-mine unknown</span></div>';
  return `<div class="ore-row ${done ? "legdone" : ""}">
    <span class="ml-cargo">${esc(o.ore)}</span><span class="ml-qty">×${num(o.need)}</span>
    <span class="ore-where">${where}</span>
  </div>`;
}

function miningLegs(m) {
  const ores = m.ores || [];
  if (!ores.length) return '<span class="sub">—</span>';
  const head = `<div class="ore-head">collect ${m.ore_any ? "any <b>one</b> of" : "all of"}</div>`;
  return `${head}<div class="orelegs">${ores.map(oreRow).join("")}</div>${locKey()}`;
}

function editorRow(m) {
  const opt = (v, l, sel) => `<option value="${v}"${sel ? " selected" : ""}>${l}</option>`;
  const statuses = ["active", "completed", "abandoned", "failed", "expired"];
  // an unresolved origin (Unknown station / Origin pending) is a placeholder, not real
  // content: show it as the input placeholder and leave the field empty so typing overwrites.
  const unknownOrigin = UNKNOWN_STATION(m.origin);
  return `<tr class="editrow"><td colspan="6"><div class="editor"
    onkeydown="edFormKey(event,'${m.mission_id}')">
    <div class="ef"><label for="ed_title">Title</label><input id="ed_title" value="${esc(m.title || "")}"></div>
    <div class="ef"><label for="ed_origin">Origin</label><input id="ed_origin" list="dl_station" value="${esc(unknownOrigin ? "" : m.origin)}" placeholder="${esc(unknownOrigin ? (m.origin || "origin") : "origin")}"></div>
    <div class="ef"><label for="ed_reward">Reward <span class="sub">(aUEC · type 12k or 1.5m)</span></label><input id="ed_reward" type="text" inputmode="decimal" value="${m.reward || ""}"></div>
    <div class="ef"><label for="ed_status">Status</label><select id="ed_status">
        ${opt("", "(from log)", true)}${statuses.map(s => opt(s, s, false)).join("")}</select></div>
    <div class="ef wide"><label>Drop-offs</label>${legTable(m.legs, "dropoff", "ed_drops", "Destination", m)}</div>
    <div class="ef wide"><label>Pickups <span class="sub">(collect missions only — leave empty for normal hauls)</span></label>${legTable(m.legs, "pickup", "ed_pickups", "From", m)}</div>
    <div class="ef btns">
      <button class="primary" onclick="saveMission('${m.mission_id}')">Save</button>
      <button onclick="cancelEdit()">Cancel</button>
      <button onclick="resetMission('${m.mission_id}')">Reset to log</button>
    </div>
  </div></td></tr>`;
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
  const rows = ms.map(m => {
    const dec = m.decoded || {};
    // dec.type/icon and dec.legal are authoritative ContractTemplate data (p4k) layered in
    // by model.Mission.decoded; structure/category/grade come from the contract-id heuristic
    // (grade/SCU are runtime). The type mark leads the chip row.
    const typeChip = dec.type ? typeMark(dec.type, dec.icon) : "";
    const tags = typeChip + [dec.structure, dec.category, dec.grade].filter(Boolean)
      .map(t => `<span class="chip">${esc(t)}</span>`).join("")
      + (dec.legal === false ? `<span class="chip chip-illegal" title="Illegal contract">⚠ Illegal</span>` : "");
    const note = m.hidden ? '<div class="sub">hidden</div>'
      : (m.partial && m.status === "active" ? '<div class="warn" style="font-size:11px">⚠ partial</div>' : "");
    const action = m.hidden
      ? `<button onclick="restoreMission('${m.mission_id}')">Restore</button>`
      : `<div class="rowact"><button onclick="editMission('${m.mission_id}')">Edit</button>` +
        `<button class="danger" onclick="deleteMission('${m.mission_id}')">Delete</button></div>`;
    const edited = m.overridden && !m.hidden ? ' <span class="chip" title="has manual edits">✎</span>' : "";
    const tr = `<tr class="${m.hidden ? "hiddenrow" : ""}">
      <td><span class="badge b-${m.status}">${esc(m.status)}</span>${note}</td>
      <td>${esc(m.title || m.contract)}${edited}<div class="sub">${esc(m.org)}</div>${tags}</td>
      <td>${(m.ores && m.ores.length && m.mining_goto)
        ? `<span class="ore-goto" title="Go to (mission marker)">▸ ${esc(m.mining_goto)}</span>`
        : stationText(m.origin)}</td>
      <td>${(m.ores && m.ores.length) ? miningLegs(m) : missionLegs(m)}</td>
      <td>${m.reward ? num(m.reward) + " aUEC" : '<span class="sub">—</span>'}</td>
      <td>${action}</td>
    </tr>`;
    return tr + (EDIT === m.mission_id && !m.hidden ? editorRow(m) : "");
  }).join("");
  return `<div class="tscroll"><table><thead><tr><th>Status</th><th>Mission</th><th>Origin</th><th>Cargo → Destination</th><th>Reward</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderMissions() { const d = curData(); if (d) setHTML("contracts", missionsTable(d.missions)); }

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
  setHTML("datalists", datalistsHtml(d.catalog));
  // EDIT_CELL guards every cargo-ops screen so an open inline editor isn't clobbered by
  // the 3s poll; DRAG_DEST guards a route drag; GRID_HOVER guards the hold highlight. Plan
  // renders only its active sub, so combining all three keeps either sub stable mid-interaction.
  if (!EDIT_CELL) setHTML("cargo", cargoView(d));
  if (!EDIT_CELL && DRAG_DEST == null && !GRID_HOVER) setHTML("plan", planView(d));
  if (EDIT === null) setHTML("contracts", missionsTable(d.missions));  // don't clobber an open editor
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
  return `<div id="holdwrap" onmouseover="boxHover(event)" onmouseout="boxHover(event)">`
    + cargoGridHtml(d.ship_grid, { scale: 22, packed, layout: d.ship_layout, access }) + `</div>`;
}

// ---- editor actions ---- //
// current override for a mission, from whatever data is displayed (live S.LAST or the
// replayed snapshot) so archive edits merge onto the overlay's existing override.
const rawOverride = (mid) => {
  const d = curData();
  return ((d && d.missions.find(m => m.mission_id === mid) || {}).raw_override) || {};
};

function editMission(mid) {
  EDIT = mid; renderMissions();
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
function cancelEdit() { EDIT = null; renderMissions(); }
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

// ---- window bridge ---- //
// Inline HTML handlers (onclick="editMission(…)", the interpolated onclick="${fn}(…)" in
// tabBar, etc.) resolve names against `window`. Under <script type="module"> top-level
// declarations are module-scoped, NOT global — so every function reachable from an inline
// handler must be re-exposed here explicitly. tests/test_window_bridge.py statically enforces
// that this block covers every handler-referenced name (fails the build on drift). Other
// modules bridge their own handlers the same way: archive.js (archive/replay + the contract-
// log type filter), stream.js (the update banner), mining.js, settings.js, jukebox.js.
Object.assign(window, {
  // contracts / mission editor
  editMission, saveMission, cancelEdit, deleteMission, resetMission, restoreMission, addLeg, edFormKey,
  // unified inline cell editor
  edOpen, edOpenKey, edKey, edCommit,
  // header / ship selector
  setMode, comboOpen, comboFilter, comboKey, comboBlur, comboPick,
  // cargo / plan tabs + route reorder
  cargoSub, resetRouteOrder, rowHover, boxHover, markDelivered,
  routeDragStart, routeDragOver, routeDragLeave, routeDrop, routeDragEnd, routeGripKey,
  // mining (Find / Plan)
  miningSub, miningFind, miningIndex,
  bpSort, bpFilterOpen, bpFilterToggle, bpFilterAll, bpFilterSearch, bpRowClick, bpStep, bpQtyInput, bpClearList, bpBuildShip, bpQtyFilter,
  // signal id (RS reading → rock / wreck)
  signalIdentify, signalAgain, signalPredict, signalKey, signalHull,
  // salvage (Ship-ID panel)
  salvageToggle, salvagePick, salvageDdOpen, salvageDdFilter, salvageDdKey,
});

// ---- initial route resolution (runs last, once all tab state + functions exist) ---- //
// Map the URL onto the dashboard. Must run after the whole module is initialised —
// activating archive/mining calls loadSessions()/initMining(), which touch state declared
// far below the nav setup. Old #hash bookmarks (the pre-path scheme, plus the pre-Cargo/Plan-
// merge #loading/#unloading/#routes/#grid) are redirected to their new path form first so
// shared links keep working; then the path drives activateTab.
const LEGACY_HASH = { contracts: "/contracts", cargo: "/cargo", plan: "/plan",
                      archive: "/archive", mining: "/mining",
                      loading: "/cargo#loading", unloading: "/cargo#unloading",
                      routes: "/plan", grid: "/plan" };
const _legacy = LEGACY_HASH[location.hash.slice(1)];
if (location.pathname === "/" && _legacy) history.replaceState(null, "", _legacy);
activateTab(tabFromPath(location.pathname), { push: false });
