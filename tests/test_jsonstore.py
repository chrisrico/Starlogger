"""atomic_write: a temp-file + os.replace write, so a concurrent reader never sees a
half-written file. A failed serialization leaves the prior file intact (os.replace is
never reached); read_json degrades a missing/corrupt file to a default.

Run: python3 -m pytest tests/test_jsonstore.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger.jsonstore import atomic_write, read_json


def test_atomic_write_roundtrips(tmp_path):
    p = str(tmp_path / "store.json")
    atomic_write(p, {"b": 2, "a": 1})
    assert read_json(p) == {"a": 1, "b": 2}


def test_failed_write_leaves_prior_file_intact(tmp_path):
    p = str(tmp_path / "store.json")
    atomic_write(p, {"good": True})
    # a set isn't JSON-serializable: json.dump raises mid-write, before os.replace runs,
    # so the live file is never swapped for the half-written temp.
    with pytest.raises(TypeError):
        atomic_write(p, {"bad": {1, 2, 3}})
    assert read_json(p) == {"good": True}


def test_read_json_missing_or_corrupt_returns_default(tmp_path):
    missing = str(tmp_path / "nope.json")
    assert read_json(missing, default={"d": 1}) == {"d": 1}
    assert read_json(missing, default=dict) == {}        # callable default = fresh factory
    corrupt = str(tmp_path / "corrupt.json")
    with open(corrupt, "w", encoding="utf-8") as f:
        f.write("{ not valid json")
    assert read_json(corrupt, default=list) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
