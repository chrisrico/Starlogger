"use strict";
// Mining tab: the Radar-Signature (RS) reference tools — Find (mineral → which RS to scan +
// ranked source rocks) and Plan (refining blueprint → deposit coverage + sources). The former
// Identify tool moved out to the top-level Signal ID page (signal.js); the rock-render helpers
// it still imports (mineralUnion/elBadge/mechHtml/feasibilityHtml) are exported from here. The
// rest of the dashboard only calls initMining() (on tab open) and the bridged inline handlers
// (miningSub + the find/plan/bp actions).
import { $, esc, num, val, th, tag, setHTML, logTable, tabBar, hintIcon } from "./dom.js";
import { getJSON, writeHeaders } from "./net.js";
import { ensureGear, currentLoadout, gearCatalog } from "./shipequip.js";
// feasibility()/suggestCrack() are globals from the classic /feasibility.js script (loaded
// before app.js), shared with the Node unit test — same pattern as cargogrid.js's window.*.
const { feasibility, suggestCrack } = window;

// ============================================================================ //
// Mining tab — RS (radar signature) + composition tools. Self-contained and
// independent of the live /api/state poll: it reads the p4k-derived mineables
// catalog via /api/{rock-lookup,rock-decompose,mineral-lookup,mineral-index,
// mining-plan}. All three sub-tools (and their own #mres-<sub> results) are built
// once; switching sub-tabs only toggles which is visible, so each keeps its inputs,
// results, and scroll. Submitting a query repaints just that sub's #mres-<sub>.
// ============================================================================ //
let MINING_SUB = "find";           // find | plan
let MINING_MINERALS = null;        // cached mineral names for the autocomplete
let MINING_BLUEPRINTS = null;      // cached {name, category} catalog for the picker
let FIND_LAST = null;              // last mineral-lookup result — re-ranked on loadout change
let MINING_INIT = false;
// Plan sub: the per-blueprint build quantity, keyed by blueprint name. Persisted so a planned
// crafting run survives a reload. The whole catalog is one table with an inline qty input per row;
// the materials breakdown is summed from the rows whose quantity > 0 — no separate list, and Clear
// resets every quantity to 0.
let BP_QTY = (() => { try { return JSON.parse(localStorage.getItem("bpQty") || "{}"); } catch (_) { return {}; } })();
const _bpSave = () => { try { localStorage.setItem("bpQty", JSON.stringify(BP_QTY)); } catch (_) {} };
// Plan sub: the table viewport's user-dragged height (resize:vertical on .bp-pick), persisted so a
// taller/shorter table survives a reload. 0 = unset → CSS default (400px).
let BP_H = (() => { const v = parseInt(localStorage.getItem("bpPickH"), 10); return v > 0 ? v : 0; })();

export async function initMining() {
  if (!MINING_INIT) {
    MINING_INIT = true;
    const grab = async (url, key) => {
      try { return (await getJSON(url))[key] || []; }
      catch (e) { return []; }
    };
    [MINING_MINERALS, MINING_BLUEPRINTS] = await Promise.all([
      grab("/api/minerals", "minerals"), grab("/api/blueprints", "blueprints")]);
    ensureGear();   // preload the mining-gear catalog for the feasibility verdict (fire-and-forget)
    // Re-rank the current Find results when the ship loadout changes (popup save) — it surfaces
    // the minability of the equipped ship.
    document.addEventListener("loadout-changed", () => {
      if (FIND_LAST) setHTML("mres-find", FIND_LAST.index
        ? indexResultHtml(FIND_LAST.index) : findResultHtml(FIND_LAST));
    });
    document.addEventListener("click", (e) => {   // close an open column-filter dropdown
      if (!e.target.closest("#bp-fpop") && !e.target.closest(".bp-fbtn")) _bpFclose();
    });
    // Build the shell now the catalogs are in. Rebuild even if an early miningSub() (a deep-link
    // or reload to a #sub hash runs miningSub synchronously, before this fetch resolves) already
    // built one — that early shell has an empty blueprint table.
    renderMiningShell();
    return;
  }
  // Re-entry (tab re-opened): build once if the shell is gone, else keep it and its state.
  if (!$("msub-find")) renderMiningShell();
}
// Switch sub-tabs by toggling visibility — never rebuild, so each sub keeps its state.
export function miningSub(sub) {
  MINING_SUB = sub;
  // Reflect into the URL #hash (/mining#find|#plan) so the active tool is a
  // shareable deep link, matching the Cargo sub-tab scheme in app.js. replaceState only —
  // toggling tools shouldn't grow the back stack. (Only meaningful on the Mining page, the
  // sole place this runs.)
  if (location.hash.slice(1) !== sub) history.replaceState(null, "", location.pathname + "#" + sub);
  if (!$("msub-" + sub)) { renderMiningShell(); return; }
  document.querySelectorAll("#mining .arch-tab").forEach(b => b.classList.toggle("active", b.dataset.sub === sub));
  document.querySelectorAll("#mining .msub").forEach(el => el.classList.toggle("hide", el.id !== "msub-" + sub));
}
// The active sub's results container — every tool repaints into its own #mres-<sub>.
const mres = () => "mres-" + MINING_SUB;

const _pct = (x) => (x == null ? "?" : Math.round(x));
const _chance = (p) => (p == null ? "" : Math.round(p * 100) + "%");

function renderMiningShell() {
  const subs = [["find", "Find mineral", findToolHtml], ["plan", "Blueprint plan", planToolHtml]];
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
  // Paint the materials breakdown now the Plan sub's results div exists (empty-state until a
  // quantity is set).
  renderBpPlan();
  _bpWatchHeight();
}
// Persist the blueprint table's user-dragged height (resize:vertical). The element is rebuilt on
// every shell render, so re-observe the current one each time; ResizeObserver fires on drag.
let _bpRO = null;
function _bpWatchHeight() {
  const el = document.querySelector("#mining .bp-pick");
  if (!el) return;
  if (_bpRO) _bpRO.disconnect();
  _bpRO = new ResizeObserver(() => {
    const h = Math.round(el.getBoundingClientRect().height);
    if (h && h !== BP_H) { BP_H = h; try { localStorage.setItem("bpPickH", h); } catch (_) {} }
  });
  _bpRO.observe(el);
}

// small shared bits ---------------------------------------------------------- //
export function elBadge(e) {
  return `<span class="mn-el"><b>${esc(e.element)}</b>` +
    ` <span class="mn-pct">${_pct(e.min_pct)}–${_pct(e.max_pct)}%</span>` +
    (e.probability != null ? ` <span class="mn-prob">${_chance(e.probability)}</span>` : "") + `</span>`;
}
// Dedupe a rock list's composition to the distinct possible minerals (keep the
// richest occurrence), so an ambiguous RS shows "what might be in there".
export function mineralUnion(rocks) {
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
export function mechHtml(rocks) {
  const m = (rocks || []).map(r => r.mechanics).find(Boolean);
  if (!m) return "";
  const bits = [];
  if (m.laser_power != null) bits.push(`laser ≥${num(m.laser_power)}`);
  if (m.resistance != null) bits.push(`resistance ${m.resistance.toFixed(2)}`);
  if (m.instability != null) bits.push(`instability ${num(Math.round(m.instability))}`);
  if (m.window_thinness != null) bits.push(`window thinness ${m.window_thinness.toFixed(2)}`);
  if (m.mass != null) bits.push(`mass ${num(m.mass)}`);
  if (!bits.length) return "";
  return `<div class="mrow"><span class="mk">cracking</span>
    <div class="mels mn-dim">${esc(bits.join(" · "))}</div></div>`;
}

// Minability ordering (best first) for ranking source rocks; unknown/unjudged sinks last.
const _FEAS_ORDER = { easy: 0, ok: 1, hard: 2, no: 3 };
const feasOrder = (f) => (f ? _FEAS_ORDER[f.tier] : 99);
// A compact verdict pill (shared by the Identify card + the Find table's Mine column).
const feasPill = (f) => f
  ? `<span class="feas feas-${f.tier}" title="${esc(f.factors.join(" · "))}">${esc(f.label)}</span>`
  : `<span class="mn-dim">—</span>`;
// The equipped mining loadout, or null when the current ship isn't a miner / has no head.
function equippedLoadout() {
  const lo = currentLoadout();
  return lo && lo.isMiningShip && lo.head ? lo : null;
}

// The feasibility row for an Identify candidate card: "" when the current ship isn't a miner
// or has no rock mechanics; a "set up gear" nudge when it's a miner with nothing fitted; else
// a coloured verdict pill + the contributing factors. Uses the first rock that carries mechanics.
export function feasibilityHtml(rocks) {
  const lo = currentLoadout();
  if (!lo || !lo.isMiningShip) return "";          // only meaningful for the equipped mining ship
  const m = (rocks || []).map(r => r.mechanics).find(Boolean);
  if (!m) return "";
  if (!lo.head) {
    return `<div class="mrow"><span class="mk">your ship</span>
      <div class="mels"><button class="feas-setup" onclick="openShipEquip()">⚙ set up mining gear</button></div></div>`;
  }
  const f = feasibility(m, lo.head, lo.modules);
  if (!f) return "";
  return `<div class="mrow"><span class="mk">your ship</span>
    <div class="mels">${feasPill(f)}
      <span class="mn-dim feas-factors">${esc(f.factors.join(" · "))}</span></div></div>`
    + (f.tier === "no" ? suggestHtml(m, lo) : "");
}

// When the equipped gear can't crack a rock, suggest a laser/module combo that would (within
// the ship's hardpoint sizes), or flag that it needs a bigger mining ship. "" when the catalog
// hasn't loaded yet or nothing helps.
function suggestHtml(mech, lo) {
  const cat = gearCatalog();
  if (!cat.heads.length) return "";                // gear catalog not fetched yet
  const s = suggestCrack(mech, cat.heads, cat.modules, lo.hardpoints);
  if (!s) return "";
  if (s.needSize) {
    return `<div class="mrow"><span class="mk"></span>
      <div class="mels feas-suggest">✦ needs an <b>S${s.needSize}</b> mining ship${
        s.needSize >= 2 ? " (e.g. MOLE)" : ""} — no S${Math.max(...lo.hardpoints, 0)} laser can crack it</div></div>`;
  }
  const { head, modules, result } = s.combo;
  const sameHead = lo.head && head.class === lo.head.class;
  const mods = modules.map(x => x.name).join(" + ");
  // Same head you have -> point at the modules to swap in; otherwise name the whole combo.
  const gear = sameHead ? `your ${head.name}${mods ? " + " + mods : ""}`
                        : [head.name, ...modules.map(x => x.name)].join(" + ");
  return `<div class="mrow"><span class="mk"></span>
    <div class="mels feas-suggest">✦ try ${esc(gear)}
      <span class="mn-dim">→ ${esc(result.label)}</span></div></div>`;
}

// ---- Find: mineral → RS to scan for + ranked source rocks (+ browse all) ---- //
function findToolHtml() {
  return `<div class="card mtool"><h3><span>Mineral → where to mine ${hintIcon(
      "Shows the RS value(s) to scan for and the richest source rocks, ranked by probability × yield.")}</span></h3>
    <div class="mform">
      <input id="mf-name" list="dl_mineral" placeholder="e.g. Bexalite" autocomplete="off"
        aria-label="Mineral name" onkeydown="if(event.key==='Enter')miningFind()">
      <button class="primary" onclick="miningFind()">Find</button>
      <button onclick="miningIndex()">Browse all</button>
    </div>
  </div>`;
}
export async function miningFind() {
  const name = val("mf-name").trim();
  if (!name) { setHTML(mres(), `<div class="empty">Enter or pick a mineral.</div>`); return; }
  setHTML(mres(), `<div class="empty">searching…</div>`);
  try {
    const r = await fetch(`/api/mineral-lookup?name=${encodeURIComponent(name)}`).then(x => x.json());
    FIND_LAST = r;
    setHTML(mres(), findResultHtml(r));
  } catch (e) { setHTML(mres(), `<div class="empty">lookup failed</div>`); }
}
function findResultHtml(r) {
  if (!r.rocks || !r.rocks.length) return `<div class="empty">No rock yields “${esc(r.mineral)}”.</div>`;
  const sigs = (r.signatures || []).map(s => `<span class="mscan-rs">${num(s)}</span>`).join("");
  // With the current ship's gear, judge each source rock's minability and rank by it (best
  // first), then by yield score; without gear, fall back to the server's yield ranking.
  const lo = equippedLoadout();
  const rocks = r.rocks.map(x => ({ ...x, _f: lo ? feasibility(x.mechanics, lo.head, lo.modules) : null }));
  if (lo) rocks.sort((a, b) => feasOrder(a._f) - feasOrder(b._f) || (b.score || 0) - (a.score || 0));
  const rows = rocks.map(x => `<tr>
    ${lo ? `<td>${feasPill(x._f)}</td>` : ""}
    <td class="lt-num">${num(x.rs)}</td><td>${esc(x.name)}</td>
    <td class="lt-num">${_pct(x.min_pct)}–${_pct(x.max_pct)}%</td>
    <td class="lt-num">${_chance(x.probability)}</td><td class="lt-num">${x.score}</td></tr>`).join("");
  const note = lo
    ? `<div class="mscan-note mn-dim">Ranked by minability with <b>${esc(lo.ship)}</b> — ${esc(lo.head.name)}${lo.modules.length ? " + " + lo.modules.map(m => esc(m.name)).join(", ") : ""}</div>`
    : `<div class="mscan-note mn-dim">Pick a mining ship + gear (⚙) to rank these by minability.</div>`;
  return `<div class="card">
    <div class="mscan"><span class="mscan-k">Scan for</span>
      <div class="mscan-vals">${sigs || '<span class="mn-dim">—</span>'}</div></div>
    ${note}
    ${logTable(
      (lo ? th("Mine", false, "Minability with your current ship's mining gear") : "") +
      th("RS", true, "Radar signature a single rock of this type reads") +
      th("Rock", false, "The mineable rock / deposit type") +
      th("Yield %", true, `Percentage of ${esc(r.mineral)} in the rock (min–max)`) +
      th("Chance", true, "Probability a rock of this type actually contains it") +
      th("Score", true, "Source ranking = probability × yield (higher is a better source)"),
      rows, "")}
  </div>`;
}
export async function miningIndex() {
  setHTML(mres(), `<div class="empty">loading…</div>`);
  try {
    const r = await fetch("/api/mineral-index").then(x => x.json());
    FIND_LAST = { index: r.minerals || [] };           // re-rank on loadout change
    setHTML(mres(), indexResultHtml(r.minerals || []));
  } catch (e) { setHTML(mres(), `<div class="empty">load failed</div>`); }
}
// A mineral's minability = the feasibility of its EASIEST source. Sources now differ in
// break difficulty (a mixed asteroid is harder than the pure surface deposit of the same
// ore), so we take the most-mineable one: if any source is crackable with this gear, the
// mineral is obtainable.
function mineralFeas(m, lo) {
  if (!lo) return null;
  const fs = (m.rocks || [])
    .map(x => x.mechanics && feasibility(x.mechanics, lo.head, lo.modules))
    .filter(Boolean);
  return fs.length ? fs.reduce((a, b) => (feasOrder(b) < feasOrder(a) ? b : a)) : null;
}
function indexResultHtml(minerals) {
  if (!minerals.length) return `<div class="empty">No mineral data.</div>`;
  const lo = equippedLoadout();
  const list = minerals.map(m => ({ ...m, _f: mineralFeas(m, lo) }));
  // Rank by minability (best first), then mineral name; without gear keep the A–Z order.
  if (lo) list.sort((a, b) => feasOrder(a._f) - feasOrder(b._f) || a.mineral.localeCompare(b.mineral));
  const rows = list.map(m => `<tr>
    ${lo ? `<td>${feasPill(m._f)}</td>` : ""}
    <td><b>${esc(m.mineral)}</b></td>
    <td>${(m.signatures || []).slice(0, 8).map(num).join(", ")}</td>
    <td>${m.rocks.slice(0, 4).map(x => esc(x.name)).join("; ")}${m.rocks.length > 4 ? ` <span class="mn-dim">…+${m.rocks.length - 4}</span>` : ""}</td>
  </tr>`).join("");
  const note = lo
    ? `<div class="mscan-note mn-dim">Ranked by minability with <b>${esc(lo.ship)}</b> — ${esc(lo.head.name)}${lo.modules.length ? " + " + lo.modules.map(x => esc(x.name)).join(", ") : ""}</div>`
    : `<div class="mscan-note mn-dim">Pick a mining ship + gear (⚙) to rank these by minability.</div>`;
  return `<div class="card"><h3><span>All minerals → source rocks</span><span class="scu">${minerals.length}</span></h3>` +
    note +
    logTable(
      (lo ? th("Mine", false, "Minability of this mineral's best source with your current gear") : "") +
      th("Mineral", false, "The refined mineral") +
      th("RS to scan", false, "Radar signature value(s) whose rocks can contain it") +
      th("Best sources", false, "The richest source rocks for this mineral"),
      rows, "") + `</div>`;
}

// ---- Plan: the blueprint build table ---- //
// One row per craftable blueprint, columns Name/Type/Subtype/Class/Quality/Size + an inline Qty.
// Every column has a spreadsheet-style multi-select filter and is click-to-sort; clicking a row
// toggles its quantity on/off. The materials breakdown below sums every row with quantity > 0.
const BP_COLS = [
  { key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "subtype", label: "Subtype" },
  { key: "cls", label: "Class" }, { key: "quality", label: "Quality" }, { key: "size", label: "Size" },
];
let BP_FILTERS = {};   // col -> Set of EXCLUDED values (unchecked in its dropdown); empty/absent = all
let BP_SORT = null;    // { col, dir: 1 | -1 }
const _bpNum = (k) => k === "size";
const _bpRA = (k) => _bpNum(k) || k === "quality";   // right-align the numeric-ish columns
const _bpCell = (b, k) => { const v = b[k]; return (v === "" || v == null) ? "" : String(v); };

function blueprintTableHtml() {
  const head = BP_COLS.map(c =>
    `<th data-col="${c.key}"${_bpRA(c.key) ? ' class="lt-num"' : ""}><span class="bp-h" onclick="bpSort('${c.key}')">${c.label}<span class="bp-sort" id="bps-${c.key}"></span></span>${c.key === "name" ? "" : `<button class="bp-fbtn" title="Filter ${c.label}" onclick="bpFilterOpen(event,'${c.key}')"><svg viewBox="0 0 12 12" width="9" height="9" aria-hidden="true"><path d="M1 2.5h10l-3.8 4.2v3.6l-2.4-1.3V6.7z" fill="currentColor"/></svg></button>`}</th>`
  ).join("") + `<th class="lt-num">Qty</th>`;
  const rows = (MINING_BLUEPRINTS || []).map((b, i) => {
    const q = BP_QTY[b.name] || 0;
    const cells = BP_COLS.map(c => {
      const v = _bpCell(b, c.key);
      return `<td${_bpRA(c.key) ? ' class="lt-num"' : ""}>${c.key === "name" ? `<b>${esc(v)}</b>` : esc(v)}</td>`;
    }).join("");
    return `<tr class="bp-prow${q ? " bp-on" : ""}" data-i="${i}" onclick="bpRowClick(event,${i})">${cells}<td class="bp-qcell" onclick="event.stopPropagation()"><button class="bp-step" aria-label="One fewer" onclick="bpStep(${i},-1)">−</button><input type="number" min="0" class="bp-qin" value="${q}" aria-label="Quantity of ${esc(b.name)}" oninput="bpQtyInput(${i},this.value)"><button class="bp-step" aria-label="One more" onclick="bpStep(${i},1)">+</button></td></tr>`;
  }).join("");
  return `<table class="logtable bp-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}
function planToolHtml() {
  return `<div class="card mtool"><h3><span>Blueprints ${hintIcon(
      "Every craftable blueprint. Filter any column (multi-select, like a spreadsheet), click a header " +
      "to sort, click a row to toggle it on, and set a quantity. Materials are summed below across " +
      "everything with a quantity, then ranked by deposit coverage.")}</span>
      <button class="bp-clear" onclick="bpClearList()" title="Reset every quantity to 0">Clear</button></h3>
    <div class="bp-pick"${BP_H ? ` style="height:${BP_H}px"` : ""}>${blueprintTableHtml()}</div>
  </div>`;
}
let _bpTimer = 0;
// Set the blueprint at catalog index i to quantity n (0 clears it): update the row state + input in
// place (never rebuild the table) and debounce the breakdown refresh.
function _bpApply(i, n) {
  const b = (MINING_BLUEPRINTS || [])[i]; if (!b) return;
  n = Math.max(0, n | 0);
  if (n > 0) BP_QTY[b.name] = n; else delete BP_QTY[b.name];
  _bpSave();
  const tr = document.querySelector(`#mining .bp-table tbody tr[data-i="${i}"]`);
  if (tr) {
    tr.classList.toggle("bp-on", n > 0);
    const inp = tr.querySelector(".bp-qin"); if (inp && +inp.value !== n) inp.value = n;
  }
  clearTimeout(_bpTimer); _bpTimer = setTimeout(renderBpPlan, 250);
}
export function bpStep(i, d) { const b = (MINING_BLUEPRINTS || [])[i]; if (b) _bpApply(i, (BP_QTY[b.name] || 0) + d); }
export function bpQtyInput(i, v) { _bpApply(i, parseInt(v, 10) || 0); }
// Click a row (outside its qty cell) to toggle it between 0 and 1 — the easy "add one".
export function bpRowClick(e, i) {
  if (e.target.closest(".bp-qcell")) return;
  const b = (MINING_BLUEPRINTS || [])[i]; if (b) _bpApply(i, (BP_QTY[b.name] || 0) > 0 ? 0 : 1);
}
// Reset every quantity to 0 (zero the inputs + clear row highlights in place) and the breakdown.
export function bpClearList() {
  BP_QTY = {}; _bpSave();
  for (const tr of document.querySelectorAll("#mining .bp-table tbody tr")) {
    tr.classList.remove("bp-on"); const inp = tr.querySelector(".bp-qin"); if (inp) inp.value = 0;
  }
  renderBpPlan();
}
// ---- click a header to sort by that column; click again to reverse ---- //
export function bpSort(col) {
  BP_SORT = BP_SORT && BP_SORT.col === col ? { col, dir: -BP_SORT.dir } : { col, dir: 1 };
  const tbody = document.querySelector("#mining .bp-table tbody"); if (!tbody) return;
  const rows = [...tbody.children];
  rows.sort((ra, rb) => {
    let a = (MINING_BLUEPRINTS[+ra.dataset.i] || {})[col], b = (MINING_BLUEPRINTS[+rb.dataset.i] || {})[col];
    if (_bpNum(col)) return ((a == null ? -1 : +a) - (b == null ? -1 : +b)) * BP_SORT.dir;
    a = (a == null ? "" : String(a)).toLowerCase(); b = (b == null ? "" : String(b)).toLowerCase();
    return (a < b ? -1 : a > b ? 1 : 0) * BP_SORT.dir;
  });
  const f = document.createDocumentFragment();
  for (const tr of rows) f.appendChild(tr);
  tbody.appendChild(f);
  for (const c of BP_COLS) { const el = $("bps-" + c.key); if (el) el.textContent = c.key === col ? (BP_SORT.dir > 0 ? " ▲" : " ▼") : ""; }
}
// ---- per-column multi-select filter (a checklist of the column's distinct values) ---- //
// Rows passing every column's filter EXCEPT `skip` — so a column's own dropdown lists the values
// still reachable given the OTHER active filters (and you can always re-check what you hid here).
const _bpVisibleExcept = (skip) => (MINING_BLUEPRINTS || []).filter(b =>
  BP_COLS.every(c => c.key === skip || !(BP_FILTERS[c.key] && BP_FILTERS[c.key].has(_bpCell(b, c.key)))));
const _bpDistinct = (col) => {
  const vals = [...new Set(_bpVisibleExcept(col).map(b => _bpCell(b, col)))];
  vals.sort(_bpNum(col) ? (a, b) => (+a) - (+b) : (a, b) => a.localeCompare(b));
  return vals;
};
let _bpFcol = null;
export function bpFilterOpen(e, col) {
  e.stopPropagation();
  let pop = $("bp-fpop");
  if (!pop) { pop = document.createElement("div"); pop.id = "bp-fpop"; pop.className = "bp-fpop"; document.body.appendChild(pop); }
  if (_bpFcol === col && pop.classList.contains("open")) { _bpFclose(); return; }
  _bpFcol = col;
  const ex = BP_FILTERS[col] || new Set();
  const opts = _bpDistinct(col).map(v =>
    `<label class="bp-fopt"><input type="checkbox" value="${esc(v)}" ${ex.has(v) ? "" : "checked"} onchange="bpFilterToggle(this.checked,this.value)"><span>${v === "" ? "(blank)" : esc(v)}</span></label>`).join("");
  pop.innerHTML = `<div class="bp-fhead"><input class="bp-fsearch" placeholder="search…" aria-label="search values" oninput="bpFilterSearch(this.value)"><label class="bp-fall"><input type="checkbox" ${ex.size ? "" : "checked"} onchange="bpFilterAll(this.checked)">All</label></div><div class="bp-fopts">${opts}</div>`;
  const r = e.currentTarget.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 248)) + "px";
  pop.style.top = (r.bottom + 4) + "px";
  pop.classList.add("open");
}
function _bpFclose() { const p = $("bp-fpop"); if (p) p.classList.remove("open"); _bpFcol = null; }
export function bpFilterToggle(checked, value) {
  if (!_bpFcol) return;
  const ex = BP_FILTERS[_bpFcol] || (BP_FILTERS[_bpFcol] = new Set());
  if (checked) ex.delete(value); else ex.add(value);
  if (!ex.size) delete BP_FILTERS[_bpFcol];
  _bpApplyFilter();
}
export function bpFilterAll(checked) {
  if (!_bpFcol) return;
  if (checked) delete BP_FILTERS[_bpFcol]; else BP_FILTERS[_bpFcol] = new Set(_bpDistinct(_bpFcol));
  for (const cb of document.querySelectorAll("#bp-fpop .bp-fopts input")) cb.checked = checked;
  _bpApplyFilter();
}
export function bpFilterSearch(q) {
  q = (q || "").trim().toLowerCase();
  for (const lab of document.querySelectorAll("#bp-fpop .bp-fopt"))
    lab.style.display = (!q || lab.textContent.toLowerCase().includes(q)) ? "" : "none";
}
function _bpApplyFilter() {
  for (const tr of document.querySelectorAll("#mining .bp-table tbody tr")) {
    const b = MINING_BLUEPRINTS[+tr.dataset.i] || {};
    tr.style.display = BP_COLS.every(c => { const ex = BP_FILTERS[c.key]; return !ex || !ex.has(_bpCell(b, c.key)); }) ? "" : "none";
  }
  for (const c of BP_COLS) { const th = document.querySelector(`#mining .bp-table th[data-col="${c.key}"]`); if (th) th.classList.toggle("bp-filtered", !!BP_FILTERS[c.key]); }
}
const _miningDur = (s) => {
  s = Math.round(s || 0); const m = Math.floor(s / 60), sec = s % 60;
  return m ? `${m}m${sec ? " " + sec + "s" : ""}` : `${sec}s`;
};
// Sum the materials of every blueprint with a quantity (server) → one deposit-coverage plan,
// painted into the Plan sub's results div below the table.
async function renderBpPlan() {
  const out = "mres-plan";
  const items = Object.entries(BP_QTY).filter(([, q]) => q > 0).map(([name, qty]) => ({ name, qty }));
  if (!items.length) {
    setHTML(out, `<div class="empty">Set a quantity on one or more blueprints to see the materials you'll need.</div>`);
    return;
  }
  setHTML(out, `<div class="empty">summing materials…</div>`);
  try {
    const agg = await fetch("/api/blueprints-plan", {
      method: "POST", headers: writeHeaders(), body: JSON.stringify({ items }),
    }).then(r => r.json());
    const plan = await fetch("/api/mining-plan", {
      method: "POST", headers: writeHeaders(), body: JSON.stringify({ minerals: agg.minerals || [] }),
    }).then(r => r.json());
    setHTML(out, breakdownHtml(agg) + planResultHtml(plan));
  } catch (e) { setHTML(out, `<div class="empty">plan failed</div>`); }
}
// The merged shopping list: every chosen recipe's materials summed by resource.
function breakdownHtml(agg) {
  const rows = (agg.requirements || []).map(r => `<tr>
    <td><b>${esc(r.resource)}</b></td>
    <td class="lt-num">${num(r.scu)} SCU</td>
    <td class="lt-num">${r.min_quality > 0 ? "Q≥" + r.min_quality : "—"}</td>
    <td>${(r.from || []).map(f => tag(f.qty > 1 ? `${f.name} ×${f.qty}` : f.name)).join(" ")}</td>
  </tr>`).join("");
  const meta = [
    agg.total_scu ? `${num(agg.total_scu)} SCU total` : "",
    agg.craft_seconds ? _miningDur(agg.craft_seconds) + " craft" : "",
  ].filter(Boolean).join(" · ");
  return `<div class="card"><h3><span>Materials needed</span><span class="scu">${meta}</span></h3>
    ${logTable(
      th("Material", false, "The mineral or resource to mine and refine") +
      th("Qty", true, "Total amount across all chosen blueprints, in SCU") +
      th("Min quality", true, "Strictest refined quality any chosen blueprint requires (— = any)") +
      th("For", false, "Which blueprints need this material"),
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
