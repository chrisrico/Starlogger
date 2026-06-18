"use strict";
// Salvage mode: identify wreck ships and the components the salvage beam can strip off them.
// Two entry points: wrecks auto-detected from the live Game.log (snapshot.detected_salvage,
// refreshed by renderSalvage every poll) shown as pills, and a searchable dropdown of EVERY
// salvageable ship (the full /api/salvage-ship catalog, grouped by manufacturer) for looking
// one up directly. Either way the ship expands to its removable-component breakdown, grouped by
// category, with non-pullable items (currently un-strippable: non-weapons over size 2) greyed.

import { $, esc, setHTML } from "./dom.js";
import { getJSON } from "./net.js";
import { curData } from "./state.js";

// Detected-wreck pills currently expanded (by ship_class), so a live re-render keeps them open.
const OPEN = new Set();
// The full salvage-ship catalog {class_lower: entry}, fetched once for the dropdown (null until
// loaded); PICKED is the selected class. Lives in its own container so the live poll (which only
// refreshes the auto pills) never clobbers the selection.
let CATALOG = null;
let PICKED = "";

// Category display order + labels (weapons/ordnance first, then the headline components).
const CATS = [
  ["weapon", "Weapons"], ["turret", "Turrets"], ["turret_base", "Turret bases"],
  ["utility_turret", "Utility turrets"], ["missile_rack", "Missile racks"],
  ["missile", "Missiles"], ["countermeasure", "Countermeasures"],
  ["power_plant", "Power plant"], ["cooler", "Coolers"], ["shield", "Shields"],
  ["quantum_drive", "Quantum drive"], ["radar", "Radar"],
];
const CAT_LABEL = Object.fromEntries(CATS);

function compRow(c) {
  const size = c.size != null ? `S${c.size}` : "";
  const grade = c.grade ? ` · ${esc(c.grade)}` : "";
  const mult = c.count > 1 ? ` ×${c.count}` : "";
  // Greyed when not currently strippable in-game (non-weapon over size 2 -- our rule, since
  // the cap isn't in the game files; see NOTES). Still listed, just tagged.
  const lock = c.pullable ? "" :
    `<span class="salv-lock" title="Currently not strippable (non-weapon over size 2)">locked</span>`;
  return `<div class="salv-comp${c.pullable ? "" : " greyed"}">`
    + `<span class="salv-cn">${esc(c.name)}${mult}</span>`
    + `<span class="salv-cm">${size}${grade}</span>${lock}</div>`;
}

function componentsHtml(components) {
  if (!components || !components.length)
    return `<div class="empty">No removable components.</div>`;
  const by = {};
  for (const c of components) (by[c.category] = by[c.category] || []).push(c);
  const blocks = CATS.filter(([k]) => by[k]).map(([k, label]) =>
    `<div class="salv-cat"><div class="salv-cat-h">${label}</div>${by[k].map(compRow).join("")}</div>`);
  for (const k of Object.keys(by)) if (!CAT_LABEL[k])   // future-proof: unknown categories
    blocks.push(`<div class="salv-cat"><div class="salv-cat-h">${esc(k)}</div>${by[k].map(compRow).join("")}</div>`);
  return `<div class="salv-comps">${blocks.join("")}</div>`;
}

// "11/15 pullable" (counting ×N multiplicities) -- the actionable number for a salvager;
// collapses to "15 pullable" when nothing's locked. Empty string for no components.
function pullSummary(components) {
  const cs = components || [];
  if (!cs.length) return "";
  const units = c => (c.count > 1 ? c.count : 1);
  const total = cs.reduce((n, c) => n + units(c), 0);
  const pull = cs.reduce((n, c) => n + (c.pullable ? units(c) : 0), 0);
  return pull === total ? `${total} pullable` : `${pull}/${total} pullable`;
}

// The expanded breakdown panel, shared by the auto pills and the dropdown. `label` is the
// header's left text, inserted RAW -- callers escape it (the pills pass a ship name; the
// dropdown a fixed string, since the <select> already names the ship, so we don't repeat it).
// The header's right side carries the pullable-count summary rather than duplicating the name.
function detailPanel(label, components, resolved) {
  const body = resolved
    ? componentsHtml(components)
    : `<div class="empty">Component data not built yet (catalog still building).</div>`;
  const sum = resolved ? pullSummary(components) : "";
  return `<div class="salv-detail"><div class="salv-detail-h"><span>${label}</span>`
    + `${sum ? `<span class="scu">${sum}</span>` : ""}</div>${body}</div>`;
}

function pill(shipClass, name, count, open, resolved, fn) {
  const mult = count > 1 ? ` ×${count}` : "";
  const cls = "salv-pill" + (open ? " open" : "") + (resolved === false ? " unresolved" : "");
  return `<button class="${cls}" aria-expanded="${open}" `
    + `onclick="${fn}('${esc(shipClass)}')">${esc(name)}${mult}</button>`;
}

// ---- auto-detected wrecks (from the live snapshot) ----
function autoHtml(d) {
  const list = (d && d.detected_salvage) || [];
  if (!list.length)
    return `<div class="empty">No salvageable wrecks detected yet this session. `
      + `Fly to a salvage site — each wreck that spawns appears here.</div>`;
  const pills = list.map(s => pill(s.ship_class, s.name, s.count, OPEN.has(s.ship_class),
                                   s.resolved, "salvageToggle")).join("");
  const detail = list.filter(s => OPEN.has(s.ship_class)).map(s =>
    detailPanel(`${esc(s.name)}${s.manufacturer ? ` · ${esc(s.manufacturer)}` : ""}`,
                s.components, s.resolved)).join("");
  return `<div class="salv-pills">${pills}</div>${detail}`;
}

export function renderSalvage(d) {
  if (!$("salv-auto")) return;   // shell not built yet (Salvage tab never opened)
  setHTML("salv-auto", autoHtml(d || curData()));
}

export function salvageToggle(shipClass) {
  OPEN.has(shipClass) ? OPEN.delete(shipClass) : OPEN.add(shipClass);
  renderSalvage(curData());
}

// ---- pick any salvageable ship: a searchable dropdown grouped by manufacturer ----
// Mirrors the blueprint picker (mining.js): an <input role="combobox"> over a floating
// listbox of manufacturer sections, type-to-filter, arrow-key navigation. Picking a ship
// renders its removable-component breakdown below.

// Catalog grouped into manufacturer sections (sorted); ships sorted by name within each.
function shipSections() {
  const byMfr = new Map();
  for (const [key, e] of Object.entries(CATALOG || {})) {
    const mfr = e.manufacturer || "Other";
    if (!byMfr.has(mfr)) byMfr.set(mfr, []);
    byMfr.get(mfr).push({ key, name: e.name || key });
  }
  return [...byMfr.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([mfr, ships]) => ({ mfr, ships: ships.sort((x, y) => x.name.localeCompare(y.name)) }));
}

let _salvId = 0;
function shipMenuHtml() {
  _salvId = 0;
  return shipSections().map(sec => {
    const items = sec.ships.map(s =>
      `<div class="salv-dd-item" role="option" id="salv-opt-${_salvId++}" data-key="${esc(s.key)}"`
      + ` data-search="${esc(s.name.toLowerCase())}" aria-selected="${s.key === PICKED}"`
      + ` onclick="salvagePick(this.dataset.key)"><span>${esc(s.name)}</span></div>`).join("");
    return `<div class="salv-dd-sec"><div class="salv-dd-grp">`
      + `<span class="salv-dd-mfr">${esc(sec.mfr)}</span>`
      + `<span class="salv-dd-n">${sec.ships.length}</span></div>${items}</div>`;
  }).join("");
}

function pickerHtml() {
  if (CATALOG == null) return `<div class="empty">loading…</div>`;
  if (!Object.keys(CATALOG).length)
    return `<div class="empty">No salvageable-ship catalog yet (still building from the game files).</div>`;
  // The combobox already shows the picked ship's name, so the detail header says "Removable
  // components" (+ the pullable summary) rather than repeating it.
  const e = PICKED ? CATALOG[PICKED] : null;
  const detail = e ? detailPanel("Removable components", e.components, true) : "";
  return `<div class="salv-pick-row">
      <div class="salv-dd">
        <input id="salv-pick" class="salv-dd-in" autocomplete="off" role="combobox"
          aria-expanded="false" aria-controls="salv-dd-list" aria-autocomplete="list"
          aria-label="Salvageable ship" placeholder="Search salvageable ships…"
          value="${e ? esc(e.name) : ""}"
          oninput="salvageDdFilter(this.value)" onfocus="salvageDdOpen(true)"
          onblur="salvageDdOpen(false)" onkeydown="salvageDdKey(event)">
        <div id="salv-dd-list" class="salv-dd-list" role="listbox" aria-label="Salvageable ships"
          onmousedown="event.preventDefault()">${shipMenuHtml()}</div>
      </div>
    </div>${detail}`;
}

export function salvagePick(key) {
  PICKED = key || "";
  setHTML("salv-pick-wrap", pickerHtml());   // closes the menu + renders the breakdown
}

// ---- combobox open / filter / keyboard (mirrors mining.js's blueprint picker) ----
export function salvageDdOpen(show) {
  const el = $("salv-dd-list"); if (!el) return;
  el.classList.toggle("open", !!show);
  const inp = $("salv-pick"); if (inp) inp.setAttribute("aria-expanded", show ? "true" : "false");
  // The card clip-path would crop the menu; drop it while open so the list can overflow.
  const card = el.closest(".card"); if (card) card.classList.toggle("dd-open", !!show);
  if (!show) _salvSetActive(null);
}
const _salvVisible = () =>
  [...($("salv-dd-list") || {}).querySelectorAll?.(".salv-dd-item") || []].filter(it => it.style.display !== "none");
const _salvActive = () => ($("salv-dd-list") || {}).querySelector?.(".salv-dd-item.salv-dd-active") || null;
function _salvSetActive(it) {
  const list = $("salv-dd-list"); if (!list) return;
  list.querySelectorAll(".salv-dd-item.salv-dd-active").forEach(e => e.classList.remove("salv-dd-active"));
  const inp = $("salv-pick");
  if (it) {
    it.classList.add("salv-dd-active");
    it.scrollIntoView({ block: "nearest" });
    if (inp) inp.setAttribute("aria-activedescendant", it.id);
  } else if (inp) {
    inp.removeAttribute("aria-activedescendant");
  }
}
// Filter items by a case-insensitive substring; hide whole sections with no visible items.
export function salvageDdFilter(q) {
  const list = $("salv-dd-list"); if (!list) return;
  list.classList.add("open");
  const needle = (q || "").trim().toLowerCase();
  for (const sec of list.querySelectorAll(".salv-dd-sec")) {
    let any = false;
    for (const it of sec.querySelectorAll(".salv-dd-item")) {
      const show = !needle || it.dataset.search.includes(needle);
      it.style.display = show ? "" : "none";
      if (show) any = true;
    }
    sec.classList.toggle("hide", !any);
  }
  _salvSetActive(null);   // the visible set changed; drop any stale arrow highlight
}
export function salvageDdKey(e) {
  if (e.key === "Escape") { salvageDdOpen(false); return; }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    salvageDdOpen(true);
    const vis = _salvVisible(); if (!vis.length) return;
    const cur = _salvActive();
    let i = cur ? vis.indexOf(cur) : -1;
    i = e.key === "ArrowDown" ? (i + 1) % vis.length : (i <= 0 ? vis.length - 1 : i - 1);
    _salvSetActive(vis[i]);
    return;
  }
  if (e.key !== "Enter") return;
  e.preventDefault();   // pick the arrow-highlighted option, else the first visible match
  const pick = _salvActive() || _salvVisible()[0];
  if (pick) salvagePick(pick.dataset.key);
}

async function renderPicker() {
  if (!$("salv-pick-wrap")) return;
  if (CATALOG == null) {
    try { CATALOG = (await getJSON("/api/salvage-ship")).ships || {}; }
    catch (e) { CATALOG = {}; }
  }
  setHTML("salv-pick-wrap", pickerHtml());
}

// ---- shell ----
function shell() {
  // Wrap the cards in .salvage (a gap'd flex column, like .mining) so they don't sit flush;
  // <span>-wrapped titles pick up the shared .card h3 cyan accent bar every other card has.
  setHTML("salvage", `
    <div class="salvage">
      <div class="card">
        <h3><span>Detected wrecks</span><small>this session</small></h3>
        <div id="salv-auto"></div>
      </div>
      <div class="card">
        <h3><span>Any salvageable ship</span></h3>
        <div id="salv-pick-wrap"></div>
      </div>
    </div>`);
}

export function initSalvage() {
  if (!$("salv-auto")) shell();
  renderSalvage(curData());
  renderPicker();
}
