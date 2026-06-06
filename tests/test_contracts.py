"""Contract template taxonomy + cargo-manifest extraction and the contract-id decode.

The static taxonomy per ContractTemplate is its SHAPE -- which `extendedTextToken`s it
carries (a string token name; its value is runtime-bound) and the `illegal` flag -- not
the grade/SCU numbers, which the records leave uninitialised. Verified against a real
4.8.0 extract.

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


def _prop(token: str) -> dict:
    """One MissionProperty, named by its (string) extendedTextToken. The value struct is
    runtime-bound in the real records, so the parser ignores it."""
    return {"_Type_": "MissionProperty", "extendedTextToken": token,
            "value": {"_Type_": "MissionPropertyValue_StringHash", "options": []}}


def _template(tokens: list, illegal: bool = False) -> dict:
    return {"_Type_": "ContractTemplate",
            "contractDisplayInfo": {"_Type_": "ContractDisplayInfo", "illegal": illegal},
            "contractProperties": [_prop(t) for t in tokens]}


def _manifest(parts: list) -> dict:
    """A CargoManifest value: (resource record name, probability) pairs."""
    return {"_Type_": "CargoManifest", "cargoFillCapacity": {
        "_Type_": "CargoFillCapacityValue_Random",
        "resources": [{"_Type_": "CargoResource",
                       "resource": {"_RecordName_": rn}, "probability": pr}
                      for rn, pr in parts]}}


def _fixture_contracts(root: str) -> None:
    tmpl = os.path.join(root, "libs/foundry/records/contracts/contracttemplates")
    cm = os.path.join(root, "libs/foundry/records/cargomanifest")

    _write(os.path.join(tmpl, "haulcargo_atob.json"),
           "ContractTemplate.HaulCargo_AtoB",
           _template(["Contractor", "CargoRouteToken", "CargoGradeToken",
                      "MissionMaxSCUSize", "ReputationRank"]))
    _write(os.path.join(tmpl, "haulcargo_singletomulti4_waste.json"),
           "ContractTemplate.HaulCargo_SingleToMulti4_Waste",
           _template(["CargoGradeToken", "MissionMaxSCUSize", "SingleToMultiToken"],
                     illegal=True))
    # a non-cargo template (no route/grade/scu tokens) — still parses, flags all False
    _write(os.path.join(tmpl, "assistshipincombat.json"),
           "ContractTemplate.AssistShipInCombat", _template(["TargetName"]))

    _write(os.path.join(cm, "mixedcargo_generic.json"),
           "CargoManifest.MixedCargo_Generic",
           _manifest([("ResourceType.Nitrogen", 0.25),
                      ("ResourceType.Processed_Food", 0.6)]))
    _write(os.path.join(cm, "illegalcargo_generic.json"),
           "CargoManifest.IllegalCargo_Generic",
           _manifest([("ResourceType.Slam_Unprocessed", 1.0)]))


def test_build_contract_taxonomy_captures_shape(tmp_path):
    root = str(tmp_path)
    _fixture_contracts(root)
    rows = scdata.build_contract_taxonomy(root, {})
    by_t = {r["template"]: r for r in rows}
    assert len(rows) == 3

    a = by_t["HaulCargo_AtoB"]
    assert a["route"] == "A → B"          # from CargoRouteToken
    assert a["graded"] is True            # has CargoGradeToken
    assert a["scu_sized"] is True         # has MissionMaxSCUSize
    assert a["rep_gated"] is True         # has ReputationRank
    assert a["illegal"] is False

    b = by_t["HaulCargo_SingleToMulti4_Waste"]
    assert b["route"] == "1 → many"       # from SingleToMultiToken
    assert b["rep_gated"] is False
    assert b["illegal"] is True

    c = by_t["AssistShipInCombat"]        # non-cargo: no route, all flags False
    assert c["route"] is None
    assert c["graded"] is False and c["scu_sized"] is False


def test_build_cargo_manifests_resolves_resources(tmp_path):
    root = str(tmp_path)
    _fixture_contracts(root)
    mans = {m["manifest"]: m for m in scdata.build_cargo_manifests(root, {})}
    assert set(mans) == {"MixedCargo_Generic", "IllegalCargo_Generic"}

    mixed = mans["MixedCargo_Generic"]["resources"]
    assert [r["commodity"] for r in mixed] == ["Nitrogen", "Processed Food"]
    assert mixed[0]["probability"] == 0.25


def _save_catalog(path: str) -> None:
    contracts.save_contracts(
        templates=[
            {"template": "HaulCargo_AtoB_Bulk_PressIce", "route": "A → B", "graded": True,
             "scu_sized": True, "rep_gated": True, "illegal": False},
            {"template": "HaulCargo_SingleToMulti4_Bulk_Waste", "route": "1 → many",
             "graded": True, "scu_sized": True, "rep_gated": False, "illegal": True},
        ],
        cargo_manifests=[{"manifest": "IllegalCargo_Generic",
                          "resources": [{"commodity": "Slam", "probability": 1.0}]}],
        game_version="4.8", path=path)
    contracts._cache["mtime"] = None   # force re-read of the new file


def test_decode_matches_template_over_runtime_suffix(tmp_path):
    path = str(tmp_path / "contracts.json")
    _save_catalog(path)
    # a live contract id carries the template name plus a runtime location/loop suffix
    dec = contracts.decode("HaulCargo_AtoB_Bulk_PressIce_Stanton_RR_ARC_L1", path=path)
    assert dec == {"legal": True, "route": "A → B"}   # legal flag is authoritative

    illegal = contracts.decode("HaulCargo_SingleToMulti4_Bulk_Waste_Pyro", path=path)
    assert illegal["legal"] is False
    assert illegal["route"] == "1 → many"


def test_decode_unknown_is_empty(tmp_path):
    path = str(tmp_path / "contracts.json")
    _save_catalog(path)
    assert contracts.decode("BountyHunter_VeryHard_Stanton", path=path) == {}
    assert contracts.decode("", path=path) == {}


def test_catalog_and_version_accessors(tmp_path):
    path = str(tmp_path / "contracts.json")
    _save_catalog(path)
    assert contracts.contracts_version(path=path) == "4.8"
    assert len(contracts.catalog(path=path)) == 2
    assert contracts.cargo_manifests(path=path)[0]["manifest"] == "IllegalCargo_Generic"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
