"use strict";
// Blueprints page: the refining/crafting planner — pick craftable blueprints (+ a one-click
// shipbuilder), set quantities, and see the summed materials, the contracts that reward those
// blueprints, and the best deposits / per-ingredient sources to mine the materials. Promoted out
// of the Mining tab to its own top-level page (Mining mode only). Self-contained and independent
// of the live /api/state poll: it reads the p4k-derived catalogs via /api/{blueprints,ships,
// ship-build,blueprints-plan,mining-plan}. The location chips/legend are reused from minerals.js.
import { $, esc, num, mount, logTable, th, tag, hintIcon } from "./dom.js";
import { html, render, nothing, unsafeHTML } from "./lit.js";
import { getJSON, writeHeaders } from "./net.js";
import { registerCombo, comboInputHtml } from "./combobox.js";
import { locChips, locKey } from "./minerals.js";
import { encodePlan, decodePlan, planLink } from "./shareplan.js";

let BP_CATALOG = null;        // cached {name, type, …, acquired} blueprint catalog for the picker
let BP_SHIPS = null;          // cached buildable ship names for the shipbuilder dropdown
let BP_INIT = false;
// Effective set of acquired blueprint NAMES (the server's log-derived value overlaid with your
// manual per-row checkbox overrides). Rebuilt whenever either changes; used to drop acquired
// blueprints from the "Reward contracts" card — no point showing how to earn one you've got.
let ACQ_NAMES = new Set();
// Manual Acquired-checkbox overrides, keyed by blueprint name → bool, layered over the log-derived
// `acquired` flag so you can correct a miss (the log + catalog don't always line up). Persisted.
let BP_ACQ = (() => { try { return JSON.parse(localStorage.getItem("bpAcq") || "{}"); } catch (_) { return {}; } })();
const _bpAcqSave = () => { try { localStorage.setItem("bpAcq", JSON.stringify(BP_ACQ)); } catch (_) {} };
// Effective acquired for a row: your override if set, else what the game log reported.
const _bpAcq = (b) => (b && b.name in BP_ACQ) ? !!BP_ACQ[b.name] : !!(b && b.acquired);
const _bpAcqTitle = (b) => (b && b.name in BP_ACQ)
  ? (BP_ACQ[b.name] ? "Marked acquired (manual)" : "Marked not acquired (manual)")
  : (b && b.acquired ? "Acquired" + (b.acquired_at ? " — " + b.acquired_at : "") + " (from your game log)" : "Not acquired");
// Per-blueprint build quantity, keyed by blueprint name. Persisted so a planned crafting run
// survives a reload. The whole catalog is one table with an inline qty input per row; the
// materials breakdown is summed from the rows whose quantity > 0 — Clear resets every qty to 0.
let BP_QTY = (() => { try { return JSON.parse(localStorage.getItem("bpQty") || "{}"); } catch (_) { return {}; } })();
const _bpSave = () => { try { localStorage.setItem("bpQty", JSON.stringify(BP_QTY)); } catch (_) {} };
// "Qty > 0" toggle — when on, the table shows only rows you've given a quantity (the selected
// builds, e.g. after the shipbuilder), on top of any column filters. Persisted.
let BP_ONLY = localStorage.getItem("bpOnly") === "1";

// ---- shared read-only snapshot ---- //
// When the page is opened via a ?code=… link (a plan another Starlogger user shared), the whole
// Blueprints page flips into a read-only view of the decoded plan. SHARED_PLAN is that {name: qty}
// snapshot; SHARED_ERR holds a message if the link was corrupt. Kept ENTIRELY separate from
// BP_QTY/localStorage so viewing someone's shared plan never touches your own saved plan — only an
// explicit "Import" copies it across. See shareplan.js for the wire format.
let BP_SHARED = false;
let SHARED_PLAN = {};
let SHARED_ERR = null;
// Called from app.js on boot when the URL carries ?code=… — decode synchronously and arm the
// read-only view. The actual render happens when activateTab('blueprint') runs initBlueprint().
export function setSharedPlan(code) {
  try { SHARED_PLAN = decodePlan(code); SHARED_ERR = null; }
  catch (_) { SHARED_PLAN = {}; SHARED_ERR = "This shared-plan link is invalid or corrupted — ask for a fresh one."; }
  BP_SHARED = true;
}

const _chance = (p) => (p == null ? "" : Math.round(p * 100) + "%");

export async function initBlueprint() {
  if (!BP_INIT) {
    BP_INIT = true;
    document.addEventListener("click", (e) => {   // close an open column-filter dropdown
      if (!e.target.closest("#bp-fpop") && !e.target.closest(".bp-fbtn")) _bpFclose();
    });
    // Build the shell immediately (an empty table) so a fresh /blueprint load paints at once,
    // then rebuild once the catalogs arrive — never leave a permanently empty table.
    renderBlueprintShell();
    try { BP_CATALOG = (await getJSON("/api/blueprints")).blueprints || []; }
    catch (_) { BP_CATALOG = []; }
    ACQ_NAMES = new Set((BP_CATALOG || []).filter(_bpAcq).map(b => b.name));
    // Buildable ships for the shipbuilder combobox: those with craftable components / a radar
    // (concepts excluded), as {name, mfr} sorted by manufacturer then name — matching the header
    // ship picker's grouping so the two dropdowns read the same.
    try {
      const sd = await getJSON("/api/ships");
      BP_SHIPS = Object.entries(sd.ships || {})
        .filter(([n, e]) => e && (e.components || e.radar) && !/\[[^\]]*concept[^\]]*\]/i.test(n))
        .map(([name, e]) => ({ name, mfr: e.manufacturer || "—" }))
        .sort((a, b) => a.mfr.localeCompare(b.mfr) || a.name.localeCompare(b.name));
    } catch (_) { BP_SHIPS = []; }
    renderBlueprintShell();   // rebuild now the catalogs are in (the populated table)
    return;
  }
  // Re-entry (tab re-opened): build once if the shell is gone, else keep it and its state.
  if (!$("mres-plan")) renderBlueprintShell();
}

function renderBlueprintShell() {
  // Reuse the shared tool-page layout (.mining, alongside Signal/Minerals) so the cards space and
  // the inputs read identically across the tool pages.
  if (BP_SHARED) {
    // Read-only snapshot view (opened via a ?code= link) — no edit controls, no filters.
    mount("blueprint", html`<div class="mining">${sharedPlanTpl()}<div id="mres-plan" class="mres"></div></div>`);
    renderBpPlan();   // the materials/contracts/deposits below are computed read-only output already
    return;
  }
  mount("blueprint", html`<div class="mining">${planToolTpl()}<div id="mres-plan" class="mres"></div></div>`);
  // Paint the materials breakdown now the results div exists (empty-state until a qty is set).
  renderBpPlan();
  _bpApplyFilter();   // honor a persisted "Qty > 0" toggle on (re)render
}

// ---- the blueprint build table ---- //
// One row per craftable blueprint, columns Name/Type/Subtype/Class/Quality/Size + an inline Qty.
// Every column has a spreadsheet-style multi-select filter and is click-to-sort; clicking a row
// toggles its quantity on/off. The materials breakdown below sums every row with quantity > 0.
const BP_COLS = [
  { key: "name", label: "Name" }, { key: "type", label: "Type" }, { key: "subtype", label: "Subtype" },
  { key: "cls", label: "Class" }, { key: "quality", label: "Quality" }, { key: "size", label: "Size" },
  { key: "acquired", label: "Acquired" },
];
let BP_FILTERS = {};   // col -> Set of EXCLUDED values (unchecked in its dropdown); empty/absent = all
let BP_SORT = null;    // { col, dir: 1 | -1 }
const _bpNum = (k) => k === "size";
const _bpRA = (k) => _bpNum(k) || k === "quality";   // right-align the numeric-ish columns
// Acquired reads as a plain "Yes"/"No" string for the funnel filter + sort (effective value, with
// your manual override applied); the cell itself renders a checkbox (below).
const _bpCell = (b, k) => {
  if (k === "acquired") return _bpAcq(b) ? "Yes" : "No";
  const v = b[k]; return (v === "" || v == null) ? "" : String(v);
};

// The blueprint table is built as a lit template, but its rows are then driven imperatively
// (sort reorders DOM rows, the column filters toggle row style.display, the qty inputs update
// in place). The static data-col/data-i attributes and the .bp-prow/.bp-qin/.bp-on hooks the
// handlers re-read off the live DOM are preserved; only the inline on* triggers are @-bindings.
function blueprintTableTpl() {
  const cols = BP_COLS;
  const head = cols.map(c =>
    html`<th data-col=${c.key} class=${_bpRA(c.key) ? "lt-num" : ""}><span class="bp-h" @click=${() => bpSort(c.key)}>${c.label}<span class="bp-sort" id="bps-${c.key}"></span></span>${c.key === "name" ? nothing : html`<button class="bp-fbtn" title=${`Filter ${c.label}`} @click=${e => bpFilterOpen(e, c.key)}>${unsafeHTML('<svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M1 2.5h10l-3.8 4.2v3.6l-2.4-1.3V6.7z" fill="currentColor"/></svg>')}</button>`}</th>`);
  const qhead = html`<th class="lt-num" data-col="qty">Qty<button class="bp-fbtn" title="Show only rows with a quantity (Qty > 0)" @click=${e => bpQtyFilter(e)}>${unsafeHTML('<svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"><path d="M1 2.5h10l-3.8 4.2v3.6l-2.4-1.3V6.7z" fill="currentColor"/></svg>')}</button></th>`;
  const rows = (BP_CATALOG || []).map((b, i) => {
    const q = BP_QTY[b.name] || 0;
    const cells = cols.map(c => {
      if (c.key === "acquired")
        return html`<td class="bp-acq-cell" @click=${e => e.stopPropagation()}><input type="checkbox" class="bp-acq" .checked=${_bpAcq(b)} aria-label=${`Acquired: ${b.name}`} title=${_bpAcqTitle(b)} @change=${e => bpAcqToggle(i, e.target.checked)}></td>`;
      const v = _bpCell(b, c.key);
      return html`<td class=${_bpRA(c.key) ? "lt-num" : ""}>${c.key === "name" ? html`<b>${v}</b>` : v}</td>`;
    });
    return html`<tr class="bp-prow${q ? " bp-on" : ""}" data-i=${i} @click=${e => bpRowClick(e, i)}>${cells}<td class="bp-qcell" @click=${e => e.stopPropagation()}><button class="bp-step" aria-label="One fewer" @click=${() => bpStep(i, -1)}>−</button><input type="number" min="0" class="bp-qin" .value=${String(q)} aria-label=${`Quantity of ${b.name}`} @input=${e => bpQtyInput(i, e.target.value)}><button class="bp-step" aria-label="One more" @click=${() => bpStep(i, 1)}>+</button></td></tr>`;
  });
  return html`<table class="logtable bp-table"><thead><tr>${head}${qhead}</tr></thead><tbody>${rows}</tbody></table>`;
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
  return (BP_SHIPS || []).filter(s => !f || s.name.toLowerCase().includes(f) || s.mfr.toLowerCase().includes(f));
};
let _sbReg = false;
function _sbRegister() {
  if (_sbReg) return; _sbReg = true;
  registerCombo("sb-ship", { entries: _sbMatches, onPick: (name) => { SB_SHIP = name; }, reset: () => SB_SHIP });
}
function shipbuilderTpl() {
  _sbRegister();
  const types = SB_CLASSES.map(c => html`<option value=${c} ?selected=${c === "Military"}>${c}</option>`);
  const box = comboInputHtml("sb-ship", { value: SB_SHIP, placeholder: "search ship…", label: "Ship to outfit", cls: "sb-box" });
  return html`<div class="sb-bar">
    <span class="sb-lbl">Outfit a ship ${unsafeHTML(hintIcon(
      "Pick a ship and a component class — this sets the Grade-A blueprints to craft each of its " +
      "components (power plant, cooler, shield, quantum drive, radar) to that class. Where a class " +
      "doesn't make a part that size, the closest class fills it; sizes with no blueprint are flagged."))}</span>
    ${box}
    <select id="sb-type" class="sb-sel" aria-label="Component class">${types}</select>
    <button class="sb-go" @click=${() => bpBuildShip()}>Add builds</button>
    <span id="sb-status" class="sb-status" role="status"></span>
  </div>`;
}
function planToolTpl() {
  return html`<div class="card mtool"><h3><span>Blueprints ${unsafeHTML(hintIcon(
      "Every craftable blueprint. Filter any column (multi-select, like a spreadsheet), click a header " +
      "to sort, click a row to add it (− or Clear to remove), and set a quantity. Materials are summed below across " +
      "everything with a quantity, then ranked by deposit coverage."))}</span>
      <span class="bp-acts">
        <button class="bp-share" @click=${() => bpShare()} title="Copy a read-only link to this plan to send to another Starlogger user">Share plan</button>
        <button class="bp-clear" @click=${() => bpClearList()} title="Reset every quantity to 0">Clear</button>
      </span></h3>
    <div class="bp-share-bar" id="bp-share-bar"></div>
    ${shipbuilderTpl()}
    <div class="bp-pick">${blueprintTableTpl()}</div>
  </div>`;
}
// Copy a read-only link to the current plan. The plan rides entirely inside the URL (?code=…), so
// the recipient — another Starlogger user — opens it on THEIR own install and it renders against
// their catalog; nothing is served from, or connects back to, this instance. The link is always
// surfaced in a selectable field too, because navigator.clipboard can be blocked over plain http
// or without a user gesture; that field is the reliable fallback.
let _shareTimer = 0;
async function bpShare() {
  const bar = $("bp-share-bar"); if (!bar) return;
  const plan = {};
  for (const [name, q] of Object.entries(BP_QTY)) if (q > 0) plan[name] = q;
  clearTimeout(_shareTimer);
  if (!Object.keys(plan).length) {
    bar.innerHTML = `<span class="sb-warn">Set a quantity on a blueprint first, then Share.</span>`;
    _shareTimer = setTimeout(() => { bar.textContent = ""; }, 4000);
    return;
  }
  const link = planLink(encodePlan(plan));
  let copied = false;
  try { await navigator.clipboard.writeText(link); copied = true; } catch (_) {}
  // Static markup only; the link goes into the input's .value PROPERTY (never interpolated as HTML).
  bar.innerHTML = copied
    ? `<span>Read-only link copied — paste it to another Starlogger user.</span><input class="bp-share-link" readonly aria-label="Shareable plan link">`
    : `<span>Copy this read-only link and send it to another Starlogger user:</span><input class="bp-share-link" readonly aria-label="Shareable plan link">`;
  const inp = bar.querySelector("input");
  if (inp) { inp.value = link; inp.focus(); inp.select(); }
  _shareTimer = setTimeout(() => { bar.textContent = ""; }, 15000);
}
// ---- the read-only shared-plan view (opened via a ?code= link) ---- //
// One row per shared blueprint, resolved against THIS install's catalog for the metadata; the
// materials/contracts/deposits cards below are produced by the same renderBpPlan() as the editable
// planner (it reads SHARED_PLAN while BP_SHARED is on). No qty inputs, sort, or filters — a snapshot.
function sharedPlanTpl() {
  if (SHARED_ERR) {
    return html`<div class="card mtool"><h3><span>Shared plan</span></h3>
      <div class="bp-share-bar"><span class="sb-warn">${SHARED_ERR}</span>
        <button class="bp-clear" @click=${() => exitSharedPlan()}>Back to my plan</button></div></div>`;
  }
  const loaded = BP_CATALOG != null;   // suppress "(not in your catalog)" until the catalog is in
  const byName = new Map((BP_CATALOG || []).map(b => [b.name, b]));
  const names = Object.keys(SHARED_PLAN);
  // A shared plan is someone else's snapshot — ownership ("Acquired") isn't meaningful here.
  const cols = BP_COLS.filter(c => c.key !== "acquired");
  const head = cols.map(c => html`<th class=${_bpRA(c.key) ? "lt-num" : ""}>${c.label}</th>`);
  const rows = names.map(name => {
    const b = byName.get(name);
    const cells = cols.map(c => {
      const v = b ? _bpCell(b, c.key) : (c.key === "name" ? name : "");
      return html`<td class=${_bpRA(c.key) ? "lt-num" : ""}>${c.key === "name"
        ? html`<b>${v}</b>${(loaded && !b) ? html` <span class="mn-dim" title="No blueprint by this name in your catalog — your game data may differ from the sharer's.">(not in your catalog)</span>` : nothing}`
        : v}</td>`;
    });
    return html`<tr>${cells}<td class="lt-num">${SHARED_PLAN[name]}</td></tr>`;
  });
  return html`<div class="card mtool">
    <h3><span>Shared plan ${unsafeHTML(hintIcon(
      "A read-only blueprint plan another Starlogger user shared with you via a ?code= link. It's a " +
      "snapshot resolved against YOUR catalog — nothing connects back to their instance. Import it to " +
      "copy these picks into your own editable plan."))}</span>
      <span class="bp-shared-badge">read-only snapshot</span></h3>
    <div class="bp-share-bar">
      <span>${names.length} blueprint${names.length === 1 ? "" : "s"} in this shared plan.</span>
      <button class="bp-share primary" @click=${() => importSharedPlan()}>Import into my plan</button>
      <button class="bp-clear" @click=${() => exitSharedPlan()}>Back to my plan</button>
    </div>
    <div class="bp-pick"><table class="logtable bp-table"><thead><tr>${head}<th class="lt-num">Qty</th></tr></thead><tbody>${rows}</tbody></table></div>
  </div>`;
}
// Copy the shared plan into your own (editable) plan, then drop out of read-only mode. Replaces
// rather than merges (a clear mental model: "load this plan"); confirm first if you'd clobber an
// existing plan.
function importSharedPlan() {
  const incoming = { ...SHARED_PLAN };
  if (!Object.keys(incoming).length) { exitSharedPlan(); return; }
  const hasOwn = Object.values(BP_QTY).some(q => q > 0);
  if (hasOwn && !window.confirm("Replace your current blueprint plan with this shared one?")) return;
  BP_QTY = incoming; _bpSave();
  exitSharedPlan();   // leaves shared mode → repaints your editable planner with the imported picks
}
// Leave the read-only snapshot and return to your own plan. Strips ?code= from the address bar so
// a reload returns to your plan, not the snapshot.
function exitSharedPlan() {
  BP_SHARED = false; SHARED_PLAN = {}; SHARED_ERR = null;
  try { history.replaceState(null, "", "/blueprint"); } catch (_) {}
  renderBlueprintShell();
}
let _bpTimer = 0;
// Set the blueprint at catalog index i to quantity n (0 clears it): update the row state + input in
// place (never rebuild the table) and debounce the breakdown refresh.
function _bpApply(i, n) {
  const b = (BP_CATALOG || [])[i]; if (!b) return;
  n = Math.max(0, n | 0);
  if (n > 0) BP_QTY[b.name] = n; else delete BP_QTY[b.name];
  _bpSave();
  const tr = document.querySelector(`#blueprint .bp-table tbody tr[data-i="${i}"]`);
  if (tr) {
    tr.classList.toggle("bp-on", n > 0);
    const inp = tr.querySelector(".bp-qin"); if (inp && +inp.value !== n) inp.value = n;
  }
  if (BP_ONLY) _bpApplyFilter();   // a row dropping to 0 (or rising above it) changes what's shown
  clearTimeout(_bpTimer); _bpTimer = setTimeout(renderBpPlan, 250);
}
function bpStep(i, d) { const b = (BP_CATALOG || [])[i]; if (b) _bpApply(i, (BP_QTY[b.name] || 0) + d); }
function bpQtyInput(i, v) { _bpApply(i, parseInt(v, 10) || 0); }
// Click a row (outside its qty/acquired cells) to SELECT it (qty 0 → 1). Clicking an already-
// selected row leaves it as-is — removing is done with the − stepper or Clear, so a stray click
// can't wipe a quantity you set.
function bpRowClick(e, i) {
  if (e.target.closest(".bp-qcell")) return;
  const b = (BP_CATALOG || [])[i]; if (b && (BP_QTY[b.name] || 0) === 0) _bpApply(i, 1);
}
// Reset every quantity to 0 (zero the inputs + clear row highlights in place) and the breakdown.
function bpClearList() {
  BP_QTY = {}; _bpSave();
  for (const tr of document.querySelectorAll("#blueprint .bp-table tbody tr")) {
    tr.classList.remove("bp-on"); const inp = tr.querySelector(".bp-qin"); if (inp) inp.value = 0;
  }
  _bpApplyFilter();   // with "Qty > 0" on, clearing empties the view
  renderBpPlan();
}
// Sync every table row's qty input + on-state from BP_QTY after a bulk change (the shipbuilder),
// without rebuilding the table — keeps the current sort, filters, and scroll.
function _bpSyncRows() {
  for (const tr of document.querySelectorAll("#blueprint .bp-table tbody tr")) {
    const b = (BP_CATALOG || [])[+tr.dataset.i]; if (!b) continue;
    const q = BP_QTY[b.name] || 0;
    tr.classList.toggle("bp-on", q > 0);
    const inp = tr.querySelector(".bp-qin"); if (inp) inp.value = q;
  }
  _bpApplyFilter();   // re-evaluate the "Qty > 0" view after a bulk change
}
// Outfit the picked ship: fetch its build list, set those blueprints' quantities (leaving your
// other picks intact), refresh the plan, and report any closest-class substitutions / unfillable
// slots inline.
async function bpBuildShip() {
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
function bpSort(col) {
  BP_SORT = BP_SORT && BP_SORT.col === col ? { col, dir: -BP_SORT.dir } : { col, dir: 1 };
  const tbody = document.querySelector("#blueprint .bp-table tbody"); if (!tbody) return;
  const rows = [...tbody.children];
  rows.sort((ra, rb) => {
    let a = (BP_CATALOG[+ra.dataset.i] || {})[col], b = (BP_CATALOG[+rb.dataset.i] || {})[col];
    if (col === "acquired") {   // sort by the EFFECTIVE Yes/No (manual override applied), not raw
      a = _bpCell(BP_CATALOG[+ra.dataset.i] || {}, col); b = _bpCell(BP_CATALOG[+rb.dataset.i] || {}, col);
    }
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
const _bpVisibleExcept = (skip) => (BP_CATALOG || []).filter(b =>
  BP_COLS.every(c => c.key === skip || !(BP_FILTERS[c.key] && BP_FILTERS[c.key].has(_bpCell(b, c.key)))));
const _bpDistinct = (col) => {
  const vals = [...new Set(_bpVisibleExcept(col).map(b => _bpCell(b, col)))];
  vals.sort(_bpNum(col) ? (a, b) => (+a) - (+b) : (a, b) => a.localeCompare(b));
  return vals;
};
let _bpFcol = null;
// The filter popup is imperative DOM (a body-appended #bp-fpop, its inputs read back off the live
// DOM by bpFilterToggle/bpFilterAll/bpFilterSearch). Only the trigger (the funnel button) is an
// @-binding.
function bpFilterOpen(e, col) {
  e.stopPropagation();
  let pop = $("bp-fpop");
  if (!pop) { pop = document.createElement("div"); pop.id = "bp-fpop"; pop.className = "bp-fpop"; document.body.appendChild(pop); }
  if (_bpFcol === col && pop.classList.contains("open")) { _bpFclose(); return; }
  _bpFcol = col;
  const ex = BP_FILTERS[col] || new Set();
  const opts = _bpDistinct(col).map(v =>
    html`<label class="bp-fopt"><input type="checkbox" value=${v} ?checked=${!ex.has(v)} @change=${(e) => bpFilterToggle(e.target.checked, v)}><span>${v === "" ? "(blank)" : v}</span></label>`);
  render(html`<div class="bp-fhead"><input class="bp-fsearch" placeholder="search…" aria-label="search values" @input=${(e) => bpFilterSearch(e.target.value)}><label class="bp-fall"><input type="checkbox" ?checked=${!ex.size} @change=${(e) => bpFilterAll(e.target.checked)}>All</label></div><div class="bp-fopts">${opts}</div>`, pop);
  const r = e.currentTarget.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 248)) + "px";
  pop.style.top = (r.bottom + 4) + "px";
  pop.classList.add("open");
}
function _bpFclose() { const p = $("bp-fpop"); if (p) p.classList.remove("open"); _bpFcol = null; }
function bpFilterToggle(checked, value) {
  if (!_bpFcol) return;
  const ex = BP_FILTERS[_bpFcol] || (BP_FILTERS[_bpFcol] = new Set());
  if (checked) ex.delete(value); else ex.add(value);
  if (!ex.size) delete BP_FILTERS[_bpFcol];
  _bpApplyFilter();
}
function bpFilterAll(checked) {
  if (!_bpFcol) return;
  if (checked) delete BP_FILTERS[_bpFcol]; else BP_FILTERS[_bpFcol] = new Set(_bpDistinct(_bpFcol));
  for (const cb of document.querySelectorAll("#bp-fpop .bp-fopts input")) cb.checked = checked;
  _bpApplyFilter();
}
function bpFilterSearch(q) {
  q = (q || "").trim().toLowerCase();
  for (const lab of document.querySelectorAll("#bp-fpop .bp-fopt"))
    lab.style.display = (!q || lab.textContent.toLowerCase().includes(q)) ? "" : "none";
}
// A row is shown when it clears every column filter AND, if the "Qty > 0" toggle is on, has a qty.
const _bpRowShown = (b) =>
  BP_COLS.every(c => { const ex = BP_FILTERS[c.key]; return !ex || !ex.has(_bpCell(b, c.key)); })
  && (!BP_ONLY || (BP_QTY[b.name] || 0) > 0);
function _bpApplyFilter() {
  for (const tr of document.querySelectorAll("#blueprint .bp-table tbody tr")) {
    const b = BP_CATALOG[+tr.dataset.i] || {};
    tr.style.display = _bpRowShown(b) ? "" : "none";
  }
  for (const c of BP_COLS) { const th = document.querySelector(`#blueprint .bp-table th[data-col="${c.key}"]`); if (th) th.classList.toggle("bp-filtered", !!BP_FILTERS[c.key]); }
  const qth = document.querySelector('#blueprint .bp-table th[data-col="qty"]'); if (qth) qth.classList.toggle("bp-filtered", BP_ONLY);
}
// Toggle the "Qty > 0" filter from the Qty column's funnel (the selected-only view); persisted.
function bpQtyFilter(e) {
  if (e) e.stopPropagation();
  BP_ONLY = !BP_ONLY;
  try { localStorage.setItem("bpOnly", BP_ONLY ? "1" : "0"); } catch (_) {}
  _bpApplyFilter();
}
// Toggle a row's Acquired checkbox. Stored as a manual override (localStorage) layered over the
// log-derived value, so you can correct a miss; checking it back to the log default drops the
// override. The cell stops the row click, so this never touches the row's qty/selection. Re-derive
// the reward-suppression set and re-apply the filter (an Acquired filter may now hide/show the row).
function bpAcqToggle(i, checked) {
  const b = (BP_CATALOG || [])[i]; if (!b) return;
  checked = !!checked;
  if (checked === !!b.acquired) delete BP_ACQ[b.name];   // back to the log default → drop the override
  else BP_ACQ[b.name] = checked;
  _bpAcqSave();
  ACQ_NAMES = new Set((BP_CATALOG || []).filter(_bpAcq).map(x => x.name));
  _bpApplyFilter();
  renderRewardCard();   // only the reward card depends on acquired state — leave materials/deposits untouched
}
const _miningDur = (s) => {
  s = Math.round(s || 0); const m = Math.floor(s / 60), sec = s % 60;
  return m ? `${m}m${sec ? " " + sec + "s" : ""}` : `${sec}s`;
};
// Last aggregated plan, cached so toggling a row's Acquired checkbox can re-render JUST the reward
// card (which alone depends on acquired state) without re-fetching or repainting the rest.
let BP_LAST_AGG = null;
// Sum the materials of every blueprint with a quantity (server) → one deposit-coverage plan,
// painted into the results div below the table.
async function renderBpPlan() {
  const out = "mres-plan";
  const src = BP_SHARED ? SHARED_PLAN : BP_QTY;   // the shared snapshot drives the cards in read-only mode
  const items = Object.entries(src).filter(([, q]) => q > 0).map(([name, qty]) => ({ name, qty }));
  if (!items.length) {
    mount(out, BP_SHARED ? html`` : html`<div class="empty">Set a quantity on one or more blueprints to see the materials you'll need.</div>`);
    return;
  }
  mount(out, html`<div class="empty">summing materials…</div>`);
  try {
    const agg = await fetch("/api/blueprints-plan", {
      method: "POST", headers: writeHeaders(), body: JSON.stringify({ items }),
    }).then(r => r.json());
    const plan = await fetch("/api/mining-plan", {
      method: "POST", headers: writeHeaders(), body: JSON.stringify({ minerals: agg.minerals || [] }),
    }).then(r => r.json());
    BP_LAST_AGG = agg;   // for renderRewardCard() on an Acquired toggle
    mount(out, html`${breakdownTpl(agg)}<div id="mres-rewards"></div>${planResultTpl(plan)}`);
    renderRewardCard();
  } catch (e) { mount(out, html`<div class="empty">plan failed</div>`); }
}
// Re-render ONLY the Reward-contracts card (its own #mres-rewards slot) from the cached plan, so
// toggling Acquired updates which blueprints the card drops without re-fetching the plan or
// repainting the materials/deposits below (acquired state doesn't affect those). lit diffs the slot.
function renderRewardCard() {
  if (!$("mres-rewards")) return;
  mount("mres-rewards", BP_LAST_AGG ? html`${unsafeHTML(contractsHtml(BP_LAST_AGG))}` : html``);
}
// The other half of "what do I need to build this": per chosen blueprint, the contracts that
// reward it, grouped by the faction that grants them. `sources` rides each aggregate item; we
// tolerate the pre-rebuild flat-string shape defensively. Pure-markup STRING helper (no handlers).
const _BP_SRC_CAP = 6;
const _bpSource = (s) => (typeof s === "string") ? { faction: s, contracts: [] } : (s || {});
function contractsHtml(agg) {
  let withSrc = 0, noSrc = 0;
  // Skip blueprints you've already acquired — no point showing how to earn one you own.
  const rows = (agg.items || []).filter(it => it.found && !ACQ_NAMES.has(it.name)).map(it => {
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
// The merged shopping list: every chosen recipe's materials summed by resource.
function breakdownTpl(agg) {
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
  const table = logTable(
    th("Material", false, "The mineral or resource to mine and refine") +
    th("Qty", true, "Total amount across all chosen blueprints, in SCU") +
    th("Min quality", true, "Strictest refined quality any chosen blueprint requires (— = any)") +
    th("For", false, "Which blueprints need this material"),
    rows, "No materials.");
  return html`<div class="card"><h3><span>Materials needed</span><span class="scu">${meta}</span></h3>
    ${unsafeHTML(table)}
  </div>`;
}
function planResultTpl(r) {
  const targets = r.targets || [];
  if (!targets.length) return html`<div class="empty">No minerals given.</div>`;
  const covRows = (r.coverage || []).slice(0, 15).map(c => `<tr>
    <td><b>${esc(c.deposit)}</b></td>
    <td class="lt-num">${c.n_covers}/${targets.length}</td>
    <td>${c.covers.map(x => tag(x)).join(" ")}</td>
    <td>${(c.signatures || []).map(num).join(", ")}</td></tr>`).join("");
  const srcs = (r.per_mineral || []).map(p => {
    const best = (p.rocks || []).slice(0, 3).map(x =>
      `${esc(x.name)} <span class="mn-dim">(RS ${num(x.rs)}${x.probability != null ? ", " + _chance(x.probability) : ""})</span>`).join("<br>");
    return `<div class="mrow"><span class="mk">${esc(p.mineral)}</span>
      <div class="mrow-v">${best || '<span class="mn-dim">no source found</span>'}${locChips(p.locations)}</div></div>`;
  }).join("");
  const covTable = logTable(
    th("Deposit", false, "A rock deposit / cluster type you can mine") +
    th("Covers", true, "How many of the blueprint's ingredients this deposit can yield") +
    th("Ingredients", false, "Which of the wanted minerals it covers") +
    th("RS", false, "Radar signature value(s) to scan for to find this deposit"),
    covRows, "No deposit yields any of these minerals.");
  return html`<div class="card"><h3><span>Best deposits — by coverage</span></h3>
      ${unsafeHTML(covTable)}
    </div>
    <div class="card"><h3><span>Per-ingredient sources</span></h3>${unsafeHTML(locKey())}<div class="mplan-srcs">${unsafeHTML(srcs)}</div></div>`;
}
