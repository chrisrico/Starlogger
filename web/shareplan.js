"use strict";
// Shareable read-only blueprint plans. A "plan" is the player's selected blueprints + build
// quantities ({blueprintName: qty}). Both Starlogger installs share the same p4k-derived catalog,
// so a shared plan only needs to carry NAMES + quantities — the recipient resolves everything else
// (type/class/materials/reward contracts) against their own /api/blueprints. The whole plan
// therefore travels INSIDE a copy-paste link (?code=…) and is decoded entirely on the recipient's
// machine: no server endpoint, no token, nothing connects back to the sharer's instance. Read-only
// is inherent — it's a snapshot, not a live link.
//
// Wire format `b1.<base64url>`: URL-safe base64 of the JSON {name: qty}. Deliberately NO crypto —
// the data is non-sensitive game data and the only stated requirement is "don't expose my
// instance", which a self-contained snapshot satisfies by construction. The codec's one job beyond
// that is to FAIL LOUD on a corrupt/truncated paste (decode throws) rather than render garbage.

// base64url ⇄ standard base64 — a URL query value can't carry +, /, or =.
const _b64url = (s) => s.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const _b64std = (s) => { s = s.replace(/-/g, "+").replace(/_/g, "/"); return s + "=".repeat((4 - (s.length % 4)) % 4); };

// Keep only {string name → positive integer qty} pairs. Shared by encode (drop unselected rows)
// and decode (sanitise whatever a hand-edited code claims).
const _onlyPos = (obj) => {
  const out = {};
  for (const [name, q] of Object.entries(obj || {})) {
    const n = Math.max(0, parseInt(q, 10) || 0);
    if (typeof name === "string" && name && n > 0) out[name] = n;
  }
  return out;
};

// Encode a {name: qty} plan to a "b1." share code. UTF-8 safe (TextEncoder) so an unusual
// blueprint name can't corrupt the blob; zero/negative quantities are dropped.
export function encodePlan(plan) {
  const json = JSON.stringify(_onlyPos(plan));
  let bin = "";
  for (const b of new TextEncoder().encode(json)) bin += String.fromCharCode(b);
  return "b1." + _b64url(btoa(bin));
}

// Decode a share code back to a {name: qty} plan. THROWS on anything that isn't a well-formed,
// non-empty b1 code — the caller shows a clear "bad link" message instead of a blank plan.
export function decodePlan(code) {
  const m = /^b1\.([A-Za-z0-9\-_]+)$/.exec((code || "").trim());
  if (!m) throw new Error("not a blueprint plan code");
  const bin = atob(_b64std(m[1]));
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  const obj = JSON.parse(new TextDecoder().decode(bytes));
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new Error("malformed plan");
  const plan = _onlyPos(obj);
  if (!Object.keys(plan).length) throw new Error("empty plan");
  return plan;
}

// ---- browser-only below (uses `location`) ---- //

// Build the copy-paste link for a code. Deliberately targets the recipient's OWN local instance
// (http://localhost:<port>) rather than location.origin — if the sharer is browsing over the
// tailnet, location.origin would point the link back at THEIR box (the very thing we must not
// expose). Both installs run the same app on the same port, so localhost is the portable target.
export function planLink(code) {
  const port = location.port || "8765";
  return `http://localhost:${port}/?code=${code}`;
}
