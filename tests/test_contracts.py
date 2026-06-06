"""Contract template taxonomy + cargo-manifest extraction and the contract-id decode.

Run: .venv/bin/python -m pytest tests/test_contracts.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import contracts, scdata


# --- tiny fixture mirroring the real DataCore record layout ----------------- #
def _write(path: str, record_name: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"_RecordName_": record_name, "_RecordValue_": value}, f)


def _prop(token: str, data) -> dict:
    """One MissionProperty whose extendedTextToken names a structured field."""
    return {"_Type_": "MissionProperty",
            "extendedTextToken": {"value": token, "data": data}}


def _template(props: list, illegal: bool = False) -> dict:
    return {"_Type_": "ContractTemplate",
            "contractDisplayInfo": {"_Type_": "ContractDisplayInfo", "illegal": illegal},
            "contractProperties": props}


def _manifest(parts: list) -> dict:
    """A CargoManifest value: (resource record name, probability) pairs."""
    return {"_Type_": "CargoManifest", "cargoFillCapacity": {
        "resources": [{"_Type_": "CargoResource",
                       "resource": {"_RecordName_": rn}, "probability": pr}
                      for rn, pr in parts]}}


def _fixture_contracts(root: str) -> None:
    tmpl = os.path.join(root, "libs/foundry/records/contracts/contracttemplates")
    cm = os.path.join(root, "libs/foundry/records/cargomanifest")

    _write(os.path.join(tmpl, "haulcargo_atob_bulk_pressice.json"),
           "ContractTemplate.HaulCargo_AtoB_Bulk_PressIce",
           _template([
               _prop("CargoGradeToken", "Bulk"),
               _prop("MissionMaxSCUSize", "32"),
               _prop("CargoRouteToken", "AToB"),
               _prop("ReputationRank", "Renown1"),
               _prop("Contractor", "AgriCorp"),
           ]))
    _write(os.path.join(tmpl, "haulcargo_singletomulti4_bulk_waste.json"),
           "ContractTemplate.HaulCargo_SingleToMulti4_Bulk_Waste",
           _template([
               _prop("CargoGradeToken", "Bulk"),
               _prop("MissionMaxSCUSize", "16"),
               _prop("SingleToMultiToken", "SingleToMulti4"),
           ], illegal=True))
    # a non-cargo template (no SCU/grade tokens) — must still parse, fields just empty
    _write(os.path.join(tmpl, "assistshipincombat.json"),
           "ContractTemplate.AssistShipInCombat", _template([]))

    _write(os.path.join(cm, "mixedcargo_generic_highvalue_scraps.json"),
           "CargoManifest.MixedCargo_Generic_HighValue_Scraps",
           _manifest([("ResourceType.Scrap_Metal", 0.8),
                      ("ResourceType.Electronic_Scrap", 0.6)]))
    _write(os.path.join(cm, "illegalcargo_generic.json"),
           "CargoManifest.IllegalCargo_Generic",
           _manifest([("ResourceType.WiDoW", 1.0)]))


def test_build_contract_taxonomy_parses_tokens(tmp_path):
    root = str(tmp_path)
    _fixture_contracts(root)
    rows = scdata.build_contract_taxonomy(root, {})
    by_t = {r["template"]: r for r in rows}
    assert len(rows) == 3

    a = by_t["HaulCargo_AtoB_Bulk_PressIce"]
    assert a["grade"] == "Bulk"
    assert a["scu_cap"] == 32          # numeric token coerced to int
    assert a["route"] == "AToB"
    assert a["rep_rank"] == "Renown1"
    assert a["contractor"] == "AgriCorp"
    assert a["illegal"] is False

    b = by_t["HaulCargo_SingleToMulti4_Bulk_Waste"]
    assert b["scu_cap"] == 16
    assert b["single_to_multi"] == "SingleToMulti4"
    assert b["illegal"] is True

    c = by_t["AssistShipInCombat"]   # no cargo tokens -> all None, still listed
    assert c["grade"] is None and c["scu_cap"] is None


def test_build_cargo_manifests_resolves_resources(tmp_path):
    root = str(tmp_path)
    _fixture_contracts(root)
    mans = {m["manifest"]: m for m in scdata.build_cargo_manifests(root, {})}
    assert set(mans) == {"MixedCargo_Generic_HighValue_Scraps", "IllegalCargo_Generic"}

    scraps = mans["MixedCargo_Generic_HighValue_Scraps"]["resources"]
    assert [r["commodity"] for r in scraps] == ["Scrap Metal", "Electronic Scrap"]
    assert scraps[0]["probability"] == 0.8


def _save_catalog(path: str) -> None:
    contracts.save_contracts(
        templates=[
            {"template": "HaulCargo_AtoB_Bulk_PressIce", "grade": "Bulk", "scu_cap": 32,
             "route": "AToB", "single_to_multi": None, "rep_rank": "Renown1",
             "contractor": "AgriCorp", "illegal": False},
            {"template": "HaulCargo_SingleToMulti4_Bulk_Waste", "grade": "Bulk", "scu_cap": 16,
             "route": None, "single_to_multi": "SingleToMulti4", "rep_rank": None,
             "contractor": None, "illegal": True},
        ],
        cargo_manifests=[{"manifest": "IllegalCargo_Generic",
                          "resources": [{"commodity": "WiDoW", "probability": 1.0}]}],
        game_version="4.8", path=path)
    contracts._cache["mtime"] = None   # force re-read of the new file


def test_decode_matches_template_over_runtime_suffix(tmp_path):
    path = str(tmp_path / "contracts.json")
    _save_catalog(path)
    # a live contract id carries the template name plus a runtime location/loop suffix
    dec = contracts.decode("HaulCargo_AtoB_Bulk_PressIce_Stanton_RR_ARC_L1", path=path)
    assert dec["grade"] == "Bulk"
    assert dec["scu_cap"] == 32
    assert dec["route"] == "AToB"
    assert dec["rep_rank"] == "Renown1"
    assert dec["legal"] is True        # illegal=False -> legal True

    illegal = contracts.decode("HaulCargo_SingleToMulti4_Bulk_Waste_Pyro", path=path)
    assert illegal["legal"] is False
    assert illegal["scu_cap"] == 16


def test_decode_longest_match_wins_and_unknown_is_empty(tmp_path):
    path = str(tmp_path / "contracts.json")
    _save_catalog(path)
    assert contracts.decode("BountyHunter_VeryHard_Stanton", path=path) == {}
    assert contracts.decode("", path=path) == {}
    # decode drops None-valued fields so the merge in model.decoded is clean
    dec = contracts.decode("HaulCargo_SingleToMulti4_Bulk_Waste_Pyro", path=path)
    assert "route" not in dec and "rep_rank" not in dec


def test_catalog_and_version_accessors(tmp_path):
    path = str(tmp_path / "contracts.json")
    _save_catalog(path)
    assert contracts.contracts_version(path=path) == "4.8"
    assert len(contracts.catalog(path=path)) == 2
    assert contracts.cargo_manifests(path=path)[0]["manifest"] == "IllegalCargo_Generic"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
