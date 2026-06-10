"use strict";
// Ship mining-equipment popup (the ⚙ button beside the ship selector). Lets the user fit a
// mining laser HEAD + its consumable MODULES to the current mining ship; the choice persists
// per ship via /api/mining-gear and feeds the Identify tab's rock-feasibility verdict.
// Self-contained: owns the gear catalog cache + the saved-selection map, renders its own
// overlay, and bridges its own inline handlers. Imported for side effects (wires close/Esc).
import { $, esc, num, toast, hintIcon } from "./dom.js";
import { getJSON, postJSON } from "./net.js";
import { curData } from "./state.js";

// The full gear catalog (heads + modules), fetched once, and the saved {ship: {head, modules}}
// selection map. Used by currentLoadout() to resolve the feasibility calc without a refetch.
let GEAR = null;                     // { heads:[...], modules:[...] }
let SELECTED = {};                   // { shipName: {head: <class|null>, modules: [<class>...]} }
let EDIT = null;                     // the ship + working selection while the popup is open

// Ensure the catalog + saved selections are loaded once (idempotent). Safe to call eagerly.
export async function ensureGear() {
  if (GEAR) return GEAR;
  try {
    const r = await getJSON("/api/mining-gear");
    GEAR = { heads: r.heads || [], modules: r.modules || [] };
    SELECTED = r.selected || {};
  } catch (_) { GEAR = { heads: [], modules: [] }; }
  return GEAR;
}

const _headByClass = (cls) => (GEAR?.heads || []).find(h => h.class === cls) || null;
const _moduleByClass = (cls) => (GEAR?.modules || []).find(m => m.class === cls) || null;

// The full gear catalog (every head + module, both sizes), for the Identify tab's
// "can't crack → try this gear" suggester. Empty until ensureGear() has run once.
export function gearCatalog() { return GEAR || { heads: [], modules: [] }; }

// The current mining ship's resolved loadout for the feasibility calc: the head + module
// RECORDS (not class strings) the user picked. Returns null when there's no current ship.
// `isMiningShip` lets the caller distinguish "miner, no gear set" from "not a miner".
export function currentLoadout() {
  const d = curData();
  if (!d || !d.ship) return null;
  const sel = SELECTED[d.ship] || {};
  return {
    ship: d.ship,
    isMiningShip: !!d.mining_ship,
    hardpoints: d.mining_hardpoints || [],
    head: sel.head ? _headByClass(sel.head) : null,
    modules: (sel.modules || []).map(_moduleByClass).filter(Boolean),
  };
}

// ---- popup ---------------------------------------------------------------- //
export async function openShipEquip() {
  const d = curData();
  const ship = d && d.ship;
  const ov = $("shipEquipOverlay");
  if (!ov) return;
  $("seTitle").textContent = ship ? `Ship equipment — ${ship}` : "Ship equipment";
  $("seMsg").textContent = ""; $("seMsg").className = "sp-msg";
  $("seBody").innerHTML = `<div class="sp-row"><span class="h">loading…</span></div>`;
  ov.classList.remove("hide"); ov.setAttribute("aria-hidden", "false");
  if (!ship) { $("seBody").innerHTML = `<div class="sp-row"><span class="h">No ship selected.</span></div>`; return; }
  try {
    await ensureGear();                                  // keep the global cache fresh too
    const r = await getJSON(`/api/mining-gear?ship=${encodeURIComponent(ship)}`);
    EDIT = {
      ship,
      hardpoints: r.hardpoints || [],
      heads: r.heads || [],                              // already filtered to this ship's sizes
      head: (r.selected && r.selected.head) || null,
      modules: (r.selected && r.selected.modules) || [],
    };
    renderEquip();
  } catch (e) {
    $("seBody").innerHTML = `<div class="sp-row"><span class="h">couldn't load gear: ${esc(e)}</span></div>`;
  }
}

export function closeShipEquip() {
  const ov = $("shipEquipOverlay");
  if (!ov) return;
  ov.classList.add("hide"); ov.setAttribute("aria-hidden", "true");
  EDIT = null;
}

// Group identical hardpoint sizes into a readable summary ("3 × S2", "S1 + S2").
function _hardpointSummary(hp) {
  const counts = {};
  hp.forEach(s => { counts[s] = (counts[s] || 0) + 1; });
  return Object.keys(counts).sort().map(s =>
    `${counts[s] > 1 ? counts[s] + " × " : ""}S${s}`).join(" + ");
}

function _headOption(h) {
  const sel = h.class === EDIT.head ? " selected" : "";
  return `<option value="${esc(h.class)}"${sel}>${esc(h.name)} — S${h.size}, ${num(h.power)} power, ${h.module_slots} slot${h.module_slots === 1 ? "" : "s"}</option>`;
}

function _moduleOptions(picked) {
  const opts = [`<option value="">— none —</option>`];
  for (const m of EDIT.modules_catalog || GEAR.modules) {
    opts.push(`<option value="${esc(m.class)}"${m.class === picked ? " selected" : ""}>${esc(m.name)} (${esc(m.manufacturer_code || "")})</option>`);
  }
  return opts.join("");
}

function renderEquip() {
  if (!EDIT.hardpoints.length) {
    $("seBody").innerHTML = `<div class="sp-row"><span class="h">This ship has no
      ship-mounted mining-laser hardpoints — its mining gear isn't configurable here.</span></div>`;
    return;
  }
  EDIT.modules_catalog = GEAR.modules;
  const head = EDIT.head ? _headByClass(EDIT.head) : null;
  const slots = head ? head.module_slots : 0;
  // Trim any saved modules beyond the current head's slot count.
  EDIT.modules = EDIT.modules.slice(0, slots);
  // One equipment category for now (Mining); the popup is generic so others can follow.
  let html = `<h3 class="se-cat">Mining</h3>`;
  html += `<div class="se-hp">Mining hardpoints: <b>${_hardpointSummary(EDIT.hardpoints)}</b></div>`;
  html += `<div class="sp-row"><div class="sp-label"><span class="t">Mining laser ${hintIcon("The head fitted to your mining turret.")}</span></div>
    <div class="sp-ctl"><select id="se-head" onchange="seHeadChange()">
      <option value="">— none —</option>${EDIT.heads.map(_headOption).join("")}</select></div></div>`;
  for (let i = 0; i < slots; i++) {
    html += `<div class="sp-row"><div class="sp-label"><span class="t">Module ${i + 1} ${hintIcon("A consumable gadget slotted into the head.")}</span></div>
      <div class="sp-ctl"><select id="se-mod-${i}" onchange="seModuleChange()">
        ${_moduleOptions(EDIT.modules[i] || "")}</select></div></div>`;
  }
  if (head) {
    // The laser's own modifiers, then each fitted module's additions stacked beneath it,
    // so the combined effect of the loadout is readable at a glance.
    html += _statLine(head.name, head.modifiers, "no inherent modifiers");
    for (const cls of EDIT.modules) {
      const mod = _moduleByClass(cls);
      if (mod) html += _statLine(mod.name, mod.modifiers, "no effect");
    }
  }
  $("seBody").innerHTML = html;
}

// One "Name: stat +x% · stat -y%" line for a head or module's modifier map.
function _statLine(name, modifiers, emptyText) {
  const mods = modifiers || {};
  const body = Object.keys(mods).length
    ? Object.entries(mods).map(([k, v]) => `${k.replace(/_/g, " ")} ${v > 0 ? "+" : ""}${v}%`).join(" · ")
    : emptyText;
  return `<div class="se-note mn-dim"><b>${esc(name)}</b>: ${esc(body)}</div>`;
}

// Head change: re-read the pick, reset modules that no longer fit, re-render the slot rows.
export function seHeadChange() {
  EDIT.head = $("se-head").value || null;
  EDIT.modules = [];                       // a new head changes the slot count → start clean
  renderEquip();
}
export function seModuleChange() {
  const head = EDIT.head ? _headByClass(EDIT.head) : null;
  const slots = head ? head.module_slots : 0;
  const mods = [];
  for (let i = 0; i < slots; i++) {
    const v = $(`se-mod-${i}`) && $(`se-mod-${i}`).value;
    if (v) mods.push(v);
  }
  EDIT.modules = mods;
  renderEquip();   // refresh the per-module stat lines beneath the laser
}

export async function saveShipEquip() {
  if (!EDIT) return closeShipEquip();
  seModuleChange();                        // capture the latest module picks
  const btn = $("seSave"); btn.disabled = true;
  try {
    await postJSON("/api/mining-gear", { ship: EDIT.ship, head: EDIT.head, modules: EDIT.modules });
    // Mirror the save into the local caches so the feasibility verdict updates immediately.
    SELECTED[EDIT.ship] = { head: EDIT.head, modules: EDIT.modules.slice() };
    closeShipEquip();
    toast("Mining loadout saved");
    document.dispatchEvent(new CustomEvent("loadout-changed"));
  } catch (e) {
    const m = $("seMsg"); m.textContent = String(e); m.className = "sp-msg err";
  } finally { btn.disabled = false; }
}

// ---- wiring (close / backdrop / Escape) ----------------------------------- //
$("seClose") && ($("seClose").onclick = closeShipEquip);
$("seCancel") && ($("seCancel").onclick = closeShipEquip);
$("seSave") && ($("seSave").onclick = saveShipEquip);
$("shipEquipOverlay") && ($("shipEquipOverlay").onclick = (e) => {
  if (e.target.id === "shipEquipOverlay") closeShipEquip();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("shipEquipOverlay") && !$("shipEquipOverlay").classList.contains("hide")) closeShipEquip();
});

// Self-bridge inline handlers (matches the jukebox/archive convention; the static
// tests/test_window_bridge.py guard unions every Object.assign(window,…) block).
Object.assign(window, { openShipEquip, closeShipEquip, seHeadChange, seModuleChange, saveShipEquip });
