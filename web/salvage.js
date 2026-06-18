"use strict";
// Salvage mode: identify wreck ships and the components the salvage beam can strip off them.
// Two entry points: wrecks auto-detected from the live Game.log (snapshot.detected_salvage,
// refreshed by renderSalvage every poll) shown as pills, and a dropdown of EVERY salvageable
// ship (the full /api/salvage-ship catalog) for looking one up directly. Either way the ship
// expands to its removable-component breakdown, grouped by category, with non-pullable items
// (currently un-strippable: non-weapons over size 2) greyed + tagged.

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

// ---- pick any salvageable ship (dropdown over the whole catalog) ----
function pickerHtml() {
  if (CATALOG == null) return `<div class="empty">loading…</div>`;
  const opts = Object.entries(CATALOG)
    .map(([key, e]) => [key, e.manufacturer ? `${e.name} · ${e.manufacturer}` : e.name])
    .sort((a, b) => a[1].localeCompare(b[1]));
  if (!opts.length)
    return `<div class="empty">No salvageable-ship catalog yet (still building from the game files).</div>`;
  const options = [`<option value="">Select a ship…</option>`].concat(
    opts.map(([key, label]) =>
      `<option value="${esc(key)}"${key === PICKED ? " selected" : ""}>${esc(label)}</option>`)
  ).join("");
  // The <select> already shows the picked ship's name+manufacturer, so the detail header below
  // says "Removable components" (+ the pullable summary) instead of repeating it.
  const e = PICKED ? CATALOG[PICKED] : null;
  const detail = e ? detailPanel("Removable components", e.components, true) : "";
  return `<div class="mform"><select id="salv-pick" class="salv-select" aria-label="Salvageable ship"`
    + ` onchange="salvagePick(this.value)">${options}</select></div>${detail}`;
}

export function salvagePick(key) {
  PICKED = key || "";
  setHTML("salv-pick-wrap", pickerHtml());   // re-render select (selection kept) + the breakdown
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
