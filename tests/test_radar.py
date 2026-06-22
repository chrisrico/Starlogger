"""Ship-radar extraction (the resource-signature / RS mining stat), the per-ship radar slot,
and the catalog cache.

Run: .venv/bin/python -m pytest tests/test_radar.py

Fixtures are tiny synthetic DataCore records mirroring the real layout + field names (verified
against the live Data.p4k); see scdata._radar. The headline invariant pinned here is the
RS_CHANNEL: index 4 of signatureDetection is the mineable/resource channel, so the stock
Surveyor-Lite (0.8) reads strictly below a maxed radar (1.0).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import radar, ships, scdata
from starlogger.scdata._p4k import load_localization
from starlogger.scdata._ships import radar_slot as parse_radar_slot
from scdata_helpers import write_record


# --- fixture helpers -------------------------------------------------------- #
def _sig(sensitivity: float, piercing: float = 0.25) -> dict:
    return {"_Type_": "SCItemRadarSignatureDetection",
            "sensitivity": sensitivity, "piercing": piercing}


def _radar_value(size: int, name_key: str, mfr: str, rs: float,
                 rs_pierce: float = 1.0, ping: float = 2.5, grade: int = 3) -> dict:
    # 8-slot signatureDetection; index 4 (RS_CHANNEL) is the resource channel, 3 & 7 disabled.
    sd = [_sig(0.8), _sig(0.8), _sig(0.8), _sig(0.0),
          _sig(rs, rs_pierce), _sig(1.0), _sig(1.0), _sig(0.0)]
    return {"Components": [
        {"_Type_": "SAttachableComponentParams", "AttachDef": {
            "Type": "Radar", "SubType": "MidRangeRadar", "Size": size, "Grade": grade,
            "Manufacturer": f"file://./.../scitemmanufacturer/{mfr}.json",
            "Localization": {"_Type_": "SCItemLocalization", "Name": name_key}}},
        {"_Type_": "SCItemRadarComponentParams",
         "signatureDetection": sd,
         "pingProperties": {"_Type_": "SCItemRadarPingProperties", "cooldownTime": ping}},
    ]}


def _fixture_records(root: str) -> None:
    rdir = os.path.join(root, "libs/foundry/records/entities/scitem/ships/radar")
    # The stock mining radar (low RS), a maxed-RS radar, and the stealth Observer (lowest RS).
    write_record(os.path.join(rdir, "radr_chco_s01_surveyorlite.json"),
                 "EntityClassDefinition.RADR_CHCO_S01_SurveyorLite",
                 _radar_value(1, "@radar_surveyor", "chco", rs=0.8))
    write_record(os.path.join(rdir, "radr_wlop_s01_abetti.json"),
                 "EntityClassDefinition.RADR_WLOP_S01_Abetti",
                 _radar_value(1, "@radar_abetti", "wlop", rs=1.0))
    write_record(os.path.join(rdir, "radr_chco_s01_observerlite.json"),
                 "EntityClassDefinition.RADR_CHCO_S01_ObserverLite",
                 _radar_value(1, "@radar_observer", "chco", rs=0.6))
    # A size-2 radar (different slot) + a dev template that must be skipped.
    write_record(os.path.join(rdir, "radr_nave_s02_snsr6.json"),
                 "EntityClassDefinition.RADR_NAVE_S02_SNSR6",
                 _radar_value(2, "@radar_snsr6", "nave", rs=1.0))
    write_record(os.path.join(rdir, "radr_s01_template.json"),
                 "EntityClassDefinition.RADR_S01_Template",
                 _radar_value(1, "@radar_template", "chco", rs=1.0))

    loc_dir = os.path.join(root, "Data/Localization/english")
    os.makedirs(loc_dir, exist_ok=True)
    with open(os.path.join(loc_dir, "global.ini"), "w", encoding="utf-8") as f:
        f.write("radar_surveyor=Surveyor-Lite\n")
        f.write("radar_abetti=Abetti\n")
        f.write("radar_observer=Observer-Lite\n")
        f.write("radar_snsr6=SNS-R6\n")
        f.write("radar_template=Template\n")
        f.write("manufacturer_NameCHCO=Chimera Communications\n")
        f.write("manufacturer_NameWLOP=WillsOp\n")
        f.write("manufacturer_NameNAVE=Nav-E7 Gadgets\n")


def _build(tmp_path) -> list:
    root = str(tmp_path / "records")
    _fixture_records(root)
    return scdata.build_radar(root, load_localization(root))


# --- extraction: the RS channel (the pinned invariant) ---------------------- #
def test_rs_read_from_channel_4(tmp_path):
    by_class = {r["class"]: r for r in _build(tmp_path)}
    assert by_class["RADR_CHCO_S01_SurveyorLite"]["rs"] == 0.8
    assert by_class["RADR_WLOP_S01_Abetti"]["rs"] == 1.0
    assert by_class["RADR_CHCO_S01_ObserverLite"]["rs"] == 0.6
    # The headline mining fact: the stock Surveyor-Lite reads strictly below a maxed radar.
    assert by_class["RADR_CHCO_S01_SurveyorLite"]["rs"] < by_class["RADR_WLOP_S01_Abetti"]["rs"]


def test_radar_fields(tmp_path):
    by_class = {r["class"]: r for r in _build(tmp_path)}
    sl = by_class["RADR_CHCO_S01_SurveyorLite"]
    assert sl["name"] == "Surveyor-Lite"
    assert sl["manufacturer_code"] == "CHCO" and sl["manufacturer"] == "Chimera Communications"
    assert sl["size"] == 1 and sl["grade"] == 3 and sl["sub_type"] == "MidRangeRadar"
    assert sl["rs_piercing"] == 1.0 and sl["ping_cooldown"] == 2.5
    assert sl["sensitivity_max"] == 1.0   # the best channel, not the RS one
    assert by_class["RADR_WLOP_S01_Abetti"]["manufacturer"] == "WillsOp"


def test_template_skipped_and_ranked_best_first(tmp_path):
    radars = _build(tmp_path)
    classes = [r["class"] for r in radars]
    assert "RADR_S01_Template" not in classes                     # dev template excluded
    # best-for-mining first: rs desc -> the 1.0s before Surveyor (0.8) before Observer (0.6).
    s1 = [r for r in radars if r["size"] == 1]
    assert [r["rs"] for r in s1] == sorted((r["rs"] for r in s1), reverse=True)
    assert s1[-1]["class"] == "RADR_CHCO_S01_ObserverLite"        # lowest RS sorts last


# --- per-ship radar slot ---------------------------------------------------- #
def _loadout(root_class: str, *installs: str) -> str:
    lines = [f"EntityClassDefinition.{root_class} (root)"]
    lines += [f"  {cls} [hardpoint]" for cls in installs]
    return "\n".join(lines) + "\n"


def test_radar_slot_parse(tmp_path):
    txt = _loadout("MISC_Prospector", "RADR_CHCO_S01_SurveyorLite", "Mining_Laser_GRIN_Arbor_S1")
    assert parse_radar_slot("MISC_Prospector", txt) == {
        "size": 1, "stock": "radr_chco_s01_surveyorlite"}
    # MOLE carries a size-2 radar.
    mole = _loadout("ARGO_MOLE", "RADR_NAVE_S02_SNSR6")
    assert parse_radar_slot("ARGO_MOLE", mole)["size"] == 2
    # No radar in the block -> None.
    assert parse_radar_slot("MISC_Prospector", _loadout("MISC_Prospector", "Foo_Bar")) is None


def test_ships_radar_slot_accessor():
    db = {"ships": {"Prospector": {"radar": {"size": 1, "stock": "radr_chco_s01_surveyorlite"}},
                    "Hull A": {}}}
    assert ships.radar_slot("Prospector", None, db) == {
        "size": 1, "stock": "radr_chco_s01_surveyorlite"}
    assert ships.radar_slot("Hull A", None, db) is None       # ship with no radar record
    assert ships.radar_slot("Nope", None, db) is None         # unknown ship


# --- catalog cache round-trip ----------------------------------------------- #
def test_save_load_round_trip(tmp_path):
    radars = _build(tmp_path)
    path = str(tmp_path / "radar.json")
    radar.save_radar(radars, game_version="4.8.0", path=path)
    assert radar.radar_version(path) == "4.8.0"
    assert radar.radar_extract_version(path) == radar.EXTRACT_VERSION
    assert radar.radar_by_class("RADR_CHCO_S01_SurveyorLite", path)["rs"] == 0.8


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
