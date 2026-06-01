"use strict";

const $ = (id) => document.getElementById(id);
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

let TAB = "missions";   // Contracts is the first/default tab
let LAST = null;      // latest snapshot
let EDIT = null;      // mission_id whose editor is open
let EDIT_ZONE = null; // zoneHostId whose station-name editor is open
let SESSIONS = null;  // archived sessions
// Contract-log filters (live in the contract-log header as a compact "Hide …" bar).
// Stored in hide-semantics; mapped to the API's show-flags in sessQ().
let HIDE_NONTRADE = localStorage.getItem("hideNonTrade") === "1";   // default: show non-cargo too
let HIDE_UNFINISHED = localStorage.getItem("hideUnfinished") !== "0";  // default: hide unfinished
const sessQ = () => {
  const p = [];
  if (HIDE_NONTRADE) p.push("trade=1");          // trade-only = hide non-trade
  if (!HIDE_UNFINISHED) p.push("unfinished=1");  // include unfinished = not hiding them
  return p.length ? "?" + p.join("&") : "";
};
function toggleHideNonTrade() {
  HIDE_NONTRADE = $("fNonTrade").checked;
  localStorage.setItem("hideNonTrade", HIDE_NONTRADE ? "1" : "0");
  loadSessions();
}
function toggleHideUnfinished() {
  HIDE_UNFINISHED = $("fUnfinished").checked;
  localStorage.setItem("hideUnfinished", HIDE_UNFINISHED ? "1" : "0");
  loadSessions();
}

// Which Archive section is expanded (accordion — only one at a time).
let ARCH_OPEN = localStorage.getItem("archOpen") || "trades";
function toggleArch(key) {
  ARCH_OPEN = ARCH_OPEN === key ? "" : key;   // click the open one to collapse all
  localStorage.setItem("archOpen", ARCH_OPEN);
  setHTML("history", sessionsView(SESSIONS));
}
// One collapsible Archive section: a clickable header (always shown) + a body that
// renders only when expanded, so the open section can fill the viewport on its own.
function logSection(key, title, headSpan, body) {
  const open = ARCH_OPEN === key;
  return `<div class="card logsec logcard${open ? " open" : ""}">
    <h3 class="logsec-h" onclick="toggleArch('${key}')">
      <span class="logsec-chev">${open ? "▾" : "▸"}</span><span>${title}</span>${headSpan || ""}</h3>
    ${open ? body : ""}
  </div>`;
}

// ---- tabs (with URL-hash deep-linking) ---- //
const TABS = ["loading", "unloading", "routes", "missions", "grid", "history"];
function activateTab(name) {
  if (!TABS.includes(name)) return;
  TAB = name;
  document.querySelectorAll("#nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hide", t.id !== name));
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  if (name === "history") loadSessions();
}
document.querySelectorAll("#nav button").forEach(b => { b.onclick = () => activateTab(b.dataset.tab); });
if (TABS.includes(location.hash.slice(1))) activateTab(location.hash.slice(1));

// ---- header: status pill, ship selector, telemetry readouts, capacity gauge ---- //

// Ship catalog for the manual selector, fetched once. name -> {manufacturer,scu,groups}.
let SHIP_DB = null;
let SHIP_MENU_OPEN = false;   // combobox popup state — guards the poll from clobbering it
let GRID_HOVER = false;       // hovering a load-order row — guards the poll from wiping the hold highlight
async function loadShipList() {
  try {
    const db = await (await fetch("/api/ships", { cache: "no-store" })).json();
    SHIP_DB = db.ships || {};
    if (LAST) { syncScenarios(); renderAll(curData()); }  // repaint now that we have the catalog
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

function shipMenuHtml(filter) {
  const items = shipMatches(filter);
  let html = `<div class="shipopt clear" onmousedown="pickShip(event,'')">— clear (use detected) —</div>`;
  if (!items.length) return html + `<div class="shipopt empty">no match</div>`;
  // data-name carries the value safely (names have parens etc.); the handler reads it
  return html + items.map(it =>
    `<div class="shipopt" data-name="${esc(it.name)}" onmousedown="pickShip(event, this.dataset.name)">
       <span class="sn">${esc(it.name)}</span><span class="om">${esc(it.mfr)}</span></div>`).join("");
}

function openShipMenu() {
  const inp = $("shipSel"), menu = $("shipMenu");
  if (!inp || !menu) return;
  inp.select();
  SHIP_MENU_OPEN = true;
  menu.innerHTML = shipMenuHtml("");   // focus shows the full list
  menu.classList.add("open");
}
function filterShipMenu() {
  const inp = $("shipSel"), menu = $("shipMenu");
  if (!inp || !menu) return;
  SHIP_MENU_OPEN = true;
  menu.innerHTML = shipMenuHtml(inp.value);
  menu.classList.add("open");
}
function onShipBlur() {
  SHIP_MENU_OPEN = false;
  const menu = $("shipMenu"); if (menu) menu.classList.remove("open");
  const inp = $("shipSel"); if (inp && LAST) inp.value = LAST.ship || "";  // drop unselected typing
}
function shipKeydown(ev) {
  if (ev.key === "Escape") { ev.target.blur(); }
  else if (ev.key === "Enter") {
    ev.preventDefault();
    const m = shipMatches(ev.target.value);
    if (m.length) pickShip(ev, m[0].name);
  }
}
function pickShip(ev, name) {
  ev.preventDefault();              // (mousedown) keep focus until we act
  SHIP_MENU_OPEN = false;
  const inp = $("shipSel");
  if (inp) { inp.value = name; inp.blur(); }   // blur releases the repaint guard
  selectShip(name);
}

async function selectShip(name) {
  try {
    const r = await fetch("/api/select-ship", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ship: name || null }),
    });
    const j = await r.json();
    if (!j.ok) { alert("Couldn't set ship: " + (j.error || r.status)); return; }
  } catch (e) { alert("Couldn't set ship: " + e); return; }
  TEST_CASE = null;   // new ship → back to live data
  refresh();
}

function statusHtml(d) {
  const online = d.logged_in;
  const pill = `<span class="pill ${online ? "online" : "offline"}"><span class="dot"></span>${online ? "In Verse" : "Main Menu"}</span>`;
  const scu = d.ship_scu != null ? ` <span class="cap">${num(d.ship_scu)} SCU</span>` : "";
  if (d.ship_detected) {
    // detected ship overrides the manual pick — show it locked, no searchable box
    return pill + `<span class="ship">SHIP <b>${esc(d.ship || "—")}</b>${scu}
      <span class="ship-auto" title="detected from the game log">● detected</span></span>`;
  }
  const box = `<span class="shipbox">
    <input id="shipSel" class="shipsel" type="text" autocomplete="off" aria-label="Ship"
      placeholder="search ship…" value="${esc(d.ship || "")}"
      onfocus="openShipMenu()" oninput="filterShipMenu()" onkeydown="shipKeydown(event)" onblur="onShipBlur()">
    <div id="shipMenu" class="shipmenu"></div></span>`;
  return pill + `<span class="ship">SHIP ${box}${scu}</span>`;
}

function readoutsHtml(d) {
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
  return items.map(([k, v, cls]) =>
    `<div class="stat ${cls}"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
}

function gaugeHtml(d) {
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
  // don't repaint the status bar while the ship search box is focused or its popup
  // is open — a poll landing mid-interaction would tear it down. (The scenario
  // picker is independent, and setHTML no-ops when its markup is unchanged.)
  const busy = SHIP_MENU_OPEN || (document.activeElement && document.activeElement.id === "shipSel");
  if (!busy) setHTML("status", statusHtml(d));
  setHTML("scenario", d.ship_detected ? "" : testControls());  // scenarios are manual-mode only
  setHTML("stats", readoutsHtml(d));
  setHTML("capacity", gaugeHtml(d));
}

// ---- autocomplete catalog (cargo + station names) ---- //
function datalistsHtml(cat) {
  if (!cat) return "";
  const opts = (a) => (a || []).map(v => `<option value="${esc(v)}"></option>`).join("");
  return `<datalist id="dl_cargo">${opts(cat.cargo)}</datalist>` +
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

// Station-name cell for a loading/unloading group header. When the group carries
// a zoneHostId it can be (re)named inline; the name persists by zone and resolves
// everywhere that zone is used (origins included).
function groupTitle(g) {
  const warn = g.has_partial ? ' <span class="warn" title="some quantities not logged">⚠</span>' : "";
  if (!g.zone) return `<span>${esc(g.location)}${warn}</span>`;
  if (EDIT_ZONE === g.zone) {
    const unknown = /^Unknown station/.test(g.location);
    return `<span class="zedit">
      <input id="zn_input" class="zn-input" list="dl_station" placeholder="Station name" value="${esc(unknown ? "" : g.location)}"
        onkeydown="if(event.key==='Enter')saveZoneName('${g.zone}');else if(event.key==='Escape')cancelZone()">
      <button class="zn-ok" onclick="saveZoneName('${g.zone}')">Set</button>
      <button class="zn-x" title="cancel" onclick="cancelZone()">✕</button></span>`;
  }
  const cls = /^Unknown station/.test(g.location) ? "loc-unknown" : "";
  return `<span class="${cls}">${esc(g.location)}${warn}
    <button class="namebtn" title="Name this station — applies everywhere it's used" onclick="editZone('${g.zone}')">✎</button></span>`;
}

function rerenderGroups() {
  if (!LAST) return;
  setHTML("loading", groupCards(LAST.loading, "loading", LAST));
  setHTML("unloading", groupCards(LAST.unloading, "unloading", LAST));
}
function editZone(zone) {
  EDIT_ZONE = zone;
  rerenderGroups();
  setTimeout(() => { const i = $("zn_input"); if (i) { i.focus(); i.select(); } }, 0);
}
function cancelZone() { EDIT_ZONE = null; rerenderGroups(); }
async function saveZoneName(zone) {
  const input = $("zn_input");
  const name = input ? input.value.trim() : "";
  try {
    const r = await fetch("/api/station-name", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ zone, name }),
    });
    const j = await r.json();
    if (!j.ok) { alert("Save failed: " + (j.error || r.status)); return; }
  } catch (e) { alert("Save failed: " + e); return; }
  EDIT_ZONE = null;
  refresh();
}

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
        ${check}<div class="rowmain"><span class="cargo">${esc(it.cargo)}</span>${tail}</div>
        <div class="qty">${QTY(it.qty)}</div></div>`;
    }).join("");
    return `<div class="card"><h3>${groupTitle(g)}
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
  try {
    const r = await fetch("/api/leg-state", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ legs, done }),
    });
    const j = await r.json();
    if (!j.ok) { alert("Update failed: " + (j.error || r.status)); return; }
  } catch (e) { alert("Update failed: " + e); return; }
  refresh();
}

// ---- trip plan (ordered itinerary above the route cards) ---- //
function bodyLabel(s) {
  if (s.body === "?") return "Unknown location";
  const sys = s.system && s.system !== "?" && s.system !== s.body ? `${esc(s.system)} · ` : "";
  const moon = s.moon ? ` › ${esc(s.moon)}` : "";
  return sys + esc(s.body) + moon;
}

function planView(plan) {
  if (!plan || !plan.stops || !plan.stops.length) return "";
  const load = plan.load || {};
  const loadItems = (load.items || [])
    .map(it => `<span class="chip">${esc(it.cargo)}${it.qty ? " " + num(it.qty) : ""}</span>`).join("");
  const loadCard = `<div class="plan-load">
    <div class="plan-step">LOAD</div>
    <div class="plan-body"><div class="plan-station">${esc(load.station || "—")}</div>
      <div class="plan-chips">${loadItems || '<span class="sub">no cargo outstanding</span>'}</div></div>
    <div class="scu">${num(plan.scu_total || 0)} SCU</div></div>`;

  // group consecutive stops under a body/moon header to show jumps
  let lastKey = null, n = 0;
  const stopCards = plan.stops.map(s => {
    const key = `${s.system}/${s.body}/${s.moon || ""}`;
    const header = key !== lastKey
      ? `<div class="plan-leg"><span class="plan-jump">${++n}</span>${bodyLabel(s)}</div>` : "";
    lastKey = key;
    const rows = s.items.map(it =>
      `<div class="row">${legCheck(it.mission_id, it.oid, false)}<div class="rowmain">
         <span class="cargo">${esc(it.cargo)}</span></div>
         <div class="qty">${QTY(it.qty)}</div></div>`).join("");
    return header + `<div class="card plan-stop"><h3><span>${esc(s.station)}</span>
        <span class="scu">${num(s.scu)} SCU</span></h3>${rows}</div>`;
  }).join("");

  return `<div class="planwrap">
    <div class="archbar"><span class="arch-title">Trip Plan · ${plan.stops.length} stop(s) · ${n || plan.stops.length} jump(s)</span>
      <span class="sub">load at origin, then fewest-jump delivery order</span></div>
    ${loadCard}
    <div class="plan-stops">${stopCards}</div>
  </div>`;
}

function routeCards(routes, d) {
  const plan = planView(d.plan);
  if (!routes.length) return plan || standby("No Routes Plotted",
    "Active contracts are bundled into <b>origin → destination</b> runs. Plot a haul to chart your routes.",
    "no active legs");
  return plan + partialNote(d) + `<div class="route-rollup"><div class="archbar"><span class="arch-title">Route Rollup</span></div><div class="grid">` + routes.map(r => {
    // each cargo chip ticks off all its legs on this route at once
    const cargo = r.cargo.map(c => {
      const legs = (c.legs || []).map(l => ({ mission_id: l.mission_id, oid: l.oid }));
      const tick = legs.length
        ? `<button class="chiptick" title="Mark this cargo delivered on this route"
            onclick='markDelivered(${JSON.stringify(legs)}, true)'>✓</button>` : "";
      return `<span class="chip">${esc(c.cargo)}${c.qty ? (" " + num(c.qty)) : ""}${tick}</span>`;
    }).join("");
    return `<div class="card route"><h3>
        <span class="ends">${esc(r.origin)}<span class="arrow">→</span>${esc(r.destination)}${r.has_partial ? ' <span class="warn">⚠</span>' : ""}</span>
        <span class="scu">${SCU(r.total_scu, r.has_partial)}</span></h3>
      <div class="row"><div>${cargo}</div></div>
      <div class="row"><div class="sub">${r.mission_count} mission(s)</div></div>
      </div>`;
  }).join("") + `</div></div>`;
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
    <td><input class="lc" list="dl_cargo" placeholder="cargo" value="${esc(cargo)}"></td>
    <td><input class="lq" type="number" placeholder="?" value="${leg.qty == null ? "" : leg.qty}"></td>
    <td><input class="ll" list="dl_station" placeholder="${esc(hint)}" value="${esc(loc)}"></td>
    <td><button type="button" class="rm" title="remove row" onclick="this.closest('tr').remove()">✕</button></td>
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
    const dests = (m.destinations || []).map(esc).join(", ") || "?";
    const row = legRow(esc(ct), '<span class="warn">?</span> SCU',
      `${dests} <span class="sub">· qty not logged</span>`, { warnCargo: true });
    return `<div class="manilegs">${row}</div>`;
  }
  return '<span class="sub">—</span>';
}

function editorRow(m) {
  const opt = (v, l, sel) => `<option value="${v}"${sel ? " selected" : ""}>${l}</option>`;
  const statuses = ["active", "completed", "abandoned", "failed", "expired"];
  // an unresolved origin ("Unknown station …") is a placeholder, not real content:
  // show it as the placeholder and leave the field empty so typing overwrites it.
  const unknownOrigin = !m.origin || /^Unknown station/.test(m.origin);
  return `<tr class="editrow"><td colspan="6"><div class="editor"
    onkeydown="if(event.key==='Enter'&&event.target.tagName==='INPUT'){event.preventDefault();saveMission('${m.mission_id}')}">
    <div class="ef"><label>Title</label><input id="ed_title" value="${esc(m.title || "")}"></div>
    <div class="ef"><label>Origin</label><input id="ed_origin" list="dl_station" value="${esc(unknownOrigin ? "" : m.origin)}" placeholder="${esc(unknownOrigin ? (m.origin || "origin") : "origin")}"></div>
    <div class="ef"><label>Reward <span class="sub">(aUEC · type 12k or 1.5m)</span></label><input id="ed_reward" type="text" inputmode="decimal" value="${m.reward || ""}"></div>
    <div class="ef"><label>Status</label><select id="ed_status">
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
    const tags = [dec.structure, dec.category, dec.grade].filter(Boolean).map(t => `<span class="chip">${esc(t)}</span>`).join("");
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
      <td>${esc(m.origin)}</td>
      <td>${missionLegs(m)}</td>
      <td>${m.reward ? num(m.reward) + " aUEC" : '<span class="sub">—</span>'}</td>
      <td>${action}</td>
    </tr>`;
    return tr + (EDIT === m.mission_id && !m.hidden ? editorRow(m) : "");
  }).join("");
  return `<table><thead><tr><th>Status</th><th>Mission</th><th>Origin</th><th>Cargo → Destination</th><th>Reward</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderMissions() { const d = curData(); if (d) setHTML("missions", missionsTable(d.missions)); }

// ---- cargo grid (current ship) ---- //
// Distinct, evenly-spread hues per destination (golden-angle), stable as long as
// the unloading group order is stable (the server sorts it alphabetically).
const destHue = (i) => Math.round((i * 137.508) % 360);

// ---- cargo groups, elevator staging + load-order packing ---- //
// Test scenarios for visualizing the loader without live missions. They're
// GENERATED for the currently selected ship (its grid + capacity come straight
// from the live snapshot), scaled to fill ≥50% of the hold, and each contract's
// size tier / container cap falls out of its quantity via the real rules (see
// [[hauling-contract-taxonomy]]). Real cargo types / stations from the logs.
const TEST_CARGO = ["Aluminum", "Titanium", "Scrap", "Stims", "Corundum", "Processed Food",
  "Tungsten", "Quartz", "Silicon", "Pressurized Ice", "Agricultural Supplies", "Carbon", "Waste"];
const TEST_STATIONS = ["Everus Harbor", "Seraphim Station", "Port Tressler", "Baijini Point",
  "August Dunlow Spaceport", "HUR-L1 Green Glade Station", "CRU-L1 Ambitious Dream Station"];

let SCENARIOS = [];      // generated for the current ship; recomputed each render
let TEST_CASE = null;    // index into SCENARIOS, or null for live data
let SCENARIO_VIEW = null; // server-built FULL snapshot for the active scenario (drives every tab)
let SCENARIO_KEY = null;  // ship+index the SCENARIO_VIEW was built for (avoids refetch)

// The snapshot every tab renders from: the active scenario's full snapshot, else live.
const curData = () => (TEST_CASE != null && SCENARIO_VIEW) ? SCENARIO_VIEW : LAST;
// optional ?scenario=<index> deep-link: auto-preview a test scenario once it's ready
let PENDING_SCENARIO = (m => m ? +m[1] : null)(/[?&]scenario=(\d+)/.exec(location.search));

// Contract size tier (rank + size word) implied by a delivery quantity, per the
// SCU bands. The synthesizer's box cap then follows from the title via tierMaxBox.
function sizeLabel(qty) {
  if (qty <= 10) return { rank: "Rookie", size: "Extra Small" };   // 1-SCU boxes
  if (qty <= 24) return { rank: "Rookie", size: "Small" };         // 4-SCU
  if (qty <= 126) return { rank: "Member", size: "Medium" };       // 8-SCU (≤Member)
  if (qty <= 600) return { rank: "Experienced", size: "Medium" };  // 16-SCU (Experienced+)
  return { rank: "Senior", size: "Large" };                        // 32-SCU
}
const titleFor = (qty) => { const s = sizeLabel(qty); return `${s.rank} Rank - Direct ${s.size} Cargo Haul`; };

// Round a target SCU to a tidy multiple of its tier's container size, so deliveries
// pack into whole boxes. `floor` keeps the result ≤ the target (used where a qty
// must not exceed the hold).
function niceQty(target, floor) {
  target = Math.max(1, target);
  const step = target <= 10 ? 1 : target <= 24 ? 4 : target <= 126 ? 8 : target <= 600 ? 16 : 32;
  const n = (floor ? Math.floor : Math.round)(target / step) * step;
  return Math.max(step, n);
}

// Generate the scenario list scaled to a ship of `cap` SCU. Each scenario loads
// ≥50% of the hold (except the deliberate over-capacity one, which exceeds it).
function genScenarios(shipName, cap) {
  const C = TEST_CARGO, ST = TEST_STATIONS, ship = shipName;
  const HUB = ST[6];   // common pickup origin (never used as a destination below)
  const mk = (cargo, qty, dest) => ({ title: titleFor(qty), cargo, qty, dest, origin: HUB });
  const q = (f) => niceQty(cap * f);               // fraction of capacity → tidy SCU
  const list = [];

  // 1. single delivery (~60%)
  const sg = q(0.6);
  list.push({ name: `Single delivery · ${num(sg)} SCU`, ship,
    route: [ST[0]], missions: [mk(C[0], sg, ST[0])] });

  // 2. three stops, distinct cargo (~62%)
  list.push({ name: "Three stops · no conflicts", ship,
    route: [ST[0], ST[1], ST[2]],
    missions: [mk(C[1], q(0.24), ST[1]), mk(C[5], q(0.22), ST[0]), mk(C[3], q(0.16), ST[2])] });

  // 3. shared cargo type → forces per-mission isolation (~74%)
  list.push({ name: `⚠ Shared cargo · ${C[0]} to two stops`, ship,
    route: [ST[1], ST[0], ST[3]],
    missions: [mk(C[0], q(0.30), ST[1]), mk(C[0], q(0.26), ST[0]), mk(C[2], q(0.18), ST[3])] });

  // 4. mixed tiers — one token mission per container size that fits, then top up
  //    with extra contracts of the LARGEST fitting tier until the load is ≥50%.
  //    Each tier index → its box size, SCU-band ceiling, and a min qty that still
  //    reads as that tier (so a filler contract keeps the largest box size).
  const demoQ = [8, 16, 96, 240, 640];             // token qty per box size 1,4,8,16,32
  const boxStep = [1, 4, 8, 16, 32];
  const bandMax = [10, 24, 126, 600, 1e12];        // tier SCU ceiling
  const tierMin = [1, 12, 32, 128, 640];           // min qty that still reads as this tier
  const inc = [];
  let baseSum = 0;
  demoQ.forEach((dq, i) => { if (baseSum + dq <= cap) { inc.push(i); baseSum += dq; } });
  if (inc.length) {
    const qtys = inc.map(i => demoQ[i]);
    const li = inc[inc.length - 1], box = boxStep[li];
    const tierCap = Math.min(Math.floor(bandMax[li] / box) * box, Math.floor(cap / box) * box);
    const target = Math.ceil(cap * 0.5);
    let total = baseSum;
    while (total < target && total + tierMin[li] <= cap && qtys.length < 24) {
      let add = Math.max(tierMin[li], Math.ceil((target - total) / box) * box);  // whole boxes, in-tier
      add = Math.min(add, tierCap, Math.floor((cap - total) / box) * box);       // ≤ band, ≤ hold room
      if (add < tierMin[li]) break;
      qtys.push(add); total += add;
    }
    const mixed = qtys.map((qy, k) => mk(C[k % C.length], qy, ST[k % ST.length]));
    list.push({ name: `Mixed tiers · ${inc.length} box size${inc.length > 1 ? "s" : ""}`, ship,
      route: mixed.map(m => m.dest), missions: mixed });
  }

  // 5. fill the hold (~92%, largest tier that fits)
  const fq = niceQty(cap * 0.92, true);
  list.push({ name: `Fill the hold · ${num(fq)} SCU`, ship, route: [ST[0]], missions: [mk(C[2], fq, ST[0])] });

  // 6. over capacity (~130%)
  const o1 = q(0.7), o2 = q(0.6);
  list.push({ name: `Heavy load · ${num(o1 + o2)} SCU vs ${num(cap)}`, ship,
    route: [ST[1], ST[0]], missions: [mk(C[7], o1, ST[1]), mk(C[8], o2, ST[0])] });

  // 7. rep grind — many contracts split across 2 stops, totaling ≥50%. Mission
  //    count scales gently with hull size; per-mission SCU then fills to target.
  const repTarget = Math.ceil(cap * 0.5);
  const repCount0 = Math.min(14, Math.max(5, Math.round(cap / 90)));
  const repEach = niceQty(repTarget / repCount0);
  const repN = Math.min(20, Math.max(repCount0, Math.ceil(repTarget / repEach)));
  list.push({ name: `Rep grind · ${repN} missions`, ship,
    route: [ST[0], ST[1]],
    missions: Array.from({ length: repN }, (_, i) => mk(C[(i + 2) % C.length], repEach, ST[i % 2])) });

  // 8. multi-drop — one contract, same cargo to two stops (~60%)
  const dq = q(0.3);
  list.push({ name: "⚠ Multi-drop · one contract, two stops", ship,
    route: [ST[5], ST[1]],
    missions: [{ title: titleFor(dq * 2), origin: HUB,
      drops: [{ cargo: C[5], qty: dq, dest: ST[5] }, { cargo: C[5], qty: dq, dest: ST[1] }] }] });

  return list;
}

// The current ship to build scenarios for: whatever the live snapshot is showing
// (detected or manually selected), with its real grid geometry + capacity.
function currentShipInfo() {
  if (!LAST || !LAST.ship || !LAST.ship_grid || !LAST.ship_grid.length) return null;
  return { name: LAST.ship, grid: LAST.ship_grid, scu: LAST.ship_scu || 0 };
}

// Recompute the scenario list for the live ship; drop a stale selection. Test
// scenarios are a manual-mode aid only — when the game has DETECTED a ship there are
// no scenarios and any active one reverts to live data (the picker is hidden too).
function syncScenarios() {
  const info = currentShipInfo();
  const detected = !!(LAST && LAST.ship_detected);
  SCENARIOS = (info && !detected) ? genScenarios(info.name, info.scu) : [];
  if (TEST_CASE != null && (detected || TEST_CASE >= SCENARIOS.length)) {
    TEST_CASE = null; SCENARIO_VIEW = null; SCENARIO_KEY = null;
  }
}

// Fetch the FULL snapshot for the active scenario from the server, which runs the
// synthetic missions through the live pipeline (loading/unloading/routes/counts) so
// the whole dashboard previews it — not just the grid. Cached by ship+selection.
async function loadScenario() {
  const info = currentShipInfo();
  const tc = (TEST_CASE != null && SCENARIOS[TEST_CASE]) || null;
  if (!tc || !info) { SCENARIO_VIEW = null; SCENARIO_KEY = null; return; }
  const key = `${info.name}·${info.scu}·${TEST_CASE}`;
  if (SCENARIO_KEY === key && SCENARIO_VIEW) return;
  try {
    SCENARIO_VIEW = await (await fetch("/api/test-snapshot", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ missions: tc.missions }),
    })).json();
    SCENARIO_KEY = key;
  } catch (e) { SCENARIO_VIEW = null; SCENARIO_KEY = null; }
}

// Render every tab from one snapshot `d` (the active scenario's, or live).
function renderAll(d) {
  if (!d) return;
  renderHeader(d);
  setHTML("datalists", datalistsHtml(d.catalog));
  if (!EDIT_ZONE) {  // don't clobber an open station-name editor
    setHTML("loading", groupCards(d.loading, "loading", d));
    setHTML("unloading", groupCards(d.unloading, "unloading", d));
  }
  setHTML("routes", routeCards(d.routes, d));
  if (!GRID_HOVER) setHTML("grid", gridView(d));  // don't wipe the hold highlight mid-hover
  if (EDIT === null) setHTML("missions", missionsTable(d.missions));  // don't clobber an open editor
}

const loadOrder = (gs) => [...gs].sort((a, b) => b.routeIdx - a.routeIdx);

// delivery position of a destination from the plotted route (0 = delivered first).
function deliveryIndex(d, dest) {
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

// All outstanding cargo, staged as elevators. Cargo is bundled per destination, then
// those bundles are packed onto AS FEW elevators as possible: two bundles may ride the
// same elevator unless they carry the SAME cargo TYPE to different stops — those boxes
// are identical and would be indistinguishable once mixed (e.g. Aluminum→A and
// Aluminum→B must stay apart, but Scrap→C can ride with either). Boxes are tagged with
// their destination's hue. An elevator is flagged `shared` when it carries a type that
// also rides another elevator, so the loader knows to load it fully before the next.
// SCU is synthesized into standard containers capped by each mission's size tier;
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

  // greedy first-fit: drop each bundle (earliest delivery first) onto the first elevator
  // that shares no cargo type with it; otherwise start a new elevator.
  const elevators = [];
  for (const bnd of Object.values(byDest).sort((a, b) => a.routeIdx - b.routeIdx)) {
    let e = elevators.find(e => ![...bnd.types].some(t => e.types.has(t)));
    if (!e) { e = { dests: [], types: new Set(), cargo: [], scu: 0, boxes: [], routeIdx: Infinity }; elevators.push(e); }
    e.dests.push(bnd.dest);
    bnd.types.forEach(t => e.types.add(t));
    for (const c of bnd.cargo) if (!e.cargo.includes(c)) e.cargo.push(c);
    e.scu += bnd.scu; e.boxes.push(...bnd.boxes);
    e.routeIdx = Math.min(e.routeIdx, bnd.routeIdx);
  }

  return elevators.map(e => ({
    shared: e.cargo.some(isAmbiguous),    // carries a type that also rides another elevator
    dest: e.dests.join(", "), cargo: e.cargo.join(", "),
    hue: hueOf[e.dests[0]], scu: e.scu, boxes: e.boxes, routeIdx: e.routeIdx,
  }));
}

// ---- test scenario picker (header, middle slot) — a selected scenario drives
// every tab via curData(); "— live data —" returns to the real session ---- //
function testControls() {
  const opts = SCENARIOS.map((tc, i) =>
    `<option value="${i}"${TEST_CASE === i ? " selected" : ""}>${esc(tc.name)}</option>`).join("");
  const note = SCENARIOS.length ? "Sample hauls scaled to the selected ship" : "Select a ship to enable";
  return `<div class="scenpick" title="${note}">
    <label class="sp-lbl" for="tcSel">Test Scenario</label>
    <select id="tcSel" class="tc-sel" onchange="pickTestCase(this.value)"${SCENARIOS.length ? "" : " disabled"}>
      <option value=""${TEST_CASE == null ? " selected" : ""}>— live data —</option>
      ${opts}</select></div>`;
}
function pickTestCase(v) {
  TEST_CASE = v === "" ? null : +v;
  if (TEST_CASE == null) { SCENARIO_VIEW = null; SCENARIO_KEY = null; renderAll(curData()); }
  else loadScenario().then(() => renderAll(curData()));   // fetch full scenario, then repaint every tab
}

// Highlight one elevator's boxes in the ship hold (dim the rest). gid===null clears.
function hlElev(gid) {
  GRID_HOVER = gid != null;   // freeze the grid repaint while the highlight is active
  const wrap = $("holdwrap");
  if (!wrap) return;
  wrap.classList.toggle("hling", gid != null);
  wrap.querySelectorAll(".cg-box").forEach(b =>
    b.classList.toggle("hl-on", gid != null && b.dataset.gid === String(gid)));
}

function gridView(d) {
  if (!d.ship) return standby("No Ship Detected",
    "Board a ship in-game — or pick one from the SHIP box — and its cargo grid appears here. Or choose a Test Scenario from the header.",
    "awaiting ship");
  if (!d.ship_grid || !d.ship_grid.length) return standby("No Grid Data",
    `<b>${esc(d.ship)}</b> isn't in the cargo-grid database, or carries no cargo grid.`,
    "no geometry");

  const groups = cargoGroups(d);
  const cap = d.ship_scu || 0;
  const totalScu = groups.reduce((a, g) => a + g.scu, 0);

  // Per-ship cargo access (from the hatch survey): banded ships unload front-to-back
  // from a hatch; "open" ships (externals / cargo lifts / multi-side) have every box
  // reachable, so load order is irrelevant.
  const access = (typeof accessFor === "function") ? accessFor(d.ship) : { open: true };
  const banded = !!access.axis;
  const NEAR = { rear: "rear", front: "front", left: "left", right: "right" };
  const accessLabel = banded
    ? (access.axis === "width"
        ? (access.both ? "side-loading · left + right hatches" : "side-loading · " + NEAR[access.near] + " hatch")
        : (access.both ? "front + rear hatches" : NEAR[access.near] + " hatch"))
    : "all-access · external grid / cargo lift";

  const head = `<div class="archbar">
    <span class="arch-title">${esc(d.ship)} · ${num(totalScu)} / ${num(cap)} SCU</span>
    <span class="sub">${accessLabel} · ${banded ? "loaded front-to-back" : "load order doesn't matter"} · <a href="/grids.html" target="_blank" style="color:var(--cyan)">all ships ↗</a></span></div>`;

  if (!groups.length) {
    const msg = TEST_CASE != null
      ? "This scenario has no deliverable cargo."
      : "No cargo to load yet — accept hauling contracts and your picked-up cargo stages here by destination. Or pick a Test Scenario from the header.";
    return head + `<div class="sub" style="margin:6px 2px 14px">${msg}</div>`
      + `<div id="holdwrap">` + cargoGridHtml(d.ship_grid, { scale: 22, packed: { placed: [] }, layout: d.ship_layout }) + `</div>`;
  }

  // Banded ships pack front-to-back: order groups first-delivered-first so group 0
  // gets the band AT the hatch and nothing later sits in front of it. Open ships:
  // layering is irrelevant; keep the existing order. packGroups tags each placed
  // box with its group index (gid) for the hover-highlight.
  const order = banded ? [...groups].sort((a, b) => a.routeIdx - b.routeIdx) : loadOrder(groups);
  const shipPacked = packGroups(d.ship_grid, order, banded ? access : null);
  const overScu = shipPacked.overflow.reduce((a, b) => a + b.scu, 0);

  // legend is by DESTINATION (deduped across every elevator's boxes), summing SCU.
  const dests = []; const seenD = {};
  order.forEach(g => g.boxes.forEach(b => {
    let e = seenD[b.dest];
    if (!e) { e = seenD[b.dest] = { dest: b.dest, hue: b.hue, scu: 0 }; dests.push(e); }
    e.scu += b.scu;
  }));
  const legend = `<div class="cg-legend">` + dests.map(x =>
    `<span class="cg-leg"><span class="cg-sw" style="background:hsl(${x.hue},64%,52%)"></span>${esc(x.dest)} <b>${num(x.scu)}</b> SCU</span>`).join("")
    + (cap ? `<span class="cg-leg"><span class="cg-sw cg-sw-free"></span>Free <b>${num(Math.max(0, cap - shipPacked.placedScu))}</b> SCU</span>` : "")
    + `</div>`;

  const over = overScu
    ? `<div class="note">⚠ ${num(overScu)} SCU won't fit this ${num(cap)} SCU hold — you'll need another run.</div>` : "";

  const ambig = order.some(g => g.shared)
    ? `<div class="note">⚠ A cargo type is bound for more than one destination — its boxes look identical. Load each elevator marked <b>⚠ shared</b> <b>fully</b> before raising the next, so the twins don't get mixed up.</div>` : "";

  // Load order. For banded ships the PHYSICAL sequence is the reverse of the band
  // order — load the deepest (last-delivered) cargo first so the first delivery ends
  // up right at the hatch. gid still indexes `order` (where each group was packed).
  const rows = order.map((g, i) => ({ g, gid: i }));
  const seqRows = banded ? [...rows].reverse() : rows;
  const seqNote = banded
    ? `load deepest (last delivered) first, so the first delivery ends up right at the ${NEAR[access.near]} hatch · hover to locate in the hold`
    : `every box is reachable here, so order doesn't matter · hover to locate in the hold`;
  const seq = `<div class="loadseq"><span class="ls-lbl">Load order <span class="sub">(${seqNote})</span></span>
    <ol>${seqRows.map(({ g, gid }) =>
      `<li onmouseenter="hlElev(${gid})" onmouseleave="hlElev(null)"><span class="cg-sw" style="background:hsl(${g.hue},64%,52%)"></span>
        <span class="ls-dest">${esc(g.dest)}</span> <span class="ls-cargo sub">${esc(g.cargo)}</span>
        ${g.shared ? '<span class="ls-alone" title="carries a cargo type split across elevators — load this one fully before the next">⚠ shared</span>' : ""}
        <span class="ls-scu sub">${num(g.scu)} SCU</span></li>`).join("")}</ol></div>`;

  const shipLbl = `<div class="elev-lbl">Ship hold <span class="sub">— ${banded ? "packed front-to-back" : "packed in load order"} · hover a load-order row to highlight its boxes</span></div>`;

  return head + legend + over + ambig + seq + shipLbl
    + `<div id="holdwrap">` + cargoGridHtml(d.ship_grid, { scale: 22, packed: shipPacked, layout: d.ship_layout }) + `</div>`;
}

// ---- editor actions ---- //
const rawOverride = (mid) => ((LAST && LAST.missions.find(m => m.mission_id === mid) || {}).raw_override) || {};

function editMission(mid) {
  EDIT = mid; renderMissions();
  // jump straight to the first field that needs filling (e.g. an unknown origin
  // or a missing cargo/qty), so you can start typing without hunting for it.
  setTimeout(() => {
    const row = document.querySelector("tr.editrow");
    if (!row) return;
    const inputs = [...row.querySelectorAll("input")];
    const target = inputs.find(i => !i.value.trim()) || inputs[0];
    if (target) { target.focus(); target.select(); }
  }, 0);
}
function cancelEdit() { EDIT = null; renderMissions(); }
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
  try {
    const r = await fetch("/api/override", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mission_id: mid, override }),
    });
    const j = await r.json();
    if (!j.ok) alert("Save failed: " + (j.error || r.status));
  } catch (e) { alert("Save failed: " + e); }
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
function fmtDuration(a, b) {
  if (!a || !b) return "";
  let s = (new Date(b) - new Date(a)) / 1000;
  if (s < 0 || !isFinite(s)) return "";
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function sessionsView(sessions) {
  if (!sessions) return `<div class="empty">loading archive…</div>`;
  if (!sessions.length) return `<div class="empty">No archived sessions yet. A session is saved here when you log out or relaunch the game.</div>`;
  // Accordion of three pooled logs (only one expanded at a time so the open one can
  // own the screen): contracts (with its Hide-* filter bar), trade loads, travel jumps.
  return `<div class="arch-acc">${contractLogView(sessions)}${tradeLogView(sessions)}${travelLogView(sessions)}</div>`;
}

// Departed → arrived elapsed, seconds-aware (jumps run seconds to minutes). "" if no arrival.
function fmtTravelTime(dep, arr) {
  if (!dep || !arr) return "";
  const s = Math.round((new Date(arr) - new Date(dep)) / 1000);
  if (s < 0 || !isFinite(s)) return "";
  const m = Math.floor(s / 60);
  return m ? `${m}m ${s % 60}s` : `${s}s`;
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
  const th = Math.floor(totalSecs / 3600), tm = Math.round((totalSecs % 3600) / 60);
  const tot = totalSecs ? ` · ${th ? th + "h " + tm + "m" : tm + "m"} in QT` : "";
  const inner = rows.length ? `<div class="logwrap"><table class="logtable">
      <thead><tr><th>Departed</th><th>Status</th><th>Route</th><th class="lt-num">Time</th><th>System</th><th class="lt-num">QT fuel</th><th>Ship</th></tr></thead>
      <tbody>${body}</tbody></table></div>` : `<div class="empty">No quantum travel in range.</div>`;
  return logSection("travel", `Travel Log · ${rows.length}`,
                    `<span class="scu">${rows.length} jumps${tot}</span>`, inner);
}

// Compact "Hide …" filter bar (lives in the contract-log header). Inverts the API's
// show-flags: checked = exclude. Both re-fetch (server-side filtering).
function contractFilterBar() {
  return `<div class="filtbar">
    <span class="filt-lbl">Hide</span>
    <label class="chk" title="Exclude non-cargo missions (couriers, combat, etc.)">
      <input type="checkbox" id="fNonTrade" ${HIDE_NONTRADE ? "checked" : ""} onchange="toggleHideNonTrade()">Non-hauling</label>
    <label class="chk" title="Exclude missions left unfinished when the session ended">
      <input type="checkbox" id="fUnfinished" ${HIDE_UNFINISHED ? "checked" : ""} onchange="toggleHideUnfinished()">Unfinished</label>
  </div>`;
}

// Flat, cross-session log of every mission contract, time-ordered (newest first) by
// when it ended (else when accepted — both now carried per mission in the archive).
function contractLogView(sessions) {
  const rows = [];
  for (const s of sessions || [])
    for (const m of s.missions || [])
      rows.push({ when: m.ended_at || m.accepted_at || s.started_at, m });
  rows.sort((a, b) => (b.when || "").localeCompare(a.when || ""));
  const total = rows.reduce((a, r) => a + (r.m.reward || 0), 0);
  const body = rows.map(r => {
    const dest = (r.m.destinations || []).filter(Boolean);
    return `<tr>
      <td class="lt-when">${fmtWhen(r.when)}</td>
      <td><span class="badge b-${r.m.status}">${esc(r.m.status)}</span></td>
      <td class="lt-title">${esc(r.m.title)}${dest.length ? ` <span class="sub">→ ${esc(dest.join(", "))}</span>` : ""}</td>
      <td class="lt-num">${r.m.reward ? num(r.m.reward) : "—"}</td></tr>`;
  }).join("");
  const inner = contractFilterBar() + (rows.length ? `<div class="logwrap"><table class="logtable">
      <thead><tr><th>When</th><th>Status</th><th>Contract</th><th class="lt-num">Reward</th></tr></thead>
      <tbody>${body}</tbody></table></div>` : `<div class="empty">No contracts in range.</div>`);
  return logSection("contracts", `Contract Log · ${rows.length}`,
                    `<span class="scu">${num(total)} aUEC</span>`, inner);
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

// Cross-session manual-trade LOAD log: each row is a buy + its sells, with realised
// profit (revenue − the cost of the sold portion). Newest load first.
function tradeLogView(sessions) {
  // Pool archived trades with the CURRENT (un-archived) session's so a just-made trade
  // shows immediately (not only after logout); pooledTrades dedups both feeds.
  const trades = pooledTrades(sessions);
  const loads = buildLoads(trades).sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
  const LOST = new Set((LAST && LAST.lost_trades) || []);
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
      <td class="lt-title">${esc(L.commodity)} ${tag}${act}</td>
      <td class="lt-shop">${esc(route)}</td>
      <td class="lt-num">${scu}</td>
      <td class="lt-num ${L.cost ? "neg" : ""}">${L.cost ? "−" + num(L.cost) : "—"}</td>
      <td class="lt-num ${L.revenue ? "pos" : ""}">${L.revenue ? "+" + num(Math.round(L.revenue)) : "—"}</td>
      <td class="lt-num ${!priced ? "" : profit >= 0 ? "pos" : "neg"}">${priced ? (profit >= 0 ? "+" : "−") + num(Math.abs(profit)) : "—"}</td></tr>`;
  }).join("");
  const inner = loads.length ? `<div class="logwrap"><table class="logtable">
      <thead><tr><th>When</th><th>Commodity</th><th>Route</th><th class="lt-num">SCU</th><th class="lt-num">Cost</th><th class="lt-num">Revenue</th><th class="lt-num">Profit</th></tr></thead>
      <tbody>${body}</tbody></table></div>` : `<div class="empty">No manual trades in range.</div>`;
  return logSection("trades", `Trade Loads · ${loads.length}`,
                    `<span class="scu ${totalProfit >= 0 ? "pos" : "neg"}">${totalProfit >= 0 ? "+" : "−"}${num(Math.abs(totalProfit))} aUEC profit</span>`, inner);
}

async function loadSessions() {
  try {
    SESSIONS = await (await fetch("/api/sessions" + sessQ(), { cache: "no-store" })).json();
  } catch (e) { SESSIONS = SESSIONS || []; }
  setHTML("history", sessionsView(SESSIONS));
}

// Flag/unflag a trade load as lost (cargo destroyed/stolen). Optimistically updates
// the live snapshot's lost set so the row re-renders immediately, then persists.
async function markTradeLost(id, lost) {
  if (LAST) {
    const set = new Set(LAST.lost_trades || []);
    lost ? set.add(id) : set.delete(id);
    LAST.lost_trades = [...set];
  }
  setHTML("history", sessionsView(SESSIONS));
  try {
    await fetch("/api/trade-lost", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trade_id: id, lost }),
    });
  } catch (e) { /* next poll reconciles from the server */ }
}

// ---- poll loop ---- //
async function refresh() {
  try {
    const d = await (await fetch("/api/state", { cache: "no-store" })).json();
    LAST = d;
    syncScenarios();                       // keep the scenario list scaled to the live ship
    if (PENDING_SCENARIO != null && PENDING_SCENARIO < SCENARIOS.length) {
      TEST_CASE = PENDING_SCENARIO; PENDING_SCENARIO = null;  // honor ?scenario= once
    }
    if (TEST_CASE != null) await loadScenario();  // rebuild the preview if the ship changed
    renderAll(curData());                  // scenario active → every tab previews it; else live
    if (TAB === "history") loadSessions();  // keep archive fresh while viewing
    const last = d.last_event_ts ? ("log " + d.last_event_ts) : "";
    const sess = d.logged_in ? ("session since " + (d.session_started_at || "?")) : "at main menu (logged out)";
    const ver = d.game_version ? (" · game " + d.game_version) : "";
    $("foot").textContent = `synced ${new Date().toLocaleTimeString()} · ${sess}${ver} · ${last} · cargo db @ ${d.ship_cargo_version || "?"}`;
  } catch (e) { $("foot").textContent = "waiting for tracker… (" + e + ")"; }
}
refresh();
loadShipList();
setInterval(refresh, 3000);

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
