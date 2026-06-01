#!/usr/bin/env python3
"""Screenshot a ship's cargo grid from the running tracker's /grids.html, for visual
debugging. Renders the real CSS-3D via headless chromium.

Usage:
    .venv-shot/bin/python grid_shot.py "<ship name or substring>" [empty|full ...]
Defaults to capturing BOTH empty and full. PNGs land in /tmp/grid_shots/.
Requires the tracker running on 127.0.0.1:8765.
"""
import sys, os
from urllib.parse import quote
from playwright.sync_api import sync_playwright

ship = sys.argv[1]
fills = sys.argv[2:] or ["empty", "full"]
outdir = "/tmp/grid_shots"
os.makedirs(outdir, exist_ok=True)
safe = ship.replace("/", "_").replace(" ", "_")

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1700, "height": 1300}, device_scale_factor=2)
    for fill in fills:
        url = f"http://127.0.0.1:8765/grids.html?ship={quote(ship)}&fill={fill}&scale=24"
        page.goto(url, wait_until="networkidle")
        try:
            page.wait_for_selector("#ships .cg-ship", timeout=8000)
        except Exception:
            print(f"!! no ship card for {ship!r} ({fill}) — check the name", file=sys.stderr)
            continue
        page.wait_for_timeout(500)  # let 3D transforms settle
        out = f"{outdir}/{safe}-{fill}.png"
        el = page.query_selector("#ships .cg-ship")
        (el or page).screenshot(path=out)
        print(out)
    browser.close()
