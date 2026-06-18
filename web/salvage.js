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
    `<div class="salv-detail"><div class="salv-detail-h">${esc(s.name)}`
    + `${s.manufacturer ? ` · ${esc(s.manufacturer)}` : ""}</div>`
    + (s.resolved ? componentsHtml(s.components)
                  : `<div class="empty">Component data not built yet (catalog still building).</div>`)
    + `</div>`).join("");
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
  const e = PICKED ? CATALOG[PICKED] : null;
  const detail = e
    ? `<div class="salv-detail"><div class="salv-detail-h">${esc(e.name)}`
      + `${e.manufacturer ? ` · ${esc(e.manufacturer)}` : ""}</div>`
      + componentsHtml(e.components) + `</div>`
    : "";
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
  setHTML("salvage", `
    <div class="card">
      <h3>Detected wrecks <small>this session</small></h3>
      <div id="salv-auto"></div>
    </div>
    <div class="card">
      <h3>Any salvageable ship</h3>
      <div id="salv-pick-wrap"></div>
    </div>`);
}

export function initSalvage() {
  if (!$("salv-auto")) shell();
  renderSalvage(curData());
  renderPicker();
}
