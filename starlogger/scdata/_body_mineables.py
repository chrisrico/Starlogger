"""Per-celestial-body mineables -- what each planet/moon yields, from the starmap.

The location side of the mining picture: where ``_mineables.py`` reads the *rock* (RS +
composition) and ``_mining_gear.py`` the *gear*, this reads *where in the 'verse* a given
mineral is found. The in-game starmap shows, per body, a description naming its mineable
materials -- and that text is the only place the body->mineral mapping lives (there is NO
structured resource field on any record).

**Driven off the localisation table (global.ini), NOT the StarMapObject records.** The
records are only an *index* of these descriptions, and an incomplete one: this DataCore
extract carries StarMapObject records for Stanton alone, while global.ini holds Pyro's body
descriptions too (and Pyro's planets list Copper, Stileron, …). Worse, several Stanton moons
(Yela, Lyria, Calliope, Clio) exist only under a ``,P``-suffixed localisation key, which a
record-driven ``@<Base>_Desc`` lookup silently misses. global.ini is the complete source, so
we scan it directly.

Each body description is a ``<base>_desc`` key (optionally with a ``,P``-style variant suffix)
whose value carries fixed section headers, each followed by ``\\n``-delimited items (the .ini
stores literal backslash-n, not real newlines):

    Potential Ship Mineables:            <- ship mining (Prospector/MOLE) -- the priority list
    Potential Ground Vehicle Mineables:  <- ROC mining (Pyro bodies)
    Potential Hand Mineables:            <- handheld / FPS mining
    Potential Harvestables:              <- gatherables
    Potential Creatures:                 <- fauna

Body name resolves from the sibling base key (``Stanton1`` -> "Hurston", ``Pyro5a_Ignis`` ->
"Ignis"); the system is the leading alphabetic run of the base key (``Stanton1`` -> Stanton,
``Pyro4`` -> Pyro). Items keep any parenthetical qualifier verbatim (``Janalite (Caves only)``).
Only bodies that list a ship or hand mineable are emitted.
"""

from __future__ import annotations

import re
import shutil

from ._p4k import ensure_binary, extract_localization, scratch_dir

# Description section header (lower-cased) -> our output key. Matched on an exact line.
_SECTIONS = {
    "potential ship mineables:": "ship_mineables",
    "potential ground vehicle mineables:": "ground_mineables",
    "potential hand mineables:": "hand_mineables",
    "potential harvestables:": "harvestables",
    "potential creatures:": "creatures",
}
_SECTION_KEYS = tuple(_SECTIONS.values())

# A body-description localisation key: "<base>_desc" with an optional ",P"-style variant
# suffix. load_localization lower-cases every key, so match against lower case. Captures base.
_DESC_KEY = re.compile(r"(.+?)_desc(,\w+)?$")

_SHIP_HEADER = "potential ship mineables:"


def _parse_sections(desc: str) -> dict:
    """Split a body description's prose into the mineable/harvest/creature lists.

    Walks the lines tracking the current section: an exact header line switches section, a
    blank line ends it, any other non-empty line is an item of the active section (leading
    prose before the first header has no active section, so it's ignored). The .ini stores
    literal ``\\n`` separators, so normalise those to real newlines first."""
    out: dict[str, list] = {v: [] for v in _SECTION_KEYS}
    cur: str | None = None
    for raw in desc.replace("\\n", "\n").split("\n"):
        line = raw.strip()
        key = _SECTIONS.get(line.lower())
        if key:
            cur = key
        elif not line:
            cur = None
        elif cur:
            out[cur].append(line)
    return out


def _system_from_base(base: str) -> str:
    """Star-system from a body's base key: the leading alphabetic run before the first
    digit (``stanton1`` -> Stanton, ``pyro5a_ignis`` -> Pyro). Falls back to the whole base."""
    m = re.match(r"([a-z]+)", base)
    return (m.group(1) if m else base).title()


def build_body_mineables(loc: dict) -> list:
    """Every celestial body that lists mineables -> ``{name, system, ship_mineables,
    ground_mineables, hand_mineables, harvestables, creatures, description}``, read from the
    localisation table (``global.ini``; ``load_localization`` lower-cases every key).

    Scans the body-description keys directly rather than the StarMapObject records (an
    incomplete index -- see the module docstring). When a body has both a plain ``_desc`` and
    a ``,P``-style variant, the plain key wins. ``description`` is kept raw (real newlines).
    Only bodies with a non-empty ship OR hand list are returned. Sorted by (system, name)."""
    # Pick one description value per body base, preferring the plain `_desc` over a `,P` variant.
    by_base: dict[str, str] = {}
    has_plain: set[str] = set()
    for key, value in loc.items():
        if _SHIP_HEADER not in value.lower():
            continue
        m = _DESC_KEY.match(key)
        if not m:
            continue
        base, suffix = m.group(1), m.group(2)
        if base in has_plain:
            continue                       # already hold the preferred plain key
        if not suffix:
            has_plain.add(base)
        by_base[base] = value

    bodies = []
    for base, value in by_base.items():
        desc = value.replace("\\n", "\n")
        sections = _parse_sections(desc)
        if not (sections["ship_mineables"] or sections["hand_mineables"]):
            continue
        name = (loc.get(base) or "").strip() or _system_from_base(base)
        bodies.append({
            "name": name,
            "system": _system_from_base(base),
            "ship_mineables": sections["ship_mineables"],
            "ground_mineables": sections["ground_mineables"],
            "hand_mineables": sections["hand_mineables"],
            "harvestables": sections["harvestables"],
            "creatures": sections["creatures"],
            "description": desc,
        })
    bodies.sort(key=lambda b: (b["system"], b["name"]))
    return bodies


def build_body_mineables_from_p4k(p4k: str, sb: str | None = None,
                                  progress=lambda m: None) -> list:
    """Extract just english ``global.ini`` from the local install and build the catalog.

    Unlike the rock/gear catalogs this needs only the localisation table (the body-mineable
    text lives there, not in a DataCore component), so it skips the heavy full ``dcb extract``
    -- a single-file ``p4k extract`` of ``global.ini`` (seconds)."""
    sb = sb or ensure_binary()
    workdir = scratch_dir("starlogger-bodymineables-")
    try:
        progress("extracting localization for body mineables")
        loc = extract_localization(p4k, sb, workdir)
        return build_body_mineables(loc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
