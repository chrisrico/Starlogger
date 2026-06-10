"use strict";
// Settings overlay (sidebar gear → the dashboard-managed settings.json). Self-contained:
// renders the live schema from /api/settings, persists via /api/settings, and hosts the
// "Check for updates" / "Shut down tracker" actions and the Advanced collapse. Imported for
// its side effects — it wires its own nav button + Escape handler at load.
import { $, esc, toast, hintIcon } from "./dom.js";
import { postJSON, postRaw, getJSON } from "./net.js";

// ---- settings overlay (sidebar gear -> dashboard-managed settings.json) ----
// Renders straight from /api/settings' schema: one row per knob, grouped, with bool ->
// checkbox / int|number -> number input / enum or any field with `options` -> <select>
// (using option_labels for friendly text when present) / string -> text input. A knob shadowed by an
// env var comes back env_override:true and is shown read-only ("set via $VAR"), since
// env wins at read time. Save POSTs only the rows the user actually changed.
let SETTINGS_SCHEMA = null;
function _settingsCtl(f) {
  const id = "set_" + f.key, dis = f.env_override ? " disabled" : "";
  let ctl;
  if (f.widget === "segmented") {
    // A segmented control (shared .modesw chrome with the header's Auto/Cargo/Mining switch).
    // For a bool it's a two-way On/Off; for an enum, one button per option. The picked value
    // lives in a hidden input so saveSettings reads it like any other knob; the buttons are
    // wired in renderSettings (no inline handlers / window bridge).
    const opts = f.type === "bool"
      ? [{ v: "on", lbl: "On" }, { v: "off", lbl: "Off" }]
      : (f.options || []).map(o => ({ v: o,
          lbl: (f.option_labels && f.option_labels[o]) || (o[0].toUpperCase() + o.slice(1)) }));
    const cur = f.type === "bool" ? (f.value ? "on" : "off") : f.value;
    const segs = opts.map(o => {
      const on = o.v === cur;
      return `<button type="button" class="modesw-opt${on ? " active" : ""}" role="radio"
        aria-checked="${on}" data-val="${esc(o.v)}"${dis}>${esc(o.lbl)}</button>`;
    }).join("");
    ctl = `<input type="hidden" id="${id}" value="${esc(cur)}">` +
      `<div class="modesw" id="${id}_seg" role="radiogroup">${segs}</div>`;
  }
  else if (f.type === "bool") ctl = `<input type="checkbox" id="${id}"${f.value ? " checked" : ""}${dis}>`;
  else if (f.type === "int" || f.type === "number") {
    const attrs = `step="${f.type === "int" ? "1" : "0.5"}" inputmode="${f.type === "int" ? "numeric" : "decimal"}"` +
      (f.min != null ? ` min="${esc(f.min)}"` : "") + (f.max != null ? ` max="${esc(f.max)}"` : "");
    const input = `<input type="number" id="${id}" ${attrs} value="${esc(f.value)}"${dis}>`;
    // A short unit (e.g. "s") rides inside the input on the right; .numf hides the native
    // spinners so it sits cleanly against the edge. No unit -> the bare input.
    ctl = f.unit ? `<span class="numf"><span class="numf-u">${esc(f.unit)}</span>${input}</span>` : input;
  }
  else if (f.type === "enum" || f.options)
    ctl = `<select id="${id}"${dis}>` + (f.options || []).map(o => {
      const lbl = (f.option_labels && f.option_labels[o]) || (o[0].toUpperCase() + o.slice(1));
      return `<option value="${esc(o)}"${o === f.value ? " selected" : ""}>${esc(lbl)}</option>`;
    }).join("") + `</select>`;
  // A `placeholder` shows ghost text when the input is blank (e.g. the default global.ini URL).
  else ctl = `<input type="text" id="${id}" value="${esc(f.value)}"` +
    (f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : "") + `${dis}>`;
  const env = f.env_override ? `<span class="sp-env">set via ${esc(f.env)}</span>` : "";
  return `<div class="sp-ctl">${ctl}${env}</div>`;
}
// Keys tucked into a collapsed "Advanced" section — rarely-touched plumbing that would
// otherwise clutter the main form. Presentation-only, so it lives in the frontend.
const SET_ADVANCED = new Set(["bind_host", "idle_timeout", "close_timeout", "update_remote", "update_branch"]);

function _settingsRow(f) {
  const help = f.help ? " " + hintIcon(f.help) : "";
  return `<div class="sp-row"><div class="sp-label"><span class="t">${esc(f.label)}${help}</span>` +
    `</div>${_settingsCtl(f)}</div>`;
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
    (g.name === "Updates" ? _updateCheckRow() : "") +
    `</div>`).join("");
  // Restart / Shut down are deliberate, rarely-touched lifecycle actions, so they sit at the
  // very bottom of the (collapsed-by-default) Advanced section, after the advanced knobs.
  let open = false;
  try { open = localStorage.getItem("setAdvOpen") === "1"; } catch (_) {}
  html += `<div class="sp-group sp-adv${open ? " open" : ""}">` +
    `<button type="button" class="sp-adv-h" id="setAdvToggle" aria-expanded="${open}">` +
    `<svg class="sp-adv-caret" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>Advanced</button>` +
    `<div class="sp-adv-body"${open ? "" : " hidden"}>` +
    advanced.map(_settingsRow).join("") + _shutdownRow() + `</div></div>`;
  $("settingsBody").innerHTML = html;
  const cb = $("checkUpdateBtn");
  if (cb) cb.onclick = checkForUpdate;
  const sd = $("shutdownBtn");
  if (sd) sd.onclick = shutdownTracker;
  const rs = $("restartBtn");
  if (rs) rs.onclick = restartTracker;
  const adv = $("setAdvToggle");
  if (adv) adv.onclick = toggleSetAdvanced;
  // Wire each segmented control's buttons to update its backing hidden input.
  $("settingsBody").querySelectorAll(".modesw .modesw-opt[data-val]").forEach(b => {
    b.onclick = () => setSegmented(b.closest(".modesw").id.replace(/_seg$/, ""), b.dataset.val);
  });
  // The check interval is meaningless when updates are off — hide its row to match; likewise
  // the global.ini URL is moot when its download is off.
  _applyUpdateModeDep();
  _applyStarstringsDep();
}

// Apply a segmented control's pick: store it in the backing hidden input + repaint the buttons.
function setSegmented(id, val) {
  const hidden = $(id);
  if (!hidden || hidden.disabled) return;
  hidden.value = val;
  $(id + "_seg")?.querySelectorAll(".modesw-opt").forEach(b => {
    const on = b.dataset.val === val;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
  if (id === "set_update_mode") _applyUpdateModeDep();
  else if (id === "set_starstrings_enabled") _applyStarstringsDep();
}

// Show/hide the "Update check interval" row to match the current Updates mode.
function _applyUpdateModeDep() {
  const um = $("set_update_mode"), row = $("set_live_update_secs")?.closest(".sp-row");
  if (um && row) row.toggleAttribute("hidden", um.value === "off");
}

// Show/hide the global.ini source URL row to match the download on/off switch.
function _applyStarstringsDep() {
  const en = $("set_starstrings_enabled"), row = $("set_starstrings_url")?.closest(".sp-row");
  if (en && row) row.toggleAttribute("hidden", en.value === "off");
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
// A "Check for updates" action row appended to the Updates group: persists a pending Updates-mode
// change, then checks now — applying on the spot in Automatic, or raising the banner in Prompt/Off.
function _updateCheckRow() {
  return `<div class="sp-row sp-action"><div class="sp-label">` +
    `<span class="t">Check for updates ${hintIcon("Check for a new build now. In <b>Automatic</b> mode it's applied immediately; in <b>Prompt</b> or <b>Off</b> mode you'll be asked to confirm via the banner.")}</span></div>` +
    `<div class="sp-ctl"><button class="sp-btn" id="checkUpdateBtn">Check now</button>` +
    `<span class="sp-note" id="checkUpdateMsg"></span></div></div>`;
}
async function checkForUpdate() {
  const btn = $("checkUpdateBtn"), msg = $("checkUpdateMsg");
  if (!btn) return;
  btn.disabled = true; msg.textContent = "Checking…"; msg.classList.remove("err");
  const done = (text, err) => { msg.textContent = text; msg.classList.toggle("err", !!err); btn.disabled = false; };
  // If the Updates mode was changed in the panel but not yet saved, persist it first so the
  // server's check honours the current selection — a just-switched Auto→Prompt prompts via the
  // banner instead of silently auto-applying. ("Save that setting change, then run the update.")
  const mf = (SETTINGS_SCHEMA || []).find(f => f.key === "update_mode");
  const um = $("set_update_mode");
  if (mf && um && !mf.env_override && um.value !== mf.value) {
    try { await postJSON("/api/settings", { update_mode: um.value }); }
    catch (e) { return done(`Couldn't save the Updates setting: ${e}`, true); }
    mf.value = um.value;   // reflect the saved value so a later Save won't resend it
  }
  // Use postRaw, NOT postJSON: every non-update outcome comes back as {ok:false, status},
  // and postJSON throws on ok:false — which would collapse them all into one opaque error.
  let r;
  try { r = await postRaw("/api/update/check"); }
  catch (e) { return done("Couldn't reach the tracker — is it still running?", true); }
  switch (r && r.status) {
    case "updating": msg.textContent = `Updating → ${esc(r.latest)}…`; break;  // server restarts; tab reloads
    case "available": return done(`Build ${esc(r.latest)} available — apply it from the banner.`);
    case "current":  return done(`Already up to date${r.build ? " (" + esc(r.build) + ")" : ""}.`);
    case "offline":  return done("Couldn't reach the update source — check your network or the configured remote.", true);
    case "blocked":  return done("Can't update: this checkout has uncommitted changes or isn't a managed git clone.", true);
    case "unavailable": return done("Updates aren't available on this install.", true);
    case "error":    return done(`Update check failed: ${esc(r.error || "unknown error")}`, true);
    default:         return done(`Update check failed${r && r.status ? " (" + esc(r.status) + ")" : ""}.`, true);
  }
}
// Lifecycle actions: Restart re-execs in place (POST /api/restart -> ON_RESTART), Shut down stops
// the process entirely (POST /api/quit -> the WSGI server's .shutdown()). Both are deliberate, so
// both are confirmed. After Restart the dashboard's stream reconnects on its own; after Shut down
// the dashboard goes dead (no auto-relaunch until the next SC launch). They share one note span.
function _shutdownRow() {
  return `<div class="sp-row sp-action"><div class="sp-label">` +
    `<span class="t">Restart or shut down ${hintIcon("<b>Restart</b> relaunches the tracker in place — the dashboard reconnects on its own. <b>Shut down</b> stops it entirely; the dashboard goes offline until it's launched again.")}</span></div>` +
    `<div class="sp-ctl"><div class="sp-btnrow">` +
    `<button class="sp-btn" id="restartBtn">Restart</button>` +
    `<button class="sp-btn danger" id="shutdownBtn">Shut down</button></div>` +
    `<span class="sp-note" id="shutdownMsg"></span></div></div>`;
}
async function restartTracker() {
  const btn = $("restartBtn"), msg = $("shutdownMsg");
  if (!btn) return;
  if (!confirm("Restart the tracker? The dashboard will briefly disconnect, then reconnect.")) return;
  btn.disabled = true; msg.textContent = "Restarting…"; msg.classList.remove("err");
  // The server re-execs right after acking, so the connection drops briefly — a fetch error here
  // is expected; the SSE stream reconnects to the same URL once the replacement is up.
  try { await postRaw("/api/restart"); } catch (_) {}
  msg.textContent = "Restarting — reconnecting…";
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
    // A bool renders as a checkbox or (widget:"segmented") a hidden input holding "on"/"off".
    if (f.type === "bool") v = el.type === "checkbox" ? el.checked : el.value === "on";
    else if (f.type === "int" || f.type === "number") {
      if (el.value.trim() === "") continue;        // left blank -> leave unchanged
      v = Number(el.value);
      if (Number.isNaN(v)) return _settingsErr(`“${f.label}” must be a number.`);
      if (f.type === "int" && !Number.isInteger(v)) return _settingsErr(`“${f.label}” must be a whole number.`);
      if (f.min != null && v < f.min) return _settingsErr(`“${f.label}” must be at least ${f.min}.`);
      if (f.max != null && v > f.max) return _settingsErr(`“${f.label}” must be at most ${f.max}.`);
    } else {
      v = el.value.trim();
      // Blank is allowed only when the field's default is itself blank (e.g. the global.ini URL,
      // where empty means "use the default" shown as placeholder); otherwise it must stay filled.
      if (v === "" && f.default !== "") return _settingsErr(`“${f.label}” can’t be empty.`);
    }
    if (v !== f.value) payload[f.key] = v;          // only send genuine changes
  }
  if (!Object.keys(payload).length) return closeSettings();
  const btn = $("settingsSave"); btn.disabled = true;
  try {
    await postJSON("/api/settings", payload);
    closeSettings();
    // Changing the bind address re-execs the server to rebind; warn that the connection
    // will blink (and that switching to "This machine only" drops other devices).
    if ("bind_host" in payload) toast("Restarting to apply the new bind address…");
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

