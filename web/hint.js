"use strict";
// Floating help tips for the inline "?" badges produced by dom.hintIcon(). One shared
// #hinttip element, positioned with fixed coords on hover/focus so it floats above any
// scrolling panel (an absolutely-positioned child would be clipped by the settings/equip
// overlay's overflow). Pure event delegation — no per-badge wiring, nothing on window.
// Imported for side effects from app.js.

let tip = null;
function ensureTip() {
  if (!tip) { tip = document.createElement("div"); tip.id = "hinttip"; document.body.appendChild(tip); }
  return tip;
}
function showTip(el) {
  const t = ensureTip();
  t.innerHTML = el.dataset.tip || "";   // data-tip round-trips back to live <code>/<b> markup
  t.classList.add("show");
  // Center over the badge, clamped to the viewport; prefer above, flip below if no room.
  const r = el.getBoundingClientRect(), tr = t.getBoundingClientRect();
  let left = r.left + r.width / 2 - tr.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
  let top = r.top - tr.height - 8;
  if (top < 8) top = r.bottom + 8;
  t.style.left = `${left}px`;
  t.style.top = `${top}px`;
}
function hideTip() { if (tip) tip.classList.remove("show"); }

const badge = (e) => e.target.closest && e.target.closest(".hint");
document.addEventListener("mouseover", (e) => { const b = badge(e); if (b) showTip(b); });
document.addEventListener("mouseout", (e) => { if (badge(e)) hideTip(); });
document.addEventListener("focusin", (e) => { const b = badge(e); if (b) showTip(b); });
document.addEventListener("focusout", (e) => { if (badge(e)) hideTip(); });
// Drop a stale tip if the page scrolls or resizes out from under it.
window.addEventListener("scroll", hideTip, true);
window.addEventListener("resize", hideTip);
