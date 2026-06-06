"""Commodity + location reference data (names, categories, station codes)."""

from __future__ import annotations

import re
import shutil
import tempfile

from ..patterns import camel_split
from ._p4k import (
    _loc_text, ensure_binary, extract_localization, query_resource_types,
)


# Location CODE keys in global.ini map the same code shapes the log emits in
# `RequestLocationInventory Location[...]` to station names (RR_ARC_L1 -> "ARC-L1
# Wide Forest Station", Stanton2_Orison -> "Orison"). Keys are lower-cased by the
# loader. Restrict to real station/POI codes and drop sub-keys + non-place values.
_LOC_CODE = re.compile(
    r"^(rr_[a-z]{2,4}_(l\d_a|l\d|leo|heo|hub|gateway)|stanton\d+_[a-z][a-z0-9_]*"
    r"|pyro\d*_[a-z][a-z0-9_]*|(stanton_pyro|pyro_stanton)_jpstation"
    r"|nyx_[a-z][a-z0-9_]*|dfm_crusader_[a-z_]+)$")
_LOC_SUB = re.compile(
    r"(desc|clinic|addr|hint|long|short|marker|title|tip|name|info|sign|notif|comm"
    r"|greeting|terminal|elevator|hangar|kiosk)", re.I)
_LOC_PLACE = re.compile(
    r"(Station|Point|Harbor|Gateway|Hub|Depot|Outpost|Port|Spaceport|HEX|Babbage"
    r"|Area18|Orison|Lorville|Levski|Refueling|Service|Retreat|City|Platform)$|^[A-Z]{3}-L\d",
    re.I)


def build_location_names(loc: dict) -> dict:
    """{location_code(lower) -> station name}, mined from global.ini. The code shape
    matches the log's Location[...] codes, so it both resolves player locations and
    seeds the station-name autocomplete."""
    codes: dict[str, str] = {}
    for k, v in loc.items():
        if not _LOC_CODE.match(k) or _LOC_SUB.search(k):
            continue
        v = (v or "").strip()
        if not v or v.startswith("@") or len(v) > 46 or "=" in v:
            continue
        if _LOC_PLACE.search(v) or k.endswith("jpstation"):
            codes[k] = v
    return codes


def _group_category(g: dict, loc: dict) -> str:
    """A ResourceTypeGroup's display category (Metal, Gas, …). Prefers the localised
    ``displayName``; falls back to a camel-split of a name/tag token; '' when neither or
    when it's a dev placeholder (e.g. "<= PLACEHOLDER =>")."""
    cat = _loc_text(g.get("displayName"), loc)
    if not cat:
        tok = g.get("tag") or g.get("name") or g.get("groupName") or ""
        cat = camel_split(str(tok).replace("_", " ")).strip()
    return "" if "placeholder" in cat.lower() else cat


def _resource_maps(rec: dict, loc: dict) -> tuple[dict, set, dict]:
    """Walk a ResourceTypeDatabase record into ``(guid->name, {commodity names},
    guid->category)``. The category is the resource's immediate containing
    ResourceTypeGroup (a nested sub-group's name wins over its parent's for its own
    resources, e.g. a refined ore tagged 'Refined' rather than the parent 'Metal')."""
    guid_map: dict[str, str] = {}
    commodity_names: set[str] = set()
    commodity_types: dict[str, str] = {}

    def walk(g: dict, parent_cat: str) -> None:
        cat = _group_category(g, loc) or parent_cat
        for r in g.get("resources", []):
            guid = (r.get("_RecordId_") or "").lower()
            dn = r.get("displayName") or ""
            name = _loc_text(dn, loc)
            if not name:  # fall back to the record-name token
                rn = r.get("_RecordName_", "")
                tok = rn.split(".", 1)[1] if "." in rn else rn
                name = camel_split(tok.replace("_", " "))
            if guid:
                guid_map[guid] = name
                if cat:
                    commodity_types[guid] = cat
            if name and isinstance(dn, str) and dn.lower().startswith("@items_commodities"):
                commodity_names.add(name)
        for sub in g.get("groups", []):
            walk(sub, cat)

    for g in rec.get("_RecordValue_", {}).get("groups", []):
        walk(g, "")
    return guid_map, commodity_names, commodity_types


def build_reference_data(p4k: str, sb: str | None = None) -> dict:
    """Commodity + station reference data from the local p4k, in one pass: extract
    global.ini once, query the ResourceTypeDatabase, and return localized commodity
    names (guid->name + a clean trade-commodity list), the commodity category taxonomy
    (guid->category) and station names (code->name + a clean station list)."""
    sb = sb or ensure_binary()
    workdir = tempfile.mkdtemp(prefix="starlogger-ref-")
    try:
        loc = extract_localization(p4k, sb, workdir)
        rec = query_resource_types(p4k, sb)
        guid_map, commodity_names, commodity_types = _resource_maps(rec, loc)
        codes = build_location_names(loc)
        return {
            "commodities": guid_map,
            "commodity_names": sorted(commodity_names),
            "commodity_types": commodity_types,
            "categories": sorted(set(commodity_types.values())),
            "location_codes": codes,
            "station_names": sorted(set(codes.values())),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
