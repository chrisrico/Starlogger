"""Where-to-mine join: a mineral name -> the surface bodies + asteroid fields it's found on.

The one place the body (``body_mineables``) and space (``space_mineables``) reverse maps are
merged into the single inline ``locations`` list the dashboard renders (cyan body chips + amber
field chips, grouped by mining method -- ``web/mining.js`` ``locChips``). Shared so both the API
endpoints (server.py) and the per-mission snapshot (snapshot.py) join the same way without a
circular import.

``method`` selects the body-mining context: ``ship`` (Prospector/MOLE) ALSO draws asteroid
fields; ``hand`` (handheld cave gems -- Aphorite et al.) and ``ground`` (ROC) are surface-only,
so they skip the space fields. ``any`` returns all three, each location tagged with the method
that mines it -- which is what the Find / Blueprint tools use so a hand-only gem or an ROC spot
shows up, not just ship. A "Hand Mined Materials" contract instead passes ``hand`` to resolve
only its gems."""

from __future__ import annotations

from .body_mineables import locations_for as _body_locations_for
from .space_mineables import locations_for as _space_locations_for

# Ship laser (Prospector/MOLE), handheld multitool, ROC ground vehicle -- the order they list in.
_METHODS = ("ship", "hand", "ground")


def mine_locations(name: str, method: str = "ship") -> list:
    """``[{place, system, kind, method}]`` (field entries also carry ``rarity``, plus ``points`` --
    the real Lagrange points grouped by planet -- when the starmap knows them) for where ``name``
    is mined. ``method`` is ``ship``/``hand``/``ground`` for one context, or ``any`` to list them
    all (each location tagged with the method that mines it). Asteroid fields are ship-only (you
    can't hand/ROC an asteroid belt), so they appear for ``ship``/``any``. Spelling reconciles via
    ``mineables._mineral_key`` inside each reverse map."""
    methods = _METHODS if method == "any" else (method,)
    locs = []
    for m in methods:
        locs += [{"place": l["body"], "system": l["system"], "kind": "body", "method": m}
                 for l in _body_locations_for(name, method=m)]
    if method in ("ship", "any"):
        for l in _space_locations_for(name):
            loc = {"place": l["field"], "system": l["system"], "kind": "field",
                   "method": "ship", "rarity": l["rarity"]}
            if l.get("points"):
                loc["points"] = l["points"]
            locs.append(loc)
    return locs
