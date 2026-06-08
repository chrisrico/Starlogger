#!/usr/bin/env python3
"""Screenshot a ship's cargo grid from the running tracker's /grids.html, for visual
debugging. Renders the real CSS-3D via headless chromium (see shot.py for the engine).

Usage:
    .venv-shot/bin/python grid_shot.py "<ship name or substring>" [empty|full ...]
Defaults to capturing BOTH empty and full. PNGs land in /tmp/grid_shots/.
Requires the tracker running on 127.0.0.1:8765.
"""
import sys
from urllib.parse import quote
from shot import session, shoot

ship = sys.argv[1]
fills = sys.argv[2:] or ["empty", "full"]
outdir = "/tmp/grid_shots"
safe = ship.replace("/", "_").replace(" ", "_")

with session(viewport=(1700, 1300), scale=2) as page:
    for fill in fills:
        url = f"/grids.html?ship={quote(ship)}&fill={fill}&scale=24"
        out = f"{outdir}/{safe}-{fill}.png"
        try:
            print(shoot(page, url, out, wait_for="#ships .cg-ship", element="#ships .cg-ship"))
        except Exception:
            print(f"!! no ship card for {ship!r} ({fill}) — check the name", file=sys.stderr)
