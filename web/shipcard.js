"use strict";
// Shared "ship component info" card: a ship's removable (salvage-beam-strippable) components,
// grouped by category, with the non-pullable ones (our size-2 rule for non-weapons) greyed +
// tagged, plus an "N/M pullable" summary. Used by Salvage mode (detected-wreck pills + the
// ship picker) and the Signal ID page (a wreck-hull reading). The markup keeps the historical
// `salv-*` classes so both pages share one stylesheet block.

import { esc } from "./dom.js";

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

export function componentsHtml(components) {
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
export function pullSummary(components) {
  const cs = components || [];
  if (!cs.length) return "";
  const units = c => (c.count > 1 ? c.count : 1);
  const total = cs.reduce((n, c) => n + units(c), 0);
  const pull = cs.reduce((n, c) => n + (c.pullable ? units(c) : 0), 0);
  return pull === total ? `${total} pullable` : `${pull}/${total} pullable`;
}

// The breakdown panel. `label` is the header's left text, inserted RAW -- callers escape it
// (Salvage's detected pills pass a ship name; its picker a fixed "Removable components"; Signal ID
// the resolved hull name). The header's right carries the pullable summary, never a repeat of the
// name. `resolved` false swaps the body for `unresolvedMsg` (e.g. catalog not built / unknown hull).
export function shipDetailPanel(label, components, resolved,
    unresolvedMsg = "Component data not built yet (catalog still building).") {
  const body = resolved ? componentsHtml(components) : `<div class="empty">${unresolvedMsg}</div>`;
  const sum = resolved ? pullSummary(components) : "";
  return `<div class="salv-detail"><div class="salv-detail-h"><span>${label}</span>`
    + `${sum ? `<span class="scu">${sum}</span>` : ""}</div>${body}</div>`;
}
