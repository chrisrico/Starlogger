"use strict";
// The app's lit-html surface, re-exported from the vendored, buildless lit bundle
// (web/vendor/lit-all.min.js — a self-contained ESM module committed to the repo like any
// other source file; NO build step, NO npm). Every module imports its lit primitives from
// here, so there's a single place that names what we use and points at the vendored file —
// swap the bundle (or pin a version) in one spot.
//
// We use lit-html only — `html`/`render` plus a few directives — NOT LitElement: the
// dashboard renders templates into the existing <div id="…"> tab containers (see dom.js's
// `mount`), it doesn't define custom elements.
export {
  html,        // tagged-template → a renderable TemplateResult
  render,      // render(template, container) — diffs into the DOM (see dom.js `mount`)
  nothing,     // render-nothing sentinel (cleaner than "")
  noChange,    // leave a binding untouched
  repeat,      // keyed list rendering (stable node identity across reorders)
  unsafeHTML,  // explicit, greppable trusted-markup injection (e.g. cargogrid's HTML string)
  classMap,    // toggle classes from an object — reflects hover/drag state on re-render
  styleMap,    // inline styles from an object
  ifDefined,   // omit an attribute when the value is null/undefined
  live,        // bind to the live DOM value (keeps a focused input from being reset on render)
  keyed,       // tie a subtree's identity to a key (force re-create when the key changes)
  when,        // when(cond, () => tplA, () => tplB) conditional helper
  map,         // map(items, (i) => tpl) lightweight list helper
} from "./vendor/lit-all.min.js";
