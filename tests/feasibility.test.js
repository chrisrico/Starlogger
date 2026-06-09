// Ship-vs-rock mining feasibility model (the JS counterpart to the Python gear suite). Run:
//   npm test            (node --test "tests/**/*.test.js")
// feasibility.js is a pure ESM module (no DOM), dynamically imported here from this CJS test.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { feasibility } = require("../web/feasibility.js");
const fz = (...a) => feasibility(...a);

// Real catalog numbers (verified against Data.p4k): heads carry a `power` + `modifiers`,
// modules carry `modifiers`. Hard rocks read laser_power 2500; resistance runs 0.33–0.6.
const ARBOR_S1 = { name: "Arbor MH1", power: 1890, modifiers: { resistance: 25, window_size: 40, instability: -35 } };
const HELIX_S2 = { name: "Helix II", power: 4080, modifiers: { resistance: -30, window_size: -40 } };
const KLEIN_S1 = { name: "Klein-S1", power: 2520, modifiers: { resistance: -45, window_size: 20, instability: 35 } };
const SABIR = { name: "Sabir", modifiers: { resistance: -50, window_size: 50, instability: 15 } };
const hardRock = { laser_power: 2500, resistance: 0.6, window_size: 1, instability: 1 };
const softRock = { laser_power: 1, resistance: 0.33 };

test("returns null without mechanics or head", async () => {
  assert.equal(await fz(null, ARBOR_S1, []), null);
  assert.equal(await fz(hardRock, null, []), null);
});

test("under-powered laser can't crack a hard rock", async () => {
  // Arbor S1 power 1890 < required 2500 -> negative margin -> 'no'.
  const f = await fz(hardRock, ARBOR_S1, []);
  assert.equal(f.tier, "no");
  assert.equal(f.label, "Can't crack");
  assert.match(f.factors[0], /power 1890 vs 2500 \(-610\)/);
});

test("strong head reduces effective resistance to Easy", async () => {
  // Helix II: power 4080 (margin +1580), resistance 0.6 * (1 - 0.30) = 0.42 ... not <=0.38,
  // so Workable; add Sabir (-50% more) -> 0.6 * (1 - 0.80) = 0.12 -> Easy.
  const workable = await fz(hardRock, HELIX_S2, []);
  assert.equal(workable.tier, "ok");
  const easy = await fz(hardRock, HELIX_S2, [SABIR]);
  assert.equal(easy.tier, "easy");
  assert.equal(easy.label, "Easy");
});

test("modules stack additively on resistance", async () => {
  // Klein S1 -45% + Sabir -50% = -95% -> 0.6 * 0.05 = 0.03; power 2520 >= 2500 (margin +20)
  // but margin < 400 keeps it Workable despite the low resistance.
  const f = await fz(hardRock, KLEIN_S1, [SABIR]);
  assert.equal(f.tier, "ok");
  assert.ok(f.factors.some(x => /resistance 0\.6 → 0\.03 \(-95%\)/.test(x)));
});

test("trivial rock with no resistance is Easy by margin", async () => {
  const f = await fz(softRock, HELIX_S2, []);
  // softRock has resistance present (0.33) so it grades on effRes; HELIX -30% -> 0.231 <=0.38,
  // margin 4079 -> Easy.
  assert.equal(f.tier, "easy");
});

test("factors list always leads with the power comparison", async () => {
  const f = await fz(hardRock, HELIX_S2, []);
  assert.match(f.factors[0], /^power 4080 vs 2500 \(\+1580\)$/);
});
