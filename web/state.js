"use strict";
// Shared mutable dashboard state — the small hot core that more than one module reads or
// writes. Held as properties on a single object `S` (not module-level `let`s) because an
// ES-module binding is read-only across files: importers can read it but never reassign
// it. Object properties sidestep that — every module mutates the SAME `S`, so writes from
// the editor, the route planner, the archive and the live stream all stay in sync.

export const S = {
  TAB: "contracts",       // active tab (Contracts is the default)
  LAST: null,             // latest live snapshot pushed over SSE

  // Manual delivery order: a persisted list of destination stations the user dragged into
  // their preferred visit sequence. When set it overrides the planner's order everywhere
  // (route cards, trip plan, and the load order via deliveryIndex). Unknown destinations
  // (new contracts) fall through to the server order until next reordered.
  ROUTE_ORDER: (() => {
    try { return JSON.parse(localStorage.getItem("routeOrder") || "null"); } catch (e) { return null; }
  })(),

  // ---- session replay ---- //
  // When a session is replayed, the WHOLE dashboard renders a reconstructed past snapshot
  // instead of live data: curData() returns REPLAY_SNAPSHOT and the live stream keeps LAST
  // fresh underneath without repainting. REPLAY_POINTS is the scrub timeline (index/ts/
  // label); REPLAY_I the current checkpoint. Archive editing is fully interactive but
  // EPHEMERAL: every edit goes to an in-memory overlay (REPLAY_EDITS) via /api/replay/edit
  // — which recomputes the snapshot exactly like live but writes nothing to disk. null
  // until the first edit (disk state shown). REPLAY_SAVED_ORDER stashes the live route
  // order while replaying (archive reordering is ephemeral — restored on exit).
  REPLAY_MODE: false, REPLAY_KEY: null, REPLAY_POINTS: [], REPLAY_I: 0, REPLAY_SNAPSHOT: null,
  REPLAY_EDITS: null, REPLAY_SAVED_ORDER: null,
};

// Session keys whose source log is gone (replay unavailable). A Set, so it's mutated in
// place — no reassignment — and can be imported directly.
export const REPLAY_UNAVAILABLE = new Set();

// The snapshot every tab renders from: the replay reconstruction while replaying, else the
// live snapshot. The single source of truth for "what is on screen right now".
export const curData = () => (S.REPLAY_MODE ? S.REPLAY_SNAPSHOT : S.LAST);
