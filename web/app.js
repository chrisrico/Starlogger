"use strict";

const $ = (id) => document.getElementById(id);
// Defensive read of an input's value by id ("" when the element isn't in the DOM yet).
const val = (id) => ($(id) || {}).value || "";
const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (n) => (n == null ? "" : Number(n).toLocaleString());

// Only touch the DOM when the rendered HTML actually changes. Kills the 3s poll
// flicker and means entrance animations replay only on real updates.
const _rendered = {};
const _seen = {};
function setHTML(id, html) {
  if (_rendered[id] === html) return;
  _rendered[id] = html;
  const el = $(id);
  el.classList.toggle("noanim", !!_seen[id]);  // entrance animation on first paint only
  el.innerHTML = html;
  _seen[id] = true;
}

let TAB = "contracts";   // Contracts is the first/default tab
let LAST = null;      // latest snapshot
let _lastRenderSig = null;  // serialized snapshot last rendered by the poll (skip identical re-renders)
let EDIT = null;      // mission_id whose editor is open (Contracts tab)
let EDIT_CELL = null; // token of the open inline editor (unified, one at a time)
let ASSET_VER = null; // frontend asset hash from the SSE `meta` frame; reload if it changes
let SESSIONS = null;  // archived sessions

// ---- session replay ---- //
// When a session is replayed, the WHOLE dashboard renders a reconstructed past
// snapshot instead of live data: curData() returns REPLAY_SNAPSHOT and the poll pauses.
// REPLAY_POINTS is the scrub timeline (index/ts/label); REPLAY_I the current checkpoint.
// Archive editing is fully interactive but EPHEMERAL: every edit goes to an in-memory
// overlay (REPLAY_EDITS) via /api/replay/edit — which recomputes the snapshot exactly
// like live but writes nothing to disk. null until the first edit (disk state shown).
let REPLAY_MODE = false, REPLAY_KEY = null, REPLAY_POINTS = [], REPLAY_I = 0, REPLAY_SNAPSHOT = null;
let REPLAY_EDITS = null, REPLAY_SAVED_ORDER = null;
let REPLAY_UNAVAILABLE = new Set();  // session keys whose source log is gone
let _scrubTimer = null;

// Which Archive section is expanded (accordion — only one at a time). Empty = all
// collapsed. Persists the user's explicit choice; no built-in default (see archDefaultSection).
let ARCH_OPEN = localStorage.getItem("archOpen") || "";
function toggleArch(key) {
  if (ARCH_OPEN === key) return;   // the open section stays open — only selecting another switches
  ARCH_OPEN = key;
  localStorage.setItem("archOpen", ARCH_OPEN);
  _archRepaint();
}
// When the Archive opens with NOTHING expanded, auto-open whichever of the Contract Log /
// Trade Loads reflects the most recent activity. If a section is already open (a previous
// selection), it's left as-is. Compared on the same data each view shows.
let ARCH_PICK = false;
function archDefaultSection() {
  let cT = "", tT = "";
  for (const s of SESSIONS || []) {
    for (const m of s.missions || []) {
      const t = m.ended_at || m.accepted_at || s.started_at || "";
      if (t > cT) cT = t;
    }
    for (const t of s.trades || []) if ((t.ts || "") > tT) tT = t.ts || "";
  }
  for (const t of (LAST && LAST.trades) || []) if ((t.ts || "") > tT) tT = t.ts || "";
  return tT > cT ? "trades" : "contracts";
}
// How the Trade Routes recommendations are ranked: total aUEC, % return, or aUEC/SCU.
let ROUTE_SORT = localStorage.getItem("routeSort") || "profit";
function setRouteSort(key) {
  ROUTE_SORT = key;
  localStorage.setItem("routeSort", key);
  _archRepaint();
}
// One Archive section as a tab descriptor; sessionsView() renders the tab bar and the
// selected section's body (only the active body is built into the DOM).
function logSection(key, title, headSpan, body) {
  return { key, title, headSpan: headSpan || "", body };
}

// ---- small render helpers (DRY the repeated archive markup) ---- //
// A scrolling log table, or an empty-state note when there are no body rows.
// `headRow` is the inner HTML of the <thead> row (the <th> cells); `bodyRows` the
// concatenated <tr>s ("" / falsy → the empty note). Callers keep full control of cells.
function logTable(headRow, bodyRows, emptyMsg) {
  return bodyRows
    ? `<div class="logwrap"><table class="logtable"><thead><tr>${headRow}</tr></thead><tbody>${bodyRows}</tbody></table></div>`
    : `<div class="empty">${emptyMsg}</div>`;
}
// A header cell; `num` right-aligns it to match a numeric column's values. `tip`
// (optional) adds a hover tooltip explaining the column.
const th = (label, num, tip) =>
  `<th${num ? ' class="lt-num"' : ""}${tip ? ` title="${esc(tip)}"` : ""}>${label}</th>`;
// A small uppercased status/category pill (the .lt-tag family).
const tag = (text, cls) => `<span class="lt-tag${cls ? " " + cls : ""}">${esc(text)}</span>`;

// ---- Cargo / Plan sub-tabs ---- //
// Loading+Unloading live under the Cargo tab; Routes+Manifest under the Plan tab,
// each behind an .arch-tabs segmented control. The active sub persists in localStorage.
// CARGO_SUB defaults to "" (auto): the active phase is inferred from the snapshot
// (current location / cargo aboard) until the user picks one explicitly — see cargoDefault.
let CARGO_SUB = localStorage.getItem("cargoSub") || "";       // "" = auto · "pickup" · "dropoff"

// ---- tabs (with URL-hash deep-linking) ---- //
const TABS = ["cargo", "plan", "contracts", "archive", "mining", "jukebox"];
function activateTab(name) {
  if (!TABS.includes(name)) return;
  TAB = name;
  document.querySelectorAll("#nav button").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hide", t.id !== name));
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  if (name === "archive") { ARCH_PICK = true; loadSessions(); }
  if (name === "mining") initMining();
  if (name === "jukebox") initJukebox();
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

document.querySelectorAll("#nav button").forEach(b => { b.onclick = () => activateTab(b.dataset.tab); });

// ---- settings overlay (sidebar gear -> dashboard-managed settings.json) ----
// Renders straight from /api/settings' schema: one row per knob, grouped, with bool ->
// checkbox / int|number -> number input / enum -> <select> / string -> text input. A knob shadowed by an
// env var comes back env_override:true and is shown read-only ("set via $VAR"), since
// env wins at read time. Save POSTs only the rows the user actually changed.
let SETTINGS_SCHEMA = null;
function _settingsCtl(f) {
  const id = "set_" + f.key, dis = f.env_override ? " disabled" : "";
  let ctl;
  if (f.type === "bool") ctl = `<input type="checkbox" id="${id}"${f.value ? " checked" : ""}${dis}>`;
  else if (f.type === "int" || f.type === "number")
    ctl = `<input type="number" id="${id}" step="${f.type === "int" ? "1" : "0.5"}" value="${esc(f.value)}"${dis}>`;
  else if (f.type === "enum")
    ctl = `<select id="${id}"${dis}>` + (f.options || []).map(o =>
      `<option value="${esc(o)}"${o === f.value ? " selected" : ""}>${esc(o[0].toUpperCase() + o.slice(1))}</option>`).join("") + `</select>`;
  else ctl = `<input type="text" id="${id}" value="${esc(f.value)}"${dis}>`;
  const env = f.env_override ? `<span class="sp-env">set via ${esc(f.env)}</span>` : "";
  return `<div class="sp-ctl">${ctl}${env}</div>`;
}
function renderSettings(schema) {
  const groups = [];
  for (const f of schema) {
    let g = groups.find(x => x.name === f.group);
    if (!g) { g = { name: f.group, fields: [] }; groups.push(g); }
    g.fields.push(f);
  }
  $("settingsBody").innerHTML = groups.map(g =>
    `<div class="sp-group"><h3 class="sp-group-h">${esc(g.name)}</h3>` +
    g.fields.map(f =>
      `<div class="sp-row"><div class="sp-label"><span class="t">${esc(f.label)}</span>` +
      `<span class="h">${esc(f.help)}</span></div>${_settingsCtl(f)}</div>`).join("") +
    (g.name === "Updates" ? _updateCheckRow() : "") +
    `</div>`).join("");
  const cb = $("checkUpdateBtn");
  if (cb) cb.onclick = checkForUpdate;
}
// A "Check for updates" action row appended to the Updates group: fetch + apply on the spot,
// no prompt (the click is the approval). Distinct from the banner, which is the passive prompt.
function _updateCheckRow() {
  return `<div class="sp-row sp-action"><div class="sp-label">` +
    `<span class="t">Check for updates</span>` +
    `<span class="h">Fetch the latest build now and apply it immediately — no prompt.</span></div>` +
    `<div class="sp-ctl"><button class="sp-btn" id="checkUpdateBtn">Check now</button>` +
    `<span class="sp-note" id="checkUpdateMsg"></span></div></div>`;
}
async function checkForUpdate() {
  const btn = $("checkUpdateBtn"), msg = $("checkUpdateMsg");
  if (!btn) return;
  btn.disabled = true; msg.textContent = "Checking…"; msg.classList.remove("err");
  try {
    const r = await postJSON("/api/update/check");
    if (r.status === "updating") msg.textContent = `Updating → ${esc(r.latest)}…`;  // server restarts; page reloads
    else if (r.status === "current") { msg.textContent = "Already up to date."; btn.disabled = false; }
    else if (r.status === "offline") { msg.textContent = "Couldn't reach the update source."; btn.disabled = false; }
    else { msg.textContent = "Updates can't run on this install."; btn.disabled = false; }
  } catch (e) {
    msg.textContent = "Check failed."; msg.classList.add("err"); btn.disabled = false;
  }
}
async function openSettings() {
  const ov = $("settingsOverlay");
  $("settingsMsg").textContent = ""; $("settingsMsg").className = "sp-msg";
  $("settingsBody").innerHTML = `<div class="sp-row"><span class="h">loading…</span></div>`;
  ov.classList.remove("hide"); ov.setAttribute("aria-hidden", "false");
  try {
    const r = await getJSON("/api/settings");
    SETTINGS_SCHEMA = r.schema || [];
    renderSettings(SETTINGS_SCHEMA);
  } catch (e) {
    $("settingsBody").innerHTML = `<div class="sp-row"><span class="h">couldn't load settings: ${esc(e)}</span></div>`;
  }
}
function closeSettings() {
  const ov = $("settingsOverlay");
  ov.classList.add("hide"); ov.setAttribute("aria-hidden", "true");
}
function _settingsErr(msg) { const m = $("settingsMsg"); m.textContent = msg; m.className = "sp-msg err"; }
async function saveSettings() {
  if (!SETTINGS_SCHEMA) return closeSettings();
  const payload = {};
  for (const f of SETTINGS_SCHEMA) {
    if (f.env_override) continue;                  // read-only: env wins at read time
    const el = $("set_" + f.key);
    if (!el) continue;
    let v;
    if (f.type === "bool") v = el.checked;
    else if (f.type === "int" || f.type === "number") {
      if (el.value.trim() === "") continue;        // left blank -> leave unchanged
      v = Number(el.value);
      if (Number.isNaN(v)) return _settingsErr(`“${f.label}” must be a number`);
    } else v = el.value.trim();
    if (v !== f.value) payload[f.key] = v;          // only send genuine changes
  }
  if (!Object.keys(payload).length) return closeSettings();
  const btn = $("settingsSave"); btn.disabled = true;
  try { await postJSON("/api/settings", payload); closeSettings(); }
  catch (e) { _settingsErr(String(e)); }
  finally { btn.disabled = false; }
}
$("navsettings") && ($("navsettings").onclick = openSettings);
$("settingsClose") && ($("settingsClose").onclick = closeSettings);
$("settingsCancel") && ($("settingsCancel").onclick = closeSettings);
$("settingsSave") && ($("settingsSave").onclick = saveSettings);
// Backdrop click closes (clicks on the panel don't reach the overlay element itself).
$("settingsOverlay") && ($("settingsOverlay").onclick = (e) => { if (e.target.id === "settingsOverlay") closeSettings(); });
// Escape closes (matches the type-filter / combobox / inline-editor convention).
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("settingsOverlay").classList.contains("hide")) closeSettings();
});

// ---- mining vs cargo mode ---- //
// Mode normally follows the snapshot: a mining vehicle (mining_ship — Prospector, MOLE,
// ROC…) hides the cargo-hauling tabs and shows Mining, and the header stats/gauge swap to
// the mining-relevant set. The MODE switch in the header lets the user pin it: "auto"
// follows detection; "cargo"/"mining" force a mode (e.g. to use the mining reference tools
// on foot, or to plan a haul while still sat in a Prospector). Persisted across sessions.
let MODE_OVERRIDE = localStorage.getItem("modeOverride") || "auto";   // auto | cargo | mining
function effectiveMining(d) {
  if (MODE_OVERRIDE === "mining") return true;
  if (MODE_OVERRIDE === "cargo") return false;
  return !!(d && d.mining_ship);                                      // auto → follow the ship
}
function setMode(m) {
  if (MODE_OVERRIDE === m) return;
  MODE_OVERRIDE = m;
  localStorage.setItem("modeOverride", m);
  const d = curData(); if (d) renderAll(d);                           // swap tabs + header at once
}
function modeSwitchHtml(d) {
  const eff = effectiveMining(d) ? "mining" : "cargo";
  return [["auto", "Auto"], ["cargo", "Cargo"], ["mining", "Mining"]].map(([k, t]) => {
    const on = MODE_OVERRIDE === k;
    const hint = (k === "auto") ? ` <small>${eff}</small>` : "";       // show what Auto resolved to
    const title = k === "auto" ? "Follow the detected ship" : `Always use ${t} mode`;
    return `<button class="modesw-opt${on ? " active" : ""}" aria-pressed="${on}"
      title="${title}" onclick="setMode('${k}')">${t}${hint}</button>`;
  }).join("");
}

// When the player is in (or the MODE switch forces) mining, the cargo-hauling tabs make no
// sense, so the Cargo and Plan tabs are hidden and the Mining tab takes their slot right
// after Contracts. Driven from renderAll on every snapshot; idempotent via MINING_LAYOUT so
// it only touches the DOM on an actual mode change.
const HAUL_TABS = ["contracts", "cargo", "plan", "archive", "jukebox"];
const MINE_TABS = ["contracts", "mining", "archive", "jukebox"];
let MINING_LAYOUT = null;   // null until the first snapshot picks a layout
function applyTabLayout(mining) {
  if (MINING_LAYOUT === mining) return;
  MINING_LAYOUT = mining;
  const order = mining ? MINE_TABS : HAUL_TABS;
  document.querySelectorAll("#nav button").forEach(b => {
    const i = order.indexOf(b.dataset.tab);
    b.classList.toggle("hide", i < 0);
    if (i >= 0) b.style.order = i;   // flex order: keep the visible slots contiguous
  });
  // If the active tab just got hidden, fall back to a sensible visible one.
  if (!order.includes(TAB)) activateTab(mining ? "mining" : "contracts");
}

// Close the Contract Log's Type-filter dropdown on any click outside it (the toggle
// button and the menu itself live inside .th-menu-wrap, so those are ignored).
document.addEventListener("click", (e) => {
  if (TYPE_MENU_OPEN && !e.target.closest(".th-menu-wrap")) { TYPE_MENU_OPEN = false; _archRepaint(); }
});
// Escape closes the Type-filter dropdown (matches the ship combobox / inline editors).
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && TYPE_MENU_OPEN) { TYPE_MENU_OPEN = false; _archRepaint(); }
});

// ---- header: status pill, ship selector, telemetry readouts, capacity gauge ---- //

// Ship catalog for the manual selector, fetched once. name -> {manufacturer,scu,groups}.
let SHIP_DB = null;
let SHIP_MENU_OPEN = false;   // combobox popup state — guards the poll from clobbering it
let SHIP_ACTIVE = -1;         // index of the keyboard-highlighted option (-1 = none)
let GRID_HOVER = false;       // hovering a load-order row — guards the poll from wiping the hold highlight
async function loadShipList() {
  try {
    const db = await getJSON("/api/ships");
    SHIP_DB = db.ships || {};
    if (LAST) renderAll(curData());  // repaint now that we have the catalog
  } catch (e) { /* leave null; the box still shows the current ship */ }
}

// concept ships (name tagged "[…Concept…]") are kept out of the picker
const shipIsConcept = (name) => /\[[^\]]*concept[^\]]*\]/i.test(name);

// catalog entries matching `filter` (by ship name or manufacturer), concepts
// removed, sorted by manufacturer then name.
function shipMatches(filter) {
  if (!SHIP_DB) return [];
  const f = (filter || "").trim().toLowerCase();
  return Object.entries(SHIP_DB)
    .filter(([n]) => !shipIsConcept(n))
    .filter(([n, v]) => !f || n.toLowerCase().includes(f) || (v.manufacturer || "").toLowerCase().includes(f))
    .map(([n, v]) => ({ name: n, mfr: v.manufacturer || "—" }))
    .sort((a, b) => a.mfr.localeCompare(b.mfr) || a.name.localeCompare(b.name));
}

// The popup's options as a flat list: a "clear" sentinel first, then the matches.
// SHIP_ACTIVE indexes into this list so Arrow keys + Enter can pick one.
function shipEntries(filter) {
  return [{ clear: true }].concat(shipMatches(filter));
}
function shipMenuHtml(filter) {
  const ents = shipEntries(filter);
  const matched = ents.length - 1;
  const rows = ents.map((e, i) => {
    const act = i === SHIP_ACTIVE ? " active" : "";
    const aria = `role="option" id="shipopt-${i}" aria-selected="${i === SHIP_ACTIVE}"`;
    if (e.clear)
      return `<div class="shipopt clear${act}" ${aria} onmousedown="pickShip(event,'')">— clear (use detected) —</div>`;
    // data-name carries the value safely (names have parens etc.); the handler reads it
    return `<div class="shipopt${act}" ${aria} data-name="${esc(e.name)}" onmousedown="pickShip(event, this.dataset.name)">
       <span class="sn">${esc(e.name)}</span><span class="om">${esc(e.mfr)}</span></div>`;
  }).join("");
  return matched ? rows : rows + `<div class="shipopt empty">no match</div>`;
}
// Paint the popup for `filter`, sync the open/active ARIA state, and keep the
// highlighted option scrolled into view.
function renderShipMenu(filter) {
  const inp = $("shipSel"), menu = $("shipMenu");
  if (!inp || !menu) return;
  SHIP_MENU_OPEN = true;
  menu.innerHTML = shipMenuHtml(filter);
  menu.classList.add("open");
  inp.setAttribute("aria-expanded", "true");
  inp.setAttribute("aria-activedescendant", SHIP_ACTIVE >= 0 ? "shipopt-" + SHIP_ACTIVE : "");
  const a = menu.querySelector(".shipopt.active");
  if (a && a.scrollIntoView) a.scrollIntoView({ block: "nearest" });
}

function openShipMenu() {
  const inp = $("shipSel"); if (inp) inp.select();
  SHIP_ACTIVE = -1;
  renderShipMenu("");   // focus shows the full list
}
function filterShipMenu() {
  SHIP_ACTIVE = -1;     // typing resets the highlight
  renderShipMenu(val("shipSel"));
}
function onShipBlur() {
  SHIP_MENU_OPEN = false;
  const menu = $("shipMenu"); if (menu) menu.classList.remove("open");
  const inp = $("shipSel");
  if (inp) { inp.setAttribute("aria-expanded", "false"); inp.setAttribute("aria-activedescendant", ""); }
  if (inp && LAST) inp.value = LAST.ship || "";  // drop unselected typing
}
// Arrow Up/Down move the highlight, Enter selects it (else the first match),
// Escape closes. Mirrors how a native <select>/combobox behaves.
function shipKeydown(ev) {
  if (ev.key === "Escape") { ev.target.blur(); return; }
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    const n = shipEntries(ev.target.value).length;
    if (!n) return;
    SHIP_ACTIVE = ev.key === "ArrowDown"
      ? Math.min(n - 1, SHIP_ACTIVE + 1)
      : Math.max(0, SHIP_ACTIVE - 1);
    renderShipMenu(ev.target.value);
  } else if (ev.key === "Enter") {
    ev.preventDefault();
    const ents = shipEntries(ev.target.value);
    if (SHIP_ACTIVE >= 0 && SHIP_ACTIVE < ents.length) {
      const e = ents[SHIP_ACTIVE];
      pickShip(ev, e.clear ? "" : e.name);
    } else {
      const m = shipMatches(ev.target.value);
      if (m.length) pickShip(ev, m[0].name);
    }
  }
}
function pickShip(ev, name) {
  ev.preventDefault();              // (mousedown) keep focus until we act
  SHIP_MENU_OPEN = false; SHIP_ACTIVE = -1;
  const inp = $("shipSel");
  if (inp) { inp.value = name; inp.blur(); }   // blur releases the repaint guard
  selectShip(name);
}

async function selectShip(name) {
  if (REPLAY_MODE) return replayEdit({ kind: "select_ship", ship: name || null });
  try { await postJSON("/api/select-ship", { ship: name || null }); }
  catch (e) { alert("Couldn't set ship: " + e); return; }
  refresh();
}

function statusHtml(d) {
  const online = d.logged_in;
  const pill = `<span class="pill ${online ? "online" : "offline"}"><span class="dot"></span>${online ? "In Verse" : "Main Menu"}</span>`;
  const scu = d.ship_scu != null ? ` <span class="cap">${num(d.ship_scu)} SCU</span>` : "";
  if (d.boarded) {
    // crewing another player's ship — show it badged as boarded, not "detected"
    const who = d.boarded_owner ? `${esc(d.boarded_owner)}'s ship` : "another ship";
    return pill + `<span class="ship">SHIP <b>${esc(d.ship || "—")}</b>${scu}
      <span class="ship-auto" title="you're aboard ${who} as crew — the manifest shows the shared haul in this hold">⚑ aboard ${who}</span></span>`;
  }
  if (d.ship_detected) {
    // detected ship overrides the manual pick — show it locked, no searchable box
    return pill + `<span class="ship">SHIP <b>${esc(d.ship || "—")}</b>${scu}
      <span class="ship-auto" title="detected from the game log">● detected</span></span>`;
  }
  const box = `<span class="shipbox">
    <input id="shipSel" class="shipsel" type="text" autocomplete="off" aria-label="Ship"
      role="combobox" aria-expanded="false" aria-controls="shipMenu" aria-autocomplete="list" aria-activedescendant=""
      placeholder="search ship…" value="${esc(d.ship || "")}"
      onfocus="openShipMenu()" oninput="filterShipMenu()" onkeydown="shipKeydown(event)" onblur="onShipBlur()">
    <div id="shipMenu" class="shipmenu" role="listbox" aria-label="Ships"></div></span>`;
  return pill + `<span class="ship">SHIP ${box}${scu}</span>`;
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
  const busy = SHIP_MENU_OPEN || (document.activeElement && document.activeElement.id === "shipSel");
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
      ${isnum ? 'type="number" min="0" step="1"' : (list ? `list="${list}"` : "")}
      value="${esc(cur)}" placeholder="${esc(opts.ph || editPlaceholder(f.k))}"
      onkeydown="edKey(event)" onblur="edCommit(this)"></span>`;
  }
  const inner = known ? esc(value)
    : `<span class="edc-unkn">${esc(opts.label || (value != null && value !== "" ? value : editPlaceholder(f.k)))}</span>`;
  return `<span class="edc${known ? "" : " is-unknown"}" data-field='${esc(JSON.stringify(f))}'
    title="${known ? "Click to correct" : "Click to set"}" onclick="edOpen(this)">${inner}<span class="edc-pen">✎</span></span>`;
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
  if (REPLAY_MODE) {
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
async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(j.error || r.status);
  return j;
}
// POST returning a raw JSON body (no {ok} envelope) — the replay snapshot/edit responses.
async function postRaw(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify(body), cache: "no-store" });
  return r.json();
}
// Archive edit: send one edit op to the ephemeral overlay, swap in the recomputed snapshot
// + updated overlay (nothing is persisted), and repaint. The single path every editor uses
// in replay mode in place of its live /api/* write.
async function replayEdit(op) {
  try {
    const j = await postRaw("/api/replay/edit",
      { key: REPLAY_KEY, at: REPLAY_I, overlay: REPLAY_EDITS, op });
    if (!j || !j.snapshot) throw new Error((j && j.error) || "edit failed");
    REPLAY_EDITS = j.overlay; REPLAY_SNAPSHOT = j.snapshot;
    EDIT = null; renderAll(curData());
  } catch (e) { alert("Edit failed: " + e); }
}
// GET + parse JSON for the live dashboard's no-cache reads (state/sessions/replay/ships).
// Mining catalog lookups use their own plain fetch (cacheable, no `ok` envelope).
async function getJSON(url) {
  return (await fetch(url, { cache: "no-store" })).json();
}
// Re-render only the edit-bearing containers from the current snapshot (used when
// opening/cancelling an inline editor, without a network round-trip).
function rerenderEdits() {
  if (!LAST) return;
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
  const d = curData(); if (d) setHTML("cargo", cargoView(d));
}
// Build an .arch-tabs segmented control: `items` is [[key,label],...]; `active` the
// selected key; `fn` the handler name invoked with the key. opts.attr(key) adds per-button
// attributes (e.g. data-sub); opts.tail is extra HTML appended inside the bar (e.g. the
// archive summary span). Shared by every secondary-nav strip (cargo/plan/archive/mining).
function tabBar(items, active, fn, opts = {}) {
  const btns = items.map(([k, t]) => {
    const attr = opts.attr ? " " + opts.attr(k) : "";
    return `<button class="arch-tab${k === active ? " active" : ""}"${attr} onclick="${fn}('${k}')">${t}</button>`;
  }).join("");
  return `<div class="arch-tabs">${btns}${opts.tail || ""}</div>`;
}
function cargoView(d) {
  const sub = cargoSubActive(d);
  const body = sub === "dropoff"
    ? groupCards(d.unloading, "unloading", d)
    : groupCards(d.loading, "loading", d);
  return tabBar([["pickup", "Loading"], ["dropoff", "Unloading"]], sub, "cargoSub") + body;
}

// ---- Plan tab: ONE section — the ordered itinerary IS the load order, with the 3D
// hold below it. Dragging a stop sets the visit & load order (ROUTE_ORDER) that both
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
  if (REPLAY_MODE) return replayEdit({ kind: "leg_state", legs, done });
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
  const reset = ROUTE_ORDER
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
          ondragstart="routeDragStart(event)" onkeydown="routeGripKey(event)">⠿</button>${hasGrid ? `<span class="ps-sw" style="background:hsl(${hue},64%,52%)"></span>` : ""}${(hasGrid && pos) ? `<span class="ps-pos" title="load #${pos}${banded ? " — loaded deepest-first" : ""}">${pos}</span>` : ""}${stationCell(s.station, s.zone)}${run.partial ? ' <span class="warn">⚠</span>' : ""}${sharedTag}</span>
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
  ROUTE_ORDER = order;
  persistRouteOrder();
  routeDragEnd();
  renderAll(curData());
}
// Persist the manual route order to localStorage — but only when live. In archive replay
// the order is ephemeral (restored on exit), so it must not bleed into the live view.
function persistRouteOrder() {
  if (REPLAY_MODE) return;
  if (ROUTE_ORDER) localStorage.setItem("routeOrder", JSON.stringify(ROUTE_ORDER));
  else localStorage.removeItem("routeOrder");
}
function resetRouteOrder() {
  ROUTE_ORDER = null;
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
  ROUTE_ORDER = order;
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
    <td><input class="lq" type="number" aria-label="Quantity in SCU" placeholder="?" value="${leg.qty == null ? "" : leg.qty}"></td>
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
      <td>${stationText(m.origin)}</td>
      <td>${missionLegs(m)}</td>
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
// The snapshot every tab renders from (live session only).
const curData = () => (REPLAY_MODE ? REPLAY_SNAPSHOT : LAST);

// Render every tab from one snapshot `d` (the live snapshot).
function renderAll(d) {
  if (!d) return;
  applyTabLayout(effectiveMining(d));   // detected mining ship (or the MODE switch) → Mining tabs
  renderHeader(d);
  setHTML("datalists", datalistsHtml(d.catalog));
  // EDIT_CELL guards every cargo-ops screen so an open inline editor isn't clobbered by
  // the 3s poll; DRAG_DEST guards a route drag; GRID_HOVER guards the hold highlight. Plan
  // renders only its active sub, so combining all three keeps either sub stable mid-interaction.
  if (!EDIT_CELL) setHTML("cargo", cargoView(d));
  if (!EDIT_CELL && DRAG_DEST == null && !GRID_HOVER) setHTML("plan", planView(d));
  if (EDIT === null) setHTML("contracts", missionsTable(d.missions));  // don't clobber an open editor
}

const loadOrder = (gs) => [...gs].sort((a, b) => b.routeIdx - a.routeIdx);

// Manual delivery order: a persisted list of destination stations the user dragged
// into their preferred visit sequence. When set it overrides the planner's order
// everywhere (route cards, trip plan, and the load order via deliveryIndex). Unknown
// destinations (new contracts) fall through to the server order until next reordered.
let ROUTE_ORDER = (() => {
  try { return JSON.parse(localStorage.getItem("routeOrder") || "null"); } catch (e) { return null; }
})();
const routeRank = (dest) => {
  const i = ROUTE_ORDER ? ROUTE_ORDER.indexOf(dest) : -1;
  return i >= 0 ? i : Infinity;
};
// Stable-sort items by the manual order, keeping the server order for ties/unknowns.
function byRouteOrder(arr, destOf) {
  if (!ROUTE_ORDER || !arr) return arr || [];
  return arr.map((x, i) => [x, i])
    .sort((a, b) => (routeRank(destOf(a[0])) - routeRank(destOf(b[0]))) || (a[1] - b[1]))
    .map(p => p[0]);
}

// delivery position of a destination from the plotted route (0 = delivered first).
function deliveryIndex(d, dest) {
  if (ROUTE_ORDER) {           // user's manual drag order wins when set
    const r = ROUTE_ORDER.indexOf(dest);
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
// current override for a mission, from whatever data is displayed (live LAST or the
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
function saveMission(mid) { postOverride(mid, buildOverride()); }
function resetMission(mid) { postOverride(mid, null); }       // clear entirely
function restoreMission(mid) {                                // un-hide, keep edits
  const ov = { ...rawOverride(mid) };
  delete ov.hidden;
  postOverride(mid, Object.keys(ov).length ? ov : null);
}
function deleteMission(mid) {                                 // hide, keep edits (Restore brings it back)
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
  if (REPLAY_MODE) return replayEdit({ kind: "override", mission_id: mid, override });
  try { await postJSON("/api/override", { mission_id: mid, override }); }
  catch (e) { alert("Save failed: " + e); }
  EDIT = null;
  refresh();
}

// ---- archive / history ---- //
function fmtWhen(iso) {
  if (!iso) return "?";
  const d = new Date(iso);
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
// Format an elapsed span in seconds. Default is Xh Ym / Xm (session length, QT total);
// {seconds:true} gives Xm Ys / Xs for short hops (per-jump travel time). "" if invalid.
function fmtElapsed(sec, { seconds = false } = {}) {
  if (sec == null || sec < 0 || !isFinite(sec)) return "";
  if (seconds) {
    sec = Math.round(sec);
    const m = Math.floor(sec / 60);
    return m ? `${m}m ${sec % 60}s` : `${sec}s`;
  }
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function fmtDuration(a, b) {
  if (!a || !b) return "";
  return fmtElapsed((new Date(b) - new Date(a)) / 1000);
}

function sessionsView(sessions) {
  if (!sessions) return `<div class="empty">loading archive…</div>`;
  if (!sessions.length) return `<div class="empty">No archived sessions yet. A session is saved here when you log out or relaunch the game.</div>`;
  // Pooled logs as horizontal tabs (Contract Log, Trade Loads — which now also carries
  // the trade-route recommendations — and Travel Log); the selected tab's body fills the
  // viewport. ARCH_OPEN is the active tab — defaulted by recency (archDefaultSection).
  const secs = [contractLogView(sessions), tradeLogView(sessions), travelLogView(sessions), sessionListView(sessions)];
  if (!secs.some(s => s.key === ARCH_OPEN)) ARCH_OPEN = secs[0].key;
  const active = secs.find(s => s.key === ARCH_OPEN) || secs[0];
  const tabs = tabBar(secs.map(s => [s.key, s.title]), ARCH_OPEN, "toggleArch",
    { tail: `<span class="arch-sum">${active.headSpan}</span>` });
  return `<div class="arch-acc">
    ${tabs}
    <div class="card logcard arch-panel">${active.body}</div>
  </div>`;
}

// Departed → arrived elapsed, seconds-aware (jumps run seconds to minutes). "" if no arrival.
function fmtTravelTime(dep, arr) {
  if (!dep || !arr) return "";
  return fmtElapsed((new Date(arr) - new Date(dep)) / 1000, { seconds: true });
}

// Pool every session's trades + the live ones, deduped (shared by the trade & travel logs).
function pooledTrades(sessions) {
  const out = [], seen = new Set();
  const add = t => {
    const k = `${t.ts}|${t.action}|${t.commodity_guid}|${t.scu}`;
    if (!seen.has(k)) { seen.add(k); out.push(t); }
  };
  for (const s of sessions || []) for (const t of s.trades || []) add(t);
  for (const t of (LAST && LAST.trades) || []) add(t);
  return out;
}
function fuelShort(n) {
  return !n ? "—" : n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : "" + n;
}

// Cross-session quantum-travel log: each jump as Status · From → To · Time · System ·
// QT fuel · Ship. Pooled with the live session's jumps. Newest first.
function travelLogView(sessions) {
  const seen = new Set(), rows = [];
  const add = t => {
    const k = `${t.ts}|${t.ship}|${t.to_code}`;
    if (!seen.has(k)) { seen.add(k); rows.push(t); }
  };
  for (const s of sessions || [])
    for (const t of s.travels || []) add(t);
  for (const t of (LAST && LAST.travels) || []) add(t);  // live session
  rows.sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
  let totalSecs = 0;
  const body = rows.map(t => {
    const status = t.arrived
      ? `<span class="lt-tag good" title="arrived ${esc(t.arrived)}">✔ arrived</span>`
      : `<span class="lt-tag" title="no arrival logged">⋯ in transit</span>`;
    const dur = fmtTravelTime(t.ts, t.arrived);
    if (t.arrived) totalSecs += Math.max(0, (new Date(t.arrived) - new Date(t.ts)) / 1000);
    const sys = t.system
      ? `<span class="qt-sys s-${t.system.replace(/\s+/g, "").toLowerCase()}">${esc(t.system)}</span>` : "";
    return `<tr>
      <td class="lt-when">${fmtWhen(t.ts)}</td>
      <td>${status}</td>
      <td class="lt-title">${esc(t.from)} <span class="qt-leg">→</span> ${esc(t.to)}</td>
      <td class="lt-num">${dur || '<span class="qt-none">—</span>'}</td>
      <td>${sys}</td>
      <td class="lt-num" title="QT fuel estimate">${fuelShort(t.fuel)}</td>
      <td class="lt-shop">${esc(t.ship || "")}</td></tr>`;
  }).join("");
  const tot = totalSecs ? ` · ${fmtElapsed(totalSecs)} in QT` : "";
  const inner = logTable(
    `<th>Departed</th><th>Status</th><th>Route</th><th class="lt-num">Time</th><th>System</th><th class="lt-num">QT fuel</th><th>Ship</th>`,
    body, "No quantum travel in range.");
  return logSection("travel", `Travel Log · ${rows.length}`,
                    `<span class="scu">${rows.length} jumps${tot}</span>`, inner);
}

// High-level contract kind, mirroring backend patterns.classify_contract for archived
// sessions that predate the stored `type` (logbackup gone, never re-archived). Falls
// back to title/is_trade only (org/contract aren't kept in the summary).
const _CT_COMBAT = ["bounty", "bounties", "eliminate", "kill", "destroy", "defeat",
  "mercenary", "security", "defend", "defence", "defense", "assault", "attack",
  "combat", "pirate", "raid", "ambush", "wanted", "hostile", "strike"];
const _CT_DELIVERY = ["deliver", "courier", "transport", "package", "parcel", "dossier",
  "retrieve", "recover", "fetch", "files", "investigate", "smuggl"];
function contractType(m) {
  if (m.type) return m.type;
  if (m.is_trade) return "Hauling";
  const hay = (m.title || "").toLowerCase();
  if (_CT_COMBAT.some(w => hay.includes(w))) return "Bounty / Combat";
  if (_CT_DELIVERY.some(w => hay.includes(w))) return "Delivery";
  return "Other";
}
// The authoritative label→slug table lives ONLY in the backend (scdata._TYPE_MAP): every
// p4k mission type ships its slug per mission (decoded.icon live, m.icon archived), so the
// frontend never restates it. The one thing that has no per-mission icon is contractType()'s
// own heuristic fallback labels (they're not p4k types) — give just those four a fixed slug.
const HEUR_SLUG = { "Hauling": "haul", "Delivery": "deliver",
  "Bounty / Combat": "bounty", "Other": "other" };
const ctSlug = (label, icon) => icon || HEUR_SLUG[label] || "other";
// The game's own mobiGlas type icon, with the label as a tooltip. Icon-forward; if the SVG
// isn't on disk (contracts not yet extracted / offline) the row reveals the text label.
function typeMark(label, icon) {
  const l = label || "Other";
  const s = ctSlug(l, icon);
  return `<span class="ct-mark ct-${s}" title="${esc(l)}"><img class="ct-ico" alt="${esc(l)}"`
    + ` src="/mission-icons/${s}.svg" onerror="this.closest('.ct-mark').classList.add('noico')">`
    + `<span class="ct-lbl lt-tag ct-${s}">${esc(l)}</span></span>`;
}

// Contract Log type filter — a set of EXCLUDED types (empty = show all), persisted.
// The open/closed dropdown state lives in globals so the 3s poll's re-render preserves
// it; CT_PRESENT caches the types the current data offers (for the All/None buttons).
let CONTRACT_TYPE_HIDDEN = new Set(JSON.parse(localStorage.getItem("ctHidden") || "[]"));
let TYPE_MENU_OPEN = false;
let CT_PRESENT = [];
function _saveCtHidden() { localStorage.setItem("ctHidden", JSON.stringify([...CONTRACT_TYPE_HIDDEN])); }
function _archRepaint() { setHTML("archive", sessionsView(SESSIONS)); }
function toggleTypeMenu() { TYPE_MENU_OPEN = !TYPE_MENU_OPEN; _archRepaint(); }
function toggleTypeFilter(t) {
  CONTRACT_TYPE_HIDDEN.has(t) ? CONTRACT_TYPE_HIDDEN.delete(t) : CONTRACT_TYPE_HIDDEN.add(t);
  _saveCtHidden(); _archRepaint();
}
function setAllTypeFilters(showAll) {
  CONTRACT_TYPE_HIDDEN = showAll ? new Set() : new Set(CT_PRESENT);
  _saveCtHidden(); _archRepaint();
}

// Flat, cross-session log of every mission contract, time-ordered (newest first) by
// when it ended (else when accepted — both now carried per mission in the archive).
// Unfinished contracts (active when the session ended) are always hidden here; they're
// still kept in the sessions file, just not shown. The Type column header carries a
// multiselect dropdown that filters rows by high-level contract kind.
function contractLogView(sessions) {
  const all = [];
  const slugOf = {};   // label -> icon slug, learned from the data (p4k types carry m.icon)
  const count = {};
  for (const s of sessions || [])
    for (const m of s.missions || [])
      if (m.status !== "unfinished") {
        const type = contractType(m);
        if (!slugOf[type] || slugOf[type] === "other") slugOf[type] = ctSlug(type, m.icon);
        count[type] = (count[type] || 0) + 1;
        all.push({ when: m.ended_at || m.accepted_at || s.started_at, m, type });
      }
  // types present, ordered by frequency (then name); the catch-all "Other" sinks last
  CT_PRESENT = Object.keys(count).sort((a, b) =>
    (a === "Other") - (b === "Other") || count[b] - count[a] || a.localeCompare(b));
  const rows = all.filter(r => !CONTRACT_TYPE_HIDDEN.has(r.type))
    .sort((a, b) => (b.when || "").localeCompare(a.when || ""));
  const total = rows.reduce((a, r) => a + (r.m.reward || 0), 0);
  const body = rows.map(r => {
    const dest = (r.m.destinations || []).filter(Boolean);
    return `<tr>
      <td class="lt-when">${fmtWhen(r.when)}</td>
      <td><span class="badge b-${r.m.status}">${esc(r.m.status)}</span></td>
      <td class="lt-type">${typeMark(r.type, r.m.icon)}</td>
      <td class="lt-title">${esc(r.m.title)}${dest.length ? ` <span class="sub">→ ${esc(dest.join(", "))}</span>` : ""}</td>
      <td class="lt-num">${r.m.reward ? num(r.m.reward) : "—"}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="lt-empty">No contracts match the selected types.</td></tr>`;
  const hidden = CT_PRESENT.filter(t => CONTRACT_TYPE_HIDDEN.has(t)).length;
  const opts = CT_PRESENT.map(t =>
    `<label class="th-opt"><input type="checkbox" ${CONTRACT_TYPE_HIDDEN.has(t) ? "" : "checked"}
       onclick="toggleTypeFilter('${t.replace(/'/g, "\\'")}')"><img class="ct-ico opt-ico" alt=""
       src="/mission-icons/${slugOf[t]}.svg" onerror="this.style.display='none'">${tag(t, "ct-" + slugOf[t])}</label>`).join("");
  const menu = `<span class="th-menu-wrap">
    <button class="th-menu-btn${hidden ? " on" : ""}" aria-haspopup="true" aria-expanded="${TYPE_MENU_OPEN}" onclick="toggleTypeMenu()">Type ▾</button>${
      TYPE_MENU_OPEN ? `<span class="th-menu">
        <span class="th-menu-act"><button onclick="setAllTypeFilters(true)">All</button><button onclick="setAllTypeFilters(false)">None</button></span>
        ${opts}</span>` : ""}</span>`;
  const inner = all.length
    ? logTable(`<th>When</th><th>Status</th><th class="th-type">${menu}</th><th>Contract</th>${th("Reward", 1)}`,
               body, "")
    : `<div class="empty">No contracts in range.</div>`;
  const typeNote = hidden ? ` · ${CT_PRESENT.length - hidden}/${CT_PRESENT.length} types` : "";
  return logSection("contracts", `Contract Log · ${rows.length}`,
                    `<span class="scu">${num(total)} aUEC${typeNote}</span>`, inner);
}

// Group manual trades into "loads": a buy paired (FIFO, per commodity) with the
// possibly-split sells that draw it down, so profit can be read per whole load even
// when a sell is split. A buy not yet fully sold is "open"/"holding" (profit only on
// the sold portion); a sell with no buy in range surfaces as a "no basis" row.
// Stable id matching State.trade_id (ts|action|guid|shop, raw shop name), so a load
// can be flagged lost server-side. Computed from fields the trade dict already has.
const tradeId = t => t.id || `${t.ts}|${t.action}|${t.commodity_guid}|${t.shop_raw || t.shop}`;

function buildLoads(trades) {
  const chrono = [...trades].sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
  const open = {};   // commodity key -> FIFO queue of open buy lots
  const loads = [];
  for (const t of chrono) {
    const key = t.commodity_guid || t.commodity;
    if (t.action === "buy") {
      const lot = { commodity: t.commodity, ts: t.ts, buyPlace: t.shop, buyScu: t.scu,
                    cost: t.auec, soldScu: 0, revenue: 0, sellPlaces: [], id: tradeId(t) };
      (open[key] = open[key] || []).push(lot);
      loads.push(lot);
    } else {
      let remain = t.scu;
      const q = open[key] || [];
      while (remain > 0 && q.length) {
        const lot = q[0], take = Math.min(lot.buyScu - lot.soldScu, remain);
        lot.soldScu += take;
        lot.revenue += t.auec * (t.scu ? take / t.scu : 1);  // prorate split sells
        if (t.shop && !lot.sellPlaces.includes(t.shop)) lot.sellPlaces.push(t.shop);
        remain -= take;
        if (lot.soldScu >= lot.buyScu - 0.001) q.shift();
      }
      if (remain > 0)  // sold more than any tracked buy → unmatched sell
        loads.push({ commodity: t.commodity, ts: t.ts, buyPlace: null, buyScu: 0, cost: 0,
                     soldScu: remain, revenue: t.auec * (t.scu ? remain / t.scu : 1),
                     sellPlaces: [t.shop], noBasis: true });
    }
  }
  return loads;
}

// Cross-session manual-trade LOAD log: the trade-route recommendations (folded in from
// the former Trade Routes tab) on top, then each load as a row (buy + its sells with
// realised profit = revenue − the cost of the sold portion). Newest load first.
function tradeLogView(sessions) {
  // Pool archived trades with the CURRENT (un-archived) session's so a just-made trade
  // shows immediately (not only after logout); pooledTrades dedups both feeds.
  const trades = pooledTrades(sessions);
  const loads = buildLoads(trades).sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
  const LOST = new Set((LAST && LAST.lost_trades) || []);
  const routesBlock = tradeRoutesBlock(loads, LOST);
  let totalProfit = 0;
  const body = loads.map(L => {
    const sold = L.soldScu, lost = L.id && LOST.has(L.id);
    // a lost load writes off the unsold remainder: realise the FULL buy cost.
    const realisedCost = lost ? L.cost : (L.buyScu ? L.cost * (sold / L.buyScu) : 0);
    const profit = Math.round(L.revenue - realisedCost);
    const priced = sold > 0 || lost;   // lost loads realise even with no sells
    if (priced) totalProfit += profit;
    const route = [L.buyPlace, (L.sellPlaces || []).join(" / ")].filter(Boolean).join(" → ") || "—";
    let tag, scu;
    if (lost) { tag = `<span class="lt-tag lost">lost</span>`; scu = `${num(sold)}/${num(L.buyScu)}`; }
    else if (L.noBasis) { tag = `<span class="lt-tag warn">no basis</span>`; scu = num(sold); }
    else if (sold >= L.buyScu) { tag = `<span class="lt-tag good">closed</span>`; scu = num(L.buyScu); }
    else if (sold > 0) { tag = `<span class="lt-tag">open</span>`; scu = `${num(sold)}/${num(L.buyScu)}`; }
    else { tag = `<span class="lt-tag">holding</span>`; scu = num(L.buyScu); }
    // a buy-based load that isn't fully sold can be marked lost (cargo destroyed /
    // stolen); a lost one can be restored. No action on fully-closed or no-basis rows.
    let act = "";
    if (lost) act = `<button class="lt-act" title="Restore — not lost" onclick='markTradeLost(${JSON.stringify(L.id)}, false)'>↩</button>`;
    else if (!L.noBasis && sold < L.buyScu) act = `<button class="lt-act" title="Mark this haul lost (cargo destroyed/stolen)" onclick='markTradeLost(${JSON.stringify(L.id)}, true)'>✕</button>`;
    return `<tr class="${lost ? "lt-lost" : ""}">
      <td class="lt-when">${fmtWhen(L.ts)}</td>
      <td class="lt-title">${esc(L.commodity)}</td>
      <td class="lt-status">${tag}${act}</td>
      <td class="lt-shop">${esc(route)}</td>
      <td class="lt-num">${scu}</td>
      <td class="lt-num ${L.cost ? "neg" : ""}">${L.cost ? "−" + num(L.cost) : "—"}</td>
      <td class="lt-num ${L.revenue ? "pos" : ""}">${L.revenue ? "+" + num(Math.round(L.revenue)) : "—"}</td>
      <td class="lt-num ${!priced ? "" : profit >= 0 ? "pos" : "neg"}">${priced ? (profit >= 0 ? "+" : "−") + num(Math.abs(profit)) : "—"}</td></tr>`;
  }).join("");
  const loadsTable = loads.length ? `<table class="logtable">
      <thead><tr><th>When</th><th>Commodity</th><th>Status</th><th>Route</th>${th("SCU", 1)}${th("Cost", 1)}${th("Revenue", 1)}${th("Profit", 1)}</tr></thead>
      <tbody>${body}</tbody></table>` : `<div class="empty">No manual trades in range.</div>`;
  // both tables share one scroll region (the recs/rank bar scroll with them)
  const inner = `<div class="logwrap">${routesBlock}`
    + `<div class="arch-sub">Loads · ${loads.length}</div>${loadsTable}</div>`;
  return logSection("trades", `Trade Loads · ${loads.length}`,
                    `<span class="scu ${totalProfit >= 0 ? "pos" : "neg"}">${totalProfit >= 0 ? "+" : "−"}${num(Math.abs(totalProfit))} aUEC profit</span>`, inner);
}

// Aggregate completed/partly-sold loads into trade ROUTES keyed by
// commodity + buy station → sell station(s). Each route rolls up every trip's sold
// SCU, realised cost (cost of the sold portion only, so open loads count fairly),
// revenue and profit, plus weighted % return and aUEC/SCU. Lost & no-basis loads are
// excluded — a route recommendation should reflect deliveries that actually completed.
function tradeRoutes(loads, lostSet) {
  const agg = {};
  for (const L of loads) {
    if (L.noBasis || !L.buyPlace || L.soldScu <= 0) continue;
    if (L.id && lostSet && lostSet.has(L.id)) continue;
    const to = (L.sellPlaces || []).join(" / ");
    if (!to) continue;
    const realisedCost = L.buyScu ? L.cost * (L.soldScu / L.buyScu) : 0;
    const key = `${L.commodity}|${L.buyPlace}|${to}`;
    const a = agg[key] || (agg[key] = {
      commodity: L.commodity, from: L.buyPlace, to, scu: 0, cost: 0, revenue: 0, trips: 0,
    });
    a.scu += L.soldScu; a.cost += realisedCost; a.revenue += L.revenue; a.trips += 1;
  }
  return Object.values(agg).map(a => ({
    ...a,
    profit: a.revenue - a.cost,
    pct: a.cost ? (a.revenue - a.cost) / a.cost : 0,
    perScu: a.scu ? (a.revenue - a.cost) / a.scu : 0,
  }));
}

const ROUTE_SORTS = { profit: "Total aUEC", pct: "% return", perScu: "aUEC / SCU" };
const pctFmt = n => (n >= 0 ? "+" : "−") + (Math.abs(n) * 100).toFixed(Math.abs(n) < 0.1 ? 1 : 0) + "%";
const signed = n => (n >= 0 ? "+" : "−") + num(Math.abs(Math.round(n)));

// Trade-route recommendations block (folded into the Trade Loads tab): rank the
// player's own buy→sell routes by the chosen metric (total profit / % return / per-SCU)
// and call out the single best of each. Built from the same trade loads as the ledger
// below it, so the caller passes the already-computed loads + lost set. Returns HTML
// (a "Top routes" subheader + recs + table); "" when there are no completed routes yet.
function tradeRoutesBlock(loads, lostSet) {
  const routes = tradeRoutes(loads, lostSet);
  if (!routes.length)
    return `<div class="arch-sub">Top routes</div>`
      + `<div class="empty">No completed trade routes yet. Buy a commodity at one station and sell it at another — your most profitable routes will surface here.</div>`;

  const sortKey = ROUTE_SORTS[ROUTE_SORT] ? ROUTE_SORT : "profit";
  const ranked = [...routes].sort((a, b) => b[sortKey] - a[sortKey]);
  const bestProfit = [...routes].sort((a, b) => b.profit - a.profit)[0];
  const bestPct = [...routes].sort((a, b) => b.pct - a.pct)[0];
  const callout = (lbl, r, val) => `<div class="rec">
      <span class="rec-lbl">${lbl}</span>
      <span class="rec-cmd">${esc(r.commodity)}</span>
      <span class="rec-route">${esc(r.from)} <span class="qt-leg">→</span> ${esc(r.to)}</span>
      <span class="rec-val pos">${val}</span></div>`;
  const recs = `<div class="recs">
    ${callout("Top earner", bestProfit, `${signed(bestProfit.profit)} aUEC`)}
    ${callout("Best margin", bestPct, pctFmt(bestPct.pct))}</div>`;

  const bar = `<div class="filtbar">
    <span class="filt-lbl">Rank by</span>
    ${Object.entries(ROUTE_SORTS).map(([k, lbl]) =>
      `<button class="seg${sortKey === k ? " on" : ""}" onclick="setRouteSort('${k}')">${lbl}</button>`).join("")}</div>`;

  const body = ranked.map((r, i) => `<tr class="${i === 0 ? "rt-best" : ""}">
      <td class="lt-title">${i === 0 ? '<span class="rt-star" title="top route by the selected metric">★</span> ' : ""}${esc(r.commodity)}</td>
      <td class="lt-shop">${esc(r.from)} <span class="qt-leg">→</span> ${esc(r.to)}</td>
      <td class="lt-num">${r.trips}</td>
      <td class="lt-num">${num(Math.round(r.scu))}</td>
      <td class="lt-num ${r.profit >= 0 ? "pos" : "neg"}">${signed(r.profit)}</td>
      <td class="lt-num ${r.pct >= 0 ? "pos" : "neg"}">${pctFmt(r.pct)}</td>
      <td class="lt-num ${r.perScu >= 0 ? "pos" : "neg"}">${signed(r.perScu)}</td></tr>`).join("");
  return `<div class="arch-sub">Top routes · ${routes.length}</div>` + recs + bar
    + `<table class="logtable">
      <thead><tr><th>Commodity</th><th>Route</th>${th("Trips", 1)}${th("SCU", 1)}${th("Profit", 1)}${th("%", 1)}${th("/SCU", 1)}</tr></thead>
      <tbody>${body}</tbody></table>`;
}

async function loadSessions() {
  try {
    SESSIONS = await getJSON("/api/sessions");
  } catch (e) { SESSIONS = SESSIONS || []; }
  if (ARCH_PICK) { if (!ARCH_OPEN) ARCH_OPEN = archDefaultSection(); ARCH_PICK = false; }  // only when none open
  _archRepaint();
}

// Flag/unflag a trade load as lost (cargo destroyed/stolen). Optimistically updates
// the live snapshot's lost set so the row re-renders immediately, then persists.
async function markTradeLost(id, lost) {
  if (REPLAY_MODE) return replayEdit({ kind: "trade_lost", trade_id: id, lost });
  if (LAST) {
    const set = new Set(LAST.lost_trades || []);
    lost ? set.add(id) : set.delete(id);
    LAST.lost_trades = [...set];
  }
  _archRepaint();
  try { await postJSON("/api/trade-lost", { trade_id: id, lost }); }
  catch (e) { /* next poll reconciles from the server */ }
}

// Every archived session as a row, newest first: when (+ duration) · player · ship(s) ·
// earned · contracts (completed/total) · trades · a Replay control. Replaying a session
// drives the WHOLE dashboard into its reconstructed past state (see enterReplay).
function sessionListView(sessions) {
  const list = [...(sessions || [])].sort((a, b) => (b.started_at || "").localeCompare(a.started_at || ""));
  const body = list.map(s => {
    const dur = fmtDuration(s.started_at, s.ended_at);
    const c = s.counts || {};
    const ships = (s.ships || []).join(", ");
    const trades = (s.trades || []).length;
    const replaying = REPLAY_MODE && REPLAY_KEY === s.key;
    const act = replaying
      ? `<button class="lt-act on" onclick="exitReplay()" title="Stop replaying this session">■ exit replay</button>`
      : REPLAY_UNAVAILABLE.has(s.key)
        ? `<span class="lt-tag" title="The source log for this session is no longer on disk">log gone</span>`
        : `<button class="lt-act" onclick='enterReplay(${JSON.stringify(s.key)})' title="Replay this session — scrub the whole dashboard through it">▶ replay</button>`;
    return `<tr class="${replaying ? "sess-replaying" : ""}">
      <td class="lt-when">${fmtWhen(s.started_at)}${dur ? ` <span class="sub">· ${dur}</span>` : ""}</td>
      <td class="lt-shop">${esc(s.player || "—")}</td>
      <td class="lt-shop">${esc(ships || "—")}</td>
      <td class="lt-num">${s.earned ? num(s.earned) : "—"}</td>
      <td class="lt-num">${c.completed || 0}/${c.total || 0}</td>
      <td class="lt-num">${trades || "—"}</td>
      <td class="lt-replay">${act}</td></tr>`;
  }).join("");
  const inner = logTable(
    `<th>Session</th><th>Player</th><th>Ship(s)</th>${th("Earned", 1)}${th("Done", 1)}${th("Trades", 1)}<th>Replay</th>`,
    body, "No archived sessions yet.");
  return logSection("sessions", `Sessions · ${list.length}`,
                    `<span class="scu">${list.length} archived</span>`, inner);
}

// ---- replay controls ---- //
// Enable a session for replay: fetch its scrub timeline, default to the final state,
// then drive the whole dashboard from the reconstructed snapshot. Live polling pauses.
async function enterReplay(key) {
  try {
    const tl = await getJSON(`/api/replay/timeline?key=${encodeURIComponent(key)}`);
    if (!tl.available || !tl.count) {
      REPLAY_UNAVAILABLE.add(key);
      _archRepaint();
      return;
    }
    REPLAY_KEY = key; REPLAY_POINTS = tl.points; REPLAY_MODE = true;
    REPLAY_EDITS = null;                  // fresh sandbox; the server seeds it on first edit
    REPLAY_SAVED_ORDER = ROUTE_ORDER;     // archive reordering is ephemeral — restore on exit
    // Land on the session's busiest checkpoint (most contracts/cargo on the dashboard)
    // rather than the last one — session-end usually has empty holds and finished
    // contracts, so defaulting there makes replay look like it did nothing. Falls back
    // to the last checkpoint when the session had no cargo activity (e.g. combat-only).
    let best = tl.count - 1, bestFill = 0;
    for (const p of tl.points) { const f = p.fill || 0; if (f >= bestFill) { bestFill = f; best = p.i; } }
    REPLAY_I = bestFill > 0 ? best : tl.count - 1;
    await loadReplayState();                       // sets REPLAY_SNAPSHOT + renders all tabs
    renderReplayBar();
    _archRepaint();     // reflect the active-replay row state
  } catch (e) {
    REPLAY_UNAVAILABLE.add(key);
    _archRepaint();
  }
}

// Fetch the snapshot for the current checkpoint and repaint every tab from it.
async function loadReplayState() {
  const bar = $("replaybar"); if (bar) bar.classList.add("rb-busy");
  try {
    // POST so any ephemeral edits (REPLAY_EDITS) stay applied while scrubbing; null overlay
    // returns the cached disk-state snapshot for this checkpoint (unchanged behaviour).
    const snap = await postRaw("/api/replay/state",
      { key: REPLAY_KEY, at: REPLAY_I, overlay: REPLAY_EDITS });
    if (snap && snap.available !== false) { REPLAY_SNAPSHOT = snap; renderAll(curData()); }
  } catch (e) { /* leave the prior frame up */ }
  if (bar) bar.classList.remove("rb-busy");
}

// Scrub: move to checkpoint i. Update the bar text immediately (so dragging feels live);
// debounce the snapshot fetch so a fast drag doesn't fire a request per pixel.
function scrubTo(i) {
  REPLAY_I = Math.max(0, Math.min(+i, REPLAY_POINTS.length - 1));
  updateReplayBar();
  clearTimeout(_scrubTimer);
  _scrubTimer = setTimeout(loadReplayState, 110);
}
function scrubStep(d) { scrubTo(REPLAY_I + d); }

function exitReplay() {
  REPLAY_MODE = false; REPLAY_KEY = null; REPLAY_SNAPSHOT = null; REPLAY_POINTS = []; REPLAY_I = 0;
  REPLAY_EDITS = null;                          // discard the ephemeral edits
  ROUTE_ORDER = REPLAY_SAVED_ORDER; REPLAY_SAVED_ORDER = null;   // restore the live route order
  renderReplayBar();
  if (LAST) renderAll(curData());                 // back to the live snapshot
  _archRepaint();
  refresh();                                       // resume live polling now
}

// Build the replay banner once (on enter/exit) so the range element stays stable while
// dragging; updateReplayBar() refreshes only the position/time/label text on each scrub.
function renderReplayBar() {
  const bar = $("replaybar"); if (!bar) return;
  const root = document.documentElement.style;
  if (!REPLAY_MODE) { bar.classList.add("hide"); bar.innerHTML = ""; root.setProperty("--replay-h", "0px"); return; }
  const n = REPLAY_POINTS.length, sess = (REPLAY_KEY || "").split("|")[0];
  bar.classList.remove("hide");
  // Two rows: the fixed-width slider controls on top, the variable-length checkpoint
  // time + event label on their own line below (left-aligned under the session time) so
  // the slider doesn't resize as you scrub. The former "REPLAY" badge is now the Exit
  // button (leftmost), vertically centered across both rows.
  bar.innerHTML = `<button class="rb-exit" onclick="exitReplay()" title="Return to live data">Exit replay</button>
    <span class="rb-body">
      <span class="rb-top">
        <span class="rb-sess">${esc(fmtWhen(sess))}</span>
        <button class="rb-step" onclick="scrubStep(-1)" title="previous checkpoint">◀</button>
        <input id="rb-scrub" class="rb-scrub" type="range" min="0" max="${Math.max(0, n - 1)}"
          value="${REPLAY_I}" oninput="scrubTo(this.value)">
        <button class="rb-step" onclick="scrubStep(1)" title="next checkpoint">▶</button>
        <span id="rb-pos" class="rb-pos"></span>
      </span>
      <span class="rb-info">
        <span id="rb-when" class="rb-when"></span>
        <span id="rb-label" class="rb-label"></span>
      </span>
    </span>`;
  root.setProperty("--replay-h", bar.offsetHeight + "px");  // archive panel subtracts this
  updateReplayBar();
}
function updateReplayBar() {
  const n = REPLAY_POINTS.length, p = REPLAY_POINTS[REPLAY_I] || {};
  const pos = $("rb-pos"), when = $("rb-when"), label = $("rb-label"), scrub = $("rb-scrub");
  if (pos) pos.textContent = `${REPLAY_I + 1}/${n}`;
  if (when) when.textContent = p.ts ? fmtWhen(p.ts) : "";
  if (label) label.textContent = p.label || "";
  if (scrub && +scrub.value !== REPLAY_I) scrub.value = REPLAY_I;  // keep slider synced for ◀/▶
}

// ---- live stream ---- //
// The tracker pushes the full snapshot over SSE whenever the log changes (real-time, no
// polling). The open connection also tells the server a dashboard is attached, so the
// tracker stays alive while this tab is open and shuts itself down only once the last tab
// closes. Shutdown is the server's job now, so this tab never self-closes; on a dropped
// connection we just show a passive banner and let EventSource auto-reconnect (which also
// reattaches silently when the tracker is restarted).

function showDisconnect(msg) {
  let el = $("dcbanner");
  if (!el) {
    el = document.createElement("div");
    el.id = "dcbanner";
    el.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:9999;" +
      "background:#5a1d1d;color:#f4dada;font:600 13px/1.4 system-ui,sans-serif;" +
      "text-align:center;padding:6px 12px";
    document.body.appendChild(el);
  }
  el.textContent = msg || "Tracker disconnected — reconnecting…";
  el.style.display = "block";
}
function hideDisconnect() {
  const el = $("dcbanner");
  if (el) el.style.display = "none";
}

// ---- update-available banner (tracker owns updating; this is the prompt) ----
// The snapshot carries `update` = {available,current,latest,compare_url,mode}. In prompt
// mode a new build shows this bar; Update now POSTs to apply (the tracker resets + restarts
// and the asset-hash reload swaps the page), View changes opens the GitHub compare, Dismiss
// hides it (the server won't re-offer that commit). auto/off never show a banner.
let _updBusy = false;
function renderUpdateBar(u) {
  const el = $("updatebar");
  if (!el) return;
  if (!u || !u.available) { el.classList.add("hide"); el.innerHTML = ""; _updBusy = false; return; }
  const view = u.compare_url
    ? `<button class="sp-btn" onclick="window.open('${esc(u.compare_url)}','_blank','noopener')">View changes</button>`
    : "";
  el.innerHTML =
    `<span class="ub-msg">⟳ New build available <code>${esc(u.current || "?")}</code> → ` +
    `<code>${esc(u.latest || "?")}</code></span>` +
    `<span class="ub-actions"><button class="sp-btn primary" onclick="applyUpdate(this)">Update now</button>` +
    `${view}<button class="sp-btn" onclick="dismissUpdate()">Dismiss</button></span>`;
  el.classList.remove("hide");
}
async function applyUpdate(btn) {
  if (_updBusy) return;
  _updBusy = true;
  if (btn) { btn.disabled = true; btn.textContent = "Updating…"; }
  try {
    await postJSON("/api/update/apply");
    // The tracker is restarting; its asset-hash bump reloads this tab into the new build.
    // Leave the button disabled until that happens.
  } catch (e) {
    _updBusy = false;
    if (btn) { btn.disabled = false; btn.textContent = "Update now"; }
    alert("Update failed: " + e);
  }
}
async function dismissUpdate() {
  try { await postJSON("/api/update/dismiss"); } catch (_) { /* best-effort */ }
  $("updatebar").classList.add("hide");
}

// ---- transient toast notifications ----
function toast(msg, kind) {
  const host = $("toaster");
  if (!host) return;
  const t = document.createElement("div");
  t.className = "toast" + (kind ? " " + kind : "");
  t.textContent = msg;
  host.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));   // trigger the enter transition
  const kill = () => { t.classList.remove("show"); setTimeout(() => t.remove(), 300); };
  t.onclick = kill;
  setTimeout(kill, 7000);
}

// Always announce a completed update. app_version is the running build's git hash; when it
// changes from the one this browser last saw (a restart re-execed into new code), an update
// just landed — covers every path (banner, Check now, auto, settings-change). localStorage
// dedupes across the reload and across tabs; the first load just seeds it (no toast).
function notifyIfUpdated(v) {
  if (!v) return;
  const k = "starlogger_build", prev = localStorage.getItem(k);
  if (prev && prev !== v) toast(`Update complete — now on build ${v} ✓`, "ok");
  localStorage.setItem(k, v);
}

// ---- jukebox: extract + play the game soundtrack decoded from the p4k ---- //
// The tab is lazy-built on first activation (initJukebox). Extraction is a one-time, ~2.6 GB
// server-side decode kicked by the Extract button (POST /api/music/extract); its progress
// rides the SSE snapshot's `music` field (jukeApplyMusicState), so there's no polling. Tracks
// have no names in the shipped soundbanks — only hashed ids — so a row is length + #id.
let JUKE_BUILT = false;       // panel skeleton injected?
let JUKE_TRACKS = [];         // manifest rows {id, file, duration, size}, longest-first
let JUKE_CUR = null;          // id of the track loaded in the player
let JUKE_PHASE = null;        // last-seen extraction phase (to catch the extracting->done edge)

function jukeFmt(sec) {
  if (sec == null) return "—";
  const s = Math.round(sec), m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}

function initJukebox() {
  if (!JUKE_BUILT) {
    setHTML("jukebox",
      `<div class="juke">
        <div class="juke-bar">
          <div class="juke-extract">
            <button class="sp-btn" id="jukeExtractBtn">Extract music</button>
            <span class="sp-note" id="jukeExtractMsg"></span>
          </div>
          <label class="juke-filter" title="Hide tracks shorter than this">Min length
            <input type="range" id="jukeMin" min="0" max="600" step="15" value="0">
            <span id="jukeMinLbl" class="juke-minlbl">0:00</span>
          </label>
        </div>
        <ul class="juke-list" id="jukeList"></ul>
        <div class="juke-player">
          <div class="juke-now" id="jukeNow">Nothing playing</div>
          <div class="juke-trans">
            <button class="juke-nav" id="jukePrev" title="Previous">⏮</button>
            <button class="juke-nav" id="jukeNext" title="Next">⏭</button>
            <audio id="jukeAudio" controls preload="none"></audio>
          </div>
        </div>
      </div>`);
    $("jukeExtractBtn").onclick = jukeExtract;
    $("jukeMin").oninput = jukeApplyFilter;
    $("jukePrev").onclick = () => jukeStep(-1);
    $("jukeNext").onclick = () => jukeStep(1);
    $("jukeAudio").onended = () => jukeStep(1);
    JUKE_BUILT = true;
    if (LAST && LAST.music) jukeApplyMusicState(LAST.music);  // reflect an in-flight extraction
  }
  jukeLoad();
}

async function jukeLoad() {
  try {
    const d = await getJSON("/api/music");
    JUKE_TRACKS = (d && d.tracks) || [];
  } catch (_) {
    JUKE_TRACKS = [];
  }
  renderJukeList();
}

function renderJukeList() {
  const list = $("jukeList");
  if (!list) return;
  if (!JUKE_TRACKS.length) {
    list.innerHTML = `<li class="juke-empty">No music extracted yet — click <b>Extract music</b> to decode the soundtrack from your game files.</li>`;
    return;
  }
  list.innerHTML = JUKE_TRACKS.map((t, i) =>
    `<li class="juke-row" data-id="${esc(t.id)}" data-dur="${t.duration || 0}" data-file="${esc(t.file)}">` +
    `<span class="juke-num">${i + 1}</span>` +
    `<span class="juke-dur">${jukeFmt(t.duration)}</span>` +
    `<span class="juke-id">#${esc(t.id)}</span></li>`).join("");
  list.querySelectorAll(".juke-row").forEach(r => {
    r.onclick = () => jukePlay(r.dataset.id);
  });
  jukeApplyFilter();
  if (JUKE_CUR) _jukeHighlight(JUKE_CUR);
}

function jukeApplyFilter() {
  const min = +($("jukeMin")?.value || 0);
  const lbl = $("jukeMinLbl");
  if (lbl) lbl.textContent = jukeFmt(min);
  let shown = 0;
  $("jukeList")?.querySelectorAll(".juke-row").forEach(r => {
    const hide = (+r.dataset.dur) < min;
    r.classList.toggle("hide", hide);
    if (!hide) shown++;
  });
}

function jukePlay(id) {
  const row = $("jukeList")?.querySelector(`.juke-row[data-id="${CSS.escape(id)}"]`);
  if (!row) return;
  const audio = $("jukeAudio");
  audio.src = "/music/" + encodeURIComponent(row.dataset.file);
  audio.play().catch(() => {});   // autoplay may be blocked until a gesture; the click counts
  JUKE_CUR = id;
  _jukeHighlight(id);
  const now = $("jukeNow");
  if (now) now.textContent = `Track #${id} · ${jukeFmt(+row.dataset.dur)}`;
}

function _jukeHighlight(id) {
  $("jukeList")?.querySelectorAll(".juke-row").forEach(r =>
    r.classList.toggle("playing", r.dataset.id === id));
}

// Step to the previous/next VISIBLE track (filter-aware); auto-advance (audio 'ended') reuses
// this with dir=+1 and simply stops at the end of the list.
function jukeStep(dir) {
  const rows = [...($("jukeList")?.querySelectorAll(".juke-row:not(.hide)") || [])];
  if (!rows.length) return;
  let i = rows.findIndex(r => r.dataset.id === JUKE_CUR);
  i = i < 0 ? (dir > 0 ? 0 : rows.length - 1) : i + dir;
  if (i < 0 || i >= rows.length) return;   // off either end → stop
  jukePlay(rows[i].dataset.id);
}

async function jukeExtract() {
  const btn = $("jukeExtractBtn"), msg = $("jukeExtractMsg");
  if (!btn) return;
  btn.disabled = true; msg.textContent = "Starting…"; msg.classList.remove("err");
  try {
    const r = await postJSON("/api/music/extract");
    if (r && r.ok === false) {
      msg.textContent = r.error || "Music extraction isn't available on this install.";
      msg.classList.add("err"); btn.disabled = false;
    }
    // success path: progress + completion arrive via the SSE snapshot (jukeApplyMusicState)
  } catch (e) {
    msg.textContent = "Couldn't start extraction."; msg.classList.add("err"); btn.disabled = false;
  }
}

// Reflect the server's extraction state (pushed in every snapshot) onto the jukebox controls.
function jukeApplyMusicState(m) {
  const wasExtracting = JUKE_PHASE === "extracting";
  JUKE_PHASE = m.phase;
  const btn = $("jukeExtractBtn"), msg = $("jukeExtractMsg");
  if (!btn || !msg) return;   // panel not built yet — phase captured for when it is
  msg.classList.remove("err");
  if (m.phase === "extracting") {
    btn.disabled = true;
    msg.textContent = m.total ? `Extracting ${m.done}/${m.total}…` : "Decoding soundbank…";
  } else if (m.phase === "error") {
    btn.disabled = false;
    msg.textContent = m.error || "Extraction failed."; msg.classList.add("err");
  } else if (m.phase === "done") {
    btn.disabled = false;
    msg.textContent = m.total ? `${m.total} tracks ✓` : "Done ✓";
    if (wasExtracting) jukeLoad();   // finished just now → pull the fresh track list in
  } else {
    btn.disabled = false;
    msg.textContent = "";
  }
}

// Apply a freshly-received live snapshot — from the SSE push or a manual refresh().
function applySnapshot(d) {
  LAST = d;
  renderUpdateBar(d.update);   // update banner is global — show it even in replay mode
  if (d.music) jukeApplyMusicState(d.music);   // push extraction progress to the jukebox (no polling)
  notifyIfUpdated(d.app_version);   // toast once when the running build changed under us
  if (REPLAY_MODE) return;   // keep LAST fresh underneath; the replay view owns the screen
  // Skip the whole render pass when the snapshot is byte-identical to the last one
  // rendered: setHTML already no-ops the DOM, this also skips building the HTML strings +
  // cargo packing. User interactions call renderAll() directly (unguarded), so an open
  // editor/drag still repaints immediately.
  const sig = JSON.stringify(d);
  if (sig !== _lastRenderSig) {
    _lastRenderSig = sig;
    renderAll(curData());                  // render every tab from the live snapshot
  }
  if (TAB === "archive") loadSessions();  // keep archive fresh while viewing
  const last = d.last_event_ts ? ("log " + d.last_event_ts) : "";
  // App build: the short git hash of the running code (logged-in state already
  // lives in the header status pill, so the footer shows the version instead).
  const build = "build " + esc(d.app_version || "?");
  // RSI's patch-notes page is what the launcher links to pre-update (then hides) —
  // make the parsed game version a link back to it. Index URL always lists the
  // current LIVE build first, so it needs no per-patch upkeep.
  const ver = d.game_version
    ? ` · game <a class="pn-link" href="https://robertsspaceindustries.com/en/patch-notes" target="_blank" rel="noopener">${esc(d.game_version)} ↗</a>`
    : "";
  $("foot").innerHTML = `synced ${esc(new Date().toLocaleTimeString())} · ${build}${ver} · ${esc(last)} · cargo db @ ${esc(d.ship_cargo_version || "?")}`;
}

// One-shot pull used by action handlers to reflect a change immediately. (The mutating
// POSTs also bump the server version, so other open tabs update via the stream; this just
// gives the acting tab an instant repaint without waiting for the round-trip push.)
async function refresh() {
  try {
    applySnapshot(await getJSON("/api/state"));
  } catch (e) {
    $("foot").textContent = `waiting for tracker… (${e})`;
  }
}

let _es = null;            // current EventSource, so we can tell a live one from a dead one
let _reconnectTimer = null;

function connectStream() {
  if (_es) { try { _es.close(); } catch (_) {} }   // drop any stale handle before reopening
  const es = _es = new EventSource("/api/stream");
  es.onopen = () => hideDisconnect();
  // Named `meta` event (NOT onmessage) carries the served-asset hash. First connect
  // records the baseline; a reconnect with a different hash means a new build replaced
  // the tracker on this port -> reload to run the new code. (The active tab survives via
  // location.hash.) A server-only relaunch keeps the same hash, so the reconnect is silent.
  es.addEventListener("meta", (e) => {
    let m; try { m = JSON.parse(e.data); } catch (_) { return; }
    if (!m || !m.assets) return;
    if (ASSET_VER === null) { ASSET_VER = m.assets; return; }
    if (m.assets !== ASSET_VER) location.reload();
  });
  es.onmessage = (e) => {
    hideDisconnect();
    try { applySnapshot(JSON.parse(e.data)); } catch (_) { /* ignore a malformed frame */ }
  };
  es.onerror = () => {
    showDisconnect();
    // EventSource auto-reconnects on a transient drop (readyState stays CONNECTING). But
    // once the browser marks it CLOSED -- which mobile Firefox/Chrome do when the tab is
    // backgrounded and the socket is reaped -- it never retries on its own, so reopen it.
    if (es.readyState === EventSource.CLOSED) {
      clearTimeout(_reconnectTimer);
      _reconnectTimer = setTimeout(ensureStream, 2000);
    }
  };
}

// Reopen the stream if it isn't currently OPEN or CONNECTING. Cheap no-op when it's healthy.
function ensureStream() {
  if (_es && _es.readyState !== EventSource.CLOSED) return;
  connectStream();
}

// A backgrounded tab can have its SSE socket killed without a usable error event, leaving a
// stale/closed stream when you return. Re-establish it (and pull a fresh snapshot so the view
// isn't stale) the moment the tab is shown again or connectivity returns.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { ensureStream(); refresh(); }
});
window.addEventListener("online", ensureStream);
window.addEventListener("pageshow", ensureStream);

connectStream();
loadShipList();

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

// ============================================================================ //
// Mining tab — RS (radar signature) + composition tools. Self-contained and
// independent of the live /api/state poll: it reads the p4k-derived mineables
// catalog via /api/{rock-lookup,rock-decompose,mineral-lookup,mineral-index,
// mining-plan}. All three sub-tools (and their own #mres-<sub> results) are built
// once; switching sub-tabs only toggles which is visible, so each keeps its inputs,
// results, and scroll. Submitting a query repaints just that sub's #mres-<sub>.
// ============================================================================ //
let MINING_SUB = "identify";       // identify | find | plan
let MINING_MINERALS = null;        // cached mineral names for the autocomplete
let MINING_BLUEPRINTS = null;      // cached {name, category} catalog for the picker
let MINING_RS = null;              // cached base RS values, seeding Identify's prediction
let IDENTIFY_HISTORY = [];         // recent valid readings {rs, summary}, newest first
const IDENTIFY_HIST_MAX = 8;       // how many recent readings to keep on screen
let MINING_INIT = false;

async function initMining() {
  if (!MINING_INIT) {
    MINING_INIT = true;
    const grab = async (url, key) => {
      try { return (await getJSON(url))[key] || []; }
      catch (e) { return []; }
    };
    [MINING_MINERALS, MINING_BLUEPRINTS, MINING_RS] = await Promise.all([
      grab("/api/minerals", "minerals"), grab("/api/blueprints", "blueprints"),
      grab("/api/rock-signatures", "signatures")]);
  }
  // Build once, only after the catalogs have loaded; switching subs then just toggles.
  if (MINING_BLUEPRINTS !== null && !$("msub-identify")) renderMiningShell();
}
// Switch sub-tabs by toggling visibility — never rebuild, so each sub keeps its state.
function miningSub(sub) {
  MINING_SUB = sub;
  if (!$("msub-" + sub)) { renderMiningShell(); return; }
  document.querySelectorAll("#mining .arch-tab").forEach(b => b.classList.toggle("active", b.dataset.sub === sub));
  document.querySelectorAll("#mining .msub").forEach(el => el.classList.toggle("hide", el.id !== "msub-" + sub));
}
// The active sub's results container — every tool repaints into its own #mres-<sub>.
const mres = () => "mres-" + MINING_SUB;

const _pct = (x) => (x == null ? "?" : Math.round(x));
const _chance = (p) => (p == null ? "" : Math.round(p * 100) + "%");

function renderMiningShell() {
  const subs = [["identify", "Identify rock", identifyToolHtml], ["find", "Find mineral", findToolHtml],
                ["plan", "Blueprint plan", planToolHtml]];
  // Same underlined sub-tab strip as the Archive tab, with a data-sub on each button so
  // miningSub() can toggle .active without a rebuild.
  const bar = tabBar(subs, MINING_SUB, "miningSub", { attr: k => `data-sub="${k}"` });
  // Each sub-tool + its own results live in a .msub section; only the active one shows.
  const sections = subs.map(([k, , toolFn]) =>
    `<div class="msub${MINING_SUB === k ? "" : " hide"}" id="msub-${k}">${toolFn()}<div id="mres-${k}" class="mres"></div></div>`).join("");
  const datalist = `<datalist id="dl_mineral">${(MINING_MINERALS || [])
      .map(m => `<option value="${esc(m)}">`).join("")}</datalist>`;
  setHTML("mining", `${datalist}<div class="mining">
    ${bar}
    ${sections}
  </div>`);
}

// small shared bits ---------------------------------------------------------- //
function elBadge(e) {
  return `<span class="mn-el"><b>${esc(e.element)}</b>` +
    ` <span class="mn-pct">${_pct(e.min_pct)}–${_pct(e.max_pct)}%</span>` +
    (e.probability != null ? ` <span class="mn-prob">${_chance(e.probability)}</span>` : "") + `</span>`;
}
// Dedupe a rock list's composition to the distinct possible minerals (keep the
// richest occurrence), so an ambiguous RS shows "what might be in there".
function mineralUnion(rocks) {
  const m = new Map();
  for (const r of rocks || []) for (const e of r.composition || []) {
    const cur = m.get(e.element);
    if (!cur || (e.probability || 0) > (cur.probability || 0)) m.set(e.element, e);
  }
  return [...m.values()].sort((a, b) => (b.probability || 0) - (a.probability || 0));
}

// Compact rock-cracking advisor line from a class's M1 mechanics (p4k); "" when absent.
// Surfaces the break-difficulty the in-game HUD doesn't show — laser power needed,
// resistance/instability, optimal-window width, mass. Uses the first rock that carries it.
function mechHtml(rocks) {
  const m = (rocks || []).map(r => r.mechanics).find(Boolean);
  if (!m) return "";
  const bits = [];
  if (m.laser_power != null) bits.push(`laser ≥${num(m.laser_power)}`);
  if (m.resistance != null) bits.push(`resistance ${m.resistance}`);
  if (m.instability != null) bits.push(`instability ${m.instability}`);
  if (m.window_size != null) bits.push(`window ${m.window_size}${m.window_max != null ? "–" + m.window_max : ""}`);
  if (m.mass != null) bits.push(`mass ${num(m.mass)}`);
  if (!bits.length) return "";
  return `<div class="mrow"><span class="mk">cracking</span>
    <div class="mels mn-dim">${esc(bits.join(" · "))}</div></div>`;
}

// ---- Identify: RS reading → rock class(es), cluster size, possible minerals ---- //
// Tuned for rapid back-to-back readings: typing a number + Enter (or Identify) shows the
// result, then clears and refocuses the box for the next reading. A strip of the last few
// readings (with their top match) stays on screen so earlier scans can be glanced at. As
// you type, the box predicts the rest from your recent readings (deposits recur while
// mining) as a selected suffix — Enter accepts it, keep typing or Esc/Backspace to override.
function identifyToolHtml() {
  return `<div class="card mtool"><h3><span>RS reading → rock</span></h3>
    <div class="mform">
      <input id="mi-rs" type="text" inputmode="numeric" autocomplete="off"
        placeholder="e.g. 9400" aria-label="Radar signature reading"
        oninput="identifyPredict(event)" onkeydown="identifyKey(event)">
      <button class="primary" onclick="miningIdentify()">Identify</button>
    </div>
    <div id="mi-hist" class="mi-hist">${identifyHistHtml()}</div>
    <p class="mhint">The radar number is <code>base RS × number of rocks</code>. RS identifies the rock
      <b>class</b>, not the exact mineral — many classes share a base, so a reading can be ambiguous.</p>
  </div>`;
}
// The recent-readings strip; chips re-run their reading when clicked.
function identifyHistHtml() {
  if (!IDENTIFY_HISTORY.length) return "";
  return `<span class="mi-hist-k">recent</span>` + IDENTIFY_HISTORY.map(h =>
    `<button class="mi-chip" onclick="identifyAgain(${h.rs})"
       title="Re-run RS ${num(h.rs)}"><b>${num(h.rs)}</b> <span>${esc(h.summary)}</span></button>`).join("");
}
// One-line gist of a reading's result, for the history chip.
function identifySummary(candidates, combos) {
  if (candidates.length) {
    const c = candidates[0];
    const deps = [...new Set(c.rocks.map(r => r.deposit_name || r.name))];
    return `${c.count}× ${deps[0]}${deps.length > 1 ? " +" + (deps.length - 1) : ""}`;
  }
  if (combos.filter(c => c.parts.length > 1).length) return "mixed cluster";
  return "no clean match";
}
function identifyAgain(rs) {
  const inp = $("mi-rs"); if (inp) inp.value = rs;
  miningIdentify();
}
// Inline prediction: while typing a prefix, complete it with a likely reading, leaving the
// guessed suffix selected. Typing replaces the selection (so the guess just refines), → /
// End accepts it natively, Enter submits, Esc/Backspace drops it. Skipped on deletes so
// editing stays free. This session's readings win (recurring deposits), then the catalog's
// base RS values seed a guess before any have been entered.
function identifyPredict(e) {
  if (e && e.inputType && e.inputType.startsWith("delete")) return;
  const inp = $("mi-rs"); if (!inp) return;
  const typed = inp.value;
  if (!typed) return;
  const pool = [...IDENTIFY_HISTORY.map(h => String(h.rs)), ...(MINING_RS || []).map(String)];
  const hit = pool.find(s => s.length > typed.length && s.startsWith(typed));
  if (hit) { inp.value = hit; inp.setSelectionRange(typed.length, hit.length); }
}
function identifyKey(e) {
  if (e.key === "Enter") { miningIdentify(); return; }
  if (e.key === "Escape") {              // drop a predicted suffix without clearing the typed part
    const inp = $("mi-rs");
    if (inp && inp.selectionStart < inp.value.length) {
      inp.value = inp.value.slice(0, inp.selectionStart);
      e.preventDefault();
    }
  }
}
async function miningIdentify() {
  const v = parseFloat(val("mi-rs"));
  if (!(v > 0)) { setHTML(mres(), `<div class="empty">Enter a positive RS reading.</div>`); return; }
  setHTML(mres(), `<div class="empty">scanning…</div>`);
  try {
    const [look, dec] = await Promise.all([
      fetch(`/api/rock-lookup?rs=${v}`).then(r => r.json()),
      fetch(`/api/rock-decompose?rs=${v}`).then(r => r.json()),
    ]);
    const candidates = look.candidates || [], combos = dec.combos || [];
    // Only a valid reading (matches one or more rocks) is kept in the strip — a miss isn't
    // recorded. A reading already in the history updates in place (re-running a chip mustn't
    // reorder it); a new one is prepended (newest first).
    const ok = candidates.length > 0 || combos.some(c => c.parts.length > 1);
    if (ok) {
      const entry = { rs: v, summary: identifySummary(candidates, combos) };
      const at = IDENTIFY_HISTORY.findIndex(h => h.rs === v);
      if (at >= 0) IDENTIFY_HISTORY[at] = entry;
      else IDENTIFY_HISTORY = [entry, ...IDENTIFY_HISTORY].slice(0, IDENTIFY_HIST_MAX);
      setHTML("mi-hist", identifyHistHtml());
    }
    // Clear + refocus so the next reading can be typed straight away.
    const inp = $("mi-rs"); if (inp) { inp.value = ""; inp.focus(); }
    setHTML(mres(), identifyResultHtml(v, candidates, combos));
  } catch (e) { setHTML(mres(), `<div class="empty">lookup failed</div>`); }
}
function identifyResultHtml(v, candidates, combos) {
  if (!candidates.length && !combos.length)
    return `<div class="empty">Nothing reads RS ${num(v)} as a clean cluster.</div>`;
  let html = "";
  if (candidates.length) {
    html += `<div class="mres-h">Single-class readings</div>`;
    html += candidates.map(c => {
      const deps = [...new Set(c.rocks.map(r => r.deposit_name || r.name))];
      const minerals = mineralUnion(c.rocks);
      const extra = deps.length > 1 ? ` <span class="mn-dim">+${deps.length - 1} more</span>` : "";
      return `<div class="card mcand">
        <h3><span>${c.count} × <b>${esc(deps[0])}</b>${extra}</span>
            <span class="scu">RS ${num(c.base_rs)}${c.count > 1 ? ` × ${c.count}` : ""}</span></h3>
        <div class="mcand-body">
          ${deps.length > 1 ? `<div class="mrow"><span class="mk">reads as</span>
             <div class="mels">${deps.map(d => tag(d)).join(" ")}</div></div>` : ""}
          <div class="mrow"><span class="mk">possible minerals</span>
            <div class="mels">${minerals.map(elBadge).join("") || '<span class="mn-dim">—</span>'}</div></div>
          ${mechHtml(c.rocks)}
        </div></div>`;
    }).join("");
  }
  const mixed = combos.filter(c => c.parts.length > 1);
  if (mixed.length) {
    html += `<div class="mres-h">Mixed-cluster interpretations</div><div class="card">` + logTable(
      th("Cluster") + th("Total RS", true) + th("Rocks", true),
      mixed.slice(0, 12).map(c =>
        `<tr><td>${c.parts.map(p => `${p.count}× ${esc(p.names[0] || ("RS " + p.base_rs))}`).join(" + ")}</td>` +
        `<td class="lt-num">${num(c.total)}</td><td class="lt-num">${c.count}</td></tr>`).join(""),
      "") + `</div>`;
  }
  return html;
}

// ---- Find: mineral → RS to scan for + ranked source rocks (+ browse all) ---- //
function findToolHtml() {
  return `<div class="card mtool"><h3><span>Mineral → where to mine</span></h3>
    <div class="mform">
      <input id="mf-name" list="dl_mineral" placeholder="e.g. Bexalite" autocomplete="off"
        aria-label="Mineral name" onkeydown="if(event.key==='Enter')miningFind()">
      <button class="primary" onclick="miningFind()">Find</button>
      <button onclick="miningIndex()">Browse all</button>
    </div>
    <p class="mhint">Shows the RS value(s) to scan for and the richest source rocks, ranked by
      probability × yield.</p>
  </div>`;
}
async function miningFind() {
  const name = val("mf-name").trim();
  if (!name) { setHTML(mres(), `<div class="empty">Enter or pick a mineral.</div>`); return; }
  setHTML(mres(), `<div class="empty">searching…</div>`);
  try {
    const r = await fetch(`/api/mineral-lookup?name=${encodeURIComponent(name)}`).then(x => x.json());
    setHTML(mres(), findResultHtml(r));
  } catch (e) { setHTML(mres(), `<div class="empty">lookup failed</div>`); }
}
function findResultHtml(r) {
  if (!r.rocks || !r.rocks.length) return `<div class="empty">No rock yields “${esc(r.mineral)}”.</div>`;
  const sigs = (r.signatures || []).map(s => `<span class="mscan-rs">${num(s)}</span>`).join("");
  const rows = r.rocks.map(x => `<tr>
    <td class="lt-num">${num(x.rs)}</td><td>${esc(x.name)}</td>
    <td class="lt-num">${_pct(x.min_pct)}–${_pct(x.max_pct)}%</td>
    <td class="lt-num">${_chance(x.probability)}</td><td class="lt-num">${x.score}</td></tr>`).join("");
  return `<div class="card">
    <div class="mscan"><span class="mscan-k">Scan for</span>
      <div class="mscan-vals">${sigs || '<span class="mn-dim">—</span>'}</div></div>
    ${logTable(
      th("RS", true, "Radar signature a single rock of this type reads") +
      th("Rock", false, "The mineable rock / deposit type") +
      th("Yield %", true, `Percentage of ${esc(r.mineral)} in the rock (min–max)`) +
      th("Chance", true, "Probability a rock of this type actually contains it") +
      th("Score", true, "Source ranking = probability × yield (higher is a better source)"),
      rows, "")}
  </div>`;
}
async function miningIndex() {
  setHTML(mres(), `<div class="empty">loading…</div>`);
  try {
    const r = await fetch("/api/mineral-index").then(x => x.json());
    setHTML(mres(), indexResultHtml(r.minerals || []));
  } catch (e) { setHTML(mres(), `<div class="empty">load failed</div>`); }
}
function indexResultHtml(minerals) {
  if (!minerals.length) return `<div class="empty">No mineral data.</div>`;
  const rows = minerals.map(m => `<tr>
    <td><b>${esc(m.mineral)}</b></td>
    <td>${(m.signatures || []).slice(0, 8).map(num).join(", ")}</td>
    <td>${m.rocks.slice(0, 4).map(x => esc(x.name)).join("; ")}${m.rocks.length > 4 ? ` <span class="mn-dim">…+${m.rocks.length - 4}</span>` : ""}</td>
  </tr>`).join("");
  return `<div class="card"><h3><span>All minerals → source rocks</span><span class="scu">${minerals.length}</span></h3>` +
    logTable(
      th("Mineral", false, "The refined mineral") +
      th("RS to scan", false, "Radar signature value(s) whose rocks can contain it") +
      th("Best sources", false, "The richest source rocks for this mineral"),
      rows, "") + `</div>`;
}

// ---- Plan: blueprint → deposit coverage + sources ---- //
// A searchable picker whose options are grouped into sections: the server tags each
// blueprint with its main {type} and a {detail} (component size, weapon model line, FPS
// weapon type, or armour set), and we lay those out as rule-separated sections with a
// sticky header carrying the full "type · detail". Selecting an item plans — no button.
const _BP_TYPE_ORDER = ["Vehicle Component", "Vehicle Weapons", "FPS Weapons", "FPS Armours"];
// Group the catalog into ordered sections keyed by (type, detail); within a section items
// are ordered by size then name (so a weapon model line reads S1→S6).
function _bpSections() {
  const byKey = new Map();
  for (const b of MINING_BLUEPRINTS || []) {
    const key = b.type + "\u0000" + (b.detail || "");
    if (!byKey.has(key)) byKey.set(key, { type: b.type, detail: b.detail || "", items: [] });
    byKey.get(key).items.push(b);
  }
  const ord = (t) => { const i = _BP_TYPE_ORDER.indexOf(t); return i < 0 ? 99 : i; };
  return [...byKey.values()]
    .sort((a, b) => ord(a.type) - ord(b.type) || a.type.localeCompare(b.type) ||
      a.detail.localeCompare(b.detail))
    .map(s => {
      s.items.sort((x, y) => (x.size ?? 99) - (y.size ?? 99) || x.name.localeCompare(y.name));
      return s;
    });
}
function blueprintMenuHtml() {
  return _bpSections().map(s => {
    const items = s.items.map(b => {
      // Vehicle weapons span sizes within a model line — tag each with its size, shown
      // leading the name (left) so the column of sizes reads at a glance.
      const sz = s.type === "Vehicle Weapons" && b.size != null ? `<span class="bp-dd-sz">S${b.size}</span>` : "";
      return `<div class="bp-dd-item" data-search="${esc(b.name.toLowerCase())}"
         onclick="bpPick(this.dataset.name)" data-name="${esc(b.name)}">${sz}<span>${esc(b.name)}</span></div>`;
    }).join("");
    const label = `<span class="bp-dd-type">${esc(s.type)}</span>` +
      (s.detail ? ` <span class="bp-dd-detail">${esc(s.detail)}</span>` : "");
    return `<div class="bp-dd-sec">
      <div class="bp-dd-grp"><span class="bp-dd-lbl">${label}</span></div>${items}</div>`;
  }).join("");
}
function planToolHtml() {
  return `<div class="card mtool"><h3><span>Blueprint mining plan</span></h3>
    <div class="mform">
      <div class="bp-dd">
        <input id="mp-bp" autocomplete="off" aria-label="Search blueprints"
          placeholder="Search blueprints by name…"
          oninput="bpFilter(this.value)" onfocus="bpOpen(true)"
          onblur="bpOpen(false)" onkeydown="bpKey(event)">
        <div id="bp-dd-list" class="bp-dd-list" onmousedown="event.preventDefault()">${blueprintMenuHtml()}</div>
      </div>
    </div>
    <p class="mhint">Pick a blueprint — grouped by type and size — to pull its required minerals straight
      from the game files. Deposits are ranked by how many of the ingredients each can yield.</p>
  </div>`;
}
function bpOpen(show) {
  const el = $("bp-dd-list"); if (!el) return;
  el.classList.toggle("open", !!show);
  // The card clips descendants via clip-path; drop it while the menu is open so the
  // dropdown can overflow past the card edge.
  const card = el.closest(".card"); if (card) card.classList.toggle("dd-open", !!show);
}
function bpPick(name) {
  const inp = $("mp-bp"); if (inp) inp.value = name;
  bpOpen(false);
  miningPlanFromBlueprint(name);
}
// Filter items by a case-insensitive substring; hide whole sections with no visible items.
function bpFilter(q) {
  const list = $("bp-dd-list"); if (!list) return;
  list.classList.add("open");
  const needle = (q || "").trim().toLowerCase();
  for (const sec of list.querySelectorAll(".bp-dd-sec")) {
    let any = false;
    for (const it of sec.querySelectorAll(".bp-dd-item")) {
      const show = !needle || it.dataset.search.includes(needle);
      it.style.display = show ? "" : "none";
      if (show) any = true;
    }
    sec.classList.toggle("hide", !any);
  }
}
function bpKey(e) {
  if (e.key === "Escape") { bpOpen(false); return; }
  if (e.key !== "Enter") return;
  const first = [...($("bp-dd-list") || {}).querySelectorAll?.(".bp-dd-item") || []]
    .find(it => it.style.display !== "none");
  if (first) bpPick(first.dataset.name);
}
const _miningDur = (s) => {
  s = Math.round(s || 0); const m = Math.floor(s / 60), sec = s % 60;
  return m ? `${m}m${sec ? " " + sec + "s" : ""}` : `${sec}s`;
};
async function miningPlanFromBlueprint(name) {
  name = (name || val("mp-bp")).trim();
  if (!name) { setHTML(mres(), `<div class="empty">Pick a blueprint.</div>`); return; }
  setHTML(mres(), `<div class="empty">loading blueprint…</div>`);
  try {
    const bp = await fetch(`/api/blueprint?name=${encodeURIComponent(name)}`).then(r => r.json());
    if (bp.ok === false) { setHTML(mres(), `<div class="empty">No blueprint “${esc(name)}”.</div>`); return; }
    const plan = await fetch("/api/mining-plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minerals: bp.minerals || [] }),
    }).then(r => r.json());
    setHTML(mres(), recipeHtml(bp) + planResultHtml(plan));
  } catch (e) { setHTML(mres(), `<div class="empty">plan failed</div>`); }
}
function recipeHtml(bp) {
  const meta = [esc(bp.category || ""), bp.craft_seconds ? _miningDur(bp.craft_seconds) : ""].filter(Boolean).join(" · ");
  const rows = (bp.requirements || []).map(r => `<tr>
    <td>${esc(r.slot || "")}</td><td><b>${esc(r.resource)}</b></td>
    <td class="lt-num">${r.scu} SCU</td>
    <td class="lt-num">${r.min_quality > 0 ? "Q≥" + r.min_quality : "—"}</td></tr>`).join("");
  return `<div class="card"><h3><span>${esc(bp.name)}</span><span class="scu">${meta}</span></h3>
    ${logTable(
      th("Slot", false, "The recipe slot this material fills") +
      th("Material", false, "The mineral or resource the slot requires") +
      th("Qty", true, "Amount needed, in SCU") +
      th("Min quality", true, "Minimum refined quality the material must meet (— = any)"),
      rows, "No materials.")}
  </div>`;
}
function planResultHtml(r) {
  const targets = r.targets || [];
  if (!targets.length) return `<div class="empty">No minerals given.</div>`;
  const covRows = (r.coverage || []).slice(0, 15).map(c => `<tr>
    <td><b>${esc(c.deposit)}</b></td>
    <td class="lt-num">${c.n_covers}/${targets.length}</td>
    <td>${c.covers.map(x => tag(x)).join(" ")}</td>
    <td>${(c.signatures || []).map(num).join(", ")}</td></tr>`).join("");
  const srcs = (r.per_mineral || []).map(p => {
    const best = (p.rocks || []).slice(0, 3).map(x =>
      `${esc(x.name)} <span class="mn-dim">(RS ${num(x.rs)}${x.probability != null ? ", " + _chance(x.probability) : ""})</span>`).join("<br>");
    return `<div class="mrow"><span class="mk">${esc(p.mineral)}</span>
      <div>${best || '<span class="mn-dim">no source found</span>'}</div></div>`;
  }).join("");
  return `<div class="card"><h3><span>Best deposits — by coverage</span></h3>
      ${logTable(
        th("Deposit", false, "A rock deposit / cluster type you can mine") +
        th("Covers", true, "How many of the blueprint's ingredients this deposit can yield") +
        th("Ingredients", false, "Which of the wanted minerals it covers") +
        th("RS", false, "Radar signature value(s) to scan for to find this deposit"),
        covRows, "No deposit yields any of these minerals.")}
    </div>
    <div class="card"><h3><span>Per-ingredient sources</span></h3><div class="mplan-srcs">${srcs}</div></div>`;
}

// ---- deep-link resolution (runs last, once all tab state + functions exist) ---- //
// Honour the URL hash, including legacy ones from before the Cargo/Plan merge: #loading /
// #unloading → Cargo (pre-selecting the phase); #routes / #grid → Plan. Must run after the
// whole module is initialised — activating #archive/#mining calls loadSessions()/initMining(),
// which touch state declared far below the nav setup.
const LEGACY_HASH = { loading: ["cargo", () => CARGO_SUB = "pickup"], unloading: ["cargo", () => CARGO_SUB = "dropoff"],
                      routes: ["plan", () => {}], grid: ["plan", () => {}] };
const _hash = location.hash.slice(1);
if (LEGACY_HASH[_hash]) { LEGACY_HASH[_hash][1](); activateTab(LEGACY_HASH[_hash][0]); }
else if (TABS.includes(_hash)) activateTab(_hash);
