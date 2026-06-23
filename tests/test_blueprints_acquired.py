"""Acquired-blueprint parsing + persistence.

Run: .venv/bin/python -m pytest tests/test_blueprints_acquired.py

The game emits a HUD notification when the player receives a crafting blueprint; we parse it
(patterns.BLUEPRINT_RECEIVED), accumulate per session (State._blueprint), and fold every
session's haul into a cumulative file (acquired.merge_acquired). The catalog names base items
while the log sometimes prefixes a graded variant, so acquired.resolve_owned reconciles the two.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import acquired, patterns
from starlogger.state import State

# Real log lines (ids intact). One acquisition logs THREE shapes; only the SHUDEvent
# "Added notification" line is the acquisition event — the others are UI noise.
SHUD = ('<2026-06-22T06:33:18.674Z> [Notice] <SHUDEvent_OnNotification> Added notification '
        '"Received Blueprint: Lawson Mining Laser: " [340] to queue. New queue size: 3, '
        'MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] '
        '[Team_CoreGameplayFeatures][Missions][Comms]\n')
LIFECYCLE = ('<2026-06-22T06:33:30.226Z> [Notice] <UpdateNotificationItem> Notification '
             '"Received Blueprint: Lawson Mining Laser: " [340], Action: Next '
             '[Team_CoreGameplayFeatures][Missions][Comms]\n')
BARE = '<2026-06-22T06:33:18.674Z>    "Received Blueprint: Lawson Mining Laser: " [340]\n'
# Grade-prefixed variant ("Ind/3/C Surveyor-Max") and a trailing-space name ("Antium Arms Moss Camo ").
SHUD_GRADED = ('<2026-06-22T06:46:22.351Z> [Notice] <SHUDEvent_OnNotification> Added notification '
               '"Received Blueprint: Ind/3/C Surveyor-Max: " [355] to queue. New queue size: 3, '
               'MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] '
               '[Team_CoreGameplayFeatures][Missions][Comms]\n')
SHUD_TRAILSPACE = ('<2026-06-03T23:29:00.000Z> [Notice] <SHUDEvent_OnNotification> Added notification '
                   '"Received Blueprint: Antium Arms Moss Camo : " [161] to queue. New queue size: 1, '
                   'MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] '
                   '[Team_CoreGameplayFeatures][Missions][Comms]\n')


# --- pattern --------------------------------------------------------------------------- #
def test_pattern_matches_acquire_notification_only():
    assert patterns.BLUEPRINT_RECEIVED.search(SHUD).group("name") == "Lawson Mining Laser"
    assert patterns.BLUEPRINT_RECEIVED.search(SHUD_GRADED).group("name") == "Ind/3/C Surveyor-Max"
    # the lazy `\s*:` eats the space before the colon, so the captured name has no trailing space
    assert patterns.BLUEPRINT_RECEIVED.search(SHUD_TRAILSPACE).group("name") == "Antium Arms Moss Camo"
    # the UI-lifecycle echo and the bare re-print must NOT match (else we'd count 3-4x per acquire)
    assert patterns.BLUEPRINT_RECEIVED.search(LIFECYCLE) is None
    assert patterns.BLUEPRINT_RECEIVED.search(BARE) is None


# --- State accumulation ---------------------------------------------------------------- #
def test_state_records_one_acquisition_ignoring_noise():
    st = State()
    st.feed(SHUD)
    st.feed(LIFECYCLE)   # noise
    st.feed(BARE)        # noise
    assert list(st.blueprints) == ["lawson mining laser"]
    assert st.blueprints["lawson mining laser"] == {
        "name": "Lawson Mining Laser", "acquired_at": "2026-06-22T06:33:18.674Z"}


def test_state_idempotent_refeed():
    st = State()
    st.feed(SHUD)
    st.feed(SHUD)   # log replay (restart / rotation) must not duplicate
    assert len(st.blueprints) == 1


def test_state_keeps_earliest_acquired_at():
    later = SHUD.replace("06:33:18.674", "09:00:00.000")
    earlier = SHUD.replace("06:33:18.674", "05:00:00.000")
    st = State()
    st.feed(later)
    st.feed(earlier)
    assert st.blueprints["lawson mining laser"]["acquired_at"] == "2026-06-22T05:00:00.000Z"


# --- cumulative file merge ------------------------------------------------------------- #
def _bp(name, ts):
    return {patterns.norm_bp_name(name): {"name": name, "acquired_at": ts}}


def test_merge_unions_and_is_idempotent(tmp_path):
    p = str(tmp_path / "acquired_blueprints.json")
    acquired.merge_acquired(_bp("Lawson Mining Laser", "2026-06-22T06:33:18Z"), path=p)
    acquired.merge_acquired(_bp("Aril Core", "2026-06-22T07:00:00Z"), path=p)
    data = json.loads(open(p).read())
    assert set(data) == {"lawson mining laser", "aril core"}

    # re-merging identical data leaves the file byte-for-byte unchanged (no churn)
    before = open(p).read()
    acquired.merge_acquired(_bp("Lawson Mining Laser", "2026-06-22T06:33:18Z"), path=p)
    assert open(p).read() == before

    # an earlier sighting wins; a later one is ignored
    acquired.merge_acquired(_bp("Lawson Mining Laser", "2026-06-01T00:00:00Z"), path=p)
    acquired.merge_acquired(_bp("Lawson Mining Laser", "2026-12-31T00:00:00Z"), path=p)
    data = json.loads(open(p).read())
    assert data["lawson mining laser"]["acquired_at"] == "2026-06-01T00:00:00Z"


def test_acquired_index_unions_file_and_live_state(tmp_path):
    p = str(tmp_path / "acquired_blueprints.json")
    acquired._cache["mtime"] = None   # the module cache is shared across paths; reset per test
    acquired.merge_acquired(_bp("Aril Core", "2026-06-22T07:00:00Z"), path=p)
    st = State()
    st.feed(SHUD)   # "Lawson Mining Laser" only in the live (not-yet-archived) session
    idx = acquired.acquired_index(st, path=p)
    assert idx == {
        "aril core": "2026-06-22T07:00:00Z",
        "lawson mining laser": "2026-06-22T06:33:18.674Z",
    }


# --- catalog reconciliation ------------------------------------------------------------ #
def test_resolve_owned_exact_and_grade_prefix():
    catalog = {"surveyor-max", "lawson mining laser", "antium arms moss camo",
               "arclight pistol battery (30 cap)"}
    index = {
        "lawson mining laser": "t1",                  # exact
        "ind/3/c surveyor-max": "t2",                 # grade-prefix strip -> "surveyor-max"
        "antium arms moss camo": "t3",                # exact (trailing space already normalized)
        "arclight pistol battery (30 cap)": "t4",     # exact, with parenthetical
        "s00 hofstede": "t5",                         # no catalog entry -> unmatched
    }
    owned = acquired.resolve_owned(index, catalog)
    assert owned == {
        "surveyor-max": "t2",
        "lawson mining laser": "t1",
        "antium arms moss camo": "t3",
        "arclight pistol battery (30 cap)": "t4",
    }
    assert "s00 hofstede" not in owned and "hofstede" not in owned
