"use strict";
// Fetch helpers shared across modules. Pure transport — no app state.

// Per-install API token injected into the served page (see server._serve_shell). Same-origin
// JS can read it; a cross-origin attacker can't. Attached to every mutating request so the
// server's guard accepts it (the CSRF + non-loopback-bind auth gate). Read once at load.
const API_TOKEN = (document.querySelector('meta[name="api-token"]') || {}).content || "";

// Headers for a JSON write, carrying the auth token when present. Shared by the helpers
// here and any module that issues its own mutating fetch (e.g. mining.js).
export const writeHeaders = () => {
  const h = { "Content-Type": "application/json" };
  if (API_TOKEN) h["X-Starlogger-Token"] = API_TOKEN;
  return h;
};

// POST expecting the {ok, …} envelope used by every live /api/* write; throws on !ok.
export async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: writeHeaders(), body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(j.error || r.status);
  return j;
}

// POST returning a raw JSON body (no {ok} envelope) — the replay snapshot/edit responses.
export async function postRaw(url, body) {
  const r = await fetch(url, { method: "POST", headers: writeHeaders(),
                              body: JSON.stringify(body), cache: "no-store" });
  return r.json();
}

// GET + parse JSON for the live dashboard's no-cache reads (state/sessions/replay/ships).
// Mining catalog lookups use their own plain fetch (cacheable, no `ok` envelope).
export async function getJSON(url) {
  return (await fetch(url, { cache: "no-store" })).json();
}
