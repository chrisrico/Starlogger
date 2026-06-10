"""Shared helper for the scdata extractor tests (mineables, mining_gear, contracts,
blueprints, salvageables): write one fake DataCore record JSON in the
{_RecordName_, _RecordValue_} shape the StarBreaker extract produces."""
from __future__ import annotations

import json
import os


def write_record(path: str, record_name: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"_RecordName_": record_name, "_RecordValue_": value}, f)
