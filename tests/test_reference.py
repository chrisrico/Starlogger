"""Commodity category taxonomy (T1): the ResourceTypeDatabase group walk that tags each
commodity with its category, plus the reference.json persist/accessor round-trip.

Run: .venv/bin/python -m pytest tests/test_reference.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import reference, scdata


def _rec() -> dict:
    """A trimmed ResourceTypeDatabase record: top-level category groups, one with a
    nested sub-group whose (more specific) name should win for its own resources."""
    return {"_RecordValue_": {"groups": [
        {"displayName": "@resource_group_metal",
         "resources": [{"_RecordId_": "GUID-IRON", "displayName": "@items_commodities_iron",
                        "_RecordName_": "ResourceType.Iron"}],
         "groups": [
             {"displayName": "@resource_group_refined",
              "resources": [{"_RecordId_": "GUID-STILERON",
                             "displayName": "@items_commodities_stileron",
                             "_RecordName_": "ResourceType.Stileron"}]}]},
        {"displayName": "@resource_group_gas",
         "resources": [{"_RecordId_": "GUID-HYDROGEN", "displayName": "@items_commodities_hydrogen",
                        "_RecordName_": "ResourceType.Hydrogen"}]},
    ]}}


_LOC = {"resource_group_metal": "Metal", "resource_group_refined": "Refined",
        "resource_group_gas": "Gas", "items_commodities_iron": "Iron",
        "items_commodities_stileron": "Stileron", "items_commodities_hydrogen": "Hydrogen"}


def test_resource_maps_tags_commodities_with_category():
    guid_map, names, types = scdata._resource_maps(_rec(), _LOC)
    assert guid_map == {"guid-iron": "Iron", "guid-stileron": "Stileron",
                        "guid-hydrogen": "Hydrogen"}
    assert names == {"Iron", "Stileron", "Hydrogen"}
    # the nested sub-group's name wins for its own resource (Stileron -> Refined, not Metal)
    assert types == {"guid-iron": "Metal", "guid-stileron": "Refined",
                     "guid-hydrogen": "Gas"}


def test_save_reference_persists_categories_and_accessors_lowercase(tmp_path):
    path = str(tmp_path / "reference.json")
    reference.save_reference(
        {"GUID-IRON": "Iron"}, {},
        commodity_types={"GUID-IRON": "Metal", "GUID-HYDROGEN": "Gas"},
        game_version="4.8", path=path)
    reference._cache["mtime"] = None  # force re-read

    # keys lowercased on load, mirroring load_commodities
    assert reference.commodity_types(path=path) == {"guid-iron": "Metal", "guid-hydrogen": "Gas"}
    assert reference.commodity_categories(path=path) == ["Gas", "Metal"]


def test_categories_absent_when_not_built(tmp_path):
    path = str(tmp_path / "reference.json")
    reference.save_reference({"GUID-IRON": "Iron"}, {}, game_version="4.8", path=path)
    reference._cache["mtime"] = None
    assert reference.commodity_types(path=path) == {}
    assert reference.commodity_categories(path=path) == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
