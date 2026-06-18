"use strict";
// Salvage mode: identify wreck ships and the components the salvage beam can strip off them.
// Two pill sources (see the feature notes): wrecks auto-detected from the live Game.log
// (snapshot.detected_salvage, refreshed by renderSalvage every poll) and a manual RS lookup
// (reuses /api/rock-lookup's salvage candidates, then /api/salvage-ship for components).
// Clicking a ship pill expands its removable-component breakdown, grouped by category, with
// non-pullable items (currently un-strippable: non-weapons over size 2) greyed + tagged.

import { $, esc, val, setHTML } from "./dom.js";
import { getJSON } from "./net.js";
import { curData } from "./state.js";

// Detected-wreck pills currently expanded (by ship_class), so a live re-render keeps them open.
const OPEN = new Set();
// Last manual-RS lookup result -- rendered into its own container so the live poll never
// clobbers it. {ships:[{class,name}], open, openName, comp} | {error} | null.
let MANUAL = null;

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

// ---- manual RS lookup (best-effort: only the few wreck hulls with a radar signature) ----
function manualHtml() {
  if (!MANUAL) return "";
  if (MANUAL.error) return `<div class="empty">${esc(MANUAL.error)}</div>`;
  if (!MANUAL.ships.length)
    return `<div class="empty">No salvageable ship hull matches that reading.</div>`;
  const pills = MANUAL.ships.map(s =>
    pill(s.name, s.name, 0, MANUAL.open === s.name, undefined, "salvageManualPick")).join("");
  let detail = "";
  if (MANUAL.open) {
    detail = `<div class="salv-detail"><div class="salv-detail-h">${esc(MANUAL.open)}</div>`
      + (MANUAL.comp == null
          ? `<div class="empty">No component data for this hull.</div>`
          : componentsHtml(MANUAL.comp))
      + `</div>`;
  }
  return `<div class="salv-pills">${pills}</div>${detail}`;
}

export async function salvageIdentify() {
  const v = parseFloat(val("salv-rs"));
  if (!(v > 0)) { MANUAL = { error: "Enter a positive RS reading." }; setHTML("salv-manual", manualHtml()); return; }
  setHTML("salv-manual", `<div class="empty">scanning…</div>`);
  try {
    const look = await getJSON(`/api/rock-lookup?rs=${v}`);
    const seen = new Set(), ships = [];
    for (const g of (look.salvage || [])) {
      if (g.kind !== "ship") continue;   // debris panels aren't a ship to strip
      for (const t of (g.targets || [])) {
        const name = t.ship || t.name;
        if (name && !seen.has(name)) { seen.add(name); ships.push({ name }); }
      }
    }
    MANUAL = { ships, open: null, comp: null };
  } catch (e) { MANUAL = { error: "lookup failed" }; }
  setHTML("salv-manual", manualHtml());
}

export async function salvageManualPick(name) {
  if (MANUAL && MANUAL.open === name) { MANUAL.open = null; MANUAL.comp = null; setHTML("salv-manual", manualHtml()); return; }
  setHTML("salv-manual", `<div class="empty">loading…</div>`);
  try {
    const hit = await getJSON(`/api/salvage-ship?name=${encodeURIComponent(name)}`);
    MANUAL.open = name;
    MANUAL.comp = (hit && hit.components) || null;   // null → no catalog entry for this hull
  } catch (e) { MANUAL.open = name; MANUAL.comp = null; }
  setHTML("salv-manual", manualHtml());
}

export function salvageKey(e) { if (e.key === "Enter") salvageIdentify(); }

// ---- shell ----
function shell() {
  setHTML("salvage", `
    <div class="card">
      <h3>Detected wrecks <small>this session</small></h3>
      <div id="salv-auto"></div>
    </div>
    <div class="card mtool">
      <h3>Identify ship by RS</h3>
      <div class="mform">
        <input id="salv-rs" type="text" inputmode="numeric" autocomplete="off"
          placeholder="e.g. 2400" aria-label="Radar signature reading" onkeydown="salvageKey(event)">
        <button class="primary" onclick="salvageIdentify()">Identify</button>
      </div>
      <div id="salv-manual"></div>
    </div>`);
}

export function initSalvage() {
  if (!$("salv-auto")) shell();
  renderSalvage(curData());
  if (MANUAL) setHTML("salv-manual", manualHtml());
}
