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

# The persistent full DataCore extract (gitignored); the integration test runs only when present.
_RECORDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "p4k", "records")


# --- parser (pure) ---------------------------------------------------------- #
def test_parse_sections_splits_headers_and_keeps_qualifiers():
    # The .ini stores LITERAL backslash-n separators (two chars), not real newlines.
    desc = ("Some flavour prose about the planet.\\n\\n"
            "Potential Ship Mineables:\\nAluminum\\nTin\\n\\n"
            "Potential Ground Vehicle Mineables:\\nBeradon\\n\\n"
            "Potential Hand Mineables:\\nAphorite\\nJanalite (Caves only)\\n\\n"
            "Potential Harvestables:\\nPitambu\\n\\n"
            "Potential Creatures:\\nKopion")
    s = _parse_sections(desc.replace("\\n", "\n"))
    assert s["ship_mineables"] == ["Aluminum", "Tin"]
    assert s["ground_mineables"] == ["Beradon"]          # ROC section (Pyro bodies)
    # parenthetical qualifier kept verbatim; leading prose ignored (no active section)
    assert s["hand_mineables"] == ["Aphorite", "Janalite (Caves only)"]
    assert s["harvestables"] == ["Pitambu"]
    assert s["creatures"] == ["Kopion"]


# --- build_body_mineables off the localisation table ------------------------ #
def test_build_body_mineables_from_loc():
    loc = {
        "stanton1": "Hurston",
        "stanton1_desc": ("Prose.\\n\\nPotential Ship Mineables:\\nAluminum\\nTin\\n\\n"
                          "Potential Hand Mineables:\\nJanalite (Caves only)"),
        # ONLY a ,P-variant key exists -- a record-driven @Stanton2c_Desc lookup would MISS this.
        "stanton2c": "Yela",
        "stanton2c_desc,p": ("Prose.\\n\\nPotential Ship Mineables:\\nQuartz\\nQuantainium\\n\\n"
                             "Potential Hand Mineables:\\nAphorite"),
        # Pyro body: no StarMapObject record in the extract, recovered from loc; has a ROC section.
        "pyro1": "Pyro I",
        "pyro1_desc": ("Prose.\\n\\nPotential Ship Mineables:\\nCopper\\n\\n"
                       "Potential Ground Vehicle Mineables:\\nBeradon\\n\\n"
                       "Potential Hand Mineables:\\nAphorite"),
        # a non-body description (no mineable sections) -> dropped
        "grimhex_desc": "Just a station, nothing mineable here.",
    }
    bodies = scdata.build_body_mineables(loc)
    by = {b["name"]: b for b in bodies}
    assert set(by) == {"Hurston", "Yela", "Pyro I"}

    assert by["Hurston"]["system"] == "Stanton"          # system from the key prefix
    assert by["Hurston"]["ship_mineables"] == ["Aluminum", "Tin"]
    assert by["Hurston"]["hand_mineables"] == ["Janalite (Caves only)"]
    assert "\\n" not in by["Hurston"]["description"]     # literal \n normalised

    # the ,P-variant body is recovered (the bug the brief warned about)
    assert by["Yela"]["ship_mineables"] == ["Quartz", "Quantainium"]
    # Pyro recovered without any record; system from prefix + ROC section captured
    assert by["Pyro I"]["system"] == "Pyro"
    assert by["Pyro I"]["ship_mineables"] == ["Copper"]
    assert by["Pyro I"]["ground_mineables"] == ["Beradon"]


def test_plain_desc_wins_over_p_variant():
    loc = {
        "stanton1": "Hurston",
        "stanton1_desc": "Plain.\\n\\nPotential Ship Mineables:\\nAluminum",
        "stanton1_desc,p": "Variant.\\n\\nPotential Ship Mineables:\\nGold",
    }
    bodies = scdata.build_body_mineables(loc)
    assert len(bodies) == 1
    assert bodies[0]["ship_mineables"] == ["Aluminum"]   # plain key preferred over ,P


# --- reverse map: mineral -> bodies, spelling-tolerant ---------------------- #
def _save_bodies(path: str) -> None:
    def body(name, system, ship):
        return {"name": name, "system": system, "ship_mineables": ship, "ground_mineables": [],
                "hand_mineables": [], "harvestables": [], "creatures": [], "description": ""}
    body_mineables.save_body_mineables([
        body("Hurston", "Stanton", ["Aluminum", "Tin", "Ouratite", "Quantainium"]),
        body("Daymar", "Stanton", ["Quartz", "Quantainium"]),
        body("Pyro I", "Pyro", ["Iron", "Copper", "Tin", "Stileron"]),
    ], game_version="4.8", path=path)
    body_mineables._cache["mtime"] = None        # force the mtime cache to re-read


def test_locations_for_reconciles_spelling(tmp_path):
    path = str(tmp_path / "body_mineables.json")
    _save_bodies(path)
    # body spelling "Aluminum" reconciles with the rock/blueprint spelling "Aluminium Ore"
    al = body_mineables.locations_for("Aluminium Ore", path=path)
    assert [(l["body"], l["system"]) for l in al] == [("Hurston", "Stanton")]
    # "Quantanium" (blueprint spelling) -> body "Quantainium"; both Stanton bodies, in order
    qt = body_mineables.locations_for("Quantanium", path=path)
    assert [l["body"] for l in qt] == ["Hurston", "Daymar"]
    # copper now spans systems (Pyro), not "only Euterpe"
    assert body_mineables.locations_for("Copper", path=path) == [{"body": "Pyro I", "system": "Pyro"}]
    # unknown mineral -> empty
    assert body_mineables.locations_for("Unobtainium", path=path) == []


# --- integration: pin the real coverage from the persistent extract ---------- #
@pytest.mark.skipif(not os.path.isdir(_RECORDS), reason="no ./p4k/records extract")
def test_body_mineables_from_extract_full_coverage():
    from starlogger.scdata._p4k import load_localization
    bodies = scdata.build_body_mineables(load_localization(_RECORDS))
    by = {b["name"]: b for b in bodies}

    # the Hurston parse (the original pin)
    assert by["Hurston"]["ship_mineables"] == ["Aluminum", "Tin", "Ouratite", "Quantainium"]
    assert by["Hurston"]["system"] == "Stanton"
    # full coverage: 14 Stanton + 11 Pyro = 25 bodies across two systems
    assert len(bodies) == 25
    assert sorted({b["system"] for b in bodies}) == ["Pyro", "Stanton"]
    # the ,P-variant Stanton bodies are recovered
    assert {"Yela", "Lyria", "Calliope", "Clio"} <= set(by)
    # Pyro recovered from loc despite no StarMapObject records
    assert by["Terminus"]["system"] == "Pyro"
    assert "Copper" in by["Terminus"]["ship_mineables"]
    # copper is NOT only on Euterpe -- it spans 6 bodies in both systems
    copper = {b["name"] for b in bodies if "Copper" in b["ship_mineables"]}
    assert copper == {"Lyria", "Clio", "Euterpe", "Pyro I", "Pyro IV", "Terminus"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
