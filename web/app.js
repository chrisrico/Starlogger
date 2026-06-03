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
let EDIT = null;      // mission_id whose editor is open (Contracts tab)
let EDIT_CELL = null; // token of the open inline editor (unified, one at a time)
let SESSIONS = null;  // archived sessions

// ---- session replay ---- //
// When a session is replayed, the WHOLE dashboard renders a reconstructed past
// snapshot instead of live data: curData() returns REPLAY_SNAPSHOT and the poll pauses.
// REPLAY_POINTS is the scrub timeline (index/ts/label); REPLAY_I the current checkpoint.
let REPLAY_MODE = false, REPLAY_KEY = null, REPLAY_POINTS = [], REPLAY_I = 0, REPLAY_SNAPSHOT = null;
let REPLAY_UNAVAILABLE = new Set();  // session keys whose source log is gone
let _scrubTimer = null;

// Which Archive section is expanded (accordion — only one at a time). Empty = all
// collapsed. Persists the user's explicit choice; no built-in default (see archDefaultSection).
let ARCH_OPEN = localStorage.getItem("archOpen") || "";
function toggleArch(key) {
  if (ARCH_OPEN === key) return;   // the open section stays open — only selecting another switches
  ARCH_OPEN = key;
  localStorage.setItem("archOpen", ARCH_OPEN);
  setHTML("history", sessionsView(SESSIONS));
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
  setHTML("history", sessionsView(SESSIONS));
}
// One Archive section as a tab descriptor; sessionsView() renders the tab bar and the
// selected section's body (only the active body is built into the DOM).
function logSection(key, title, headSpan, body) {
  return { key, title, headSpan: headSpan || "", body };
}

// ---- tabs (with URL-hash deep-linking) ---- //
const TABS = ["loading", "unloading", "routes", "missions", "grid", "history"];
function activateTab(name) {
  if (!TABS.includes(name)) return;
  TAB = name;
  document.querySelectorAll("#nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hide", t.id !== name));
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  if (name === "history") { ARCH_PICK = true; loadSessions(); }
}
document.querySelectorAll("#nav button").forEach(b => { b.onclick = () => activateTab(b.dataset.tab); });
if (TABS.includes(location.hash.slice(1))) activateTab(location.hash.slice(1));

// Close the Contract Log's Type-filter dropdown on any click outside it (the toggle
// button and the menu itself live inside .th-menu-wrap, so those are ignored).
document.addEventListener("click", (e) => {
  if (TYPE_MENU_OPEN && !e.target.closest(".th-menu-wrap")) { TYPE_MENU_OPEN = false; _archRepaint(); }
});

// ---- header: status pill, ship selector, telemetry readouts, capacity gauge ---- //

// Ship catalog for the manual selector, fetched once. name -> {manufacturer,scu,groups}.
let SHIP_DB = null;
let SHIP_MENU_OPEN = false;   // combobox popup state — guards the poll from clobbering it
let GRID_HOVER = false;       // hovering a load-order row — guards the poll from wiping the hold highlight
async function loadShipList() {
  try {
    const db = await (await fetch("/api/ships", { cache: "no-store" })).json();
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
  if (REPLAY_MODE) return;
  try {
    const r = await fetch("/api/select-ship", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ship: name || null }),
    });
    const j = await r.json();
    if (!j.ok) { alert("Couldn't set ship: " + (j.error || r.status)); return; }
  } catch (e) { alert("Couldn't set ship: " + e); return; }
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
  // is open — a poll landing mid-interaction would tear it down.
  const busy = SHIP_MENU_OPEN || (document.activeElement && document.activeElement.id === "shipSel");
  if (!busy) setHTML("status", statusHtml(d));
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

// ---- unified in-place editor for unknown values --------------------------- //
// One mechanism for every cargo-ops screen (loading, unloading, routes, trip plan).
// A `field` describes what to persist: {k, zone?, mid?, oid?} where k is:
//   station — keyed by zoneHostId  -> /api/station-name (resolves everywhere)
//   origin  — keyed by mission_id  -> /api/override (merged)
//   cargo|qty — keyed by mission_id+objective id -> /api/leg-field
// Click a value to edit it in place; Enter/blur commits, Escape cancels. The token
// identifies which single cell is open so the 3s poll never yanks it mid-edit.
const UNKNOWN_STATION = (s) => !s || /^Unknown station/.test(s);
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
  if (REPLAY_MODE) return;   // replay is read-only — past state can't be edited
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
// Re-render only the edit-bearing containers from the current snapshot (used when
// opening/cancelling an inline editor, without a network round-trip).
function rerenderEdits() {
  if (!LAST) return;
  const d = curData(); if (!d) return;
  setHTML("loading", groupCards(d.loading, "loading", d));
  setHTML("unloading", groupCards(d.unloading, "unloading", d));
  setHTML("routes", routeCards(d.routes, d));
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
  if (REPLAY_MODE) return;
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
         <span class="cargo">${cargoCell(it.cargo, it.mission_id, it.oid)}</span></div>
         <div class="qty">${qtyCell(it.qty, it.mission_id, it.oid)}</div></div>`).join("");
    return header + `<div class="card plan-stop"><h3><span>${stationCell(s.station, s.zone)}</span>
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
  // Apply the user's manual drag order (if any) to both the trip plan and the rollup
  // so every view — and the load order — agrees.
  const planSorted = (d.plan && d.plan.stops)
    ? { ...d.plan, stops: byRouteOrder(d.plan.stops, s => s.station) } : d.plan;
  const plan = planView(planSorted);
  if (!routes.length) return plan || standby("No Routes Plotted",
    "Active contracts are bundled into <b>origin → destination</b> runs. Plot a haul to chart your routes.",
    "no active legs");
  const ordered = byRouteOrder(routes, r => r.destination);
  const reset = ROUTE_ORDER
    ? `<button class="route-reset" title="Forget the manual order; revert to the planner's fewest-jump order" onclick="resetRouteOrder()">↺ auto order</button>` : "";
  const cards = ordered.map(r => {
    // each cargo chip ticks off all its legs on this route at once; a single-leg chip
    // is also inline-editable (commodity + qty), matching the other screens.
    const cargo = r.cargo.map(c => {
      const legs = (c.legs || []).map(l => ({ mission_id: l.mission_id, oid: l.oid }));
      const one = legs.length === 1 ? legs[0] : null;
      const tick = legs.length
        ? `<button class="chiptick" title="Mark this cargo delivered on this route"
            onclick='markDelivered(${JSON.stringify(legs)}, true)'>✓</button>` : "";
      const cName = one ? cargoCell(c.cargo, one.mission_id, one.oid) : esc(c.cargo);
      const cQty = c.qty ? " " + num(c.qty) : (one ? " " + qtyCell(null, one.mission_id, one.oid) : "");
      return `<span class="chip">${cName}${cQty}${tick}</span>`;
    }).join("");
    // drag handle only (so clicking the station/cargo cells to edit never starts a drag);
    // the card is the drop target.
    return `<div class="card route" data-dest="${esc(r.destination)}"
        ondragover="routeDragOver(event)" ondragleave="routeDragLeave(event)"
        ondrop="routeDrop(event)" ondragend="routeDragEnd(event)"><h3>
        <span class="ends"><span class="route-grip" draggable="true" title="Drag to reorder the run"
          ondragstart="routeDragStart(event)">⠿</span>${stationCell(r.origin, r.origin_zone)}<span class="arrow">→</span>${stationCell(r.destination, r.dest_zone)}${r.has_partial ? ' <span class="warn">⚠</span>' : ""}</span>
        <span class="scu">${SCU(r.total_scu, r.has_partial)}</span></h3>
      <div class="row"><div>${cargo}</div></div>
      <div class="row"><div class="sub">${r.mission_count} mission(s)</div></div>
      </div>`;
  }).join("");
  return plan + partialNote(d)
    + `<div class="route-rollup"><div class="archbar"><span class="arch-title">Route Rollup</span>`
    + `<span class="sub">drag runs to set your visit &amp; load order</span>${reset}</div>`
    + `<div class="grid" id="routegrid">${cards}</div></div>`;
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
  localStorage.setItem("routeOrder", JSON.stringify(order));
  routeDragEnd();
  renderAll(curData());
}
function resetRouteOrder() {
  ROUTE_ORDER = null;
  localStorage.removeItem("routeOrder");
  renderAll(curData());
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
// The snapshot every tab renders from (live session only).
const curData = () => (REPLAY_MODE ? REPLAY_SNAPSHOT : LAST);

// Render every tab from one snapshot `d` (the live snapshot).
function renderAll(d) {
  if (!d) return;
  renderHeader(d);
  setHTML("datalists", datalistsHtml(d.catalog));
  // EDIT_CELL guards every cargo-ops screen so an open inline editor isn't clobbered
  // by the 3s poll; DRAG_DEST guards a route drag in progress.
  if (!EDIT_CELL) {
    setHTML("loading", groupCards(d.loading, "loading", d));
    setHTML("unloading", groupCards(d.unloading, "unloading", d));
  }
  if (!EDIT_CELL && DRAG_DEST == null) setHTML("routes", routeCards(d.routes, d));
  if (!GRID_HOVER) setHTML("grid", gridView(d));  // don't wipe the hold highlight mid-hover
  if (EDIT === null) setHTML("missions", missionsTable(d.missions));  // don't clobber an open editor
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
    "Board a ship in-game — or pick one from the SHIP box — and its cargo grid appears here.",
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
    <span class="sub">${accessLabel} · ${banded ? "loaded front-to-back" : "load order doesn't matter"}</span></div>`;

  if (!groups.length) {
    const msg = "No cargo to load yet — accept hauling contracts and your picked-up cargo stages here by destination.";
    return head + `<div class="sub" style="margin:6px 2px 14px">${msg}</div>`
      + `<div id="holdwrap">` + cargoGridHtml(d.ship_grid, { scale: 22, packed: { placed: [] }, layout: d.ship_layout, access }) + `</div>`;
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
    + `<div id="holdwrap">` + cargoGridHtml(d.ship_grid, { scale: 22, packed: shipPacked, layout: d.ship_layout, access }) + `</div>`;
}

// ---- editor actions ---- //
const rawOverride = (mid) => ((LAST && LAST.missions.find(m => m.mission_id === mid) || {}).raw_override) || {};

function editMission(mid) {
  if (REPLAY_MODE) return;   // replay is read-only
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
  if (REPLAY_MODE) return;   // belt-and-suspenders: no live writes during replay
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
  // Pooled logs as horizontal tabs (Contract Log, Trade Loads — which now also carries
  // the trade-route recommendations — and Travel Log); the selected tab's body fills the
  // viewport. ARCH_OPEN is the active tab — defaulted by recency (archDefaultSection).
  const secs = [contractLogView(sessions), tradeLogView(sessions), travelLogView(sessions), sessionListView(sessions)];
  if (!secs.some(s => s.key === ARCH_OPEN)) ARCH_OPEN = secs[0].key;
  const active = secs.find(s => s.key === ARCH_OPEN) || secs[0];
  const tabs = secs.map(s =>
    `<button class="arch-tab${s.key === ARCH_OPEN ? " active" : ""}" onclick="toggleArch('${s.key}')">${s.title}</button>`).join("");
  return `<div class="arch-acc">
    <div class="arch-tabs">${tabs}<span class="arch-sum">${active.headSpan}</span></div>
    <div class="card logcard arch-panel">${active.body}</div>
  </div>`;
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
const CT_ORDER = ["Hauling", "Bounty / Combat", "Delivery", "Other"];
const ctSlug = t => ({ "Hauling": "haul", "Bounty / Combat": "bounty",
  "Delivery": "deliver", "Other": "other" }[t] || "other");

// Contract Log type filter — a set of EXCLUDED types (empty = show all), persisted.
// The open/closed dropdown state lives in globals so the 3s poll's re-render preserves
// it; CT_PRESENT caches the types the current data offers (for the All/None buttons).
let CONTRACT_TYPE_HIDDEN = new Set(JSON.parse(localStorage.getItem("ctHidden") || "[]"));
let TYPE_MENU_OPEN = false;
let CT_PRESENT = [];
function _saveCtHidden() { localStorage.setItem("ctHidden", JSON.stringify([...CONTRACT_TYPE_HIDDEN])); }
function _archRepaint() { setHTML("history", sessionsView(SESSIONS)); }
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
  for (const s of sessions || [])
    for (const m of s.missions || [])
      if (m.status !== "unfinished")
        all.push({ when: m.ended_at || m.accepted_at || s.started_at, m, type: contractType(m) });
  // distinct types present, canonical order first then any extras
  CT_PRESENT = [...new Set(all.map(r => r.type))]
    .sort((a, b) => ((CT_ORDER.indexOf(a) + 1) || 99) - ((CT_ORDER.indexOf(b) + 1) || 99));
  const rows = all.filter(r => !CONTRACT_TYPE_HIDDEN.has(r.type))
    .sort((a, b) => (b.when || "").localeCompare(a.when || ""));
  const total = rows.reduce((a, r) => a + (r.m.reward || 0), 0);
  const body = rows.map(r => {
    const dest = (r.m.destinations || []).filter(Boolean);
    return `<tr>
      <td class="lt-when">${fmtWhen(r.when)}</td>
      <td><span class="badge b-${r.m.status}">${esc(r.m.status)}</span></td>
      <td><span class="lt-tag ct-${ctSlug(r.type)}">${esc(r.type)}</span></td>
      <td class="lt-title">${esc(r.m.title)}${dest.length ? ` <span class="sub">→ ${esc(dest.join(", "))}</span>` : ""}</td>
      <td class="lt-num">${r.m.reward ? num(r.m.reward) : "—"}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="lt-empty">No contracts match the selected types.</td></tr>`;
  const hidden = CT_PRESENT.filter(t => CONTRACT_TYPE_HIDDEN.has(t)).length;
  const opts = CT_PRESENT.map(t =>
    `<label class="th-opt"><input type="checkbox" ${CONTRACT_TYPE_HIDDEN.has(t) ? "" : "checked"}
       onclick="toggleTypeFilter('${t.replace(/'/g, "\\'")}')"><span class="lt-tag ct-${ctSlug(t)}">${esc(t)}</span></label>`).join("");
  const menu = `<span class="th-menu-wrap">
    <button class="th-menu-btn${hidden ? " on" : ""}" onclick="toggleTypeMenu()">Type ▾</button>${
      TYPE_MENU_OPEN ? `<span class="th-menu">
        <span class="th-menu-act"><button onclick="setAllTypeFilters(true)">All</button><button onclick="setAllTypeFilters(false)">None</button></span>
        ${opts}</span>` : ""}</span>`;
  const inner = all.length ? `<div class="logwrap"><table class="logtable">
      <thead><tr><th>When</th><th>Status</th><th class="th-type">${menu}</th><th>Contract</th><th class="lt-num">Reward</th></tr></thead>
      <tbody>${body}</tbody></table></div>` : `<div class="empty">No contracts in range.</div>`;
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
      <thead><tr><th>When</th><th>Commodity</th><th>Status</th><th>Route</th><th class="lt-num">SCU</th><th class="lt-num">Cost</th><th class="lt-num">Revenue</th><th class="lt-num">Profit</th></tr></thead>
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
      <thead><tr><th>Commodity</th><th>Route</th><th class="lt-num">Trips</th><th class="lt-num">SCU</th>
        <th class="lt-num">Profit</th><th class="lt-num">%</th><th class="lt-num">/SCU</th></tr></thead>
      <tbody>${body}</tbody></table>`;
}

async function loadSessions() {
  try {
    SESSIONS = await (await fetch("/api/sessions", { cache: "no-store" })).json();
  } catch (e) { SESSIONS = SESSIONS || []; }
  if (ARCH_PICK) { if (!ARCH_OPEN) ARCH_OPEN = archDefaultSection(); ARCH_PICK = false; }  // only when none open
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
  const inner = list.length ? `<div class="logwrap"><table class="logtable">
      <thead><tr><th>Session</th><th>Player</th><th>Ship(s)</th><th class="lt-num">Earned</th>
        <th class="lt-num">Done</th><th class="lt-num">Trades</th><th>Replay</th></tr></thead>
      <tbody>${body}</tbody></table></div>` : `<div class="empty">No archived sessions yet.</div>`;
  return logSection("sessions", `Sessions · ${list.length}`,
                    `<span class="scu">${list.length} archived</span>`, inner);
}

// ---- replay controls ---- //
// Enable a session for replay: fetch its scrub timeline, default to the final state,
// then drive the whole dashboard from the reconstructed snapshot. Live polling pauses.
async function enterReplay(key) {
  try {
    const tl = await (await fetch(`/api/replay/timeline?key=${encodeURIComponent(key)}`, { cache: "no-store" })).json();
    if (!tl.available || !tl.count) {
      REPLAY_UNAVAILABLE.add(key);
      setHTML("history", sessionsView(SESSIONS));
      return;
    }
    REPLAY_KEY = key; REPLAY_POINTS = tl.points; REPLAY_I = tl.count - 1; REPLAY_MODE = true;
    await loadReplayState();                       // sets REPLAY_SNAPSHOT + renders all tabs
    renderReplayBar();
    setHTML("history", sessionsView(SESSIONS));     // reflect the active-replay row state
  } catch (e) {
    REPLAY_UNAVAILABLE.add(key);
    setHTML("history", sessionsView(SESSIONS));
  }
}

// Fetch the snapshot for the current checkpoint and repaint every tab from it.
async function loadReplayState() {
  const bar = $("replaybar"); if (bar) bar.classList.add("rb-busy");
  try {
    const snap = await (await fetch(
      `/api/replay/state?key=${encodeURIComponent(REPLAY_KEY)}&at=${REPLAY_I}`, { cache: "no-store" })).json();
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
  renderReplayBar();
  if (LAST) renderAll(curData());                 // back to the live snapshot
  setHTML("history", sessionsView(SESSIONS));
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
  bar.innerHTML = `<span class="rb-tag">▶ REPLAY</span>
    <span class="rb-sess">${esc(fmtWhen(sess))}</span>
    <button class="rb-step" onclick="scrubStep(-1)" title="previous checkpoint">◀</button>
    <input id="rb-scrub" class="rb-scrub" type="range" min="0" max="${Math.max(0, n - 1)}"
      value="${REPLAY_I}" oninput="scrubTo(this.value)">
    <button class="rb-step" onclick="scrubStep(1)" title="next checkpoint">▶</button>
    <span id="rb-pos" class="rb-pos"></span>
    <span id="rb-when" class="rb-when"></span>
    <span id="rb-label" class="rb-label"></span>
    <button class="rb-exit" onclick="exitReplay()">Exit replay</button>`;
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

// ---- poll loop ---- //
async function refresh() {
  if (REPLAY_MODE) return;   // replay pauses live polling; exitReplay() resumes it
  try {
    const d = await (await fetch("/api/state", { cache: "no-store" })).json();
    LAST = d;
    renderAll(curData());                  // render every tab from the live snapshot
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
