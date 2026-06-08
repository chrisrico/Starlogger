"use strict";
// Settings overlay (sidebar gear → the dashboard-managed settings.json). Self-contained:
// renders the live schema from /api/settings, persists via /api/settings, and hosts the
// "Check for updates" / "Shut down tracker" actions and the Advanced collapse. Imported for
// its side effects — it wires its own nav button + Escape handler at load.
import { $, esc } from "./dom.js";
import { postJSON, postRaw, getJSON } from "./net.js";

// ---- settings overlay (sidebar gear -> dashboard-managed settings.json) ----
// Renders straight from /api/settings' schema: one row per knob, grouped, with bool ->
// checkbox / int|number -> number input / enum -> <select> / string -> text input. A knob shadowed by an
// env var comes back env_override:true and is shown read-only ("set via $VAR"), since
// env wins at read time. Save POSTs only the rows the user actually changed.
let SETTINGS_SCHEMA = null;
function _settingsCtl(f) {
  const id = "set_" + f.key, dis = f.env_override ? " disabled" : "";
  let ctl;
  if (f.type === "bool") ctl = `<input type="checkbox" id="${id}"${f.value ? " checked" : ""}${dis}>`;
  else if (f.type === "int" || f.type === "number")
    ctl = `<input type="number" id="${id}" step="${f.type === "int" ? "1" : "0.5"}" value="${esc(f.value)}"${dis}>`;
  else if (f.type === "enum")
    ctl = `<select id="${id}"${dis}>` + (f.options || []).map(o =>
      `<option value="${esc(o)}"${o === f.value ? " selected" : ""}>${esc(o[0].toUpperCase() + o.slice(1))}</option>`).join("") + `</select>`;
  else ctl = `<input type="text" id="${id}" value="${esc(f.value)}"${dis}>`;
  const env = f.env_override ? `<span class="sp-env">set via ${esc(f.env)}</span>` : "";
  return `<div class="sp-ctl">${ctl}${env}</div>`;
}
// Keys tucked into a collapsed "Advanced" section — rarely-touched plumbing that would
// otherwise clutter the main form. Presentation-only, so it lives in the frontend.
const SET_ADVANCED = new Set(["bind_host", "idle_timeout", "close_timeout", "update_remote", "update_branch"]);

function _settingsRow(f) {
  return `<div class="sp-row"><div class="sp-label"><span class="t">${esc(f.label)}</span>` +
    `<span class="h">${esc(f.help)}</span></div>${_settingsCtl(f)}</div>`;
}

function renderSettings(schema) {
  const groups = [];
  const advanced = [];
  for (const f of schema) {
    if (SET_ADVANCED.has(f.key)) { advanced.push(f); continue; }
    let g = groups.find(x => x.name === f.group);
    if (!g) { g = { name: f.group, fields: [] }; groups.push(g); }
    g.fields.push(f);
  }
  let html = groups.map(g =>
    `<div class="sp-group"><h3 class="sp-group-h">${esc(g.name)}</h3>` +
    g.fields.map(_settingsRow).join("") +
    (g.name === "Updates" ? _updateCheckRow() + _shutdownRow() : "") +
    `</div>`).join("");
  if (advanced.length) {
    let open = false;
    try { open = localStorage.getItem("setAdvOpen") === "1"; } catch (_) {}
    html += `<div class="sp-group sp-adv${open ? " open" : ""}">` +
      `<button type="button" class="sp-adv-h" id="setAdvToggle" aria-expanded="${open}">` +
      `<svg class="sp-adv-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>Advanced</button>` +
      `<div class="sp-adv-body"${open ? "" : " hidden"}>` + advanced.map(_settingsRow).join("") + `</div></div>`;
  }
  $("settingsBody").innerHTML = html;
  const cb = $("checkUpdateBtn");
  if (cb) cb.onclick = checkForUpdate;
  const sd = $("shutdownBtn");
  if (sd) sd.onclick = shutdownTracker;
  const adv = $("setAdvToggle");
  if (adv) adv.onclick = toggleSetAdvanced;
}

function toggleSetAdvanced() {
  const grp = $("setAdvToggle")?.closest(".sp-adv");
  if (!grp) return;
  const open = !grp.classList.contains("open");
  grp.classList.toggle("open", open);
  grp.querySelector(".sp-adv-body")?.toggleAttribute("hidden", !open);
  $("setAdvToggle").setAttribute("aria-expanded", open ? "true" : "false");
  try { localStorage.setItem("setAdvOpen", open ? "1" : "0"); } catch (_) {}
}
// A "Check for updates" action row appended to the Updates group: fetch + apply on the spot,
// no prompt (the click is the approval). Distinct from the banner, which is the passive prompt.
function _updateCheckRow() {
  return `<div class="sp-row sp-action"><div class="sp-label">` +
    `<span class="t">Check for updates</span>` +
    `<span class="h">Fetch the latest build now and apply it immediately — no prompt.</span></div>` +
    `<div class="sp-ctl"><button class="sp-btn" id="checkUpdateBtn">Check now</button>` +
    `<span class="sp-note" id="checkUpdateMsg"></span></div></div>`;
}
async function checkForUpdate() {
  const btn = $("checkUpdateBtn"), msg = $("checkUpdateMsg");
  if (!btn) return;
  btn.disabled = true; msg.textContent = "Checking…"; msg.classList.remove("err");
  const done = (text, err) => { msg.textContent = text; msg.classList.toggle("err", !!err); btn.disabled = false; };
  // Use postRaw, NOT postJSON: every non-update outcome comes back as {ok:false, status},
  // and postJSON throws on ok:false — which would collapse them all into one opaque error.
  let r;
  try { r = await postRaw("/api/update/check"); }
  catch (e) { return done("Couldn't reach the tracker — is it still running?", true); }
  switch (r && r.status) {
    case "updating": msg.textContent = `Updating → ${esc(r.latest)}…`; break;  // server restarts; tab reloads
    case "current":  return done(`Already up to date${r.build ? " (" + esc(r.build) + ")" : ""}.`);
    case "offline":  return done("Couldn't reach the update source — check your network or the configured remote.", true);
    case "blocked":  return done("Can't update: this checkout has uncommitted changes or isn't a managed git clone.", true);
    case "unavailable": return done("Updates aren't available on this install.", true);
    case "error":    return done(`Update check failed: ${esc(r.error || "unknown error")}`, true);
    default:         return done(`Update check failed${r && r.status ? " (" + esc(r.status) + ")" : ""}.`, true);
  }
}
// Stop the tracker process entirely (POST /api/quit -> the WSGI server's .shutdown()). Deliberate,
// so it's confirmed; the dashboard goes dead afterwards (no auto-relaunch until the next SC launch).
function _shutdownRow() {
  return `<div class="sp-row sp-action"><div class="sp-label">` +
    `<span class="t">Shut down tracker</span>` +
    `<span class="h">Stop the tracker process. The dashboard will go offline until it's launched again.</span></div>` +
    `<div class="sp-ctl"><button class="sp-btn danger" id="shutdownBtn">Shut down</button>` +
    `<span class="sp-note" id="shutdownMsg"></span></div></div>`;
}
async function shutdownTracker() {
  const btn = $("shutdownBtn"), msg = $("shutdownMsg");
  if (!btn) return;
  if (!confirm("Shut down the tracker? The dashboard will go offline until it's launched again.")) return;
  btn.disabled = true; msg.textContent = "Shutting down…"; msg.classList.remove("err");
  // The server stops right after acking, so the connection drops — a fetch error here is success.
  try { await postRaw("/api/quit"); } catch (_) {}
  msg.textContent = "Tracker stopped.";
}
async function openSettings() {
  const ov = $("settingsOverlay");
  $("settingsMsg").textContent = ""; $("settingsMsg").className = "sp-msg";
  $("settingsBody").innerHTML = `<div class="sp-row"><span class="h">loading…</span></div>`;
  ov.classList.remove("hide"); ov.setAttribute("aria-hidden", "false");
  try {
    const r = await getJSON("/api/settings");
    SETTINGS_SCHEMA = r.schema || [];
    renderSettings(SETTINGS_SCHEMA);
  } catch (e) {
    $("settingsBody").innerHTML = `<div class="sp-row"><span class="h">couldn't load settings: ${esc(e)}</span></div>`;
  }
}
function closeSettings() {
  const ov = $("settingsOverlay");
  ov.classList.add("hide"); ov.setAttribute("aria-hidden", "true");
}
function _settingsErr(msg) { const m = $("settingsMsg"); m.textContent = msg; m.className = "sp-msg err"; }
async function saveSettings() {
  if (!SETTINGS_SCHEMA) return closeSettings();
  const payload = {};
  for (const f of SETTINGS_SCHEMA) {
    if (f.env_override) continue;                  // read-only: env wins at read time
    const el = $("set_" + f.key);
    if (!el) continue;
    let v;
    if (f.type === "bool") v = el.checked;
    else if (f.type === "int" || f.type === "number") {
      if (el.value.trim() === "") continue;        // left blank -> leave unchanged
      v = Number(el.value);
      if (Number.isNaN(v)) return _settingsErr(`“${f.label}” must be a number`);
    } else v = el.value.trim();
    if (v !== f.value) payload[f.key] = v;          // only send genuine changes
  }
  if (!Object.keys(payload).length) return closeSettings();
  const btn = $("settingsSave"); btn.disabled = true;
  try {
    await postJSON("/api/settings", payload);
    closeSettings();
  }
  catch (e) { _settingsErr(String(e)); }
  finally { btn.disabled = false; }
}
$("navsettings") && ($("navsettings").onclick = openSettings);
$("settingsClose") && ($("settingsClose").onclick = closeSettings);
$("settingsCancel") && ($("settingsCancel").onclick = closeSettings);
$("settingsSave") && ($("settingsSave").onclick = saveSettings);
// Backdrop click closes (clicks on the panel don't reach the overlay element itself).
$("settingsOverlay") && ($("settingsOverlay").onclick = (e) => { if (e.target.id === "settingsOverlay") closeSettings(); });
// Escape closes (matches the type-filter / combobox / inline-editor convention).
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("settingsOverlay").classList.contains("hide")) closeSettings();
});

