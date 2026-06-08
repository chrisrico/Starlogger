"use strict";
// Pure DOM + formatting primitives shared by every dashboard module. No app state lives
// here — just the helpers that read the DOM, escape/format values, and the small render
// building blocks (logTable/th/tag) the table views are assembled from.

export const $ = (id) => document.getElementById(id);

// Defensive read of an input's value by id ("" when the element isn't in the DOM yet).
export const val = (id) => ($(id) || {}).value || "";

export const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

export const num = (n) => (n == null ? "" : Number(n).toLocaleString());

// Only touch the DOM when the rendered HTML actually changes. Kills repaint flicker and
// means entrance animations replay only on real updates.
const _rendered = {};
const _seen = {};
export function setHTML(id, html) {
  if (_rendered[id] === html) return;
  _rendered[id] = html;
  const el = $(id);
  el.classList.toggle("noanim", !!_seen[id]);  // entrance animation on first paint only
  el.innerHTML = html;
  _seen[id] = true;
}

// ---- small render helpers (DRY the repeated table/log markup) ---- //
// A scrolling log table, or an empty-state note when there are no body rows.
// `headRow` is the inner HTML of the <thead> row (the <th> cells); `bodyRows` the
// concatenated <tr>s ("" / falsy → the empty note). Callers keep full control of cells.
export function logTable(headRow, bodyRows, emptyMsg) {
  return bodyRows
    ? `<div class="logwrap"><table class="logtable"><thead><tr>${headRow}</tr></thead><tbody>${bodyRows}</tbody></table></div>`
    : `<div class="empty">${emptyMsg}</div>`;
}
// A header cell; `num` right-aligns it to match a numeric column's values. `tip`
// (optional) adds a hover tooltip explaining the column.
export const th = (label, num, tip) =>
  `<th${num ? ' class="lt-num"' : ""}${tip ? ` title="${esc(tip)}"` : ""}>${label}</th>`;
// A small uppercased status/category pill (the .lt-tag family).
export const tag = (text, cls) => `<span class="lt-tag${cls ? " " + cls : ""}">${esc(text)}</span>`;
