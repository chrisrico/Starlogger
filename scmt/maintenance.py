"""Shared maintenance ops, callable from the CLI and the live service.

`run_cleanup` is the epoch-aware prune behind both `tracker.py --cleanup` and the
automatic cleanup the running tracker fires when it detects a new server epoch in
the log (see scmt/state.py's epoch-change hook). Keeping the logic here means the
one-shot command and the live trigger can never drift apart.
"""

from __future__ import annotations

from . import patterns
from .overrides import get_overrides, prune_overrides
from .stations import prune_station_names, zone_epoch


def scan_log(log_path: str) -> tuple[set, set]:
    """Read the current log and return (epochs, keep_mission_ids): every server
    epoch its markers mention, and the override mission_ids still present in it
    (live, or restored by a crash-relaunch -- a UUID substring test is exact)."""
    text = open(log_path, encoding="utf-8", errors="replace").read()
    epochs = {e for e in (zone_epoch(m.group("zone"))
                          for m in patterns.MARKER.finditer(text)) if e is not None}
    keep_mids = {mid for mid in get_overrides() if mid in text}
    return epochs, keep_mids


def run_cleanup(log_path: str, dry_run: bool = False) -> dict:
    """Epoch-prune station_names.json + overrides.json against the current log.
    Returns {skipped, epochs, stations, overrides}; skips (writes nothing) when
    the log has no markers, since we can't identify the current epoch then."""
    epochs, keep_mids = scan_log(log_path)
    if not epochs:
        return {"skipped": True, "epochs": set(), "stations": None, "overrides": None}
    return {
        "skipped": False,
        "epochs": epochs,
        "stations": prune_station_names(epochs, dry_run=dry_run),
        "overrides": prune_overrides(keep_mids, dry_run=dry_run),
    }
