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
// Plan sub's crafting list: the blueprints to build, each with a quantity. Persisted so a
// planned crafting run survives a reload (same localStorage convention as the route order and
// jukebox). Picking from the combobox adds to this list rather than replacing one selection.
let BP_LIST = (() => { try { return JSON.parse(localStorage.getItem("bpList") || "[]"); } catch (_) { return []; } })();
const _bpSave = () => { try { localStorage.setItem("bpList", JSON.stringify(BP_LIST)); } catch (_) {} };

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
  }
  // Build once, only after the catalogs have loaded; switching subs then just toggles.
  if (MINING_BLUEPRINTS !== null && !$("msub-find")) renderMiningShell();
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
  // Repaint the persisted crafting list now the Plan sub's results div exists.
  if (BP_LIST.length) renderBpPlan();
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

// ---- Plan: blueprint → deposit coverage + sources ---- //
// A filterable table of every craftable blueprint (the server tags each with a {type} and a
// {detail} — component size, weapon model line, FPS type, or armour set). The whole catalog is
// visible and scannable; the filter narrows it and clicking a row adds that blueprint to the
// crafting list below. Rows index into MINING_BLUEPRINTS (catalog order) so the click handler
// needs no name escaping.
function blueprintTableHtml() {
  const rows = (MINING_BLUEPRINTS || []).map((b, i) => {
    const detail = [b.type, b.detail].filter(Boolean).join(" · ") +
      (b.type === "Vehicle Weapons" && b.size != null ? ` · S${b.size}` : "");
    const hay = `${b.name} ${b.type || ""} ${b.detail || ""}`.toLowerCase();
    return `<tr class="bp-prow" data-search="${esc(hay)}" onclick="bpAdd(${i})" title="Add to crafting list">
      <td><b>${esc(b.name)}</b></td><td class="bp-ptype">${esc(detail)}</td></tr>`;
  }).join("");
  return logTable(th("Blueprint", false, "Click a row to add it to your crafting list") +
                  th("Type", false, "Category · model line / size"), rows, "No blueprints.");
}
function planToolHtml() {
  return `<div class="card mtool"><h3><span>Blueprints ${hintIcon(
      "Filter and click blueprints to add them to your crafting list. The materials needed are " +
      "summed across the whole list, and deposits are ranked by how many ingredients each yields.")}</span></h3>
    <div class="mform">
      <input id="bp-filter" autocomplete="off" aria-label="Filter blueprints"
        placeholder="Filter blueprints by name or type…" oninput="bpFilter(this.value)">
    </div>
    <div class="bp-pick">${blueprintTableHtml()}</div>
  </div>`;
}
// Add the catalog blueprint at index i to the crafting list (bump qty if already present).
export function bpAdd(i) {
  const b = (MINING_BLUEPRINTS || [])[i]; if (!b) return;
  const row = BP_LIST.find(r => r.name.toLowerCase() === b.name.toLowerCase());
  if (row) row.qty += 1; else BP_LIST.push({ name: b.name, qty: 1 });
  _bpSave(); renderBpPlan();
}
// Crafting-list row actions — by index, since the list re-renders fully after each change.
export function bpListQty(i, delta) {
  const row = BP_LIST[i]; if (!row) return;
  row.qty += delta;
  if (row.qty < 1) BP_LIST.splice(i, 1);
  _bpSave(); renderBpPlan();
}
export function bpListRemove(i) { BP_LIST.splice(i, 1); _bpSave(); renderBpPlan(); }
export function bpListClear() { BP_LIST = []; _bpSave(); renderBpPlan(); }
// Filter the blueprint table by a case-insensitive substring of name/type.
export function bpFilter(q) {
  const needle = (q || "").trim().toLowerCase();
  for (const tr of document.querySelectorAll("#mining .bp-prow"))
    tr.style.display = (!needle || tr.dataset.search.includes(needle)) ? "" : "none";
}
const _miningDur = (s) => {
  s = Math.round(s || 0); const m = Math.floor(s / 60), sec = s % 60;
  return m ? `${m}m${sec ? " " + sec + "s" : ""}` : `${sec}s`;
};
// Sum the crafting list's materials (server) → one deposit-coverage plan for the whole list,
// and repaint the Plan sub: the editable list, the merged shopping list, then the best deposits.
async function renderBpPlan() {
  const out = "mres-plan";
  if (!BP_LIST.length) {
    setHTML(out, `<div class="empty">Add blueprints to build a crafting list — the materials you'll need are summed across all of them.</div>`);
    return;
  }
  setHTML(out, bpListHtml() + `<div class="empty">summing materials…</div>`);
  try {
    const agg = await fetch("/api/blueprints-plan", {
      method: "POST", headers: writeHeaders(), body: JSON.stringify({ items: BP_LIST }),
    }).then(r => r.json());
    const plan = await fetch("/api/mining-plan", {
      method: "POST", headers: writeHeaders(), body: JSON.stringify({ minerals: agg.minerals || [] }),
    }).then(r => r.json());
    setHTML(out, bpListHtml() + breakdownHtml(agg) + planResultHtml(plan));
  } catch (e) { setHTML(out, bpListHtml() + `<div class="empty">plan failed</div>`); }
}
// The editable crafting list: a row per blueprint with a − qty + stepper and a remove ✕.
function bpListHtml() {
  const rows = BP_LIST.map((r, i) => `<tr>
    <td><b>${esc(r.name)}</b></td>
    <td class="bp-qty">
      <button class="bp-step" aria-label="One fewer" onclick="bpListQty(${i},-1)">−</button>
      <span class="bp-n">${r.qty}</span>
      <button class="bp-step" aria-label="One more" onclick="bpListQty(${i},1)">+</button>
    </td>
    <td class="lt-num"><button class="bp-rm" aria-label="Remove" onclick="bpListRemove(${i})">✕</button></td>
  </tr>`).join("");
  return `<div class="card mtool"><h3><span>Crafting list</span>
      <button class="bp-clear" onclick="bpListClear()">Clear</button></h3>
    ${logTable(
      th("Blueprint", false, "A blueprint you want to craft") +
      th("Qty", true, "How many to craft — the material totals scale with this") +
      th("", true, ""),
      rows, "No blueprints yet.")}
  </div>`;
}
// The merged shopping list: every recipe's materials summed by resource across the whole list.
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
      th("Qty", true, "Total amount across the whole list, in SCU") +
      th("Min quality", true, "Strictest refined quality any blueprint in the list requires (— = any)") +
      th("For", false, "Which blueprints in the list need this material"),
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
