// Ship-vs-rock mining feasibility model (the JS counterpart to the Python gear suite). Run:
//   node --test tests/feasibility.test.js
// feasibility.js is a dual CJS/ESM script (no DOM), require()'d here.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { feasibility, suggestCrack } = require("../web/feasibility.js");
const fz = (...a) => feasibility(...a);

// Real catalog numbers (verified vs Data.p4k): heads carry a `power` + resistance/window
// modifiers; modules carry `modifiers`. Standard rocks need laser_power 2500.
const ARBOR_S1 = { name: "Arbor MH1", power: 1890, modifiers: { resistance: 25, window_size: 40, instability: -35 } };
const HELIX_S1 = { name: "Helix I", power: 3150, modifiers: { resistance: -30, window_size: -40 } };
const HELIX_S2 = { name: "Helix II", power: 4080, modifiers: { resistance: -30, window_size: -40 } };
const SABIR = { name: "Sabir", modifiers: { resistance: -50, window_size: 50, instability: 15 } };

// Real per-material blended resistances (verified vs Data.p4k mineableelements).
const lindinium = { laser_power: 2500, resistance: 0.80 };
const bexalite = { laser_power: 2500, resistance: 0.58 };
const torite = { laser_power: 2500, resistance: 0.25 };
const aluminum = { laser_power: 2500, resistance: -0.35 };
const trivial = { laser_power: 1, resistance: 0.33 };

test("returns null without mechanics or head", () => {
  assert.equal(fz(null, HELIX_S1, []), null);
  assert.equal(fz(lindinium, null, []), null);
});

test("matches in-game results for a Helix I loadout", () => {
  // The ground truth that prompted the model: Lindinium/Bexalite impossible, Torite very
  // difficult, Aluminum easy — all on a Helix I (power 3150, resistance -30%).
  assert.equal(fz(lindinium, HELIX_S1, []).tier, "no");   // effPower 1386 < 2500
  assert.equal(fz(bexalite, HELIX_S1, []).tier, "no");    // effPower 1871 < 2500
  const t = fz(torite, HELIX_S1, []);
  assert.equal(t.tier, "hard");                            // effPower 2599, margin +99 (the edge)
  assert.match(t.factors[0], /^power 3150 → 2599 effective \(need 2500, \+99\)$/);
  assert.equal(fz(aluminum, HELIX_S1, []).tier, "easy");  // effPower 3922, margin +1422
});

test("resistance modules crack what the head alone can't", () => {
  // Helix I alone can't crack Bexalite; adding Sabir (-50% resistance) stacks with the head's
  // -30% to -80%, dropping effective resistance enough to make the crack viable.
  assert.equal(fz(bexalite, HELIX_S1, []).tier, "no");
  const withMod = fz(bexalite, HELIX_S1, [SABIR]);
  assert.notEqual(withMod.tier, "no");                     // effRes 0.58*0.2=0.116 -> effPower 2785
  assert.ok(withMod.factors.some(x => /resistance 0\.58 → 0\.116 \(-80%\)/.test(x)));
});

test("a weak, resistance-worsening laser can't crack a hard rock", () => {
  // Arbor S1 (power 1890, resistance +25%) on Lindinium: effRes 1.0, effPower ~0 -> Impossible.
  const f = fz(lindinium, ARBOR_S1, []);
  assert.equal(f.tier, "no");
  assert.equal(f.label, "Impossible");
});

test("trivial rocks (required power 1) grade Easy", () => {
  assert.equal(fz(trivial, HELIX_S1, []).tier, "easy");
});

test("factors list always leads with the effective-power comparison", () => {
  const f = fz(aluminum, HELIX_S2, []);
  assert.match(f.factors[0], /^power 4080 → \d+ effective \(need 2500, \+\d+\)$/);
});

// ---- suggestCrack: "can't crack → try this gear" -------------------------- //

const CATALOG = {
  heads: [
    { class: "helix_s1", name: "Helix I", size: 1, power: 3150, module_slots: 2, modifiers: { resistance: -30 } },
    { class: "klein_s1", name: "Klein-S1", size: 1, power: 2520, module_slots: 0, modifiers: { resistance: -45 } },
    { class: "helix_s2", name: "Helix II", size: 2, power: 4080, module_slots: 3, modifiers: { resistance: -30 } },
  ],
  modules: [
    { class: "rime", name: "Rime", modifiers: { resistance: -24.8 } },
    { class: "lifeline", name: "Lifeline", modifiers: { resistance: -15.5 } },
    { class: "surge", name: "Surge", modifiers: { resistance: -15.5 } },
    { class: "focus3", name: "Focus III", modifiers: { window_size: 40 } },  // no resistance: ignored
  ],
};

test("suggestCrack proposes an in-size laser+module combo", () => {
  // Bexalite (0.58) on an S1 ship: Helix I + Rime + Lifeline drops effRes enough to reach Hard.
  // The exact module set also pins that Focus III (window-only, margin 0) is never picked.
  const s = suggestCrack(bexalite, CATALOG.heads, CATALOG.modules, [1]);
  assert.ok(s.combo);
  assert.equal(s.combo.head.name, "Helix I");
  assert.deepEqual(s.combo.modules.map(m => m.name), ["Rime", "Lifeline"]);
  assert.equal(s.combo.result.tier, "hard");
});

test("suggestCrack flags when a rock needs a bigger mining ship", () => {
  // Lindinium (0.80) can't be cracked by any S1 head even maxed -> needs an S2 hardpoint.
  const s = suggestCrack(lindinium, CATALOG.heads, CATALOG.modules, [1]);
  assert.equal(s.combo, undefined);
  assert.equal(s.needSize, 2);
});

test("a power booster is a valid crack lever, not just resistance", () => {
  // Rock 0.45 on Helix I: head-only effPower 2158 < 2500 (can't crack). A single Rieger +25%
  // power lifts grossPower to 3938 → effPower 2697 → crackable, with no resistance help at all.
  const rock = { laser_power: 2500, resistance: 0.45 };
  const cat = {
    heads: [{ class: "helix_s1", name: "Helix I", size: 1, power: 3150, module_slots: 2, modifiers: { resistance: -30 } }],
    modules: [
      { class: "rieger3", name: "Rieger-C3", modifiers: { power: 25, window_size: -1 } },
      { class: "focus3", name: "Focus III", modifiers: { window_size: 40 } },
    ],
  };
  assert.equal(fz(rock, cat.heads[0], []).tier, "no");
  const s = suggestCrack(rock, cat.heads, cat.modules, [1]);
  assert.ok(s.combo);
  assert.deepEqual(s.combo.modules.map(m => m.name), ["Rieger-C3"]);
  assert.notEqual(s.combo.result.tier, "no");
});
