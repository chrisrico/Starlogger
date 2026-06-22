"""Per-celestial-body mineables: starmap-description parsing + the mineral->body reverse map.

Run: .venv/bin/python -m pytest tests/test_body_mineables.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import body_mineables, scdata
from starlogger.scdata._body_mineables import _parse_sections
from scdata_helpers import write_record

# The persistent full DataCore extract (gitignored); the integration test runs only when present.
_RECORDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "p4k", "records")


# --- parser (pure, no extract needed) --------------------------------------- #
def test_parse_sections_splits_headers_and_keeps_qualifiers():
    # The .ini stores LITERAL backslash-n separators (two chars), not real newlines.
    desc = ("Some flavour prose about the planet.\\n\\n"
            "Potential Ship Mineables:\\nAluminum\\nTin\\n\\n"
            "Potential Hand Mineables:\\nAphorite\\nJanalite (Caves only)\\n\\n"
            "Potential Harvestables:\\nPitambu\\n\\n"
            "Potential Creatures:\\nKopion")
    s = _parse_sections(desc.replace("\\n", "\n"))
    assert s["ship_mineables"] == ["Aluminum", "Tin"]
    # parenthetical qualifier kept verbatim; leading prose ignored (no active section)
    assert s["hand_mineables"] == ["Aphorite", "Janalite (Caves only)"]
    assert s["harvestables"] == ["Pitambu"]
    assert s["creatures"] == ["Kopion"]


# --- build_body_mineables against synthetic StarMapObject records ------------ #
def _starmap_record(root: str, system: str, body: str, name_key: str, desc_key: str) -> None:
    p = os.path.join(root, "libs/foundry/records/starmap/pu/system", system, body,
                     f"starmapobject.{body}.json")
    write_record(p, f"StarMapObject.{body}",
                 {"_Type_": "StarMapObject", "name": name_key, "description": desc_key})


def test_build_body_mineables_parses_records(tmp_path):
    root = str(tmp_path)
    _starmap_record(root, "teston", "teston1", "@TestOn1", "@TestOn1_Desc")
    # a body with no mineable sections (just prose) -> must be dropped
    _starmap_record(root, "teston", "teston2", "@TestOn2", "@TestOn2_Desc")
    # the system star (no description) -> dropped
    _starmap_record(root, "teston", "testonstar", "@TestOnStar", "@TestOnStar_Desc")

    loc = {
        "teston1": "Test Prime\xa0",   # trailing nbsp, like the real "Magda" record
        "teston1_desc": ("Prose.\\n\\nPotential Ship Mineables:\\nAluminum\\nTin\\n\\n"
                         "Potential Hand Mineables:\\nJanalite (Caves only)"),
        "teston2": "Barren Rock",
        "teston2_desc": "Nothing worth mining here.",
        "testonstar": "Test Star",
        # testonstar_desc intentionally absent
    }
    bodies = scdata.build_body_mineables(root, loc)

    assert len(bodies) == 1                       # only the body with sections survives
    b = bodies[0]
    assert b["name"] == "Test Prime"              # nbsp stripped
    assert b["system"] == "Teston"                # from the record path
    assert b["ship_mineables"] == ["Aluminum", "Tin"]
    assert b["hand_mineables"] == ["Janalite (Caves only)"]
    assert "\\n" not in b["description"]          # literal \n normalised to real newlines


# --- reverse map: mineral -> bodies, spelling-tolerant ---------------------- #
def _save_bodies(path: str) -> None:
    def body(name, system, ship):
        return {"name": name, "system": system, "ship_mineables": ship,
                "hand_mineables": [], "harvestables": [], "creatures": [], "description": ""}
    body_mineables.save_body_mineables([
        body("Hurston", "Stanton", ["Aluminum", "Tin", "Ouratite", "Quantainium"]),
        body("Daymar", "Stanton", ["Quartz", "Quantainium"]),
    ], game_version="4.8", path=path)
    body_mineables._cache["mtime"] = None        # force the mtime cache to re-read


def test_locations_for_reconciles_spelling(tmp_path):
    path = str(tmp_path / "body_mineables.json")
    _save_bodies(path)
    # body spelling "Aluminum" reconciles with the rock/blueprint spelling "Aluminium Ore"
    al = body_mineables.locations_for("Aluminium Ore", path=path)
    assert [(l["body"], l["system"]) for l in al] == [("Hurston", "Stanton")]
    # "Quantanium" (blueprint spelling) -> body "Quantainium"; both bodies, de-duped, in order
    qt = body_mineables.locations_for("Quantanium", path=path)
    assert [l["body"] for l in qt] == ["Hurston", "Daymar"]
    # unknown mineral -> empty
    assert body_mineables.locations_for("Unobtainium", path=path) == []


# --- integration: pin the real Hurston parse from the persistent extract ----- #
@pytest.mark.skipif(not os.path.isdir(_RECORDS), reason="no ./p4k/records extract")
def test_hurston_ship_mineables_from_extract():
    from starlogger.scdata._p4k import load_localization
    bodies = scdata.build_body_mineables(_RECORDS, load_localization(_RECORDS))
    hur = next(b for b in bodies if b["name"] == "Hurston")
    assert hur["ship_mineables"] == ["Aluminum", "Tin", "Ouratite", "Quantainium"]
    assert hur["system"] == "Stanton"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
