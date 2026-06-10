"use strict";
// Archive tab (Contract Log · Trade Loads · Travel Log · Sessions) + session replay. Pools
// every archived session with the live one, builds the cross-session logs and the trade-route
// recommendations, and drives the whole dashboard through a reconstructed past snapshot while
// replaying. Shares the hot state (S.LAST / S.REPLAY_* / S.ROUTE_ORDER) via state.js, calls
// the core render dispatch (renderAll / replayEdit), and pulls live data via stream's refresh.
import { $, esc, num, setHTML, logTable, footLbl, footNum, th, tag, tabBar } from "./dom.js";
import { postJSON, postRaw, getJSON } from "./net.js";
import { S, REPLAY_UNAVAILABLE, curData } from "./state.js";
import { renderAll, replayEdit } from "./app.js";
import { refresh } from "./stream.js";

let SESSIONS = null;     // archived sessions (fetched from /api/sessions)
let _scrubTimer = null;  // debounce for replay scrubbing

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
  for (const t of (S.LAST && S.LAST.trades) || []) if ((t.ts || "") > tT) tT = t.ts || "";
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
// selected section's body (only the active body is built into the DOM). Per-tab totals now
// ride in each table's own <tfoot> column-totals row, so the section carries no summary.
function logSection(key, title, body) {
  return { key, title, body };
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
  const tabs = tabBar(secs.map(s => [s.key, s.title]), ARCH_OPEN, "toggleArch");
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
  for (const t of (S.LAST && S.LAST.trades) || []) add(t);
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
  for (const t of (S.LAST && S.LAST.travels) || []) add(t);  // live session
  rows.sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
  let totalSecs = 0, totalFuel = 0;
  const body = rows.map(t => {
    const status = t.arrived
      ? `<span class="lt-tag good" title="arrived ${esc(t.arrived)}">✔ arrived</span>`
      : `<span class="lt-tag" title="no arrival logged">⋯ in transit</span>`;
    const dur = fmtTravelTime(t.ts, t.arrived);
    if (t.arrived) totalSecs += Math.max(0, (new Date(t.arrived) - new Date(t.ts)) / 1000);
    totalFuel += t.fuel || 0;
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
  // Column totals: total time spent in QT (Time col) and total estimated QT fuel burned.
  const foot = rows.length
    ? footLbl(`${rows.length} jump${rows.length === 1 ? "" : "s"}`, 3)
      + footNum(totalSecs ? fmtElapsed(totalSecs) : "—", "", "total time in quantum travel")
      + "<td></td>"
      + footNum(fuelShort(totalFuel), "", "total estimated QT fuel")
      + "<td></td>"
    : "";
  const inner = logTable(
    `<th>Departed</th><th>Status</th><th>Route</th><th class="lt-num">Time</th><th>System</th><th class="lt-num">QT fuel</th><th>Ship</th>`,
    body, "No quantum travel in range.", foot);
  return logSection("travel", `Travel Log · ${rows.length}`, inner);
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
      <td class="lt-type">${tag(r.type, "ct-" + slugOf[r.type])}</td>
      <td class="lt-title">${esc(r.m.title)}${dest.length ? ` <span class="sub">→ ${esc(dest.join(", "))}</span>` : ""}</td>
      <td class="lt-num">${r.m.reward ? num(r.m.reward) : "—"}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="lt-empty">No contracts match the selected types.</td></tr>`;
  const hidden = CT_PRESENT.filter(t => CONTRACT_TYPE_HIDDEN.has(t)).length;
  const opts = CT_PRESENT.map(t =>
    `<label class="th-opt"><input type="checkbox" ${CONTRACT_TYPE_HIDDEN.has(t) ? "" : "checked"}
       onclick="toggleTypeFilter('${t.replace(/'/g, "\\'")}')">${tag(t, "ct-" + slugOf[t])}</label>`).join("");
  const menu = `<span class="th-menu-wrap">
    <button class="th-menu-btn${hidden ? " on" : ""}" aria-haspopup="true" aria-expanded="${TYPE_MENU_OPEN}" onclick="toggleTypeMenu()">Type ▾</button>${
      TYPE_MENU_OPEN ? `<span class="th-menu">
        <span class="th-menu-act"><button onclick="setAllTypeFilters(true)">All</button><button onclick="setAllTypeFilters(false)">None</button></span>
        ${opts}</span>` : ""}</span>`;
  const typeNote = hidden ? ` · ${CT_PRESENT.length - hidden}/${CT_PRESENT.length} types` : "";
  // Column total: total reward across the shown contracts (Reward col).
  const foot = rows.length
    ? footLbl(`${rows.length} contract${rows.length === 1 ? "" : "s"}${typeNote}`, 4)
      + footNum(num(total), "", "total reward")
    : "";
  const inner = all.length
    ? logTable(`<th>When</th><th>Status</th><th class="th-type">${menu}</th><th>Contract</th>${th("Reward", 1)}`,
               body, "", foot)
    : `<div class="empty">No contracts in range.</div>`;
  return logSection("contracts", `Contract Log · ${rows.length}`, inner);
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
  const LOST = new Set((S.LAST && S.LAST.lost_trades) || []);
  const routesBlock = tradeRoutesBlock(loads, LOST);
  let totalProfit = 0, totSold = 0, totCost = 0, totRev = 0;
  const body = loads.map(L => {
    const sold = L.soldScu, lost = L.id && LOST.has(L.id);
    // a lost load writes off the unsold remainder: realise the FULL buy cost.
    const realisedCost = lost ? L.cost : (L.buyScu ? L.cost * (sold / L.buyScu) : 0);
    const profit = Math.round(L.revenue - realisedCost);
    const priced = sold > 0 || lost;   // lost loads realise even with no sells
    if (priced) totalProfit += profit;
    totSold += sold; totCost += L.cost || 0; totRev += L.revenue || 0;
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
  // Column totals: SCU moved, total buy cost, total revenue, realised profit.
  const loadsFoot = footLbl(`${loads.length} load${loads.length === 1 ? "" : "s"}`, 4)
    + footNum(totSold ? num(Math.round(totSold)) : "—", "", "total SCU sold")
    + footNum(totCost ? "−" + num(Math.round(totCost)) : "—", totCost ? "neg" : "")
    + footNum(totRev ? "+" + num(Math.round(totRev)) : "—", totRev ? "pos" : "")
    + footNum((totalProfit >= 0 ? "+" : "−") + num(Math.abs(totalProfit)), totalProfit >= 0 ? "pos" : "neg", "realised profit");
  const loadsTable = loads.length ? `<table class="logtable">
      <thead><tr><th>When</th><th>Commodity</th><th>Status</th><th>Route</th>${th("SCU", 1)}${th("Cost", 1)}${th("Revenue", 1)}${th("Profit", 1)}</tr></thead>
      <tbody>${body}</tbody><tfoot><tr>${loadsFoot}</tr></tfoot></table>` : `<div class="empty">No manual trades in range.</div>`;
  // both tables share one scroll region (the recs/rank bar scroll with them)
  const inner = `<div class="logwrap">${routesBlock}`
    + `<div class="arch-sub">Loads · ${loads.length}</div>${loadsTable}</div>`;
  return logSection("trades", `Trade Loads · ${loads.length}`, inner);
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
  // Column totals: trips, SCU, profit summed; %/per-SCU as the overall (cost/SCU-weighted) blend.
  const tot = routes.reduce((a, r) => ({
    trips: a.trips + r.trips, scu: a.scu + r.scu, cost: a.cost + r.cost, profit: a.profit + r.profit,
  }), { trips: 0, scu: 0, cost: 0, profit: 0 });
  const oPct = tot.cost ? tot.profit / tot.cost : 0, oPer = tot.scu ? tot.profit / tot.scu : 0;
  const foot = footLbl(`${routes.length} route${routes.length === 1 ? "" : "s"}`, 2)
    + footNum(tot.trips, "", "total trips")
    + footNum(num(Math.round(tot.scu)), "", "total SCU delivered")
    + footNum(signed(tot.profit), tot.profit >= 0 ? "pos" : "neg", "total profit")
    + footNum(pctFmt(oPct), oPct >= 0 ? "pos" : "neg", "overall % return")
    + footNum(signed(oPer), oPer >= 0 ? "pos" : "neg", "overall aUEC / SCU");
  return `<div class="arch-sub">Top routes · ${routes.length}</div>` + recs + bar
    + `<table class="logtable">
      <thead><tr><th>Commodity</th><th>Route</th>${th("Trips", 1)}${th("SCU", 1)}${th("Profit", 1)}${th("%", 1)}${th("/SCU", 1)}</tr></thead>
      <tbody>${body}</tbody><tfoot><tr>${foot}</tr></tfoot></table>`;
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
  if (S.REPLAY_MODE) return replayEdit({ kind: "trade_lost", trade_id: id, lost });
  if (S.LAST) {
    const set = new Set(S.LAST.lost_trades || []);
    lost ? set.add(id) : set.delete(id);
    S.LAST.lost_trades = [...set];
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
  let totEarned = 0, totDone = 0, totContracts = 0, totTrades = 0;
  const body = list.map(s => {
    const dur = fmtDuration(s.started_at, s.ended_at);
    const c = s.counts || {};
    const ships = (s.ships || []).join(", ");
    const trades = (s.trades || []).length;
    totEarned += s.earned || 0; totDone += c.completed || 0; totContracts += c.total || 0; totTrades += trades;
    const replaying = S.REPLAY_MODE && S.REPLAY_KEY === s.key;
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
  // Column totals: aUEC earned, contracts completed/total, trades — across every session.
  const foot = list.length
    ? footLbl(`${list.length} session${list.length === 1 ? "" : "s"}`, 3)
      + footNum(totEarned ? num(totEarned) : "—", "", "total aUEC earned")
      + footNum(`${totDone}/${totContracts}`, "", "contracts completed / total")
      + footNum(totTrades || "—", "", "total trades")
      + "<td></td>"
    : "";
  const inner = logTable(
    `<th>Session</th><th>Player</th><th>Ship(s)</th>${th("Earned", 1)}${th("Done", 1)}${th("Trades", 1)}<th>Replay</th>`,
    body, "No archived sessions yet.", foot);
  return logSection("sessions", `Sessions · ${list.length}`, inner);
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
    S.REPLAY_KEY = key; S.REPLAY_POINTS = tl.points; S.REPLAY_MODE = true;
    S.REPLAY_EDITS = null;                  // fresh sandbox; the server seeds it on first edit
    S.REPLAY_SAVED_ORDER = S.ROUTE_ORDER;     // archive reordering is ephemeral — restore on exit
    // Land on the session's busiest checkpoint (most contracts/cargo on the dashboard)
    // rather than the last one — session-end usually has empty holds and finished
    // contracts, so defaulting there makes replay look like it did nothing. Falls back
    // to the last checkpoint when the session had no cargo activity (e.g. combat-only).
    let best = tl.count - 1, bestFill = 0;
    for (const p of tl.points) { const f = p.fill || 0; if (f >= bestFill) { bestFill = f; best = p.i; } }
    S.REPLAY_I = bestFill > 0 ? best : tl.count - 1;
    await loadReplayState();                       // sets S.REPLAY_SNAPSHOT + renders all tabs
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
    // POST so any ephemeral edits (S.REPLAY_EDITS) stay applied while scrubbing; null overlay
    // returns the cached disk-state snapshot for this checkpoint (unchanged behaviour).
    const snap = await postRaw("/api/replay/state",
      { key: S.REPLAY_KEY, at: S.REPLAY_I, overlay: S.REPLAY_EDITS });
    if (snap && snap.available !== false) { S.REPLAY_SNAPSHOT = snap; renderAll(curData()); }
  } catch (e) { /* leave the prior frame up */ }
  if (bar) bar.classList.remove("rb-busy");
}

// Scrub: move to checkpoint i. Update the bar text immediately (so dragging feels live);
// debounce the snapshot fetch so a fast drag doesn't fire a request per pixel.
function scrubTo(i) {
  S.REPLAY_I = Math.max(0, Math.min(+i, S.REPLAY_POINTS.length - 1));
  updateReplayBar();
  clearTimeout(_scrubTimer);
  _scrubTimer = setTimeout(loadReplayState, 110);
}
function scrubStep(d) { scrubTo(S.REPLAY_I + d); }

function exitReplay() {
  S.REPLAY_MODE = false; S.REPLAY_KEY = null; S.REPLAY_SNAPSHOT = null; S.REPLAY_POINTS = []; S.REPLAY_I = 0;
  S.REPLAY_EDITS = null;                          // discard the ephemeral edits
  S.ROUTE_ORDER = S.REPLAY_SAVED_ORDER; S.REPLAY_SAVED_ORDER = null;   // restore the live route order
  renderReplayBar();
  if (S.LAST) renderAll(curData());                 // back to the live snapshot
  _archRepaint();
  refresh();                                       // resume live polling now
}

// Build the replay banner once (on enter/exit) so the range element stays stable while
// dragging; updateReplayBar() refreshes only the position/time/label text on each scrub.
function renderReplayBar() {
  const bar = $("replaybar"); if (!bar) return;
  const root = document.documentElement.style;
  if (!S.REPLAY_MODE) { bar.classList.add("hide"); bar.innerHTML = ""; root.setProperty("--replay-h", "0px"); return; }
  const n = S.REPLAY_POINTS.length, sess = (S.REPLAY_KEY || "").split("|")[0];
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
          value="${S.REPLAY_I}" oninput="scrubTo(this.value)">
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
  const n = S.REPLAY_POINTS.length, p = S.REPLAY_POINTS[S.REPLAY_I] || {};
  const pos = $("rb-pos"), when = $("rb-when"), label = $("rb-label"), scrub = $("rb-scrub");
  if (pos) pos.textContent = `${S.REPLAY_I + 1}/${n}`;
  if (when) when.textContent = p.ts ? fmtWhen(p.ts) : "";
  if (label) label.textContent = p.label || "";
  if (scrub && +scrub.value !== S.REPLAY_I) scrub.value = S.REPLAY_I;  // keep slider synced for ◀/▶
}

// Activate the Archive tab: flag that a default section should be auto-picked on the next
// load, then fetch. Called by the core tab switcher (activateTab).
function activateArchiveTab() { ARCH_PICK = true; loadSessions(); }

export { loadSessions, activateArchiveTab, typeMark };

// ---- window bridge (this module's inline-handler targets) ---- //
// Same mechanism as the core bridge (see tests/test_window_bridge.py). enterReplay and
// markTradeLost use single-quoted onclick handlers; bridged here all the same.
Object.assign(window, {
  toggleArch, setRouteSort, toggleTypeMenu, toggleTypeFilter, setAllTypeFilters,
  scrubTo, scrubStep, exitReplay, enterReplay, markTradeLost,
});
