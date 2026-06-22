"use strict";
// Mining tab: the Radar-Signature (RS) reference tools — Find (mineral → which RS to scan +
// ranked source rocks) and Plan (refining blueprint → deposit coverage + sources). The former
// Identify tool moved out to the top-level Signal ID page (signal.js); the rock-render helpers
// it still imports (mineralUnion/elBadge/mechHtml/feasibilityHtml) are exported from here. The
// rest of the dashboard only calls initMining() (on tab open) and the bridged inline handlers
// (miningSub + the find/plan/bp actions).
import { $, esc, num, val, th, tag, setHTML, logTable, tabBar, hintIcon } from "./dom.js";
import { getJSON, writeHeaders } from "./net.js";
import { registerCombo, comboInputHtml } from "./combobox.js";
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
let MINING_SHIPS = null;           // cached buildable ship names for the shipbuilder dropdown
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
// Plan sub: "Qty > 0" toggle — when on, the table shows only rows you've given a quantity (the
// selected builds, e.g. after the shipbuilder), on top of any column filters. Persisted.
let BP_ONLY = localStorage.getItem("bpOnly") === "1";

export async function initMining() {
  if (!MINING_INIT) {
    MINING_INIT = true;
    const grab = async (url, key) => {
      try { return (await getJSON(url))[key] || []; }
      catch (e) { return []; }
    };
    [MINING_MINERALS, MINING_BLUEPRINTS] = await Promise.all([
      grab("/api/minerals", "minerals"), grab("/api/blueprints", "blueprints")]);
    // Buildable ships for the shipbuilder combobox: those with craftable components / a radar
    // (concepts excluded), as {name, mfr} sorted by manufacturer then name — matching the header
    // ship picker's grouping so the two dropdowns read the same.
    try {
      const sd = await getJSON("/api/ships");
      MINING_SHIPS = Object.entries(sd.ships || {})
        .filter(([n, e]) => e && (e.components || e.radar) && !/\[[^\]]*concept[^\]]*\]/i.test(n))
        .map(([name, e]) => ({ name, mfr: e.manufacturer || "—" }))
        .sort((a, b) => a.mfr.localeCompare(b.mfr) || a.name.localeCompare(b.name));
    } catch (_) { MINING_SHIPS = []; }
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
  _bpApplyFilter();   // honor a persisted "Qty > 0" toggle on (re)render
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
// Location tags share ONE theme across two axes: colour = WHERE (cyan = planetary surface,
// amber = space asteroid field / belt / Lagrange), and the row lead = HOW (Ship / Hand / ROC).
// Attached by the server to mineral-lookup + mining-plan as
// `locations:[{place,system,kind,method,rarity?,points?}]`. `locKey()` renders the legend.
const METHOD_LABEL = { ship: "Ship", hand: "Hand", ground: "ROC" };
const METHOD_ORDER = ["ship", "hand", "ground"];

// One location -> chip HTML. A surface body / plain field is a single chip; an archetype field
// (Lagrange A..F, Pyro Warm/Cool) carrying grouped `points` renders one head chip per PLANET
// (with the rarity) + that planet's small L# tags, each planet on its own `.mloc-group` row — so
// you see places you can fly to (Crusader L1/L2, Hurston L3) not the opaque archetype label.
function _locChip(l) {
  const field = l.kind === "field";
  const rar = field && l.rarity ? ` <span class="mn-dim">${esc(l.rarity)}</span>` : "";
  const groups = field && Array.isArray(l.points) ? l.points.filter((g) => g && g.planet) : [];
  if (groups.length) {
    const tip = l.place ? ` title="${esc(l.place)}"` : "";
    return groups.map((g) => {
      const ls = (g.lpoints || [])
        .map((p) => `<span class="lt-tag mloc-chip mloc-field mloc-pt">${esc(p)}</span>`)
        .join(" ");
      return `<span class="mloc-group"><span class="lt-tag mloc-chip mloc-field"${tip}>` +
        `${esc(g.planet)}${rar}</span>${ls}</span>`;
    }).join("");
  }
  const sys = l.system ? ` · ${esc(l.system)}` : "";
  return `<span class="lt-tag mloc-chip${field ? " mloc-field" : ""}">${esc(l.place)}${sys}${rar}</span>`;
}

export function locChips(locations, extra = "") {
  if (!locations || !locations.length) return extra ? `<div class="mloc">${extra}</div>` : "";
  // Group by mining method into its own labelled row (Ship / Hand / ROC), in canonical order;
  // colour still distinguishes planetary vs space within each row. See `locKey()`.
  const byMethod = new Map();
  for (const l of locations) {
    const m = l.method || "ship";
    (byMethod.get(m) || byMethod.set(m, []).get(m)).push(l);
  }
  const order = [...METHOD_ORDER.filter((m) => byMethod.has(m)),
                 ...[...byMethod.keys()].filter((m) => !METHOD_ORDER.includes(m))];
  const rows = order.map((m) => {
    const chips = byMethod.get(m).map(_locChip).join(" ");
    return `<div class="mloc-method"><span class="mloc-method-k">${esc(METHOD_LABEL[m] || m)}</span>${chips}</div>`;
  }).join("");
  // `extra` is an optional trailing element (e.g. the mining-contract card's "+N more" chip).
  return `<div class="mloc"><span class="mloc-k">Mined on</span>${rows}${extra}</div>`;
}

// The legend decoding the location tags: colour = where, row lead = how. Rendered once per view
// that shows `locChips` (Find result, Plan sources, mining-contract section).
export function locKey() {
  const sw = (cls, label) => `<span class="mloc-kc"><i class="mloc-sw ${cls}"></i>${label}</span>`;
  const how = Object.values(METHOD_LABEL)
    .map((l) => `<span class="mloc-method-k">${l}</span>`).join("");
  return `<div class="mloc-key"><span class="mloc-key-k">Key</span>` +
    sw("mloc-sw-body", "Planetary surface") +
    sw("mloc-sw-field", "Space — field / Lagrange") +
    `<span class="mloc-kc mloc-key-how">${how}<span class="mn-dim">mining method</span></span></div>`;
}
function findResultHtml(r) {
  // No ship-mineable source rock — but it may still be mined directly (hand cave gem / ROC ore).
  // Show where it IS mineable + the key, instead of a dead "no rock" message.
  if (!r.rocks || !r.rocks.length) {
    if (!r.locations || !r.locations.length)
      return `<div class="empty">No rock yields “${esc(r.mineral)}”.</div>`;
    return `<div class="card">
      <div class="mscan-note mn-dim">No ship-mineable rock yields ${esc(r.mineral)} — it's mined directly:</div>
      ${locChips(r.locations)}
      ${locKey()}</div>`;
  }
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
    ${locChips(r.locations)}
    ${r.locations && r.locations.length ? locKey() : ""}
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
    `<th data-col="${c.key}"${_bpRA(c.key) ? ' class="lt-num"' : ""}><span class="bp-h" onclick="bpSort('${c.key}')">${c.label}<span class="bp-sort" id="bps-${c.key}"></span></span>${c.key === "name" ? "" : `<button class="bp-fbtn" title="Filter ${c.label}" onclick="bpFilterOpen(event,'${c.key}')"><svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M1 2.5h10l-3.8 4.2v3.6l-2.4-1.3V6.7z" fill="currentColor"/></svg></button>`}</th>`
  ).join("") + `<th class="lt-num" data-col="qty">Qty<button class="bp-fbtn" title="Show only rows with a quantity (Qty > 0)" onclick="bpQtyFilter(event)"><svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M1 2.5h10l-3.8 4.2v3.6l-2.4-1.3V6.7z" fill="currentColor"/></svg></button></th>`;
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
// ---- Shipbuilder: outfit a whole ship's components to a chosen class in one click ---- //
// Pick a ship + a component class; /api/ship-build returns the Grade-A blueprints to craft each
// of its slots (chosen class where it makes the part, else the closest class) and we drop those
// quantities into the planner. Always Grade A -- the only tier worth crafting.
const SB_CLASSES = ["Civilian", "Military", "Industrial", "Competition", "Stealth"];
// The ship to outfit, picked via the shared combobox (the same one the header ship picker uses),
// registered under id "sb-ship". SB_SHIP is the committed choice; _sbMatches feeds the options.
let SB_SHIP = "";
const _sbMatches = (filter) => {
  const f = (filter || "").trim().toLowerCase();
  return (MINING_SHIPS || []).filter(s => !f || s.name.toLowerCase().includes(f) || s.mfr.toLowerCase().includes(f));
};
let _sbReg = false;
function _sbRegister() {
  if (_sbReg) return; _sbReg = true;
  registerCombo("sb-ship", { entries: _sbMatches, onPick: (name) => { SB_SHIP = name; }, reset: () => SB_SHIP });
}
function shipbuilderHtml() {
  _sbRegister();
  const types = SB_CLASSES.map(c => `<option value="${c}"${c === "Military" ? " selected" : ""}>${c}</option>`).join("");
  return `<div class="sb-bar">
    <span class="sb-lbl">Outfit a ship ${hintIcon(
      "Pick a ship and a component class — this sets the Grade-A blueprints to craft each of its " +
      "components (power plant, cooler, shield, quantum drive, radar) to that class. Where a class " +
      "doesn't make a part that size, the closest class fills it; sizes with no blueprint are flagged.")}</span>
    ${comboInputHtml("sb-ship", { value: SB_SHIP, placeholder: "search ship…", label: "Ship to outfit", cls: "sb-box" })}
    <select id="sb-type" class="sb-sel" aria-label="Component class">${types}</select>
    <button class="sb-go" onclick="bpBuildShip()">Add builds</button>
    <span id="sb-status" class="sb-status" role="status"></span>
  </div>`;
}
function planToolHtml() {
  return `<div class="card mtool"><h3><span>Blueprints ${hintIcon(
      "Every craftable blueprint. Filter any column (multi-select, like a spreadsheet), click a header " +
      "to sort, click a row to toggle it on, and set a quantity. Materials are summed below across " +
      "everything with a quantity, then ranked by deposit coverage.")}</span>
      <button class="bp-clear" onclick="bpClearList()" title="Reset every quantity to 0">Clear</button></h3>
    ${shipbuilderHtml()}
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
  if (BP_ONLY) _bpApplyFilter();   // a row dropping to 0 (or rising above it) changes what's shown
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
  _bpApplyFilter();   // with "Qty > 0" on, clearing empties the view
  renderBpPlan();
}
// Sync every table row's qty input + on-state from BP_QTY after a bulk change (the shipbuilder),
// without rebuilding the table — keeps the current sort, filters, and scroll.
function _bpSyncRows() {
  for (const tr of document.querySelectorAll("#mining .bp-table tbody tr")) {
    const b = (MINING_BLUEPRINTS || [])[+tr.dataset.i]; if (!b) continue;
    const q = BP_QTY[b.name] || 0;
    tr.classList.toggle("bp-on", q > 0);
    const inp = tr.querySelector(".bp-qin"); if (inp) inp.value = q;
  }
  _bpApplyFilter();   // re-evaluate the "Qty > 0" view after a bulk change
}
// Outfit the picked ship: fetch its build list, set those blueprints' quantities (leaving your
// other picks intact), refresh the plan, and report any closest-class substitutions / unfillable
// slots inline.
export async function bpBuildShip() {
  const ship = SB_SHIP || (($("sb-ship") || {}).value || "").trim();
  const cls = ($("sb-type") || {}).value || "Military";
  const status = $("sb-status");
  if (!ship) { if (status) status.innerHTML = `<span class="sb-warn">Pick a ship first.</span>`; return; }
  if (status) status.textContent = "matching…";
  let r;
  try { r = await getJSON(`/api/ship-build?ship=${encodeURIComponent(ship)}&cls=${encodeURIComponent(cls)}`); }
  catch (e) { if (status) status.innerHTML = `<span class="sb-warn">Lookup failed.</span>`; return; }
  const builds = r.builds || [];
  if (!builds.length) {
    if (status) status.innerHTML = `<span class="sb-warn">No craftable components for ${esc(r.ship || ship)}.</span>`;
    return;
  }
  for (const b of builds) BP_QTY[b.name] = b.qty;
  _bpSave(); _bpSyncRows();
  clearTimeout(_bpTimer); _bpTimer = setTimeout(renderBpPlan, 50);
  const subs = builds.filter(b => b.substituted);
  const parts = [`Set ${builds.length} build${builds.length > 1 ? "s" : ""} — <b>${esc(r.ship || ship)}</b> · ${esc(r.cls || cls)} (Grade A)`];
  if (subs.length) parts.push(`<span class="sb-sub">closest class: ${subs.map(b => `${esc(b.slot)} S${b.size}→${esc(b.cls)}`).join(", ")}</span>`);
  if ((r.unmatched || []).length) parts.push(`<span class="sb-warn">no blueprint: ${r.unmatched.map(u => `${esc(u.slot)} S${u.size}`).join(", ")}</span>`);
  if (status) status.innerHTML = parts.join(" · ");
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
// A row is shown when it clears every column filter AND, if the "Qty > 0" toggle is on, has a qty.
const _bpRowShown = (b) =>
  BP_COLS.every(c => { const ex = BP_FILTERS[c.key]; return !ex || !ex.has(_bpCell(b, c.key)); })
  && (!BP_ONLY || (BP_QTY[b.name] || 0) > 0);
function _bpApplyFilter() {
  for (const tr of document.querySelectorAll("#mining .bp-table tbody tr")) {
    const b = MINING_BLUEPRINTS[+tr.dataset.i] || {};
    tr.style.display = _bpRowShown(b) ? "" : "none";
  }
  for (const c of BP_COLS) { const th = document.querySelector(`#mining .bp-table th[data-col="${c.key}"]`); if (th) th.classList.toggle("bp-filtered", !!BP_FILTERS[c.key]); }
  const qth = document.querySelector('#mining .bp-table th[data-col="qty"]'); if (qth) qth.classList.toggle("bp-filtered", BP_ONLY);
}
// Toggle the "Qty > 0" filter from the Qty column's funnel (the selected-only view); persisted.
export function bpQtyFilter(e) {
  if (e) e.stopPropagation();
  BP_ONLY = !BP_ONLY;
  try { localStorage.setItem("bpOnly", BP_ONLY ? "1" : "0"); } catch (_) {}
  _bpApplyFilter();
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
    setHTML(out, breakdownHtml(agg) + contractsHtml(agg) + planResultHtml(plan));
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
// The other half of "what do I need to build this": per chosen blueprint, the contracts that
// reward it, grouped by the faction that grants them. `sources` rides each aggregate item; we
// tolerate the pre-rebuild flat-string shape defensively.
const _BP_SRC_CAP = 6;
const _bpSource = (s) => (typeof s === "string") ? { faction: s, contracts: [] } : (s || {});
function contractsHtml(agg) {
  let withSrc = 0, noSrc = 0;
  const rows = (agg.items || []).filter(it => it.found).map(it => {
    const srcs = (it.sources || []).map(_bpSource).filter(s => s.faction);
    if (!srcs.length) { noSrc++; return ""; }
    withSrc++;
    const lines = srcs.map(s => {
      const all = s.contracts || [];
      const chips = all.slice(0, _BP_SRC_CAP).map(t => tag(t)).join(" ");
      const more = all.length > _BP_SRC_CAP ? ` <span class="mn-dim">+${all.length - _BP_SRC_CAP} more</span>` : "";
      const body = chips ? chips + more : `<span class="mn-dim">no specific contract</span>`;
      return `<div class="bp-src"><span class="bp-fac">${esc(s.faction)}</span> ${body}</div>`;
    }).join("");
    return `<div class="mrow"><span class="mk">${esc(it.name)}</span><div class="bp-srcs">${lines}</div></div>`;
  }).join("");
  if (!withSrc) return "";
  const foot = noSrc
    ? `<div class="mn-dim mplan-foot">${noSrc} selected blueprint${noSrc > 1 ? "s have" : " has"} no known reward contract (craft-only).</div>`
    : "";
  return `<div class="card"><h3><span>Reward contracts</span><span class="scu">where to earn these blueprints</span></h3>
    <div class="mplan-srcs">${rows}${foot}</div></div>`;
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
      <div>${best || '<span class="mn-dim">no source found</span>'}${locChips(p.locations)}</div></div>`;
  }).join("");
  return `<div class="card"><h3><span>Best deposits — by coverage</span></h3>
      ${logTable(
        th("Deposit", false, "A rock deposit / cluster type you can mine") +
        th("Covers", true, "How many of the blueprint's ingredients this deposit can yield") +
        th("Ingredients", false, "Which of the wanted minerals it covers") +
        th("RS", false, "Radar signature value(s) to scan for to find this deposit"),
        covRows, "No deposit yields any of these minerals.")}
    </div>
    <div class="card"><h3><span>Per-ingredient sources</span></h3>${locKey()}<div class="mplan-srcs">${srcs}</div></div>`;
}
