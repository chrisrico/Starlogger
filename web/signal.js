"use strict";
// Signal ID — a top-level page (useful in every mode) that turns a Refinery-Station (radar)
// reading into an identification: a mineable rock class (with its likely minerals + the
// equipped ship's cracking feasibility) AND/OR a salvageable ship hull. It's the former mining
// "Identify rock" tool, promoted out of the Mining tab so mining and salvage teams share one
// "what is that contact?" readout. Rock rendering reuses mining.js's helpers; a wreck-hull
// reading slots the shared removable-component breakdown (shipcard.js) under hull pills.
import { $, esc, num, val, tag, setHTML, hintIcon, logTable, th } from "./dom.js";
import { getJSON } from "./net.js";
import { S } from "./state.js";
import { ensureGear } from "./shipequip.js";
import { mineralUnion, elBadge, mechHtml, feasibilityHtml } from "./mining.js";
import { shipDetailPanel } from "./shipcard.js";

// Recent readings (newest first), persisted in localStorage scoped to the play SESSION — they
// survive reloads and reset on a NEW session. Keyed off the LIVE snapshot (S.LAST), same as the
// tool's previous home in mining.js (storage key unchanged, so a session's strip carries over).
let HISTORY = [];
const HIST_MAX = 16;
let LAST = null;                   // last result {v, candidates, combos, salvage} — re-rendered on loadout change
const HIST_KEY = "miIdentifyHist";
let HIST_SESSION = null;
let RS = null;                     // base RS values, seeding the type-ahead prediction
let CATALOG = null;                // salvage-ship catalog (resolved by hull display name, not class)
let HULL = null;                   // selected hull name (which pill drives the component breakdown)
let INIT = false;

const _liveSession = () => { try { return (S.LAST && S.LAST.session_started_at) || null; } catch (_) { return null; } };

function loadHistory() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(HIST_KEY) || "null"); } catch (_) { /* ignore */ }
  HISTORY = (stored && Array.isArray(stored.readings)) ? stored.readings.slice(0, HIST_MAX) : [];
  HIST_SESSION = stored ? (stored.session ?? null) : null;
  syncSignalSession();
}
function persistHistory() {
  try {
    localStorage.setItem(HIST_KEY, JSON.stringify({ session: HIST_SESSION, readings: HISTORY }));
  } catch (_) { /* quota/private-mode — fine, just won't persist */ }
}
// Reset the strip when the live session advances; otherwise leave it (persists until next
// session, even logged out). Cheap string compare — safe to call on every snapshot.
export function syncSignalSession() {
  const cur = _liveSession();
  if (cur != null && cur !== HIST_SESSION) {
    HIST_SESSION = cur;
    HISTORY = [];
    persistHistory();
    if ($("signal-hist")) setHTML("signal-hist", histHtml());
  }
}
loadHistory();

// ---- the reading tool ---- //
function signalToolHtml() {
  return `<div class="card mtool"><h3><span>Signal ID — RS reading ${hintIcon(
      "Punch in a Refinery-Station (radar) reading to identify the contact: a mineable rock " +
      "<b>class</b> (with its likely minerals) and/or a salvageable ship hull. The number is " +
      "<code>base RS × number of rocks</code>; bases can be shared, so a reading can be ambiguous.")}</span></h3>
    <div class="mform">
      <input id="signal-rs" type="text" inputmode="numeric" autocomplete="off"
        placeholder="e.g. 9400" aria-label="Radar signature reading"
        oninput="signalPredict(event)" onkeydown="signalKey(event)">
      <button class="primary" onclick="signalIdentify()">Identify</button>
    </div>
    <div id="signal-hist" class="mi-hist">${histHtml()}</div>
  </div>`;
}
function histHtml() {
  if (!HISTORY.length) return "";
  return HISTORY.map(h =>
    `<button class="mi-chip" onclick="signalAgain(${h.rs})"
       title="RS ${num(h.rs)} — ${esc(h.summary)}"><b>${num(h.rs)}</b><span>${esc(h.summary)}</span></button>`).join("");
}
function summaryOf(candidates, combos, salvage = []) {
  if (candidates.length) {
    const c = candidates[0];
    const deps = [...new Set(c.rocks.map(r => r.deposit_name || r.name))];
    const dep = (deps[0] || "").replace(/\s*\((?:ore|raw)\)\s*$/i, "");
    return `${c.count}× ${dep}${deps.length > 1 ? " +" + (deps.length - 1) : ""}`;
  }
  if (combos.filter(c => c.parts.length > 1).length) return "mixed cluster";
  if (salvage.length) return salvage[0].label;
  return "no clean match";
}
export function signalAgain(rs) {
  const inp = $("signal-rs"); if (inp) inp.value = rs;
  signalIdentify();
}
// Inline prediction: complete a typed prefix with a likely reading, suffix left selected.
// This session's readings win (recurring contacts), then the catalog's base RS values.
export function signalPredict(e) {
  if (e && e.inputType && e.inputType.startsWith("delete")) return;
  const inp = $("signal-rs"); if (!inp) return;
  const typed = inp.value; if (!typed) return;
  const pool = [...HISTORY.map(h => String(h.rs)), ...(RS || []).map(String)];
  const hit = pool.find(s => s.length > typed.length && s.startsWith(typed));
  if (hit) { inp.value = hit; inp.setSelectionRange(typed.length, hit.length); }
}
export function signalKey(e) {
  if (e.key === "Enter") { signalIdentify(); return; }
  if (e.key === "Escape") {
    const inp = $("signal-rs");
    if (inp && inp.selectionStart < inp.value.length) {
      inp.value = inp.value.slice(0, inp.selectionStart);
      e.preventDefault();
    }
  }
}
export async function signalIdentify() {
  const v = parseFloat(val("signal-rs"));
  if (!(v > 0)) { setHTML("signal-result", `<div class="empty">Enter a positive RS reading.</div>`); return; }
  syncSignalSession();   // a new session clears the strip first, so this reading opens it
  setHTML("signal-result", `<div class="empty">scanning…</div>`);
  try {
    const [look, dec] = await Promise.all([
      getJSON(`/api/rock-lookup?rs=${v}`),
      getJSON(`/api/rock-decompose?rs=${v}`),
    ]);
    const candidates = look.candidates || [], combos = dec.combos || [], salvage = look.salvage || [];
    // Only a valid reading is kept in the strip; an existing one updates in place, a new one
    // prepends (newest first).
    const ok = candidates.length > 0 || salvage.length > 0 || combos.some(c => c.parts.length > 1);
    if (ok) {
      const entry = { rs: v, summary: summaryOf(candidates, combos, salvage) };
      const at = HISTORY.findIndex(h => h.rs === v);
      if (at >= 0) HISTORY[at] = entry;
      else HISTORY = [entry, ...HISTORY].slice(0, HIST_MAX);
      persistHistory();
      setHTML("signal-hist", histHtml());
    }
    const inp = $("signal-rs"); if (inp) { inp.value = ""; inp.focus(); }
    HULL = hullNames(salvage)[0] || null;   // default-select the first hull so its breakdown shows
    LAST = { v, candidates, combos, salvage };
    renderResult();
  } catch (e) { setHTML("signal-result", `<div class="empty">lookup failed</div>`); }
}

// ---- result rendering ---- //
function renderResult() {
  if (!LAST || !$("signal-result")) return;
  const { v, candidates, combos, salvage } = LAST;
  setHTML("signal-result", resultHtml(v, candidates, combos, salvage));
}
function resultHtml(v, candidates, combos, salvage = []) {
  if (!candidates.length && !combos.length && !salvage.length)
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
  if (salvage.length) html += salvageSectionHtml(salvage);
  return html;
}
// A wreck has no mineral composition, only an identity (a whole-ship hull, or n flat-2000
// debris panels), so it gets its own labelled section. For a ship hull the candidate hulls are
// PILLS (driven by the signal, not a dropdown) and the selected one slots in the shared
// removable-component breakdown (resolved by ship CLASS against the salvage-ship catalog).
function salvageSectionHtml(salvage) {
  return `<div class="mres-h">Salvage targets</div>` + salvage.map(c => {
    const head = `<h3><span><b>${esc(c.label)}</b></span>
          <span class="scu">RS ${num(c.base_rs)}${c.count > 1 ? ` × ${c.count}` : ""}</span></h3>`;
    if (c.kind === "ship") {
      // The salvageables catalog identifies hulls by display name (its `class` is a debris-record
      // id, not the ship base class), so the pills carry — and the breakdown resolves by — NAME.
      const hulls = [], seen = new Set();
      for (const t of c.targets || []) {
        const nm = t.name; if (!nm || seen.has(nm)) continue;
        seen.add(nm); hulls.push(nm);
      }
      const pills = hulls.map(nm =>
        `<button class="salv-pill${nm === HULL ? " open" : ""}" aria-expanded="${nm === HULL}"
           data-hull="${esc(nm)}" onclick="signalHull(this.dataset.hull)">${esc(nm)}</button>`).join("");
      return `<div class="card mcand salv">${head}
        <div class="mcand-body"><div class="mrow">
          <span class="mk">ship hull</span><div class="mels">${pills}</div>
        </div></div>
        ${HULL ? hullBreakdown(HULL) : ""}</div>`;
    }
    // debris panels: the part is noise (all read 2000), so just name the distinct donor ships
    const ships = [...new Set((c.targets || []).map(t => t.ship || t.name))];
    const more = (c.targets || []).length >= 12 ? ' <span class="mn-dim">…</span>' : "";
    return `<div class="card mcand salv">${head}
      <div class="mcand-body"><div class="mrow">
        <span class="mk">any of</span>
        <div class="mels">${ships.map(n => tag(n)).join(" ")}${more}</div>
      </div></div></div>`;
  }).join("");
}
// Distinct hull names across all ship-hull candidates (first one is the default selection).
function hullNames(salvage) {
  const out = [];
  for (const c of salvage || []) if (c.kind === "ship")
    for (const t of c.targets || []) if (t.name && !out.includes(t.name)) out.push(t.name);
  return out;
}
// Resolve a hull display name to its salvage-ship catalog entry (name / name_full match, the
// same logic as the backend salvage_ships.lookup) — the two catalogs share no class key.
function hullEntry(name) {
  if (!CATALOG) return null;
  const t = (name || "").trim().toLowerCase();
  for (const e of Object.values(CATALOG))
    if (t === (e.name || "").toLowerCase() || t === (e.name_full || "").toLowerCase()) return e;
  return null;
}
function hullBreakdown(name) {
  if (CATALOG === null) return shipDetailPanel("…", [], false, "Loading component data…");
  const e = hullEntry(name);
  return e
    ? shipDetailPanel(`${esc(e.name)}${e.manufacturer ? ` · ${esc(e.manufacturer)}` : ""}`, e.components, true)
    : shipDetailPanel(esc(name), [], false, "No stock-salvage component data for this hull yet.");
}
export function signalHull(name) {
  HULL = name || null;
  renderResult();   // re-render so the selected hull's breakdown (and pill highlight) update
}

// ---- shell + init ---- //
function shell() {
  setHTML("signal", `<div class="signal">${signalToolHtml()}<div id="signal-result" class="mres"></div></div>`);
}
async function ensureCatalogs() {
  if (INIT) return;
  INIT = true;
  ensureGear();   // mining-gear catalog backs the rock-feasibility verdict (fire-and-forget)
  // RS catalog seeds the prediction; the salvage-ship catalog resolves a hull to its components.
  try { RS = (await getJSON("/api/rock-signatures")).signatures || []; } catch (_) { RS = []; }
  try { CATALOG = (await getJSON("/api/salvage-ship")).ships || {}; } catch (_) { CATALOG = {}; }
  if (LAST) renderResult();   // catalog arrived — fill in any pending hull breakdown
  // Re-render so the rock-feasibility row tracks the equipped ship when the loadout changes.
  document.addEventListener("loadout-changed", () => { if (LAST) renderResult(); });
}
export function initSignal() {
  if (!$("signal-rs")) shell();
  syncSignalSession();
  ensureCatalogs();
}
