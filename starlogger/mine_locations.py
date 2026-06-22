"""Where-to-mine join: a mineral name -> the surface bodies + asteroid fields it's found on.

The one place the body (``body_mineables``) and space (``space_mineables``) reverse maps are
merged into the single inline ``locations`` list the dashboard renders (cyan body chips + amber
field chips -- ``web/mining.js`` ``locChips``). Shared so both the API endpoints (server.py) and
the per-mission snapshot (snapshot.py) join the same way without a circular import.

``method`` selects the body-mining context: ``ship`` (Prospector/MOLE, the default the Find /
Blueprint tools use) ALSO draws asteroid fields; ``hand`` (handheld cave gems -- Aphorite et al.)
and ``ground`` (ROC) are surface-only, so they skip the space fields. This is what lets a
"Hand Mined Materials" contract resolve its gems while a "Ship Mined Ore" contract resolves ores."""

from __future__ import annotations

from .body_mineables import locations_for as _body_locations_for
from .space_mineables import locations_for as _space_locations_for


def mine_locations(name: str, method: str = "ship") -> list:
    """``[{place, system, kind}]`` (field entries also carry ``rarity``) for where ``name`` is
    mined under ``method``. Bodies come from the method-matched list; asteroid fields are added
    only for ``ship`` (you can't hand-mine an asteroid belt). Spelling reconciles via
    ``mineables._mineral_key`` inside each reverse map."""
    locs = [{"place": l["body"], "system": l["system"], "kind": "body"}
            for l in _body_locations_for(name, method=method)]
    if method == "ship":
        locs += [{"place": l["field"], "system": l["system"], "kind": "field", "rarity": l["rarity"]}
                 for l in _space_locations_for(name)]
    return locs
