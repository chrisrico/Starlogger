// HTML-escaping contract for web/dom.js's esc(). Run:
//   node --test tests/dom.test.js
// dom.js is a pure ES module under a CommonJS package, so it isn't require()/import()-able
// here without restructuring it. esc() is a self-contained expression, so we evaluate the
// real source directly — the assertion still tracks the shipped code, not a copy.

"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

function loadEsc() {
  const src = fs.readFileSync(path.join(__dirname, "..", "web", "dom.js"), "utf8");
  const m = src.match(/export const esc = ([\s\S]*?);\n/);
  assert.ok(m, "could not locate esc in dom.js");
  return eval("(" + m[1] + ")");   // eslint-disable-line no-eval -- evaluating our own source
}

test("esc encodes every HTML-significant char, including the single quote", () => {
  const esc = loadEsc();
  // The single quote MUST be encoded: inline handler markup interpolates values into
  // single-quoted JS-string arguments, where a bare ' would break out of the string.
  assert.equal(esc(`&<>"'`), "&amp;&lt;&gt;&quot;&#39;");
  assert.equal(esc("O'Brien"), "O&#39;Brien");
  assert.equal(esc("<script>"), "&lt;script&gt;");
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
});
