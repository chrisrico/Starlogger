// Mode auto-detection contract for web/app.js's effectiveMode(). Run:
//   node --test tests/mode.test.js
// app.js is an ES module with import side-effects, so (like dom.test.js for esc) we don't import
// it — we extract the REAL effectiveMode source and evaluate it, so the assertions track shipped
// code, not a copy. Regression guard: a salvage WRECK sighting (detected_salvage) must NOT lock a
// non-salvage ship (e.g. a Drake Ironclad) into Salvage mode — auto mode follows the CURRENT ship.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SRC = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const FN = SRC.match(/function effectiveMode\(d\) \{[\s\S]*?\n\}/);
assert.ok(FN, "could not locate effectiveMode() in web/app.js");

// Build a callable effectiveMode with MODE_OVERRIDE (a module-global in app.js) pinned to `ov`.
function effectiveModeWith(ov) {
  // eslint-disable-next-line no-new-func
  return new Function("d", `const MODE_OVERRIDE = ${JSON.stringify(ov)};\n${FN[0]}\nreturn effectiveMode(d);`);
}
const auto = effectiveModeWith("auto");

test("a sighted wreck does NOT force a cargo ship into salvage mode (the Ironclad bug)", () => {
  assert.equal(auto({ salvage_ship: false, mining_ship: false, detected_salvage: [{}, {}] }), "cargo");
});

test("a salvage vessel auto-detects salvage", () => {
  assert.equal(auto({ salvage_ship: true }), "salvage");
});

test("a mining vessel wins over salvage", () => {
  assert.equal(auto({ mining_ship: true, salvage_ship: true }), "mining");
});

test("a plain cargo ship is cargo", () => {
  assert.equal(auto({ salvage_ship: false, mining_ship: false, detected_salvage: [] }), "cargo");
});

test("a pinned MODE override always wins over detection", () => {
  assert.equal(effectiveModeWith("salvage")({ mining_ship: true }), "salvage");
  assert.equal(effectiveModeWith("cargo")({ salvage_ship: true }), "cargo");
});
