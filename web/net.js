"use strict";
// Fetch helpers shared across modules. Pure transport — no app state.

// POST expecting the {ok, …} envelope used by every live /api/* write; throws on !ok.
export async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(j.error || r.status);
  return j;
}

// POST returning a raw JSON body (no {ok} envelope) — the replay snapshot/edit responses.
export async function postRaw(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                              body: JSON.stringify(body), cache: "no-store" });
  return r.json();
}

// GET + parse JSON for the live dashboard's no-cache reads (state/sessions/replay/ships).
// Mining catalog lookups use their own plain fetch (cacheable, no `ok` envelope).
export async function getJSON(url) {
  return (await fetch(url, { cache: "no-store" })).json();
}
