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
//   grossPwr = head.power × (1 + Σ power-modifiers%)             (modules boost/sap the beam)
//   effPower = grossPwr × (1 − effRes)                           (resistance saps what's left)
//   margin   = effPower − rock.required-power                    (required ≈ 2500; 1 = trivial)
//
// margin < 0 means the beam can't overcome the rock at all → "Impossible". Above that, how
// far the margin clears the requirement grades the crack from Hard → Workable → Easy. This is
// why a negative-resistance rock (quartz −0.7) is trivially easy and Lindinium (0.8) is a wall.
// The `power` modifier is the module's beam-power delta (Rieger +25%, Rime −15%): it's a real
// crack lever, so a power booster can lift an "Impossible" rock to crackable, and a resistance
// module that costs power (Rime) is judged on its net effect, not its headline resistance cut.
//
// The OPTIMAL WINDOW is the second difficulty axis: the rock's window_thinness (Iron −0.9 …
// Savrilium 2.28; thinner = higher) sets how narrow the green charge zone is, and gear
// window_size modifiers scale it (Hofstede +60%, Helix −40% — the Helix's famous tradeoff).
// Effective width = (1 − thinness/THIN_FULL) × (1 + window-modifiers%). A tight window can't
// stop a beam that overpowers the rock — it makes the hold finicky — so it never creates
// "Impossible"; below TIGHT_BELOW the verdict grades one step harder (Easy→Workable→Hard).
// This re-validates the Helix-I ground truth: Torite's "very difficult" is the +99 margin AND
// a ~0.10 window; Aluminum (thinness −0.4) keeps a 0.70 window even on the Helix and stays
// Easy. Rocks with no thinness data are neutral on this axis.
// `mech` is a rock's `mechanics` (laser_power, resistance, instability, window_thinness…);
// `head`/`modules` are gear records (each a `power` and/or `modifiers` map). Returns
//   { tier: 'easy'|'ok'|'hard'|'no', label, margin, factors:[string…] }
// or null when there's no mechanics or no head to judge.
(function (global) {
  // Fractions of the rock's required power that the margin must clear for each tier.
  const HARD_AT = 0.30, EASY_AT = 0.55;
  // Window leg: thinness at which the green zone has fully closed (catalog max is 2.28),
  // and the effective width below which the crack grades one step harder.
  const THIN_FULL = 2.5, TIGHT_BELOW = 0.45;

  function feasibility(mech, head, modules) {
    if (!mech || !head) return null;
    const round = (x) => Math.round(x);
    const gear = [head, ...(modules || [])];
    const sumPct = (key) => gear.reduce((a, g) => a + ((g.modifiers || {})[key] || 0), 0);
    const resPct = sumPct("resistance"), winPct = sumPct("window_size"), instPct = sumPct("instability");
    const powPct = sumPct("power");

    const power = head.power || 0;
    const required = mech.laser_power || 2500;       // rock's full-charge power; default the std rock
    const baseRes = mech.resistance != null ? mech.resistance : 0;
    const effRes = +(baseRes * (1 + resPct / 100)).toFixed(3);
    const grossPower = power * (1 + powPct / 100);   // modules boost (Rieger) or sap (Rime) the beam
    const effPower = grossPower * (1 - effRes);
    const margin = effPower - required;

    const factors = [
      `power ${round(power)}${powPct ? ` ${powPct > 0 ? "+" : ""}${powPct}%` : ""} → ${round(effPower)} effective (need ${round(required)}, ${margin >= 0 ? "+" : ""}${round(margin)})`,
    ];
    if (mech.resistance != null) {
      factors.push(`resistance ${mech.resistance}${resPct ? ` → ${effRes} (${resPct > 0 ? "+" : ""}${resPct}%)` : ""}`);
    }
    // Effective optimal-window width (1 = a normal rock on neutral gear); null = no data.
    const width = mech.window_thinness != null
      ? +(Math.max(0, 1 - mech.window_thinness / THIN_FULL) * (1 + winPct / 100)).toFixed(3)
      : null;
    const tight = width != null && width < TIGHT_BELOW;
    if (width != null) {
      factors.push(`window ${width.toFixed(2)}× (thinness ${mech.window_thinness}`
        + `${winPct ? `, gear ${winPct > 0 ? "+" : ""}${winPct}%` : ""})`
        + (tight ? " → tight: one grade harder" : ""));
    } else if (winPct) {
      factors.push(`window ${winPct > 0 ? "+" : ""}${winPct}%`);
    }
    if (instPct) factors.push(`instability ${instPct > 0 ? "+" : ""}${instPct}%`);

    let tier, label;
    if (margin < 0) { tier = "no"; label = "Impossible"; }
    else if (margin < HARD_AT * required) { tier = "hard"; label = "Hard"; }
    else if (margin < EASY_AT * required) { tier = "ok"; label = "Workable"; }
    else { tier = "easy"; label = "Easy"; }
    // A tight window makes the hold finicky, not the rock unbreakable: one grade harder,
    // never Impossible (that stays power-only).
    if (tight && tier === "easy") { tier = "ok"; label = "Workable"; }
    else if (tight && tier === "ok") { tier = "hard"; label = "Hard"; }
    return { tier, label, margin: +margin.toFixed(1), width, factors };
  }

  const TIER_ORDER = { easy: 0, ok: 1, hard: 2, no: 3 };

  // Given a rock the current gear can't crack, find gear that would. `heads`/`modules` are the
  // full catalog; `allowedSizes` are the ship's mining-hardpoint sizes (it can only fit a head
  // matching one). Two crack levers — cut resistance (Rime/Lifeline) or boost power (Rieger) —
  // so for each head we rank every module by how much it actually moves *this* rock's margin and
  // stack the best, fewest first. (A window/yield-only module like Focus moves margin 0 → never
  // suggested: a window can soften the grade of a crackable rock but can't escape "Impossible",
  // which is power-only. A resistance cut that costs net power is judged on its real margin
  // gain.) Returns:
  //   { combo: { head, modules, result } }  a fittable laser+modules that cracks it (minimal), or
  //   { needSize: <n> }                     when only a bigger hardpoint can (suggest that ship), or
  //   null                                  when nothing in the catalog cracks it.
  function suggestCrack(mech, heads, modules, allowedSizes) {
    if (!mech || !heads || !heads.length) return null;
    const allowed = new Set(allowedSizes || []);
    const pool = modules || [];
    // Fewest crack-helping modules that bring `head` to crackable; null if even maxed it can't.
    const minimalCrack = (head) => {
      const slots = head.module_slots || 0;
      const base = feasibility(mech, head, []);
      const baseMargin = base ? base.margin : -Infinity;
      // Rank this head's modules by single-module margin gain; keep only the ones that help.
      const ranked = pool
        .map((m) => ({ m, gain: (feasibility(mech, head, [m]).margin) - baseMargin }))
        .filter((x) => x.gain > 0)
        .sort((a, b) => b.gain - a.gain)
        .map((x) => x.m);
      const cap = Math.min(slots, ranked.length);
      for (let k = 0; k <= cap; k++) {
        const f = feasibility(mech, head, ranked.slice(0, k));
        if (f && f.tier !== "no") return { head, modules: ranked.slice(0, k), result: f, count: k };
      }
      return null;
    };
    const rank = (a, b) =>
      a.count - b.count                                                   // fewest modules
      || TIER_ORDER[a.result.tier] - TIER_ORDER[b.result.tier]           // then the better verdict
      || (b.head.power || 0) - (a.head.power || 0);                       // then the stronger head
    const inSize = heads.filter((h) => allowed.has(h.size)).map(minimalCrack).filter(Boolean);
    if (inSize.length) { inSize.sort(rank); return { combo: inSize[0] }; }
    const sizes = heads.filter((h) => minimalCrack(h)).map((h) => h.size);
    return sizes.length ? { needSize: Math.min(...sizes) } : null;
  }

  global.feasibility = feasibility;
  global.suggestCrack = suggestCrack;
  // Node (test runner) only; the `module` guard is false in a browser <script>.
  if (typeof module !== "undefined" && module.exports) module.exports = { feasibility, suggestCrack };
})(typeof window !== "undefined" ? window : globalThis);
