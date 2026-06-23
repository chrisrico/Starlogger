"use strict";
// Round-trip + tamper contract for web/shareplan.js's plan codec. Run:
//   node --test tests/shareplan.test.js
// shareplan.js is an ES module under a CommonJS package, so (like dom.test.js) we lift the pure
// codec functions out of the source and eval them — the assertions track the shipped code, not a
// copy. The browser-only half (planLink, uses `location`) is sliced off so nothing here needs DOM.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

function loadCodec() {
  const src = fs.readFileSync(path.join(__dirname, "..", "web", "shareplan.js"), "utf8");
  const marker = "// ---- browser-only";
  const cut = src.indexOf(marker);
  assert.ok(cut > 0, "could not find the browser-only marker in shareplan.js");
  const pure = src.slice(0, cut).replace(/\bexport /g, "");
  // define the codec in an IIFE scope and hand back its functions
  return eval(`(() => { ${pure}; return { encodePlan, decodePlan }; })()`); // eslint-disable-line no-eval
}

test("a plan round-trips through encode→decode unchanged", () => {
  const { encodePlan, decodePlan } = loadCodec();
  const plan = { IDRIS_Powerplant_01_T1: 2, M50_Frame_S2_01_T1: 1 };
  const code = encodePlan(plan);
  assert.match(code, /^b1\.[A-Za-z0-9\-_]+$/);
  assert.deepEqual(decodePlan(code), plan);
});

test("the code is URL-safe even for non-ASCII names (no +, /, = to escape in ?code=)", () => {
  const { encodePlan, decodePlan } = loadCodec();
  const plan = { "ÿÿÿ_blueprintÿ": 255 };          // forces bytes that base64 to +/ in std alphabet
  const code = encodePlan(plan);
  assert.ok(!/[+/=]/.test(code), code);
  assert.deepEqual(decodePlan(code), plan);          // …and still round-trips through UTF-8
});

test("encode drops zero/negative quantities (only the real selection ships)", () => {
  const { encodePlan, decodePlan } = loadCodec();
  assert.deepEqual(decodePlan(encodePlan({ a: 3, b: 0, c: -2 })), { a: 3 });
});

test("decode fails LOUD on a corrupt / truncated / non-plan code", () => {
  const { encodePlan, decodePlan } = loadCodec();
  assert.throws(() => decodePlan("not-a-code"));     // missing b1. prefix
  assert.throws(() => decodePlan("b1.@@@@"));         // illegal base64url chars
  assert.throws(() => decodePlan(""));                // empty
  assert.throws(() => decodePlan("b1."));             // no payload
  assert.throws(() => decodePlan(encodePlan({})));    // valid b1 of an EMPTY plan is still rejected
});

test("decode sanitises a hand-edited code (string qty coerced, junk dropped)", () => {
  const { decodePlan } = loadCodec();
  const enc = (obj) => "b1." + Buffer.from(JSON.stringify(obj)).toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  assert.deepEqual(decodePlan(enc({ x: "4", y: 0, z: 2 })), { x: 4, z: 2 });
});
