"use strict";
// Signal ID — a top-level page (useful in every mode) that turns a Radar Signature (RS)
// reading into an identification: a mineable rock class (with its likely minerals + the
// equipped ship's cracking feasibility) AND/OR a salvageable ship hull. It's the former mining
// "Identify rock" tool, promoted out of the Mining tab so mining and salvage teams share one
// "what is that contact?" readout. Rock rendering reuses mining.js's helpers; a wreck-hull
// reading slots the shared removable-component breakdown (shipcard.js) under hull pills.
import { $, num, val, tag, mount, hintIcon, logTable, th } from "./dom.js";
import { html, nothing, unsafeHTML } from "./lit.js";
import { getJSON } from "./net.js";
import { S } from "./state.js";
import { ensureGear } from "./shipequip.js";
import { mineralUnion, elBadge, mechHtml, feasibilityHtml } from "./mining.js";
import { componentsHtml, pullSummary } from "./shipcard.js";

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
    if ($("signal-hist")) mount("signal-hist", histTpl());
  }
}
loadHistory();

// ---- the reading tool ---- //
function signalToolTpl() {
  // hintIcon returns a trusted HTML string (combobox.js's dom helper) → unsafeHTML.
  return html`<div class="card mtool"><h3><span>Signal ID — RS reading ${unsafeHTML(hintIcon(
      "Punch in a Radar Signature (RS) reading to identify the contact: a mineable rock " +
      "<b>class</b> (with its likely minerals) and/or a salvageable ship hull. The number is " +
      "<code>base RS × number of rocks</code>; bases can be shared, so a reading can be ambiguous."))}</span></h3>
    <div class="mform">
      <input id="signal-rs" type="text" inputmode="numeric" autocomplete="off"
        placeholder="e.g. 9400" aria-label="Radar signature reading"
        @input=${signalPredict} @keydown=${signalKey}>
      <button class="primary" @click=${() => signalIdentify()}>Identify</button>
    </div>
    <div id="signal-hist" class="mi-hist"></div>
  </div>`;
}
function histTpl() {
  if (!HISTORY.length) return nothing;
  return HISTORY.map(h =>
    html`<button class="mi-chip" @click=${() => signalAgain(h.rs)}
       title=${`RS ${num(h.rs)} — ${h.summary}`}><b>${num(h.rs)}</b><span>${h.summary}</span></button>`);
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
  if (!(v > 0)) { mount("signal-result", html`<div class="empty">Enter a positive RS reading.</div>`); return; }
  syncSignalSession();   // a new session clears the strip first, so this reading opens it
  mount("signal-result", html`<div class="empty">scanning…</div>`);
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
      mount("signal-hist", histTpl());
    }
    const inp = $("signal-rs"); if (inp) { inp.value = ""; inp.focus(); }
    HULL = hullNames(salvage)[0] || null;   // default-select the first hull so its breakdown shows
    LAST = { v, candidates, combos, salvage };
    renderResult();
  } catch (e) { mount("signal-result", html`<div class="empty">lookup failed</div>`); }
}

// ---- result rendering ---- //
function renderResult() {
  if (!LAST || !$("signal-result")) return;
  const { v, candidates, combos, salvage } = LAST;
  mount("signal-result", resultTpl(v, candidates, combos, salvage));
}
function resultTpl(v, candidates, combos, salvage = []) {
  if (!candidates.length && !combos.length && !salvage.length)
    return html`<div class="empty">Nothing reads RS ${num(v)} as a clean cluster.</div>`;
  const parts = [];
  if (candidates.length) {
    parts.push(html`<div class="mres-h">Single-class readings</div>`);
    parts.push(candidates.map(c => {
      const deps = [...new Set(c.rocks.map(r => r.deposit_name || r.name))];
      const minerals = mineralUnion(c.rocks);
      const extra = deps.length > 1 ? html` <span class="mn-dim">+${deps.length - 1} more</span>` : nothing;
      // elBadge/mechHtml are trusted HTML-string helpers (mining.js) → unsafeHTML; feasibilityHtml
      // returns a lit template now → rendered directly.
      const badges = minerals.map(elBadge).join("");
      return html`<div class="card mcand">
        <h3><span>${c.count} × <b>${deps[0]}</b>${extra}</span>
            <span class="scu">RS ${num(c.base_rs)}${c.count > 1 ? ` × ${c.count}` : ""}</span></h3>
        <div class="mcand-body">
          ${deps.length > 1 ? html`<div class="mrow"><span class="mk">reads as</span>
             <div class="mels">${deps.map(d => unsafeHTML(tag(d)))}</div></div>` : nothing}
          <div class="mrow"><span class="mk">possible minerals</span>
            <div class="mels">${badges ? unsafeHTML(badges) : html`<span class="mn-dim">—</span>`}</div></div>
          ${unsafeHTML(mechHtml(c.rocks))}
          ${feasibilityHtml(c.rocks)}
        </div></div>`;
    }));
  }
  const mixed = combos.filter(c => c.parts.length > 1);
  if (mixed.length) {
    // logTable/th return trusted HTML strings (dom.js) → unsafeHTML the assembled table.
    const table = logTable(
      th("Cluster") + th("Total RS", true) + th("Rocks", true),
      mixed.slice(0, 12).map(c =>
        `<tr><td>${c.parts.map(p => `${p.count}× ${escTag(p.names[0] || ("RS " + p.base_rs))}`).join(" + ")}</td>` +
        `<td class="lt-num">${num(c.total)}</td><td class="lt-num">${c.count}</td></tr>`).join(""),
      "");
    parts.push(html`<div class="mres-h">Mixed-cluster interpretations</div><div class="card">${unsafeHTML(table)}</div>`);
  }
  if (salvage.length) parts.push(salvageSectionTpl(salvage));
  return html`${parts}`;
}
// The mixed-cluster table is built as a trusted HTML string (it goes through unsafeHTML), so a
// part name interpolated into it must still be HTML-escaped here — lit's auto-escaping doesn't
// reach inside an unsafeHTML payload. (Inline same as dom.js's esc, kept local now esc is gone.)
const escTag = (s) => (s == null ? "" : String(s)).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// A wreck has no mineral composition, only an identity (a whole-ship hull, or n flat-2000 debris
// panels), so it gets its own labelled section. A ship hull names itself in the card title and
// renders its strippable components directly into the card — no repeated name, no nested panel;
// picker pills appear only when the reading is ambiguous (several hulls share the base RS).
function salvageSectionTpl(salvage) {
  return html`<div class="mres-h">Salvage targets</div>${
    salvage.map(c => (c.kind === "ship" ? hullCardTpl(c) : panelCardTpl(c)))}`;
}
function panelCardTpl(c) {
  // Debris panels: the part is noise (all read 2000), so just name the distinct donor ships.
  const ships = [...new Set((c.targets || []).map(t => t.ship || t.name))];
  const more = (c.targets || []).length >= 12 ? html` <span class="mn-dim">…</span>` : nothing;
  return html`<div class="card mcand salv">
    <h3><span><b>${c.label}</b></span>
        <span class="scu">RS ${num(c.base_rs)}${c.count > 1 ? ` × ${c.count}` : ""}</span></h3>
    <div class="mcand-body"><div class="mrow">
      <span class="mk">any of</span>
      <div class="mels">${ships.map(n => unsafeHTML(tag(n)))}${more}</div>
    </div></div></div>`;
}
// One salvageable-hull card. The title names the (selected) hull once; components render straight
// into the card (no nested breakdown panel). Pills show only when >1 hull shares this base RS.
function hullCardTpl(c) {
  const hulls = [], seen = new Set();
  for (const t of c.targets || []) { const nm = t.name; if (nm && !seen.has(nm)) { seen.add(nm); hulls.push(nm); } }
  const multi = hulls.length > 1;
  const sel = (HULL && hulls.includes(HULL)) ? HULL : (hulls[0] || null);
  const e = sel ? hullEntry(sel) : null;

  const title = multi
    ? html`<b>${sel || c.label}</b> <span class="mn-dim">+${hulls.length - 1} more</span>`
    : e ? html`<b>${e.name}</b>${e.manufacturer ? ` · ${e.manufacturer}` : ""}`
        : html`<b>${sel || c.label}</b>`;
  const sum = e ? pullSummary(e.components) : "";   // plain text → interpolated directly
  const scu = `RS ${num(c.base_rs)}${c.count > 1 ? ` × ${c.count}` : ""}${sum ? ` · ${sum}` : ""}`;

  const picker = multi
    ? html`<div class="mcand-body"><div class="mrow"><span class="mk">ship hull</span>
        <div class="mels">${hulls.map(nm =>
          html`<button class="salv-pill${nm === sel ? " open" : ""}"
             aria-expanded=${nm === sel} @click=${() => signalHull(nm)}>${nm}</button>`)}</div>
      </div></div>`
    : nothing;
  // componentsHtml returns a trusted HTML string (shipcard.js) → unsafeHTML.
  const body = e ? unsafeHTML(componentsHtml(e.components))
    : CATALOG === null ? html`<div class="empty">Loading component data…</div>`
    : html`<div class="empty">No stock-salvage component data for this hull yet.</div>`;

  return html`<div class="card mcand salv">
    <h3><span>${title}</span><span class="scu">${scu}</span></h3>
    ${picker}${body}</div>`;
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
export function signalHull(name) {
  HULL = name || null;
  renderResult();   // re-render so the selected hull's breakdown (and pill highlight) update
}

// ---- shell + init ---- //
function shell() {
  mount("signal", html`<div class="signal">${signalToolTpl()}<div id="signal-result" class="mres"></div></div>`);
  // #signal-hist is a plain child of the lit-rendered #signal shell; lit owns its parent but has
  // no binding inside it, so the history strip is rendered into it on its own (mount) — the same
  // independent container that signalIdentify/syncSignalSession refresh later.
  mount("signal-hist", histTpl());
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
