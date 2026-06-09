"use strict";
// Mining tab: the Refinery-Station (RS) reference tools — Identify (a scanner reading →
// rock class(es) + likely minerals), Find (mineral → which RS to scan + ranked source
// rocks), and Plan (refining blueprint → deposit coverage + sources). Self-contained: it
// owns all MINING_*/IDENTIFY_* state and talks to its own /api/rock-*, /api/mineral-*,
// /api/blueprint* endpoints. The rest of the dashboard only calls initMining() (on tab
// open) and the bridged inline handlers (miningSub + the identify/find/plan/bp actions).
import { $, esc, num, val, th, tag, setHTML, logTable, tabBar } from "./dom.js";
import { getJSON } from "./net.js";
import { S } from "./state.js";
import { ensureGear, currentLoadout } from "./shipequip.js";
// feasibility() is a global from the classic /feasibility.js script (loaded before app.js),
// shared with the Node unit test — same pattern as cargogrid.js's window.* helpers.
const { feasibility } = window;

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
const IDENTIFY_HIST_MAX = 16;      // how many recent readings to keep (2 rows of 8)
let IDENTIFY_LAST = null;          // last result {v, candidates, combos} — re-rendered on loadout change
let FIND_LAST = null;              // last mineral-lookup result — re-ranked on loadout change
let MINING_INIT = false;

// Recent readings persist in localStorage, scoped to the current play SESSION: they survive
// page reloads, and reset when a NEW session begins (a different non-null session start than
// the one they were saved under). A null session (logged out / not started) never resets —
// the readings carry over "until the next session". Keyed off the LIVE snapshot (S.LAST),
// not the replay view, so replaying an archived session can't wipe the live readings.
const IDENTIFY_HIST_KEY = "miIdentifyHist";
let IDENTIFY_HIST_SESSION = null;  // the session start the in-memory readings belong to
const _liveSession = () => { try { return (S.LAST && S.LAST.session_started_at) || null; } catch (_) { return null; } };

function loadIdentifyHistory() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(IDENTIFY_HIST_KEY) || "null"); } catch (_) { /* ignore */ }
  IDENTIFY_HISTORY = (stored && Array.isArray(stored.readings)) ? stored.readings.slice(0, IDENTIFY_HIST_MAX) : [];
  IDENTIFY_HIST_SESSION = stored ? (stored.session ?? null) : null;
  syncIdentifySession();
}
function persistIdentifyHistory() {
  try {
    localStorage.setItem(IDENTIFY_HIST_KEY, JSON.stringify({ session: IDENTIFY_HIST_SESSION, readings: IDENTIFY_HISTORY }));
  } catch (_) { /* quota/private-mode — fine, just won't persist */ }
}
// Reset the strip when the live session has advanced to a new one; otherwise leave it alone
// (including while logged out, so readings persist until the next session). Repaints #mi-hist
// when it's on screen. Cheap (a string compare) — safe to call on every snapshot.
export function syncIdentifySession() {
  const cur = _liveSession();
  if (cur != null && cur !== IDENTIFY_HIST_SESSION) {
    IDENTIFY_HIST_SESSION = cur;
    IDENTIFY_HISTORY = [];
    persistIdentifyHistory();
    if ($("mi-hist")) setHTML("mi-hist", identifyHistHtml());
  }
}
loadIdentifyHistory();

export async function initMining() {
  if (!MINING_INIT) {
    MINING_INIT = true;
    const grab = async (url, key) => {
      try { return (await getJSON(url))[key] || []; }
      catch (e) { return []; }
    };
    [MINING_MINERALS, MINING_BLUEPRINTS, MINING_RS] = await Promise.all([
      grab("/api/minerals", "minerals"), grab("/api/blueprints", "blueprints"),
      grab("/api/rock-signatures", "signatures")]);
    ensureGear();   // preload the mining-gear catalog for the feasibility verdict (fire-and-forget)
    // Re-render the current Identify + Find results when the ship loadout changes (popup save)
    // — both surface the feasibility/minability of the equipped ship.
    document.addEventListener("loadout-changed", () => {
      if (IDENTIFY_LAST) setHTML("mres-identify", identifyResultHtml(
        IDENTIFY_LAST.v, IDENTIFY_LAST.candidates, IDENTIFY_LAST.combos));
      if (FIND_LAST) setHTML("mres-find", FIND_LAST.index
        ? indexResultHtml(FIND_LAST.index) : findResultHtml(FIND_LAST));
    });
  }
  syncIdentifySession();   // opening the tab after a relog → reconcile the persisted strip
  // Build once, only after the catalogs have loaded; switching subs then just toggles.
  if (MINING_BLUEPRINTS !== null && !$("msub-identify")) renderMiningShell();
}
// Switch sub-tabs by toggling visibility — never rebuild, so each sub keeps its state.
export function miningSub(sub) {
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
function feasibilityHtml(rocks) {
  const lo = currentLoadout();
  if (!lo || !lo.isMiningShip) return "";          // only meaningful for the equipped mining ship
  const m = (rocks || []).map(r => r.mechanics).find(Boolean);
  if (!m) return "";
  if (!lo.head) {
    return `<div class="mrow"><span class="mk">your ship</span>
      <div class="mels"><button class="feas-setup" onclick="openShipEquip()">🔧 set up mining gear</button></div></div>`;
  }
  const f = feasibility(m, lo.head, lo.modules);
  if (!f) return "";
  return `<div class="mrow"><span class="mk">your ship</span>
    <div class="mels">${feasPill(f)}
      <span class="mn-dim feas-factors">${esc(f.factors.join(" · "))}</span></div></div>`;
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
// The recent-readings strip: a grid of uniform two-line chips (RS reading on top, result
// gist below). Chips re-run their reading when clicked; the title carries the full,
// untruncated summary since the bottom line is ellipsis-truncated.
function identifyHistHtml() {
  if (!IDENTIFY_HISTORY.length) return "";
  return IDENTIFY_HISTORY.map(h =>
    `<button class="mi-chip" onclick="identifyAgain(${h.rs})"
       title="RS ${num(h.rs)} — ${esc(h.summary)}"><b>${num(h.rs)}</b><span>${esc(h.summary)}</span></button>`).join("");
}
// One-line gist of a reading's result, for the history chip.
function identifySummary(candidates, combos) {
  if (candidates.length) {
    const c = candidates[0];
    const deps = [...new Set(c.rocks.map(r => r.deposit_name || r.name))];
    // Drop the redundant material-state suffix ((Ore)/(Raw)) for the compact chip; keep
    // meaningful ones like (C-Type)/(Pure).
    const dep = (deps[0] || "").replace(/\s*\((?:ore|raw)\)\s*$/i, "");
    return `${c.count}× ${dep}${deps.length > 1 ? " +" + (deps.length - 1) : ""}`;
  }
  if (combos.filter(c => c.parts.length > 1).length) return "mixed cluster";
  return "no clean match";
}
export function identifyAgain(rs) {
  const inp = $("mi-rs"); if (inp) inp.value = rs;
  miningIdentify();
}
// Inline prediction: while typing a prefix, complete it with a likely reading, leaving the
// guessed suffix selected. Typing replaces the selection (so the guess just refines), → /
// End accepts it natively, Enter submits, Esc/Backspace drops it. Skipped on deletes so
// editing stays free. This session's readings win (recurring deposits), then the catalog's
// base RS values seed a guess before any have been entered.
export function identifyPredict(e) {
  if (e && e.inputType && e.inputType.startsWith("delete")) return;
  const inp = $("mi-rs"); if (!inp) return;
  const typed = inp.value;
  if (!typed) return;
  const pool = [...IDENTIFY_HISTORY.map(h => String(h.rs)), ...(MINING_RS || []).map(String)];
  const hit = pool.find(s => s.length > typed.length && s.startsWith(typed));
  if (hit) { inp.value = hit; inp.setSelectionRange(typed.length, hit.length); }
}
export function identifyKey(e) {
  if (e.key === "Enter") { miningIdentify(); return; }
  if (e.key === "Escape") {              // drop a predicted suffix without clearing the typed part
    const inp = $("mi-rs");
    if (inp && inp.selectionStart < inp.value.length) {
      inp.value = inp.value.slice(0, inp.selectionStart);
      e.preventDefault();
    }
  }
}
export async function miningIdentify() {
  const v = parseFloat(val("mi-rs"));
  if (!(v > 0)) { setHTML(mres(), `<div class="empty">Enter a positive RS reading.</div>`); return; }
  syncIdentifySession();   // a new session clears the strip first, so this reading opens it
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
      persistIdentifyHistory();
      setHTML("mi-hist", identifyHistHtml());
    }
    // Clear + refocus so the next reading can be typed straight away.
    const inp = $("mi-rs"); if (inp) { inp.value = ""; inp.focus(); }
    IDENTIFY_LAST = { v, candidates, combos };
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
          ${feasibilityHtml(c.rocks)}
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
    : `<div class="mscan-note mn-dim">Pick a mining ship + gear (🔧) to rank these by minability.</div>`;
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
// A mineral's minability = the feasibility of its best (richest) source that carries
// mechanics. Every source of a given mineral shares the same break difficulty in the data,
// so the best source is representative.
function mineralFeas(m, lo) {
  if (!lo) return null;
  const mech = (m.rocks || []).map(x => x.mechanics).find(Boolean);
  return mech ? feasibility(mech, lo.head, lo.modules) : null;
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
    : `<div class="mscan-note mn-dim">Pick a mining ship + gear (🔧) to rank these by minability.</div>`;
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
export function bpOpen(show) {
  const el = $("bp-dd-list"); if (!el) return;
  el.classList.toggle("open", !!show);
  // The card clips descendants via clip-path; drop it while the menu is open so the
  // dropdown can overflow past the card edge.
  const card = el.closest(".card"); if (card) card.classList.toggle("dd-open", !!show);
}
export function bpPick(name) {
  const inp = $("mp-bp"); if (inp) inp.value = name;
  bpOpen(false);
  miningPlanFromBlueprint(name);
}
// Filter items by a case-insensitive substring; hide whole sections with no visible items.
export function bpFilter(q) {
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
export function bpKey(e) {
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
