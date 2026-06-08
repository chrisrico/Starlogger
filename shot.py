#!/usr/bin/env python3
"""Reusable headless-chromium screenshotter for the tracker's web UI.

Renders the real CSS/3D via Playwright's bundled Chromium against the running tracker
(127.0.0.1:8765 by default), so it's a faithful capture of what the browser draws — used
both as a library (see grid_shot.py) and as a small CLI for ad-hoc captures of any page.

As a library:
    from shot import session, shoot
    with session(viewport=(1500, 1000)) as page:
        shoot(page, "/", "/tmp/dash.png", clicks=["text=Archive"], element=".arch-panel")

As a CLI:
    .venv-shot/bin/python shot.py <path-or-url> -o OUT [-w WAIT_SEL] [-e ELEMENT_SEL]
        [-c CLICK_SEL ...] [--viewport WxH] [--scale N] [--settle MS]
    # path is relative to the tracker base; a full http(s):// URL is used verbatim.
Requires the tracker running on 127.0.0.1:8765 (override with $SHOT_BASE).
"""
import os
import sys
import argparse
from contextlib import contextmanager
from playwright.sync_api import sync_playwright

BASE = os.environ.get("SHOT_BASE", "http://127.0.0.1:8765")


def resolve_url(path):
    """A full URL is used as-is; anything else is joined onto the tracker base."""
    if path.startswith(("http://", "https://")):
        return path
    return BASE.rstrip("/") + "/" + path.lstrip("/")


@contextmanager
def session(viewport=(1700, 1300), scale=2):
    """A headless-chromium page, torn down on exit. Reuse one session across many shoot()
    calls (e.g. a loop over ship fills / archive tabs) to avoid relaunching the browser."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": viewport[0], "height": viewport[1]}, device_scale_factor=scale)
        try:
            yield page
        finally:
            browser.close()


def shoot(page, url, out, *, wait_for=None, element=None, clicks=(),
          settle_ms=500, wait_until="load", timeout=8000):
    # NB: the live dashboard holds an open SSE stream, so "networkidle" never fires — default
    # to "load" and lean on `wait_for` / `settle_ms` for readiness instead.
    """Navigate `page` to `url`, optionally wait for `wait_for`, run each `clicks` selector
    (settling between), then save a PNG of `element` (CSS selector) or the whole page to
    `out`. Returns the output path. Raises if `wait_for` never appears."""
    page.goto(resolve_url(url), wait_until=wait_until)
    if wait_for:
        page.wait_for_selector(wait_for, timeout=timeout)   # raises on miss — caller decides
    for sel in clicks:
        page.click(sel, timeout=timeout)
        page.wait_for_timeout(settle_ms)
    if settle_ms:
        page.wait_for_timeout(settle_ms)                    # let transitions/3D settle
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    target = page.query_selector(element) if element else None
    (target or page).screenshot(path=out)
    return out


def _parse_viewport(s):
    w, h = s.lower().split("x")
    return int(w), int(h)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Headless screenshot of a tracker page/element.")
    ap.add_argument("url", help="page path (relative to the tracker base) or full http(s) URL")
    ap.add_argument("-o", "--out", default="/tmp/shot.png", help="output PNG path")
    ap.add_argument("-w", "--wait", help="CSS selector to wait for before shooting")
    ap.add_argument("-e", "--element", help="CSS selector to screenshot (default: whole page)")
    ap.add_argument("-c", "--click", action="append", default=[],
                    help="selector to click before shooting (repeatable; e.g. 'text=Archive')")
    ap.add_argument("--viewport", type=_parse_viewport, default=(1500, 1000), help="WxH, e.g. 1500x1000")
    ap.add_argument("--scale", type=int, default=2, help="device scale factor")
    ap.add_argument("--settle", type=int, default=500, help="ms to wait after nav/clicks")
    args = ap.parse_args(argv)

    with session(viewport=args.viewport, scale=args.scale) as page:
        try:
            out = shoot(page, args.url, args.out, wait_for=args.wait, element=args.element,
                        clicks=args.click, settle_ms=args.settle)
        except Exception as e:
            print(f"!! capture failed: {e}", file=sys.stderr)
            return 1
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
