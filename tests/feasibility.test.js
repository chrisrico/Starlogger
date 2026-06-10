// Ship-vs-rock mining feasibility model (the JS counterpart to the Python gear suite). Run:
//   node --test tests/feasibility.test.js
// feasibility.js is a dual CJS/ESM script (no DOM), require()'d here.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { feasibility } = require("../web/feasibility.js");
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
  // Arbor S1 (power 1890, resistance +25%) on Lindinium: effRes 1.0, effPower ~0 -> Can't crack.
  const f = fz(lindinium, ARBOR_S1, []);
  assert.equal(f.tier, "no");
  assert.equal(f.label, "Can't crack");
});

test("trivial rocks (required power 1) grade Easy", () => {
  assert.equal(fz(trivial, HELIX_S1, []).tier, "easy");
});

test("factors list always leads with the effective-power comparison", () => {
  const f = fz(aluminum, HELIX_S2, []);
  assert.match(f.factors[0], /^power 4080 → \d+ effective \(need 2500, \+\d+\)$/);
});
