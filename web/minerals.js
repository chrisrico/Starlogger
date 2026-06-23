"use strict";
// Minerals tab: the Radar-Signature (RS) reference tool — Find (mineral → which RS to scan +
// ranked source rocks, plus where to mine it). The Identify tool moved out to the Signal ID page
// (signal.js); the blueprint/crafting planner moved out to its own Blueprints page (blueprint.js).
// The rock-render helpers signal.js imports (mineralUnion/elBadge/mechHtml/feasibilityHtml) and
// the location chips/legend (locChips/locKey, also used by app.js + blueprint.js) live here.
// The rest of the dashboard only calls initMinerals() on tab open.
import { $, esc, num, val, th, mount, logTable, hintIcon } from "./dom.js";
import { html, nothing, unsafeHTML } from "./lit.js";
import { getJSON } from "./net.js";
import { ensureGear, currentLoadout, gearCatalog, openShipEquip } from "./shipequip.js";
// feasibility()/suggestCrack() are globals from the classic /feasibility.js script (loaded
// before app.js), shared with the Node unit test — same pattern as cargogrid.js's window.*.
const { feasibility, suggestCrack } = window;

// ============================================================================ //
// Minerals tab — RS (radar signature) + composition lookup. Self-contained and
// independent of the live /api/state poll: it reads the p4k-derived mineables
// catalog via /api/{mineral-lookup,mineral-index}. Built once on first open; a
// query repaints just the results div (#mres-find).
// ============================================================================ //
let MINING_MINERALS = null;        // cached mineral names for the autocomplete
let FIND_LAST = null;              // last mineral-lookup result — re-ranked on loadout change
let MINERALS_INIT = false;

export async function initMinerals() {
  if (!MINERALS_INIT) {
    MINERALS_INIT = true;
    try { MINING_MINERALS = (await getJSON("/api/minerals")).minerals || []; }
    catch (_) { MINING_MINERALS = []; }
    ensureGear();   // preload the mining-gear catalog for the feasibility verdict (fire-and-forget)
    // Re-rank the current Find results when the ship loadout changes (popup save) — it surfaces
    // the minability of the equipped ship.
    document.addEventListener("loadout-changed", () => {
      if (FIND_LAST) mount("mres-find", FIND_LAST.index
        ? indexResultTpl(FIND_LAST.index) : findResultTpl(FIND_LAST));
    });
    renderMineralsShell();
    return;
  }
  // Re-entry (tab re-opened): build once if the shell is gone, else keep it and its state.
  if (!$("mres-find")) renderMineralsShell();
}

function renderMineralsShell() {
  const datalist = html`<datalist id="dl_mineral">${(MINING_MINERALS || []).map(m => html`<option value=${m}></option>`)}</datalist>`;
  // Reuse the shared tool-page layout (.mining, alongside Signal/Blueprints).
  mount("minerals", html`${datalist}<div class="mining">${findToolTpl()}<div id="mres-find" class="mres"></div></div>`);
}

const _pct = (x) => (x == null ? "?" : Math.round(x));
const _chance = (p) => (p == null ? "" : Math.round(p * 100) + "%");

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
// Returns a lit template (signal.js renders it directly). feasPill/suggestHtml are still
// handler-free string helpers → unsafeHTML; the ⚙ button binds @click=${openShipEquip} now.
export function feasibilityHtml(rocks) {
  const lo = currentLoadout();
  if (!lo || !lo.isMiningShip) return nothing;       // only meaningful for the equipped mining ship
  const m = (rocks || []).map(r => r.mechanics).find(Boolean);
  if (!m) return nothing;
  if (!lo.head) {
    return html`<div class="mrow"><span class="mk">your ship</span>
      <div class="mels"><button class="feas-setup" @click=${openShipEquip}>⚙ set up mining gear</button></div></div>`;
  }
  const f = feasibility(m, lo.head, lo.modules);
  if (!f) return nothing;
  return html`<div class="mrow"><span class="mk">your ship</span>
    <div class="mels">${unsafeHTML(feasPill(f))}
      <span class="mn-dim feas-factors">${f.factors.join(" · ")}</span></div></div>${f.tier === "no" ? unsafeHTML(suggestHtml(m, lo)) : nothing}`;
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
function findToolTpl() {
  return html`<div class="card mtool"><h3><span>Mineral → where to mine ${unsafeHTML(hintIcon(
      "Shows the RS value(s) to scan for and the richest source rocks, ranked by probability × yield."))}</span></h3>
    <div class="mform">
      <input id="mf-name" list="dl_mineral" placeholder="e.g. Bexalite" autocomplete="off"
        aria-label="Mineral name" @keydown=${e => { if (e.key === "Enter") miningFind(); }}>
      <button class="primary" @click=${() => miningFind()}>Find</button>
      <button @click=${() => miningIndex()}>Browse all</button>
    </div>
  </div>`;
}
async function miningFind() {
  const name = val("mf-name").trim();
  if (!name) { mount("mres-find", html`<div class="empty">Enter or pick a mineral.</div>`); return; }
  mount("mres-find", html`<div class="empty">searching…</div>`);
  try {
    const r = await fetch(`/api/mineral-lookup?name=${encodeURIComponent(name)}`).then(x => x.json());
    FIND_LAST = r;
    mount("mres-find", findResultTpl(r));
  } catch (e) { mount("mres-find", html`<div class="empty">lookup failed</div>`); }
}
// Location tags share ONE theme across two axes: colour = WHERE (cyan = planetary surface,
// amber = space asteroid field / belt / Lagrange), and the row lead = HOW (Ship / Hand / ROC).
// Attached by the server to mineral-lookup + mining-plan as
// `locations:[{place,system,kind,method,rarity?,points?}]`. `locKey()` renders the legend.
// Kept as STRING helpers (pure markup, no event handlers): app.js + blueprint.js consume them via
// unsafeHTML, and here they're wrapped in unsafeHTML(...) wherever they land in a lit template.
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
// that shows `locChips` (Find result, Blueprints sources, mining-contract section).
export function locKey() {
  const sw = (cls, label) => `<span class="mloc-kc"><i class="mloc-sw ${cls}"></i>${label}</span>`;
  const how = Object.values(METHOD_LABEL)
    .map((l) => `<span class="mloc-method-k">${l}</span>`).join("");
  return `<div class="mloc-key"><span class="mloc-key-k">Key</span>` +
    sw("mloc-sw-body", "Planetary surface") +
    sw("mloc-sw-field", "Space — field / Lagrange") +
    `<span class="mloc-kc mloc-key-how">${how}<span class="mn-dim">mining method</span></span></div>`;
}
function findResultTpl(r) {
  // No ship-mineable source rock — but it may still be mined directly (hand cave gem / ROC ore).
  // Show where it IS mineable + the key, instead of a dead "no rock" message.
  if (!r.rocks || !r.rocks.length) {
    if (!r.locations || !r.locations.length)
      return html`<div class="empty">No rock yields “${r.mineral}”.</div>`;
    return html`<div class="card">
      <div class="mscan-note mn-dim">No ship-mineable rock yields ${r.mineral} — it's mined directly:</div>
      ${unsafeHTML(locChips(r.locations))}
      ${unsafeHTML(locKey())}</div>`;
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
    ? html`<div class="mscan-note mn-dim">Ranked by minability with <b>${lo.ship}</b> — ${lo.head.name}${lo.modules.length ? " + " + lo.modules.map(m => m.name).join(", ") : ""}</div>`
    : html`<div class="mscan-note mn-dim">Pick a mining ship + gear (⚙) to rank these by minability.</div>`;
  const table = logTable(
    (lo ? th("Mine", false, "Minability with your current ship's mining gear") : "") +
    th("RS", true, "Radar signature a single rock of this type reads") +
    th("Rock", false, "The mineable rock / deposit type") +
    th("Yield %", true, `Percentage of ${esc(r.mineral)} in the rock (min–max)`) +
    th("Chance", true, "Probability a rock of this type actually contains it") +
    th("Score", true, "Source ranking = probability × yield (higher is a better source)"),
    rows, "");
  return html`<div class="card">
    <div class="mscan"><span class="mscan-k">Scan for</span>
      <div class="mscan-vals">${sigs ? unsafeHTML(sigs) : html`<span class="mn-dim">—</span>`}</div></div>
    ${note}
    ${unsafeHTML(locChips(r.locations))}
    ${r.locations && r.locations.length ? unsafeHTML(locKey()) : nothing}
    ${unsafeHTML(table)}
  </div>`;
}
async function miningIndex() {
  mount("mres-find", html`<div class="empty">loading…</div>`);
  try {
    const r = await fetch("/api/mineral-index").then(x => x.json());
    FIND_LAST = { index: r.minerals || [] };           // re-rank on loadout change
    mount("mres-find", indexResultTpl(r.minerals || []));
  } catch (e) { mount("mres-find", html`<div class="empty">load failed</div>`); }
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
function indexResultTpl(minerals) {
  if (!minerals.length) return html`<div class="empty">No mineral data.</div>`;
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
    ? html`<div class="mscan-note mn-dim">Ranked by minability with <b>${lo.ship}</b> — ${lo.head.name}${lo.modules.length ? " + " + lo.modules.map(x => x.name).join(", ") : ""}</div>`
    : html`<div class="mscan-note mn-dim">Pick a mining ship + gear (⚙) to rank these by minability.</div>`;
  const table = logTable(
    (lo ? th("Mine", false, "Minability of this mineral's best source with your current gear") : "") +
    th("Mineral", false, "The refined mineral") +
    th("RS to scan", false, "Radar signature value(s) whose rocks can contain it") +
    th("Best sources", false, "The richest source rocks for this mineral"),
    rows, "");
  return html`<div class="card"><h3><span>All minerals → source rocks</span><span class="scu">${minerals.length}</span></h3>
    ${note}
    ${unsafeHTML(table)}</div>`;
}
