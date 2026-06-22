"use strict";
// A searchable, keyboard-navigable combobox shared by the header ship picker and the
// shipbuilder's outfit picker — one implementation, same .shipbox/.shipsel/.shipmenu/.shipopt
// chrome. Each instance registers a config keyed by its input id; the inline on* handlers in the
// rendered markup dispatch back here by id, so a single set of logic drives every dropdown
// (matches the codebase's "regenerated HTML + inline handlers" pattern — no listener re-binding).
import { $, val } from "./dom.js";
import { html, render } from "./lit.js";

// id -> { entries(filter) -> [{name, mfr} | {clear, label}], onPick(name), reset() -> string, active }
const COMBOS = {};

// Register (or re-register) an instance. `entries(filter)` returns the option list (a `{clear}`
// entry renders the "clear" sentinel and picks as ""); `onPick(name)` commits a choice; `reset()`
// is the value restored when focus leaves without a fresh pick.
export function registerCombo(id, { entries, onPick, reset }) {
  COMBOS[id] = { entries, onPick, reset, active: -1 };
}

// The input + (empty) menu, as a lit template. The `.shipsel` width/chrome come from styles.css;
// pass `cls` to scope per-instance overrides onto the wrapper. Handlers bind via lit @event (no
// window bridge); the menu element is populated imperatively by _render on focus/input/key.
export function comboInputHtml(id, { value = "", placeholder = "search…", label = "", cls = "" } = {}) {
  return html`<span class="shipbox${cls ? " " + cls : ""}">
    <input id=${id} class="shipsel" type="text" autocomplete="off" aria-label=${label}
      role="combobox" aria-expanded="false" aria-controls=${id + "-menu"} aria-autocomplete="list" aria-activedescendant=""
      placeholder=${placeholder} value=${value}
      @focus=${() => comboOpen(id)} @input=${() => comboFilter(id)} @keydown=${(e) => comboKey(id, e)} @blur=${() => comboBlur(id)}>
    <div id=${id + "-menu"} class="shipmenu" role="listbox" aria-label=${label}></div></span>`;
}

function _menuTpl(c, id, filter) {
  const ents = c.entries(filter) || [];
  const real = ents.filter(e => !e.clear).length;
  const rows = ents.map((e, i) => {
    const act = i === c.active ? " active" : "";
    const sel = i === c.active;
    if (e.clear)
      return html`<div class="shipopt clear${act}" role="option" id=${id + "-opt-" + i} aria-selected=${sel} @mousedown=${(ev) => comboPick(ev, id, "")}>${e.label || "— clear —"}</div>`;
    return html`<div class="shipopt${act}" role="option" id=${id + "-opt-" + i} aria-selected=${sel} data-name=${e.name} @mousedown=${(ev) => comboPick(ev, id, e.name)}><span class="sn">${e.name}</span><span class="om">${e.mfr || ""}</span></div>`;
  });
  return real ? html`${rows}` : html`${rows}<div class="shipopt empty">no match</div>`;
}

function _render(id, filter) {
  const c = COMBOS[id], inp = $(id), menu = $(id + "-menu");
  if (!c || !inp || !menu) return;
  render(_menuTpl(c, id, filter), menu);
  menu.classList.add("open");
  inp.setAttribute("aria-expanded", "true");
  inp.setAttribute("aria-activedescendant", c.active >= 0 ? `${id}-opt-${c.active}` : "");
  const a = menu.querySelector(".shipopt.active");
  if (a && a.scrollIntoView) a.scrollIntoView({ block: "nearest" });
}

export function comboOpen(id) { const c = COMBOS[id]; if (!c) return; const inp = $(id); if (inp) inp.select(); c.active = -1; _render(id, ""); }
export function comboFilter(id) { const c = COMBOS[id]; if (!c) return; c.active = -1; _render(id, val(id)); }
export function comboBlur(id) {
  const c = COMBOS[id]; if (!c) return;
  const inp = $(id), menu = $(id + "-menu");
  if (menu) menu.classList.remove("open");
  if (inp) { inp.setAttribute("aria-expanded", "false"); inp.setAttribute("aria-activedescendant", ""); if (c.reset) inp.value = c.reset() || ""; }
}
export function comboKey(id, ev) {
  const c = COMBOS[id]; if (!c) return;
  if (ev.key === "Escape") { ev.target.blur(); return; }
  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    const n = (c.entries(ev.target.value) || []).length; if (!n) return;
    c.active = ev.key === "ArrowDown" ? Math.min(n - 1, c.active + 1) : Math.max(0, c.active - 1);
    _render(id, ev.target.value);
  } else if (ev.key === "Enter") {
    ev.preventDefault();
    const ents = c.entries(ev.target.value) || [];
    if (c.active >= 0 && c.active < ents.length) { const e = ents[c.active]; comboPick(ev, id, e.clear ? "" : e.name); }
    else { const first = ents.find(e => !e.clear); if (first) comboPick(ev, id, first.name); }
  }
}
export function comboPick(ev, id, name) {
  if (ev) ev.preventDefault();      // (mousedown) keep focus until we commit
  const c = COMBOS[id]; if (!c) return;
  c.active = -1;
  const inp = $(id); if (inp) inp.value = name;
  c.onPick(name);                   // commit BEFORE blur so the blur's reset() sees the new value
  if (inp) inp.blur();
}
