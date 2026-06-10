"use strict";
// Pure ship-vs-rock mining feasibility model. A classic (non-module) script, mirroring
// cargogrid.js: loaded via <script src> so it exposes `window.feasibility`, and also
// module.exports'd so tests/feasibility.test.js can require() it in Node. No DOM, no deps.
//
// The model (validated against in-game results — Lindinium/Bexalite impossible, Torite
// "very difficult", Aluminum easy, on a Helix I): a rock resists the laser, so the laser's
// EFFECTIVE breaking power is its raw power scaled down by the rock's effective resistance:
//
//   effRes   = rock.resistance × (1 + Σ resistance-modifiers%)   (head + modules, additive)
//   effPower = head.power × (1 − effRes)                         (resistance saps the beam)
//   margin   = effPower − rock.required-power                    (required ≈ 2500; 1 = trivial)
//
// margin < 0 means the beam can't overcome the rock at all → "Can't crack". Above that, how
// far the margin clears the requirement grades the crack from Hard → Workable → Easy. This is
// why a negative-resistance rock (quartz −0.7) is trivially easy and Lindinium (0.8) is a wall.
// `mech` is a rock's `mechanics` (laser_power, resistance, instability, window_thinness…);
// `head`/`modules` are gear records (each a `power` and/or `modifiers` map). Returns
//   { tier: 'easy'|'ok'|'hard'|'no', label, factors:[string…] }
// or null when there's no mechanics or no head to judge.
(function (global) {
  // Fractions of the rock's required power that the margin must clear for each tier.
  const HARD_AT = 0.30, EASY_AT = 0.55;

  function feasibility(mech, head, modules) {
    if (!mech || !head) return null;
    const round = (x) => Math.round(x);
    const gear = [head, ...(modules || [])];
    const sumPct = (key) => gear.reduce((a, g) => a + ((g.modifiers || {})[key] || 0), 0);
    const resPct = sumPct("resistance"), winPct = sumPct("window_size"), instPct = sumPct("instability");

    const power = head.power || 0;
    const required = mech.laser_power || 2500;       // rock's full-charge power; default the std rock
    const baseRes = mech.resistance != null ? mech.resistance : 0;
    const effRes = +(baseRes * (1 + resPct / 100)).toFixed(3);
    const effPower = power * (1 - effRes);
    const margin = effPower - required;

    const factors = [
      `power ${round(power)} → ${round(effPower)} effective (need ${round(required)}, ${margin >= 0 ? "+" : ""}${round(margin)})`,
    ];
    if (mech.resistance != null) {
      factors.push(`resistance ${mech.resistance}${resPct ? ` → ${effRes} (${resPct > 0 ? "+" : ""}${resPct}%)` : ""}`);
    }
    if (winPct) factors.push(`window ${winPct > 0 ? "+" : ""}${winPct}%`);
    if (instPct) factors.push(`instability ${instPct > 0 ? "+" : ""}${instPct}%`);

    let tier, label;
    if (margin < 0) { tier = "no"; label = "Can't crack"; }
    else if (margin < HARD_AT * required) { tier = "hard"; label = "Hard"; }
    else if (margin < EASY_AT * required) { tier = "ok"; label = "Workable"; }
    else { tier = "easy"; label = "Easy"; }
    return { tier, label, factors };
  }

  global.feasibility = feasibility;
  // Node (test runner) only; the `module` guard is false in a browser <script>.
  if (typeof module !== "undefined" && module.exports) module.exports = { feasibility };
})(typeof window !== "undefined" ? window : globalThis);
