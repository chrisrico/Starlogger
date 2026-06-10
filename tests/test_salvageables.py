"""Salvageable-wreck RS extraction and the salvage reverse-lookup.

Run: .venv/bin/python -m pytest tests/test_salvageables.py  (or plain `python tests/test_salvageables.py`)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import salvageables, scdata
from scdata_helpers import write_record


# --- tiny fixture mirroring the real DataCore record layout ----------------- #
def _write(path: str, record_name: str, rs: float) -> None:
    write_record(path, record_name, {"Components": [
        {"_Type_": "SSCSignatureSystemParams",
         "radarProperties": {"baseSignatureParams": {
             "signatures": [0.0, 0.0, 0.0, 0.0, rs, 0.0, 0.0, 0.0]}}},
    ]})


def _fixture_records(root: str) -> None:
    # CamelCase filenames, as StarBreaker writes them -- exercises the case-robust glob.
    ents = os.path.join(root, "libs/foundry/records/entities/scsalvageable")
    cases = [
        # whole-ship structural hulls -- RS identifies the ship
        ("SalvageableDebris_AvengerTitan", 1700.0),
        ("SalvageableDebris_C2", 2400.0),
        ("SalvageableDebris_890", 3000.0),
        ("SalvageableDebris_Redeemer", 2000.0),     # shares base 2000 with panels
        # ship-debris panels -- all flat 2000 regardless of size label
        ("SalvageableRepairable_ShipDebris_M_Avenger_Wing_a", 2000.0),
        ("SalvageableRepairable_ShipDebris_XL_Jav_Panel_b", 2000.0),
        ("SalvageableRepairable_ShipDebris_S_Vangaurd_Tail", 2000.0),
        # dropped: bare generic, test, templates, mission/level props
        ("SalvageableDebris", 1700.0),
        ("SalvageableDebris_test", 1700.0),
        ("Salvageable_TEMPLATE", 0.0),
        ("SalvageableRepairable_Delving_Pipe", 100.0),
        ("SalvageableRepairable_Door_Pipe_Rockcracker", 100.0),
    ]
    for cls, rs in cases:
        _write(os.path.join(ents, cls.lower() + ".json"),
               "EntityClassDefinition." + cls, rs)


def test_build_salvageables_keeps_only_targets(tmp_path):
    root = str(tmp_path)
    _fixture_records(root)
    wrecks = scdata.build_salvageables(root)
    by_cls = {w["class"]: w for w in wrecks}

    # 4 ship hulls + 3 panels; every prop/template/test/generic is dropped.
    assert len(wrecks) == 7
    assert "SalvageableDebris" not in by_cls           # bare generic
    assert "SalvageableDebris_test" not in by_cls
    assert "Salvageable_TEMPLATE" not in by_cls         # RS 0 anyway
    assert not any("Pipe" in c for c in by_cls)         # mission/level props


def test_build_salvageables_ship_and_panel_fields(tmp_path):
    root = str(tmp_path)
    _fixture_records(root)
    by_cls = {w["class"]: w for w in scdata.build_salvageables(root)}

    avenger = by_cls["SalvageableDebris_AvengerTitan"]
    assert avenger == {"class": "SalvageableDebris_AvengerTitan", "name": "Avenger Titan",
                       "rs": 1700, "kind": "ship", "ship": "Avenger Titan"}
    assert by_cls["SalvageableDebris_C2"]["name"] == "C2"
    assert by_cls["SalvageableDebris_890"]["name"] == "890 Jump"

    panel = by_cls["SalvageableRepairable_ShipDebris_M_Avenger_Wing_a"]
    assert panel["kind"] == "panel"
    assert panel["rs"] == 2000
    assert panel["size"] == "M"
    assert panel["ship"] == "Avenger"
    assert panel["part"] == "Wing"            # trailing "_a" variant dropped
    assert panel["name"] == "Avenger Wing (M)"
    # the data's "Vangaurd" typo is normalised
    assert by_cls["SalvageableRepairable_ShipDebris_S_Vangaurd_Tail"]["ship"] == "Vanguard"


def test_salvage_lookup_ship_vs_panel(tmp_path):
    path = str(tmp_path / "salvageables.json")
    _fixture_records(str(tmp_path / "recs"))
    salvageables.save_salvageables(scdata.build_salvageables(str(tmp_path / "recs")), path=path)

    # 1700 -> the Avenger Titan hull only
    av = salvageables.salvage_lookup(1700, path=path)
    assert len(av) == 1 and av[0]["kind"] == "ship" and av[0]["count"] == 1
    assert "Avenger Titan" in av[0]["label"]

    # 2000 -> BOTH the Redeemer hull and a single debris panel (shared base)
    both = salvageables.salvage_lookup(2000, path=path)
    kinds = {c["kind"] for c in both}
    assert kinds == {"ship", "panel"}
    panel = next(c for c in both if c["kind"] == "panel")
    assert panel["count"] == 1 and panel["label"] == "1 ship-debris panel"

    # 6000 -> 3 debris panels (3 x 2000); the Redeemer hull also reads as 3
    six = salvageables.salvage_lookup(6000, path=path)
    panel6 = next(c for c in six if c["kind"] == "panel")
    assert panel6["count"] == 3 and panel6["label"] == "3 ship-debris panels"

    # an off-grid reading matches nothing
    assert salvageables.salvage_lookup(1234, path=path) == []


def test_salvage_signatures(tmp_path):
    path = str(tmp_path / "salvageables.json")
    _fixture_records(str(tmp_path / "recs"))
    salvageables.save_salvageables(scdata.build_salvageables(str(tmp_path / "recs")), path=path)
    assert salvageables.salvage_signatures(path=path) == [1700, 2000, 2400, 3000]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
