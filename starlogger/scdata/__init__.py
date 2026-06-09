"""Read game data straight from the local Data.p4k via StarBreaker (see ._p4k).

Split from the former single scdata.py module into a package by extraction job; this
re-exports the public API so ``from . import scdata`` / ``scdata.X`` callers are unchanged.
"""
from ._p4k import SB_VERSION, find_p4k
from ._ships import build_ships
from ._reference import build_reference_data, _resource_maps
from ._mineables import build_mineables, build_mineables_from_p4k
from ._mining_gear import build_mining_gear, build_mining_gear_from_p4k
from ._blueprints import build_blueprints, build_blueprints_from_p4k
from ._contracts import (
    build_contract_taxonomy, build_contract_generators, build_cargo_manifests,
    build_contracts_from_p4k,
)
from ._music import (
    build_music_from_p4k, scan_songs, select_full_songs, dump_music_hirc, FULL_SONG_MIN_DUR,
)
from ._music_context import (
    build_context_labels, context_for_media, track_context,
    best_song_ids, is_quality_song, load_allowlist,
)

__all__ = [
    "SB_VERSION", "find_p4k", "build_ships",
    "build_reference_data", "_resource_maps",
    "build_mineables", "build_mineables_from_p4k",
    "build_mining_gear", "build_mining_gear_from_p4k",
    "build_blueprints", "build_blueprints_from_p4k",
    "build_contract_taxonomy", "build_contract_generators", "build_cargo_manifests",
    "build_contracts_from_p4k",
    "build_music_from_p4k", "scan_songs", "select_full_songs", "dump_music_hirc",
    "FULL_SONG_MIN_DUR",
    "build_context_labels", "context_for_media", "track_context",
    "best_song_ids", "is_quality_song", "load_allowlist",
]
