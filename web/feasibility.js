"use strict";
// Pure ship-vs-rock mining feasibility model. A classic (non-module) script, mirroring
// cargogrid.js: loaded via <script src> so it exposes `window.feasibility`, and also
// module.exports'd so tests/feasibility.test.js can require() it in Node. No DOM, no deps.
//
// SC mining is a charge-window minigame, not a power>threshold gate, so this grades rather
// than returns a boolean: the laser's extraction power must REACH the rock's required power
// to crack it at all; then the EFFECTIVE resistance (the rock's resistance after the head +
// modules' percentage modifiers, which stack additively in-game) and the combined optimal-
// window adjustment grade how fiddly the crack is. `mech` is a rock's `mechanics`
// (laser_power, resistance, window_size, instability…); `head`/`modules` are gear records
// (each with a `power` and/or `modifiers` map). Returns
//   { tier: 'easy'|'ok'|'hard'|'no', label, factors:[string…] }
// or null when there's no mechanics or no head to judge.
(function (global) {
  function feasibility(mech, head, modules) {
    if (!mech || !head) return null;
    const round = (x) => Math.round(x);
    const gear = [head, ...(modules || [])];
    const sumPct = (key) => gear.reduce((a, g) => a + ((g.modifiers || {})[key] || 0), 0);
    const resPct = sumPct("resistance"), winPct = sumPct("window_size"), instPct = sumPct("instability");
    const required = mech.laser_power || 0;          // 1 (trivial) or 2500 (hard) in the data
    const margin = (head.power || 0) - required;
    const factors = [`power ${round(head.power || 0)} vs ${round(required)} (${margin >= 0 ? "+" : ""}${round(margin)})`];

    let effRes = null;
    if (mech.resistance != null) {
      effRes = +(mech.resistance * (1 + resPct / 100)).toFixed(3);
      factors.push(`resistance ${mech.resistance}${resPct ? ` → ${effRes} (${resPct > 0 ? "+" : ""}${resPct}%)` : ""}`);
    }
    if (winPct) factors.push(`window ${winPct > 0 ? "+" : ""}${winPct}%`);
    if (instPct) factors.push(`instability ${instPct > 0 ? "+" : ""}${instPct}%`);

    let tier, label;
    if (margin < 0) { tier = "no"; label = "Can't crack"; }
    else if (effRes == null) { tier = margin >= 400 ? "easy" : "ok"; label = margin >= 400 ? "Easy" : "Workable"; }
    else if (effRes >= 0.52 || winPct <= -50) { tier = "hard"; label = "Hard"; }     // hard rock / narrowed window
    else if (effRes <= 0.38 && margin >= 400) { tier = "easy"; label = "Easy"; }
    else { tier = "ok"; label = "Workable"; }
    return { tier, label, factors };
  }

  global.feasibility = feasibility;
  // Node (test runner) only; the `module` guard is false in a browser <script>.
  if (typeof module !== "undefined" && module.exports) module.exports = { feasibility };
})(typeof window !== "undefined" ? window : globalThis);
