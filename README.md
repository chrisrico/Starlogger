<p align="center">
  <img src="assets/social-preview.png" alt="Starlogger" width="760">
</p>

# Starlogger

**Starlogger** tails Star Citizen's `Game.log` and serves a web dashboard at
**http://127.0.0.1:8765** that models your accepted cargo missions and **groups
the work by route** — what to load at each origin, what to drop at each
destination — plus a 3-D **cargo-grid loader**, a per-session **archive**, and
**session replay** that scrubs the whole dashboard through any past session.

Runs on **Linux (Wine/Proton)** and **native Windows** — the same codebase
auto-detects the install for each.

## Install (Linux)

One command — clones into `$XDG_DATA_HOME/starlogger` (≈ `~/.local/share/starlogger`),
builds its venv, and points your Star Citizen launcher at the tracker:

```bash
curl -fsSL https://raw.githubusercontent.com/chrisrico/starlogger/main/install.sh | bash
```

Then just launch Star Citizen as usual: the tracker rides along with the game and
**checks for updates on every launch**. Nothing else to do.

- **Updating**: the running tracker owns updates. It checks for a new build shortly after
  launch and periodically thereafter, and the dashboard shows a banner
  (Update now / View changes / Dismiss). "View changes" opens the GitHub diff between your
  installed and the latest commit. The **Updates** setting governs this: *Prompt* (default —
  the banner), *Automatic* (apply silently), or *Off* (never check); `STARLOGGER_UPDATE_MODE`
  (or the legacy `STARLOGGER_AUTO_UPDATE` / `STARLOGGER_NO_UPDATE`) is the env escape hatch.
  Set `STARLOGGER_UPDATE_REMOTE` to a local clone path (or any git URL) to update from there
  instead of GitHub (`STARLOGGER_UPDATE_BRANCH` overrides the branch, default `main`) —
  configuring a non-origin source **still honors the Updates mode**; it no longer forces a
  silent update. Changing the remote/branch in the Settings panel and saving applies that
  source immediately (the save is the approval).
- To install elsewhere, set `STARLOGGER_DATA_DIR` before running the command.
- If the [LUG Helper](https://github.com/starcitizen-lug/lug-helper) ever reverts the
  `.desktop` launcher, **re-run the install command** — it re-asserts the launcher
  (and doubles as a manual update/repair).

The only runtime dependency is Flask. The ship cargo-grid database (`ships.json`) is
generated locally from your game install on first run (it is **not** bundled with the
repo — see [Ship cargo data](#ship-cargo-data)).

## Manual setup (dev / Windows)

Run from a checkout without the installer:

**Linux:**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Windows (PowerShell or cmd):**
```bat
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then run it directly:

```bash
.venv/bin/python tracker.py        # Linux: auto-detect Game.log, serve :8765
.venv\Scripts\python tracker.py    # Windows: same (or double-click run-tracker.bat)
```

The dashboard **auto-opens in your browser**; launching again while it's already
serving just exits ("already running …") instead of stacking windows. Leave it
running while you play — it polls every few seconds and resets when you relaunch the
game. The LIVE `Game.log` is auto-detected (Windows:
`%ProgramFiles%\…\StarCitizen\LIVE\Game.log`; Linux: derived from `WINEPREFIX`).

**Flags & env vars:**

- `--host ADDR` / `--port N` — bind address / port (default `127.0.0.1:8765`)
- `--log PATH` / `STARLOGGER_LOG` — use a specific `Game.log` (non-default install)
- `--no-browser` / `STARLOGGER_NO_BROWSER=1` — don't auto-open the browser
- `--once` — parse once, print JSON, exit; `--rebuild` — backfill the archive from `logbackups/`
- `--recover-stations` — backfill the `zoneHostId → name` map from your logs, then exit
- `--cleanup` (with `--dry-run` to preview) — epoch-aware prune of stale
  `station_names.json` / `overrides.json` rows, then exit
- `STARLOGGER_DATA_DIR` — where generated `*.json` + the extractor binary live (default
  `$XDG_DATA_HOME/starlogger` ≈ `~/.local/share/starlogger`; `%LOCALAPPDATA%\starlogger` on Windows)

### Tests

```bash
.venv/bin/python -m pytest                  # Python + e2e (browser tests run if Chromium present)
.venv/bin/python -m pytest -m "not browser" # skip the browser e2e tests
npm test                                    # JS: cargo-grid packer (node --test; needs Node, no deps)
```

The core suites are dependency-light — pytest needs only the venv, and the JS suite uses Node's
built-in test runner (no `node_modules`). The packer's invariants (orientations, no-float
support, container caps, hold classification) live in `tests/cargogrid.test.js`.

**Headless-browser (e2e) tests** (`tests/test_e2e.py`, marked `browser`) drive the real dashboard
in Chromium via Playwright — covering jukebox playback/persistence, the Settings *Advanced*
collapse, and auto-play. They need the dev extras:

```bash
.venv/bin/pip install -r requirements-dev.txt   # pytest-playwright + playwright
.venv/bin/python -m playwright install chromium  # one-time browser download
.venv/bin/python -m pytest -m browser            # run just the e2e tests
```

They are fully isolated — a throwaway temp data dir (`STARLOGGER_DATA_DIR`), a real server on an
ephemeral port, and a generated silent ogg — so they never touch your install or the source tree,
and they `skip` gracefully when Chromium isn't installed.

## Run it with the game

**Linux:** the [installer](#install-linux) wires this up — your Star Citizen
`.desktop` runs `lib/sc-run.sh`, which backgrounds the dashboard (the running tracker
handles updates itself — see **Updating** above) and then `exec`s LUG's `sc-launch.sh`. The tracker is tied to the game's lifetime via
`run-tracker.sh`'s `setpriv --pdeathsig`, so the kernel stops it whenever the game
launcher exits — even on SIGKILL — with no `kill` line to get wrong. It skips if
`:8765` is already serving. (The LUG Helper may revert the `.desktop` on update;
re-run the install command to restore it.)

**Windows:** run `run-tracker.bat` in a terminal (or make a desktop shortcut to it);
Ctrl-C stops it. `STARLOGGER_DATA_DIR` defaults to `%LOCALAPPDATA%\starlogger`.

## Dashboard

The tabs are **mode-aware** (and deep-linked via the URL hash). In **hauling**
mode you get **Contracts**, **Cargo**, **Plan**, and **Archive**; when you're in a
mining ship (or pin it with the **MODE** switch — Auto / Cargo / Mining — at the
top), the cargo-hauling **Cargo** and **Plan** tabs make way for a **Mining** tab.
**Cargo** and **Plan** each hold two views behind a segmented toggle:

- **Contracts** — full mission table with per-row **Edit** / **Delete**.
- **Cargo** — **Loading** (per pickup station: total SCU and the cargo to load,
  with each parcel's destination; legs grey out once picked up) ⇄ **Unloading**
  (per destination: total SCU and cargo to drop, with its origin). Opens on the
  phase your current location implies.
- **Plan** — **Routes** (origin → destination pairs aggregated into an ordered
  trip) ⇄ **Manifest**, the **cargo-grid loader**: an isometric 3-D view of your
  ship's hold packed in delivery order (first-out on top), with a **load
  sequence** of elevators to bring up. Each bay is labelled (Rear, Mid, Nose,
  Module 1…) and a **▲ FWD** marker shows the bow.
- **Mining** — **identify** a scanned rock by its Rock Signature (decomposing a
  mixed cluster), a **mineral finder** (which rocks carry a given mineral), and a
  **blueprint plan** (a crafting recipe's minerals mapped to where to mine them).
  Built from the p4k mining data (see [Ship cargo data](#ship-cargo-data)).
- **Archive** — pooled, cross-session logs: a **Contract Log** (with a high-level
  type filter), **Trade Loads** (manual buy/sell P&L plus your best trade routes),
  a **Travel Log** of quantum jumps, and a **Sessions** list with **replay** —
  pick a session and scrub the entire dashboard through its past states.

The header stats and capacity gauge are mode-aware too, and follow the
game-detected ship — or one you pick in the **SHIP** box at the top. Non-trade
missions (couriers, combat) appear only in the Archive. A **Jukebox** button in the
header opens an overlay to play and curate the game soundtrack (decoded from your
own `Data.p4k`, same as the ship/mining data). The all-ships grid reference is at
**/grids.html**.

## Ship cargo data

`ships.json` (per-ship SCU, deck-accurate grid geometry, names, manufacturer,
role) is read **straight from the game's own `Data.p4k`** — no third-party site —
via [StarBreaker](https://github.com/diogotr7/StarBreaker), a Rust extractor the
tracker downloads once (SHA-256-pinned, per-OS) into `STARLOGGER_DATA_DIR/bin`. It
rebuilds only when the game's **major version changes**, at background priority. The
extracted data is **not redistributed** — it's generated on your own machine from your
own copy of the game. If `Data.p4k` isn't found next to `Game.log`, cargo data is
simply unavailable (the grid falls back to empty) until you run against an install.

## Fixing recovered data

The log isn't always complete, so any mission is editable from the **Contracts**
tab: **Edit** (title, origin, reward, and per-leg cargo/qty/destination — with
autocomplete and `12k`/`1.5m` reward shorthand), **Delete** (hide; restorable),
and **Reset to log**. Edits persist in `overrides.json`, keyed by mission id.

Two common gaps:

- **Missing pickup names** — the log rarely prints origin station names, so the
  tracker learns a `zoneHostId → name` map (persisted in `station_names.json`).
  Rename any station from its Loading/Unloading card, or run `--recover-stations`
  to backfill the map from your `logbackups/`.
- **Missing quantities** — accepting missions in a burst can drop their quantity
  lines; those show as `?` and are flagged partial. Fill them in via **Edit**.

## Project layout

```
install.sh        Linux: one-shot installer (clone + venv + patch .desktop)
tracker.py        CLI entry point
lib/sc-run.sh     Linux: game launcher — prompts to update, starts tracker, execs sc-launch.sh
run-tracker.sh    Linux: start the tracker for a play session (sc-launch hook)
run-tracker.bat   Windows: start the tracker for a play session
starlogger/       package:
                    config · patterns · model · state (log parser) · snapshot ·
                    planner · jsonstore · overrides · stations · tradeflags ·
                    ships · scdata (Data.p4k extraction) · catalogs (p4k-cache
                    refresh loop) · reference · contracts · mineables ·
                    blueprints · music (p4k-derived catalogs) · archive · replay ·
                    replay_edit (session replay + ephemeral edits) ·
                    maintenance · tailer · settings · server (Flask)
web/              dashboard front-end:
                    index.html · styles.css · app.js · cargogrid.js (3-D grid
                    renderer) · grids.html (all-ships reference) ·
                    logo.svg / icon.svg (brand)
assets/           social-preview.png · icon.png (repo/brand images)
```

Generated data lives in `STARLOGGER_DATA_DIR` (default `~/.local/share/starlogger`,
or `$XDG_DATA_HOME/starlogger`; `%LOCALAPPDATA%\starlogger` on Windows): `overrides.json`,
`sessions.json`, `settings.json`, `station_names.json`, `trade_flags.json`,
`music_curation.json`, the p4k-derived `ships.json`, `reference.json`, `mineables.json`,
`blueprints.json`, `contracts.json`, `music.json` (+ the decoded `music/` oggs), and the
extractor in `bin/`.
