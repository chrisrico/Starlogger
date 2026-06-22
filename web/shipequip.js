"use strict";
// Ship mining-equipment popup (the ⚙ button beside the ship selector). Lets the user fit a
// mining laser HEAD + its consumable MODULES + a RADAR to the current mining ship; each slot's
// options are ranked best-for-mining (★ marks the top pick). The choice persists per ship via
// /api/mining-gear and feeds the Identify tab's rock-feasibility verdict (head + modules; the
// radar is a prospecting quality-of-life pick, tracked but not part of the crack math).
// Self-contained: owns the gear catalog cache + the saved-selection map, renders its own
// overlay, and bridges its own inline handlers. Imported for side effects (wires close/Esc).
import { $, esc, num, toast, hintIcon } from "./dom.js";
import { getJSON, postJSON } from "./net.js";
import { curData } from "./state.js";

// The full gear catalog (heads + modules + radars), fetched once, and the saved
// {ship: {head, modules, radar}} selection map. Used by currentLoadout() to resolve the
// feasibility calc without a refetch.
let GEAR = null;                     // { heads:[...], modules:[...], radars:[...] }
let SELECTED = {};                   // { shipName: {head, modules:[...], radar} }
let EDIT = null;                     // the ship + working selection while the popup is open

// Ensure the catalog + saved selections are loaded once (idempotent). Safe to call eagerly.
export async function ensureGear() {
  if (GEAR) return GEAR;
  try {
    const r = await getJSON("/api/mining-gear");
    GEAR = { heads: r.heads || [], modules: r.modules || [], radars: r.radars || [] };
    SELECTED = r.selected || {};
  } catch (_) { GEAR = { heads: [], modules: [], radars: [] }; }
  return GEAR;
}

const _headByClass = (cls) => (GEAR?.heads || []).find(h => h.class === cls) || null;
const _moduleByClass = (cls) => (GEAR?.modules || []).find(m => m.class === cls) || null;

// ---- best-for-mining ranking ---------------------------------------------- //
// Reference rock resistance for ranking HEADS by effective extraction power, faithful to the
// feasibility model (effPower = power × (1 − resistance × (1 + resistance-mod%/100))). A fixed
// mid-hardness reference -> a stable best-first order; the Identify tab still judges real rocks.
const REF_RES = 0.5;
const _headScore = (h) => (h.power || 0) *
  (1 - REF_RES * (1 + (((h.modifiers || {}).resistance) || 0) / 100));
// MODULES: the two crack levers are +power% and −resistance% (both lift the margin); window_size
// is a minor softener. Higher = better. Passives (always-on) are ranked ahead of consumables.
const _moduleScore = (m) => {
  const x = m.modifiers || {};
  return (x.power || 0) - (x.resistance || 0) + (x.window_size || 0) * 0.1;
};
// RADARS rank purely on the resource-signature (RS) detection stat, piercing as a tiebreak.
const _radarSort = (a, b) => (b.rs - a.rs) || (b.rs_piercing - a.rs_piercing)
  || a.name.localeCompare(b.name);

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
      radars: r.radars || [],                            // already filtered to this ship's radar size
      radar_slot: r.radar_slot || null,                  // {size, stock} for this ship
      head: (r.selected && r.selected.head) || null,
      modules: (r.selected && r.selected.modules) || [],
      radar: (r.selected && r.selected.radar) || null,
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

function _headOption(h, best) {
  const sel = h.class === EDIT.head ? " selected" : "";
  const star = _headScore(h) >= best - 1e-9 ? "★ " : "";
  const res = (h.modifiers || {}).resistance;
  const resTxt = res ? `, ${res > 0 ? "+" : ""}${res}% resist` : "";
  return `<option value="${esc(h.class)}"${sel}>${star}${esc(h.name)} — S${h.size}, ${num(h.power)} power${resTxt}, ${h.module_slots} slot${h.module_slots === 1 ? "" : "s"}</option>`;
}

function _moduleOptions(picked) {
  // Passives first, then by crack score; ★ the best passive(s) that actually help.
  const cat = [...(EDIT.modules_catalog || GEAR.modules)]
    .sort((a, b) => (a.active ? 1 : 0) - (b.active ? 1 : 0) || _moduleScore(b) - _moduleScore(a));
  const passiveScores = cat.filter(m => !m.active).map(_moduleScore);
  const best = passiveScores.length ? Math.max(...passiveScores) : -Infinity;
  const opts = [`<option value="">— none —</option>`];
  for (const m of cat) {
    const star = (!m.active && best > 0 && _moduleScore(m) >= best - 1e-9) ? "★ " : "";
    opts.push(`<option value="${esc(m.class)}"${m.class === picked ? " selected" : ""}>${star}${esc(m.name)} (${esc(m.manufacturer_code || "")})</option>`);
  }
  return opts.join("");
}

function _radarOption(r, bestRs, stock) {
  const sel = r.class === EDIT.radar ? " selected" : "";
  const star = r.rs >= bestRs - 1e-9 ? "★ " : "";
  const isStock = stock && r.class.toLowerCase() === stock ? " · stock" : "";
  return `<option value="${esc(r.class)}"${sel}>${star}${esc(r.name)} (${esc(r.manufacturer_code || "")}) — RS ${Math.round((r.rs || 0) * 100)}%${isStock}</option>`;
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
  html += `<div class="se-rec"><button type="button" class="se-rec-btn" onclick="applyRecommendedGear()">★ Apply recommended</button>
    <span class="mn-dim">best head, modules &amp; radar for this ship</span></div>`;
  html += `<div class="se-hp">Mining hardpoints: <b>${_hardpointSummary(EDIT.hardpoints)}</b></div>`;
  // Heads ranked by effective extraction power (best first; ★ = top).
  const heads = [...EDIT.heads].sort((a, b) => _headScore(b) - _headScore(a));
  const headBest = heads.length ? _headScore(heads[0]) : -Infinity;
  html += `<div class="sp-row"><div class="sp-label"><span class="t">Mining laser ${hintIcon("The head fitted to your mining turret. Ranked by effective extraction power.")}</span></div>
    <div class="sp-ctl"><select id="se-head" onchange="seHeadChange()">
      <option value="">— none —</option>${heads.map(h => _headOption(h, headBest)).join("")}</select></div></div>`;
  for (let i = 0; i < slots; i++) {
    html += `<div class="sp-row"><div class="sp-label"><span class="t">Module ${i + 1} ${hintIcon("A gadget slotted into the head. Ranked by crack benefit (power + resistance).")}</span></div>
      <div class="sp-ctl"><select id="se-mod-${i}" onchange="seModuleChange()">
        ${_moduleOptions(EDIT.modules[i] || "")}</select></div></div>`;
  }
  // Radar slot: ranked by resource-signature (RS); only shown when the ship has a radar slot
  // and the catalog has matching radars.
  if (EDIT.radars && EDIT.radars.length) {
    const radars = [...EDIT.radars].sort(_radarSort);
    const bestRs = radars[0].rs;
    const stock = ((EDIT.radar_slot || {}).stock || "").toLowerCase();
    const tied = radars.filter(r => r.rs >= bestRs - 1e-9).length;
    html += `<div class="sp-row"><div class="sp-label"><span class="t">Radar ${hintIcon("Sets how far off you can read a deposit's composition (resource signature, RS). Minor for mining.")}</span></div>
      <div class="sp-ctl"><select id="se-radar" onchange="seRadarChange()">
        <option value="">— none —</option>${radars.map(r => _radarOption(r, bestRs, stock)).join("")}</select></div></div>`;
    html += `<div class="se-note mn-dim">Radar barely affects mining — it only sets how far off a deposit's
      composition is readable (RS). ${tied > 1 ? `${tied} radars tie at the top (RS ${Math.round(bestRs * 100)}%); pick any.` : ""}
      The head + modules drive yield.</div>`;
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
export function seRadarChange() {
  EDIT.radar = ($("se-radar") && $("se-radar").value) || null;
}

// One-click best loadout: top head (effective power), its slots filled with the top distinct
// passive modules, and the top-RS radar. The user can still override any slot afterwards.
export function applyRecommendedGear() {
  const heads = [...EDIT.heads].sort((a, b) => _headScore(b) - _headScore(a));
  EDIT.head = heads.length ? heads[0].class : null;
  const head = EDIT.head ? _headByClass(EDIT.head) : null;
  const slots = head ? head.module_slots : 0;
  EDIT.modules = [...(GEAR.modules || [])]
    .filter(m => !m.active && _moduleScore(m) > 0)
    .sort((a, b) => _moduleScore(b) - _moduleScore(a))
    .slice(0, slots).map(m => m.class);
  if (EDIT.radars && EDIT.radars.length) EDIT.radar = [...EDIT.radars].sort(_radarSort)[0].class;
  renderEquip();
}

export async function saveShipEquip() {
  if (!EDIT) return closeShipEquip();
  seModuleChange();                        // capture the latest module picks
  seRadarChange();                         // and the radar pick
  const btn = $("seSave"); btn.disabled = true;
  try {
    await postJSON("/api/mining-gear",
      { ship: EDIT.ship, head: EDIT.head, modules: EDIT.modules, radar: EDIT.radar });
    // Mirror the save into the local caches so the feasibility verdict updates immediately.
    SELECTED[EDIT.ship] = { head: EDIT.head, modules: EDIT.modules.slice(), radar: EDIT.radar };
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
Object.assign(window, { openShipEquip, closeShipEquip, seHeadChange, seModuleChange,
  seRadarChange, applyRecommendedGear, saveShipEquip });
