"""Log line patterns and the small decoders that turn raw log/contract strings
into human-friendly values. No state here -- pure parsing helpers."""

from __future__ import annotations

import re


def camel_split(s: str) -> str:
    """Insert a space at each lowercase->uppercase boundary ("PortOlisar" -> "Port
    Olisar"). Acronym runs are left intact by design ("FPSWeapons" stays as-is); the
    blueprint category decoder in scdata uses its own stricter, acronym-aware split."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)


# --------------------------------------------------------------------------- #
# Log line patterns
# --------------------------------------------------------------------------- #

TS = re.compile(r"^<(?P<ts>[0-9T:\-.]+Z)>")

ACCEPTED = re.compile(
    r'Added notification "Contract Accepted:\s*(?P<title>.*?)"\s*\[\d+\]'
    r".*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\]"
)
COMPLETE_NOTE = re.compile(
    r'Added notification "Contract Complete:\s*(?P<title>.*?)"\s*\[\d+\]'
    r".*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\]"
)
FAILED_NOTE = re.compile(
    r'Added notification "Contract Failed:\s*(?P<title>.*?)"\s*\[\d+\]'
    r".*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\]"
)
ABANDONED_NOTE = re.compile(
    r'Added notification "Contract (?:Abandoned|Cancelled|Canceled):\s*(?P<title>.*?)"\s*\[\d+\]'
    r".*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\]"
)
MARKER = re.compile(
    r"Creating objective marker:\s*missionId\s*\[(?P<mid>[0-9a-f-]+)\],\s*"
    r"generator name\s*\[(?P<gen>[^\]]*)\],\s*contract\s*\[(?P<contract>[^\]]*)\],\s*"
    r"contractDefinitionId\[(?P<cdef>[^\]]*)\],\s*objectiveId\s*\[(?P<oid>[^\]]*)\],\s*"
    r"markerEntityId\s*\[(?P<meid>\d+)\],\s*zoneHostId\s*\[(?P<zone>\d+)\],\s*"
    r"position\s*\[x:\s*(?P<x>[-0-9.]+),\s*y:\s*(?P<y>[-0-9.]+),\s*z:\s*(?P<z>[-0-9.]+)\]"
)
# "New Objective: Deliver 0/77 SCU of Quartz to Seraphim Station: "
DELIVER = re.compile(
    r'Added notification "New Objective:\s*Deliver\s*(?P<have>\d+)/(?P<need>\d+)\s*SCU '
    r"of\s*(?P<cargo>[A-Za-z][A-Za-z ]*?)\s*to\s*(?P<loc>[^:\"]+?)\s*:"
    r'\s*"\s*\[\d+\].*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\],\s*ObjectiveId:\s*\[(?P<oid>[^\]]*)\]'
)
# Defensive: pickup objectives are normally "Collect N SCU of X from Y".
COLLECT = re.compile(
    r'Added notification "New Objective:\s*Collect\s*(?P<have>\d+)/(?P<need>\d+)\s*SCU '
    r"of\s*(?P<cargo>[A-Za-z][A-Za-z ]*?)\s*from\s*(?P<loc>[^:\"]+?)\s*:"
    r'\s*"\s*\[\d+\].*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\],\s*ObjectiveId:\s*\[(?P<oid>[^\]]*)\]'
)
OBJ_UPSERT = re.compile(
    r"ObjectiveUpserted push message for:\s*mission_id\s*(?P<mid>[0-9a-f-]+)\s*-\s*"
    r"objective_id\s*(?P<oid>\S+)\s*-\s*state\s*MISSION_OBJECTIVE_STATE_(?P<state>\w+)"
)
# MINING contracts (Shubin purchase orders) state their requirement differently from hauling:
# "New Objective: 0/15 of Aphorite: " -- a bare count of an ore, no SCU/station. The digits
# immediately after "New Objective:" (no "Deliver"/"Collect"/"SCU") keep this from matching the
# hauling DELIVER/COLLECT above. ObjectiveId is empty in these, so callers key by ore name.
MINING_ORE = re.compile(
    r'Added notification "New Objective:\s*(?P<have>\d+)/(?P<need>\d+)\s+of\s+'
    r'(?P<ore>[A-Za-z][A-Za-z ]*?)\s*:\s*"\s*\[\d+\].*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\]'
)
# "New Objective: Collect and deliver one of the following:: " -- marks the ore objectives as
# alternatives (any single one satisfies the contract).
MINING_ANY = re.compile(
    r'Added notification "New Objective:\s*Collect and deliver one of the following:'
    r'.*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\]'
)
# "New Objective: Go to HDMS-Perlman: " -- the navigate-to-the-dig-site start step.
MINING_GOTO = re.compile(
    r'Added notification "New Objective:\s*Go to\s+(?P<loc>[^:"]+?)\s*:\s*"\s*\[\d+\]'
    r'.*?MissionId:\s*\[(?P<mid>[0-9a-f-]+)\]'
)
# Player's current location (where the client requests its own inventory), e.g.
#   <RequestLocationInventory> Player[Name] requested inventory for Location[Stanton2_Orison]
# The code is "<System><index>_<Place>" (Place may itself contain underscores);
# vaguer orbital codes like "RR_CRU_LEO" decode to None and are ignored.
PLAYER_LOCATION = re.compile(
    r"RequestLocationInventory>\s*Player\[(?P<player>[^\]]+)\]\s*"
    r"requested inventory for\s*Location\[(?P<loc>[^\]]+)\]"
)
_LOC_SYS_PREFIX = re.compile(r"^[A-Za-z]+\d+[a-z]?$")  # "Stanton2", "Stanton2a"
# 3-letter body code (also the Lagrange prefixes) for vaguer orbital codes
_LOC_BODY_CODE = {"CRU": "Crusader", "HUR": "Hurston", "ARC": "ArcCorp", "MIC": "microTech"}


def decode_location(code: str) -> tuple[str | None, bool]:
    """Decode a Location[...] code into (name, is_station).

    Prefers the p4k station catalog (authoritative: "RR_ARC_L1" -> "ARC-L1 Wide Forest
    Station"); falls back to the structural heuristic when the code isn't catalogued.

    "Stanton2_Orison" -> ("Orison", True)   — a precise station.
    "RR_CRU_LEO"       -> ("Crusader", False) — only the body (orbital region).
    unrecognized       -> (None, False).
    """
    from . import reference  # lazy: reference imports scdata which imports this module's siblings
    named = reference.resolve_code(code)
    if named:
        return (named, True)
    parts = code.split("_")
    if len(parts) >= 2 and _LOC_SYS_PREFIX.match(parts[0]):
        place = " ".join(parts[1:]).strip()
        if place:
            # codes may be CamelCase ("PortTressler") or already spaced; split case
            # boundaries so the name matches the planner's station table.
            return (camel_split(place), True)
    for p in parts:
        if p.upper() in _LOC_BODY_CODE:
            return (_LOC_BODY_CODE[p.upper()], False)
    return (None, False)
MISSION_ENDED = re.compile(
    r"MissionEnded push message for:\s*mission_id\s*(?P<mid>[0-9a-f-]+)\s*-\s*"
    r"mission_state\s*MISSION_STATE_(?P<state>\w+)"
)
END_MISSION = re.compile(
    r"Ending mission for player\.\s*MissionId\[(?P<mid>[0-9a-f-]+)\]\s*"
    r"Player\[(?P<player>[^\]]*)\].*?CompletionType\[(?P<ctype>[^\]]*)\]\s*"
    r"Reason\[(?P<reason>[^\]]*)\]"
)
AWARD = re.compile(r'Added notification "Awarded\s*(?P<amt>\d+)\s*aUEC')

# A crafting blueprint the player acquired -- the HUD notification "Received Blueprint:
# <name>: " on the mission/comms bus. Anchor on the SHUDEvent "Added notification" form so
# the UI-lifecycle echoes (UpdateNotificationItem ... Action: Next/StartFade/Remove) and the
# bare quoted re-print don't double-count: this matches exactly once per acquisition. The
# trailing ": " before the closing quote is an empty count field. (This message type isn't in
# docs/game-log-catalog.md -- it predates the catalog.) Locked by tests/test_blueprints_acquired.py.
BLUEPRINT_RECEIVED = re.compile(
    r'Added notification "Received Blueprint:\s*(?P<name>.*?)\s*:\s*"\s*\[\d+\]'
)

# Manual commodity-terminal trades (NOT mission cargo). The game logs the player
# pressing Buy/Sell at a trade kiosk via CEntityComponentCommodityUIProvider. The
# request line is the only record -- there's no settle/confirm follow-up -- so a
# parsed trade is "submitted", treated as effective. SCU is taken from the box
# data (boxSize x unitAmount); the `quantity[...]` field is inconsistent (cSCU on
# buy, SCU on sell). The commodity is logged only as a `resourceGUID`; its name is
# resolved from the game's ResourceTypeDatabase (see starlogger/reference.py).
#   ...SendCommodityBuyRequest> ... shopName[SCShop_...] ... price[1067040.000000]
#      ... resourceGUID[35121003-...] ... quantity[28800.000000 cSCU]
#      Cargo Box Data: boxSize[16.000000] | unitAmount[18] ...
#   ...SendCommoditySellRequest> ... shopName[SCShop_...] ... amount[793520.000000]
#      ... resourceGUID[9e65a7bd-...] ... Cargo Box Data:  [boxSize[16] | unitAmount[14]] ...
# The SCU-from-box rule is locked by tests/test_patterns.py::test_trade_buy_sell_capture_box_not_quantity
# and tests/test_trades.py (parse + idempotent refeed).
_BOX = r".*?boxSize\[(?P<box>[0-9.]+)\]\s*\|\s*unitAmount\[(?P<units>\d+)\]"
TRADE_BUY = re.compile(
    r"SendCommodityBuyRequest>.*?shopName\[(?P<shop>[^\]]*)\].*?kioskId\[(?P<kiosk>\d+)\].*?"
    r"price\[(?P<auec>[0-9.]+)\].*?resourceGUID\[(?P<guid>[0-9a-fA-F-]+)\]" + _BOX
)
TRADE_SELL = re.compile(
    r"SendCommoditySellRequest>.*?shopName\[(?P<shop>[^\]]*)\].*?kioskId\[(?P<kiosk>\d+)\].*?"
    r"amount\[(?P<auec>[0-9.]+)\].*?resourceGUID\[(?P<guid>[0-9a-fA-F-]+)\]" + _BOX
)
# A trade line only carries the shop NAME ("SCShop_Admin_lt_base_g" -> "Admin", which
# isn't a station); the kiosk ENTITY name carries the place ("…CommodityKiosk_kiosk_
# cordys_2_a-015" -> "Cordys"). It's logged separately, tied to the trade by kioskId.
KIOSK_BIND = re.compile(r"CommodityKiosk_(?P<ent>[A-Za-z0-9_-]+?)\s*\[(?P<kid>\d+)\]")

# Shop names are entity codes ("SCShop_ht_delta_shubin_m_store"), not station names
# (and on a different id namespace than mission zoneHostIds, so the station-name map
# can't resolve them). Strip the SCShop_ wrapper + size/tech noise tokens to a
# readable best-effort label ("Shubin", "Admin"); the raw code is kept alongside.
_SHOP_NOISE = {"scshop", "ht", "lt", "mt", "m", "s", "g", "l", "delta", "alpha",
               "beta", "gamma", "store", "shop", "base", "stand", "kiosk", "terminal",
               "standard", "lowtech", "commoditykiosk", "softlock", "a", "b", "c"}


def _clean_place(code: str, noise: set) -> list[str]:
    """Split a shop/kiosk entity code into meaningful place tokens: drop the noise
    words, single letters, and pure-digit segments ("kiosk_cordys_2_a-015" -> ["cordys"])."""
    toks = [t for t in re.split(r"[_\s-]+", code or "") if t]
    return [t for t in toks if t.lower() not in noise and len(t) > 1 and not t.isdigit()]


def friendly_shop(shop: str) -> str:
    """Best-effort readable label for a commodity-shop entity code."""
    keep = _clean_place(shop, _SHOP_NOISE)
    keep = keep or [t for t in re.split(r"[_\s-]+", shop or "") if t and t.lower() != "scshop"]
    return " ".join(w.capitalize() for w in keep) or (shop or "Trade terminal")


def friendly_kiosk(ent: str) -> str:
    """Place name from a kiosk entity suffix ("kiosk_cordys_2_a-015" -> "Cordys"); ''
    when nothing meaningful remains."""
    return " ".join(w.capitalize() for w in _clean_place(ent, _SHOP_NOISE))

# Login/logout boundary: gamerules SC_Frontend (menu) vs SC_Default (in the PU).
SESSION = re.compile(r'eCVS_InGame.*?gamerules="(?P<gr>SC_\w+)"')

# Clean quit-to-desktop / client close. Unlike a logout it never passes through the
# SC_Frontend (main-menu) boundary, so it's the only in-log end marker for that path.
# Verified to appear at most once per session and always as the final log line, so
# acting on it can't wipe a still-running session. (A hard kill / alt-F4 writes
# nothing -- the log just stops -- and stays uncatchable until the next launch.)
SHUTDOWN = re.compile(r"CCIGBroker::FastShutdown")

# Quantum travel. The route calc names a friendly START location and the destination
# (an internal code); arrival marks the jump complete. Both carry the ship entity.
#   …RSI_Hermes_<eid>[<eid>]|CSCItemNavigation::CalculateRoute|Projected Start Location
#     is Stanton Gateway for route to destination pyro3 …[QuantumTravel]
#   …RSI_Hermes_<eid>[…]|CSCItemNavigation::OnQuantumDriveArrived|Quantum Drive has arrived…
# The ship token is bounded ({0,63}) so the lazy match can't backtrack quadratically: the `_`
# lives in BOTH the char class and the literal `_\d+[..]` separator, so an unbounded `+?` over a
# long hostile line is O(n^2). A real ship entity token is short, so the cap is invisible to
# matching but makes the worst case linear. (Backstopped by a line-length cap in State.feed.)
QT_ROUTE = re.compile(
    r"(?P<ship>[A-Za-z][A-Za-z0-9_]{0,63}?)_\d+\[\d+\]\|CSCItemNavigation::CalculateRoute\|"
    r"Projected Start Location is (?P<frm>.+?) for route to destination (?P<to>\S+)"
)
QT_ARRIVED = re.compile(
    r"(?P<ship>[A-Za-z][A-Za-z0-9_]{0,63}?)_\d+\[\d+\]\|CSCItemNavigation::OnQuantumDriveArrived"
)
# Same CalculateRoute event, second line: the QT fuel estimate for the jump.
QT_FUEL = re.compile(
    r"(?P<ship>[A-Za-z][A-Za-z0-9_]{0,63}?)_\d+\[\d+\]\|CSCItemNavigation::CalculateRoute\|"
    r"Successfully calculated route to (?P<to>\S+) fuel estimate (?P<fuel>[0-9.]+)"
)


def qt_system(code: str) -> str:
    """Classify a quantum destination code by system (for a travel tag). Inter-system
    crossings (jump points) get "Jump Point"; otherwise the home system, best-effort."""
    c = re.sub(r"\.\w+$|_?\{[^}]*\}", "", code).lower()
    if re.search(r"(pyro|stan|stanton|terra|nyx|sol)-(pyro|stan|stanton|terra|nyx|sol)", c) \
            or "_jp" in c or c.endswith("jpstation"):
        return "Jump Point"
    if "pyro" in c:
        return "Pyro"
    if "nyx" in c:
        return "Nyx"
    if "terra" in c:
        return "Terra"
    if re.search(r"stan|_s\d|cru|hur|arc|mic", c):
        return "Stanton"
    return ""

_ROMAN = {"1": "I", "2": "II", "3": "III", "4": "IV", "5": "V", "6": "VI", "7": "VII"}


_SYS = {"pyro": "Pyro", "stan": "Stanton", "stanton": "Stanton", "terra": "Terra",
        "nyx": "Nyx", "sol": "Sol", "magnus": "Magnus", "castra": "Castra"}


def decode_qt_dest(code: str) -> str:
    """Best-effort friendly name for a quantum destination code. The start location is
    already named in the log; destinations are internal codes (``rs_ext_pyro5_l2`` ->
    "Pyro V L2", ``pyro3`` -> "Pyro III", ``pyro-stan_jp1`` -> "Pyro–Stanton Jump Point",
    ``Rayari_Cluster_001_Frost_{guid}.socpak`` -> "Rayari Cluster 001 Frost")."""
    c = re.sub(r"\.\w+$", "", code)             # strip a .socpak/.entity suffix
    c = re.sub(r"_?\{[^}]*\}", "", c)           # strip a {guid}
    c = re.sub(r"^(loc_|rs_ext_)+", "", c, flags=re.I)
    # inter-system jump point: "<sysA>-<sysB>[_jpN]"
    jp = re.fullmatch(r"([a-z]+)-([a-z]+)(?:_jp\d*)?", c, re.I)
    if jp:
        a = _SYS.get(jp.group(1).lower(), jp.group(1).capitalize())
        b = _SYS.get(jp.group(2).lower(), jp.group(2).capitalize())
        return f"{a}–{b} Jump Point"
    out: list[str] = []
    for p in c.split("_"):
        pl = p.lower()
        m = re.fullmatch(r"(pyro|stanton|stan)(\d)?", pl)
        if m:
            sysn = {"pyro": "Pyro", "stanton": "Stanton", "stan": "Stanton"}[m.group(1)]
            out.append(f"{sysn} {_ROMAN[m.group(2)]}" if m.group(2) else sysn)
        elif re.fullmatch(r"l\d", pl) or pl in ("leo", "heo"):
            out.append(p.upper())
        elif pl.startswith("jp"):
            out.append("Jump Point " + pl[2:])
        elif pl == "rr":
            continue
        else:
            out.append(p.upper() if len(p) <= 2 else p.capitalize())
    return " ".join(x for x in out if x).strip() or code


# Game version header: "Branch: sc-alpha-4.8.0-hotfix" + "Changelist: 11875683".
VERSION = re.compile(r"Branch:.*?(\d+\.\d+(?:\.\d+)?)")
CHANGELIST = re.compile(r"Changelist:\s*(\d+)")

# Ship detection. Comms channel join names the ship; Vehicle Control Flow ties a
# ship entity to the local client (authoritative).
SHIP_CHANNEL = re.compile(r"joined channel '(?P<ship>[^':]+?)\s*:\s*(?P<player>[^']+)'")
# Multicrew: a ship's comms channel is named '<Ship> : <Owner>'. The self HUD
# notification ("You have joined/left ...") is the one signal that names a ship you
# BOARDED as crew (Owner != you) — the driver-only Vehicle Control Flow never does.
CHANNEL_JOIN = re.compile(r"You have joined channel '(?P<ship>[^':]+?)\s*:\s*(?P<owner>[^']+)'")
CHANNEL_LEAVE = re.compile(r"You have left(?: the)? channel '(?P<ship>[^':]+?)\s*:\s*(?P<owner>[^']+)'")
VEHICLE_CTRL = re.compile(
    r"Vehicle Control Flow>\s*\w+::(?P<act>SetDriver|ClearDriver):\s*Local client node "
    r"\[\d+\][^']*'(?P<ent>[A-Za-z][A-Za-z0-9_]+?)_\d+'"
)
# A salvageable wreck spawning at the salvage site: the game registers each unmanned-salvage
# hull's resource host, whose token is `<BASE_SHIP_CLASS>_Unmanned_Salvage_<numericEntityId>`
# (e.g. "Host  :AEGS_Gladius_Unmanned_Salvage_387873708417"). The line carries NO MissionId,
# so this only says "that wreck is out there", not which contract -- see state._salvage_spawn /
# model.SalvageTarget. `base` keeps the variant suffix (CRUS_Starlifter_C2); it keys straight
# into salvage_ships.json. The ship token is bounded ({0,63}) like QT_ROUTE so the lazy match
# can't backtrack quadratically on a hostile line (State.feed's 64 KB cap is the other backstop).
SALVAGE_SPAWN = re.compile(
    r"AddHostedNode\b.*?\bHost\s*:\s*"
    r"(?P<base>[A-Za-z][A-Za-z0-9_]{0,63}?)_Unmanned_Salvage_(?P<eid>\d+)"
)

_TAG = re.compile(r"<[^>]+>")  # strip <EM4> ... </EM4> markup from titles


# --------------------------------------------------------------------------- #
# Decoders
# --------------------------------------------------------------------------- #


def clean_title(s: str) -> str:
    s = _TAG.sub("", s)
    s = s.replace("[BP]*", "").replace("*", "")
    return re.sub(r"\s+", " ", s).strip(" :")


def norm_bp_name(name: str) -> str:
    """Normalize a blueprint name for matching log "Received Blueprint:" acquisitions to
    the static catalog: collapse internal whitespace and lowercase. The single source of
    this normalization (state, acquired, and the server all route through it) so a name
    like "Antium Arms Moss Camo " (trailing space) matches the catalog entry."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


# A graded-variant prefix the game stamps on some blueprint notifications that the catalog
# name lacks: "Ind/3/C Surveyor-Max" -> "Surveyor-Max", "Sth/2/A Spicule" -> "Spicule",
# "S00 Hofstede" -> "Hofstede". Stripped as a fallback when the full name misses the catalog.
_BP_GRADE_PREFIX = re.compile(r"^(?:[A-Za-z]{1,4}/\d+/[A-Za-z0-9]+|S\d+)\s+")


def strip_bp_grade(name: str) -> str:
    """Drop a leading grade-code prefix from a blueprint name (see _BP_GRADE_PREFIX)."""
    return _BP_GRADE_PREFIX.sub("", name or "")


def classify_end(*tokens: str | None, default: str = "completed") -> str:
    """Map free-form CompletionType / Reason / mission_state text to a status.
    Substring match, since the game spells these several ways (Abandon/Abandoned)."""
    blob = " ".join(t for t in tokens if t).lower()
    if "abandon" in blob or "cancel" in blob or "forfeit" in blob:
        return "abandoned"
    if "expire" in blob:
        return "expired"
    if "fail" in blob:
        return "failed"
    if "complet" in blob or "success" in blob:
        return "completed"
    return default


def major_version(v: str | None) -> str:
    """Major.minor of a version string (4.8.0 -> 4.8); '' for unknown."""
    if not v:
        return ""
    m = re.match(r"(\d+\.\d+)", v)
    return m.group(1) if m else v


# --- contract id decoding --------------------------------------------------- #

_STRUCTURE = {
    "AToB": "A → B",
    "SingleToMulti2": "1 → 2 drops",
    "SingleToMulti3": "1 → 3 drops",
    "SingleToMulti4": "1 → 4 drops",
    "SingleToMulti": "1 → many",
    "Multi2ToSingle": "2 → 1 drop",
    "Multi4ToSingle": "4 → 1 drop",
    "MultiToSingle": "many → 1",
}


# High-level contract "kind" for the Archive's Contract Log filter. Cargo hauls are
# their own bucket (the bulk of the data); everything else is split by keyword over the
# org/title/contract strings. Order matters: hauling wins, then the more specific
# bounty/combat keywords, then delivery, else Other. Keep the JS mirror in app.js
# (contractType) in sync — sessions whose logbackup is gone fall back to it.
_COMBAT_WORDS = (
    "bounty", "bounties", "eliminate", "kill", "destroy", "defeat", "mercenary",
    "merc ", "security", "defend", "defence", "defense", "assault", "attack",
    "combat", "pirate", "raid", "ambush", "score", "wanted", "hostile", "strike",
)
_DELIVERY_WORDS = (
    "deliver", "delivery", "courier", "transport", "package", "parcel", "dossier",
    "retrieve", "recover", "fetch", "files", "data heist", "investigate", "smuggl",
)


def classify_contract(contract: str = "", org: str = "", title: str = "",
                      is_trade: bool = False) -> str:
    """A mission's high-level kind: 'Hauling' (cargo haul), 'Bounty / Combat',
    'Delivery', or 'Other'. Keyword scan over org+title+contract; hauling wins."""
    if is_trade:
        return "Hauling"
    hay = f"{org} {title} {contract}".lower()
    if any(w in hay for w in _COMBAT_WORDS):
        return "Bounty / Combat"
    if any(w in hay for w in _DELIVERY_WORDS):
        return "Delivery"
    return "Other"


def decode_contract(raw: str) -> dict:
    out: dict = {"structure": None, "category": None, "grade": None}
    for k, v in _STRUCTURE.items():
        if k in raw:
            out["structure"] = v
            break
    for cat in ("RefinedOre", "RawOre", "NonMetal", "Waste", "Processed", "Agricultural"):
        if cat in raw:
            out["category"] = re.sub(r"(?<!^)(?=[A-Z])", " ", cat)
            break
    m = re.search(r"(Supply|Small|Medium|Large)Grade(\d*)", raw)
    if m:
        out["grade"] = m.group(1) + " Grade" + (f" {m.group(2)}" if m.group(2) else "")
    return out


# Commodity name fragments as they appear in contract ids, longest-first so a
# greedy scan splits compound tokens (PressIceProcFood -> Pressurized Ice +
# Processed Food). Recovers cargo *type* when the game omits the objective text.
_COMMODITY_ATOMS = [
    ("PressurizedIce", "Pressurized Ice"), ("PressurisedIce", "Pressurized Ice"),
    ("PressIce", "Pressurized Ice"),
    ("ProcessedFood", "Processed Food"), ("ProcFood", "Processed Food"),
    ("MedicalSupplies", "Medical Supplies"), ("AgriculturalSupplies", "Agricultural Supplies"),
    ("ScrapWaste", "Scrap"), ("ScrapMetal", "Scrap"), ("Scrap", "Scrap"),
    ("Waste", "Waste"), ("Stims", "Stims"),
    ("Aluminium", "Aluminum"), ("Aluminum", "Aluminum"), ("Titanium", "Titanium"),
    ("Corundum", "Corundum"), ("Quartz", "Quartz"), ("Silicon", "Silicon"),
    ("Carbon", "Carbon"), ("Tin", "Tin"), ("Hydrogen", "Hydrogen"),
    ("Chlorine", "Chlorine"), ("Fluorine", "Fluorine"), ("Iodine", "Iodine"),
    ("Ammonia", "Ammonia"), ("Tungsten", "Tungsten"), ("Copper", "Copper"),
    ("Iron", "Iron"), ("Gold", "Gold"), ("Diamond", "Diamond"),
    ("Agricium", "Agricium"), ("Laranite", "Laranite"), ("Bexalite", "Bexalite"),
    ("Hephaestanite", "Hephaestanite"), ("Taranite", "Taranite"), ("Borase", "Borase"),
    ("Beryl", "Beryl"), ("Aphorite", "Aphorite"), ("Dolivine", "Dolivine"),
]

# Canonical commodity display names for editor autocomplete. The atoms above plus
# a few that only appear in objective text (no contract-id token), so the
# auto-complete list is reasonably complete even before the log mentions them.
COMMODITY_NAMES = sorted(
    {disp for _, disp in _COMMODITY_ATOMS}
    | {
        "Distilled Spirits", "Fresh Food", "Nitrogen", "Hydrogen Fuel",
        "Quantum Fuel", "Astatine", "Helium", "Methane", "Neon", "Omnaprop",
        "Stileron", "Ranta Dung", "Golden Medmon", "Revenant Tree Pollen",
        "Widow", "SLAM", "Maze", "Altruciatoxin",
        "Compboard", "Recycled Material Composite", "Ship Ammunition",
    }
)

_CARGO_TOKEN = re.compile(
    r"_(?:RefinedOre|RawOre|NonMetal|Metal|Waste|Processed|Agricultural|Gas)"
    r"_(?:Mixed_)?([A-Za-z]+?)_Stanton"
)


def decode_cargo_from_contract(raw: str) -> list[str]:
    """Best-effort list of commodities a contract carries, from its id string."""
    m = _CARGO_TOKEN.search(raw)
    if not m:
        return []
    token = m.group(1)
    found: list[str] = []
    i = 0
    while i < len(token):
        for frag, disp in _COMMODITY_ATOMS:
            if token.startswith(frag, i):
                if disp not in found:
                    found.append(disp)
                i += len(frag)
                break
        else:
            i += 1
    return found


# --- ship names ------------------------------------------------------------- #

_MANUFACTURERS = {
    "CRUS": "Crusader", "DRAK": "Drake", "MISC": "MISC", "RSI": "RSI",
    "AEGS": "Aegis", "ANVL": "Anvil", "ARGO": "Argo", "ORIG": "Origin",
    "BANU": "Banu", "CNOU": "Consolidated Outland", "GRIN": "Greycat",
    "MRAI": "Mirai", "TMBL": "Tumbril", "XIAN": "Xi'an", "XNAA": "Xi'an",
}


_MFR_NAMES = set(_MANUFACTURERS.values())


def friendly_ship(entity: str) -> str:
    """Display name for a log vehicle entity class (e.g. ``MISC_Freelancer``). Prefers
    the localised name from the cargo database (built from the game files); falls back
    to a manufacturer-code split before that database exists (e.g. first run)."""
    from . import ships  # lazy: ships imports this module
    name = ships.ship_display_name(entity)
    if name:
        return name
    parts = entity.split("_")
    mfr = _MANUFACTURERS.get(parts[0], parts[0]) if parts else entity
    model = " ".join(parts[1:]) if len(parts) > 1 else entity
    return f"{mfr} {model}".strip()


def canonical_ship_name(name: str) -> str:
    """Normalize a display ship name to the canonical one used everywhere else.

    The comms-channel ship name carries the manufacturer prefix ("Crusader Mercury
    Star Runner", "MISC Freelancer MAX") while seat detection yields the bare model
    ("Mercury Star Runner", "Freelancer MAX"), so the same ship would be recorded
    twice. Strip a leading manufacturer word (or two, e.g. "Consolidated Outland")
    when the remainder is a known ship; otherwise return the name unchanged."""
    from . import ships  # lazy: ships imports this module
    name = (name or "").strip()
    known = ships.known_ship_names()
    if name in known:
        return name
    parts = name.split()
    for n in (2, 1):
        if len(parts) > n and " ".join(parts[:n]) in _MFR_NAMES:
            rest = " ".join(parts[n:])
            if rest in known:
                return rest
    return name


def resolve_ship_name(name: str) -> str | None:
    """Resolve a comms-channel ship name ("<Manufacturer> <Model>", e.g. "Crusader C1
    Spirit") to its cargo-DB display name — or None when it isn't a ship channel at all
    (party / global / mission comms have no manufacturer prefix). Used to detect a ship
    boarded as crew. More forgiving than `canonical_ship_name`: after stripping the
    manufacturer it also matches the model word-order-insensitively, so a channel name
    whose word order differs from the DB's still resolves. Falls back to the stripped
    model for a real (manufacturer-prefixed) ship the DB doesn't carry."""
    from . import ships  # lazy: ships imports this module
    name = (name or "").strip()
    known = ships.known_ship_names()
    if name in known:
        return name
    parts = name.split()
    strip_n = next((n for n in (2, 1)
                    if len(parts) > n and " ".join(parts[:n]) in _MFR_NAMES), 0)
    if not strip_n:                       # no manufacturer prefix -> not a ship channel
        return None
    model = " ".join(parts[strip_n:])
    if model in known:
        return model
    toks = frozenset(w.lower() for w in parts[strip_n:])
    hits = [k for k in known if frozenset(w.lower() for w in k.split()) == toks]
    return hits[0] if len(hits) == 1 else (model or None)
