"use strict";
// Live data + page-update layer. Owns the SSE connection to the tracker (the dashboard's
// only data feed — pushed, never polled), turns each pushed snapshot into a full re-render
// via applySnapshot, and runs the "new build available" banner + the served-asset-hash
// reload. Calls back into the core render dispatch (renderAll) and the archive/jukebox
// modules; refresh() is the one-shot pull the editor and archive reach for after a write.
import { $, esc, toast } from "./dom.js";
import { postJSON, getJSON } from "./net.js";
import { S, curData } from "./state.js";
import { renderAll } from "./app.js";
import { loadSessions } from "./archive.js";
import { jukeApplyMusicState } from "./jukebox.js";

let _lastRenderSig = null;  // serialized snapshot last rendered (skip identical re-renders)
let ASSET_VER = null;       // served-asset hash from the SSE `meta` frame; reload if it changes

// ---- live stream ---- //
// The tracker pushes the full snapshot over SSE whenever the log changes (real-time, no
// polling). The open connection also tells the server a dashboard is attached, so the
// tracker stays alive while this tab is open and shuts itself down only once the last tab
// closes. Shutdown is the server's job now, so this tab never self-closes; on a dropped
// connection we just show a passive banner and let EventSource auto-reconnect (which also
// reattaches silently when the tracker is restarted).

function showDisconnect(msg) {
  let el = $("dcbanner");
  if (!el) {
    el = document.createElement("div");
    el.id = "dcbanner";
    el.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:9999;" +
      "background:#5a1d1d;color:#f4dada;font:600 13px/1.4 system-ui,sans-serif;" +
      "text-align:center;padding:6px 12px";
    document.body.appendChild(el);
  }
  el.textContent = msg || "Tracker disconnected — reconnecting…";
  el.style.display = "block";
}
function hideDisconnect() {
  const el = $("dcbanner");
  if (el) el.style.display = "none";
}

// ---- update-available banner (tracker owns updating; this is the prompt) ----
// The snapshot carries `update` = {available,current,latest,compare_url,mode}. In prompt
// mode a new build shows this bar; Update now POSTs to apply (the tracker resets + restarts
// and the asset-hash reload swaps the page), View changes opens the GitHub compare, Dismiss
// hides it (the server won't re-offer that commit). auto/off never show a banner.
let _updBusy = false;
function renderUpdateBar(u) {
  const el = $("updatebar");
  if (!el) return;
  if (_updBusy) return;   // an apply is in flight: stay dismissed until the restart reloads us
  if (!u || !u.available) { el.classList.add("hide"); el.innerHTML = ""; _updBusy = false; return; }
  const view = u.compare_url
    ? `<button class="sp-btn" onclick="window.open('${esc(u.compare_url)}','_blank','noopener')">View changes</button>`
    : "";
  el.innerHTML =
    `<span class="ub-msg">⟳ New build available <code>${esc(u.current || "?")}</code> → ` +
    `<code>${esc(u.latest || "?")}</code></span>` +
    `<span class="ub-actions"><button class="sp-btn primary" onclick="applyUpdate()">Update now</button>` +
    `${view}<button class="sp-btn" onclick="dismissUpdate()">Dismiss</button></span>`;
  el.classList.remove("hide");
}
async function applyUpdate() {
  if (_updBusy) return;
  _updBusy = true;
  const el = $("updatebar");
  if (el) el.classList.add("hide");   // dismiss immediately; the restart will reload this tab
  try {
    await postJSON("/api/update/apply");
    // The tracker is restarting; its asset-hash bump reloads this tab into the new build.
  } catch (e) {
    _updBusy = false;
    if (el) el.classList.remove("hide");   // apply failed — bring the banner back so it can retry
    alert("Update failed: " + e);
  }
}
async function dismissUpdate() {
  try { await postJSON("/api/update/dismiss"); } catch (_) { /* best-effort */ }
  $("updatebar").classList.add("hide");
}

// Always announce a completed update. app_version is the running build's git hash; when it
// changes from the one this browser last saw (a restart re-execed into new code), an update
// just landed — covers every path (banner, Check now, auto, settings-change). localStorage
// dedupes across the reload and across tabs; the first load just seeds it (no toast).
function notifyIfUpdated(v) {
  if (!v) return;
  const k = "starlogger_build", prev = localStorage.getItem(k);
  if (prev && prev !== v) toast(`Update complete — now on build ${v} ✓`, "ok");
  localStorage.setItem(k, v);
}

function applySnapshot(d) {
  S.LAST = d;
  renderUpdateBar(d.update);   // update banner is global — show it even in replay mode
  if (d.music) jukeApplyMusicState(d.music);   // push extraction progress to the jukebox (no polling)
  notifyIfUpdated(d.app_version);   // toast once when the running build changed under us
  if (S.REPLAY_MODE) return;   // keep S.LAST fresh underneath; the replay view owns the screen
  // Skip the whole render pass when the snapshot is byte-identical to the last one
  // rendered: setHTML already no-ops the DOM, this also skips building the HTML strings +
  // cargo packing. User interactions call renderAll() directly (unguarded), so an open
  // editor/drag still repaints immediately.
  const sig = JSON.stringify(d);
  if (sig !== _lastRenderSig) {
    _lastRenderSig = sig;
    renderAll(curData());                  // render every tab from the live snapshot
  }
  if (S.TAB === "archive") loadSessions();  // keep archive fresh while viewing
  const last = d.last_event_ts ? ("log " + d.last_event_ts) : "";
  // App build: the short git hash of the running code (logged-in state already
  // lives in the header status pill, so the footer shows the version instead).
  const build = "build " + esc(d.app_version || "?");
  // RSI's patch-notes page is what the launcher links to pre-update (then hides) —
  // make the parsed game version a link back to it. Index URL always lists the
  // current LIVE build first, so it needs no per-patch upkeep.
  const ver = d.game_version
    ? ` · game <a class="pn-link" href="https://robertsspaceindustries.com/en/patch-notes" target="_blank" rel="noopener">${esc(d.game_version)} ↗</a>`
    : "";
  $("foot").innerHTML = `synced ${esc(new Date().toLocaleTimeString())} · ${build}${ver} · ${esc(last)} · cargo db @ ${esc(d.ship_cargo_version || "?")}`;
}

// One-shot pull used by action handlers to reflect a change immediately. (The mutating
// POSTs also bump the server version, so other open tabs update via the stream; this just
// gives the acting tab an instant repaint without waiting for the round-trip push.)
// Exported: archive.js's replay exit / trade-lost flows pull a fresh snapshot through this.
export async function refresh() {
  try {
    applySnapshot(await getJSON("/api/state"));
  } catch (e) {
    $("foot").textContent = `waiting for tracker… (${e})`;
  }
}

let _es = null;            // current EventSource, so we can tell a live one from a dead one
let _reconnectTimer = null;

function connectStream() {
  if (_es) { try { _es.close(); } catch (_) {} }   // drop any stale handle before reopening
  const es = _es = new EventSource("/api/stream");
  es.onopen = () => hideDisconnect();
  // Named `meta` event (NOT onmessage) carries the served-asset hash. First frame records
  // the baseline; any later frame with a different hash -> reload to run the new code. The
  // server re-sends it both on reconnect (a relaunch replaced the build on this port) and
  // mid-stream (the frontend files changed under a still-running tracker), so a stale tab
  // refreshes itself either way. (The active tab survives the reload via the URL path — the
  // server's SPA fallback re-serves index.html for /<tab>, which boots back to that tab.) An
  // unchanged hash is silent -- a server-only relaunch never reloads.
  es.addEventListener("meta", (e) => {
    let m; try { m = JSON.parse(e.data); } catch (_) { return; }
    if (!m || !m.assets) return;
    if (ASSET_VER === null) { ASSET_VER = m.assets; return; }
    if (m.assets !== ASSET_VER) location.reload();
  });
  es.onmessage = (e) => {
    hideDisconnect();
    try { applySnapshot(JSON.parse(e.data)); } catch (_) { /* ignore a malformed frame */ }
  };
  es.onerror = () => {
    showDisconnect();
    // EventSource auto-reconnects on a transient drop (readyState stays CONNECTING). But
    // once the browser marks it CLOSED -- which mobile Firefox/Chrome do when the tab is
    // backgrounded and the socket is reaped -- it never retries on its own, so reopen it.
    if (es.readyState === EventSource.CLOSED) {
      clearTimeout(_reconnectTimer);
      _reconnectTimer = setTimeout(ensureStream, 2000);
    }
  };
}

// Reopen the stream if it isn't currently OPEN or CONNECTING. Cheap no-op when it's healthy.
function ensureStream() {
  if (_es && _es.readyState !== EventSource.CLOSED) return;
  connectStream();
}

// A backgrounded tab can have its SSE socket killed without a usable error event, leaving a
// stale/closed stream when you return. Re-establish it (and pull a fresh snapshot so the view
// isn't stale) the moment the tab is shown again or connectivity returns.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { ensureStream(); refresh(); }
});
window.addEventListener("online", ensureStream);
window.addEventListener("pageshow", ensureStream);

export { connectStream };

// ---- window bridge (update-banner inline handlers) ---- //
// (refresh is exported above for the editor/archive; the bootstrap in app.js calls
// connectStream once everything is wired.)
Object.assign(window, { applyUpdate, dismissUpdate });
