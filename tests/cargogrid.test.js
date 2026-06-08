// Cargo-grid packer invariants (the JS counterpart to the Python suite). Run with:
//   npm test            (node --test "tests/**/*.test.js")
// cargogrid.js emits HTML strings and touches no DOM, so it require()s straight into Node.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  orientations, packBoxes, synthBoxes, accessFor, packGroups,
} = require("../web/cargogrid.js");

// -- fixtures: a grid is [{ grids: [cell] }]; a cell is a 3-D region of the hold. --
const cell = (x, z, w, l, h, name) =>
  ({ x, y: 0, z, width: w, length: l, height: h, name });
const bay = (...cells) => ({ grids: cells });
const box = (scu, dims) => ({ dims, scu });

// Rebuild the per-bay occupancy from placed boxes (z is the vertical/stacking axis).
function occupied(placed) {
  const s = new Set();
  for (const b of placed)
    for (let z = b.z; z < b.z + b.h; z++)
      for (let y = b.y; y < b.y + b.l; y++)
        for (let x = b.x; x < b.x + b.w; x++) s.add(`${b.bi},${x},${y},${z}`);
  return s;
}
// Every box above the bay floor must rest on a fully-occupied footprint below it.
function noFloatHolds(placed) {
  const s = occupied(placed);
  for (const b of placed) {
    if (b.z === 0) continue;                       // on the floor
    for (let y = b.y; y < b.y + b.l; y++)
      for (let x = b.x; x < b.x + b.w; x++)
        if (!s.has(`${b.bi},${x},${y},${b.z - 1}`)) return false;
  }
  return true;
}

// ---- orientations: yaw only, height never becomes the long axis ---- //
test("orientations swap the footprint but never stand a box on end", () => {
  assert.deepEqual(orientations([2, 4, 2]), [[2, 4, 2], [4, 2, 2]]);
  assert.deepEqual(orientations([1, 2, 1]), [[1, 2, 1], [2, 1, 1]]);
  assert.deepEqual(orientations([2, 2, 2]), [[2, 2, 2]]);   // cube: a single orientation
  for (const dims of [[2, 4, 2], [2, 8, 2], [1, 2, 1]])
    for (const o of orientations(dims))
      assert.equal(o[2], dims[2], "height must stay vertical");
});

// ---- no-float support: stacked boxes rest on a filled footprint ---- //
test("a supported stack packs densely with no float", () => {
  const grid = [bay(cell(0, 0, 2, 2, 2))];          // 2x2 footprint, 2 high -> capacity 8
  const r = packBoxes(grid, Array.from({ length: 8 }, () => box(1, [1, 1, 1])));
  assert.equal(r.placed.length, 8);
  assert.equal(r.overflow.length, 0);
  assert.ok(r.placed.some(b => b.z > 0), "expected the pack to stack");
  assert.ok(noFloatHolds(r.placed), "every stacked box must rest on a full footprint");
});

test("a box that would overhang is rejected, never floated", () => {
  // 2-wide x 1 x 2-high cell. A 1-SCU box takes floor column 0; the 2-wide box then
  // can't fit the floor (column 1 alone is too narrow) and must NOT float at z=1 over
  // the empty column 1 — so it overflows instead of half-resting on the 1-SCU box.
  const grid = [bay(cell(0, 0, 2, 1, 2))];
  const r = packBoxes(grid, [box(1, [1, 1, 1]), box(2, [2, 1, 1])]);
  assert.equal(r.placed.length, 1, "only the floor box fits");
  assert.equal(r.overflow.length, 1, "the unsupported box overflows, not floats");
  assert.ok(noFloatHolds(r.placed));
});

// ---- a full hold overflows the surplus, never overfills ---- //
test("packBoxes overflows what doesn't fit", () => {
  const grid = [bay(cell(0, 0, 1, 1, 1))];          // capacity 1
  const r = packBoxes(grid, [box(1, [1, 1, 1]), box(1, [1, 1, 1])]);
  assert.equal(r.capacity, 1);
  assert.equal(r.placed.length, 1);
  assert.equal(r.overflow.length, 1);
});

// ---- container cap: no box exceeds the tier cap; 1 SCU always fills the remainder ---- //
test("synthBoxes honours the cap and always closes the remainder", () => {
  const a = synthBoxes(35, 16);
  assert.equal(a.reduce((s, b) => s + b.scu, 0), 35);
  assert.ok(a.every(b => b.scu <= 16), "no container may exceed the cap");
  assert.ok(a.some(b => b.scu === 16), "should use the largest allowed tier");
  const ones = synthBoxes(5, 1);                     // cap 1 -> all 1-SCU boxes
  assert.equal(ones.length, 5);
  assert.ok(ones.every(b => b.scu === 1));
});

// ---- hold classification: the per-ship secondary hint resolves and discriminates ---- //
test("accessFor resolves access + the secondary-hold hint", () => {
  const iron = accessFor("Ironclad");
  assert.equal(iron.axis, "depth");
  assert.ok(iron.hint && iron.hint.secondary, "Ironclad carries a secondary-hold hint");
  assert.ok(iron.hint.secondary.test("Secure Front Locker"));
  assert.ok(!iron.hint.secondary.test("Secure Rear Room"));

  assert.ok(!accessFor("Hermes").hint, "Hermes has no secondary hint");
  assert.deepEqual(accessFor("No Such Ship"), { open: true });   // unlisted -> open
  assert.equal(accessFor("C2 Hercules").both, true);             // prefix-tolerant match
});

// ---- DUAL-END: a hold with a hatch at BOTH ends loads from each end inward ---- //
test("packGroups loads a both-ends hold from each end inward", () => {
  // one long fore/aft hold (depth 10, capacity 20); access = C2 (depth/rear/both).
  const grid = [bay(cell(0, 0, 2, 10, 1, "Main"))];
  const A = { scu: 4, hue: 0, dest: "A", boxes: [box(2, [2, 1, 1]), box(2, [2, 1, 1])] };
  const B = { scu: 4, hue: 1, dest: "B", boxes: [box(2, [2, 1, 1]), box(2, [2, 1, 1])] };
  const r = packGroups(grid, [A, B], accessFor("C2 Hercules Starlifter"));
  assert.equal(r.strategy, "dualend");
  assert.equal(r.overflow.length, 0);
  const ys = gid => r.placed.filter(b => b.gid === gid).map(b => b.y);   // y = depth position
  assert.ok(Math.max(...ys(0)) < Math.min(...ys(1)), "the two stops sit at opposite ends");
  assert.ok(Math.min(...ys(1)) >= 6, "the 2nd stop loads from the FAR hatch, not beside the 1st");
  assert.ok(noFloatHolds(r.placed));
});

// contrast: a single-hatch ship bands both stops consecutively from the one hatch.
test("packGroups without both bands both stops from the one hatch", () => {
  const grid = [bay(cell(0, 0, 2, 10, 1, "Main"))];
  const A = { scu: 4, hue: 0, dest: "A", boxes: [box(2, [2, 1, 1]), box(2, [2, 1, 1])] };
  const B = { scu: 4, hue: 1, dest: "B", boxes: [box(2, [2, 1, 1]), box(2, [2, 1, 1])] };
  const r = packGroups(grid, [A, B], accessFor("Freelancer"));         // depth/rear, single hatch
  assert.equal(r.strategy, "linear");
  const ys = gid => r.placed.filter(b => b.gid === gid).map(b => b.y);
  assert.ok(Math.max(...ys(0)) < Math.min(...ys(1)), "2nd stop sits just behind the 1st");
  assert.ok(Math.min(...ys(1)) <= 3, "both stops stay near the one hatch, not the far end");
});

// ---- secondary holds take only spillover: cargo that fits the primary stays there ---- //
test("packGroups fills the primary hold before the secondary", () => {
  const grid = [
    bay(cell(0, 0, 2, 4, 2, "Main")),               // bi 0: primary, capacity 16
    bay(cell(0, 0, 2, 2, 2, "Secure Front Locker")),// bi 1: secondary (matches the hint)
  ];
  const group = { scu: 16, hue: 0, dest: "A", boxes: [box(8, [2, 2, 2]), box(8, [2, 2, 2])] };
  const r = packGroups(grid, [group], accessFor("Ironclad"));
  assert.equal(r.placed.length, 2);
  assert.ok(r.spill, "the secondary hold is recognised as spillover space");
  assert.ok(r.placed.every(b => b.bi === 0), "cargo that fits the primary never spills");
});
