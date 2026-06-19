"""Multicrew ship detection: when the local player boards ANOTHER player's ship as crew,
the boarded ship is detected from its comms-channel join ("You have joined channel
'<Ship> : <Owner>'", Owner != you) and cleared again when they re-board/pilot their own.

Disk-isolated: the ships module's cargo-DB lookups are monkeypatched, so no ships.json read.

Run: python3 -m pytest tests/test_state.py
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import ships
from starlogger.state import State

KNOWN = {"Hermes", "Ironclad", "C1 Spirit", "Freelancer MAX"}   # all named as officially marketed


def _chan(verb, ship, owner):
    # verb: "joined channel" | "left the channel"
    return (f'<2026-06-06T00:00:00.000Z> [Notice] <SHUDEvent_OnNotification> Added '
            f'notification "You have {verb} \'{ship} : {owner}\'." [1] to queue.\n')


def _clear_driver(ent):
    return (f"<2026-06-06T00:00:01.000Z> [Notice] <Vehicle Control Flow> "
            f"CVehicleMovementBase::ClearDriver: Local client node [204772152312] "
            f"releasing control token for '{ent}_267739545027' [267739545027] [Team_CGP4][Vehicle]\n")


def _st(monkeypatch, player="WonkoTheSane1"):
    monkeypatch.setattr(ships, "known_ship_names", lambda db=None: set(KNOWN))
    monkeypatch.setattr(ships, "ship_display_name", lambda ent, db=None: None)  # force mfr-split fallback
    st = State()
    st.player = player
    return st


def test_board_other_players_ship(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "Drake Ironclad", "caged-danimal"))
    assert st.boarded_ship == "Ironclad"
    assert st.boarded_owner == "caged-danimal"
    # marketing name with a multi-word model: strip "Crusader", match the official "C1 Spirit"
    st.feed(_chan("joined channel", "Crusader C1 Spirit", "caged-danimal"))
    assert st.boarded_ship == "C1 Spirit"


def test_own_ship_channel_is_not_boarding(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "RSI Hermes", "WonkoTheSane1"))
    assert st.boarded_ship is None


def test_unknown_or_nonship_channel_ignored(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "global", "system"))            # not a known ship
    st.feed(_chan("joined channel", "Some Party : leader", "leader"))  # noise
    assert st.boarded_ship is None


def test_revert_when_rejoining_own_ship_channel(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "Drake Ironclad", "SmokeInFog"))
    assert st.boarded_ship == "Ironclad"
    st.feed(_chan("joined channel", "RSI Hermes", "WonkoTheSane1"))  # back on your own
    assert st.boarded_ship is None


def test_revert_on_piloting_own_ship(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "Drake Ironclad", "SmokeInFog"))
    assert st.boarded_ship == "Ironclad"
    st.feed(_clear_driver("MISC_Freelancer"))       # you take control of your own ship
    assert st.boarded_ship is None
    assert st.ship                                  # own ship still detected


def test_revert_on_leaving_the_channel(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "Drake Ironclad", "SmokeInFog"))
    st.feed(_chan("left the channel", "Drake Ironclad", "SmokeInFog"))
    assert st.boarded_ship is None


def test_leaving_a_different_channel_keeps_boarded(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "Drake Ironclad", "SmokeInFog"))
    st.feed(_chan("left the channel", "RSI Hermes", "someoneelse"))  # unrelated
    assert st.boarded_ship == "Ironclad"


def test_reset_clears_boarded(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "Drake Ironclad", "SmokeInFog"))
    assert st.boarded_ship == "Ironclad"
    st.reset()
    assert st.boarded_ship is None and st.boarded_owner is None


# --- snapshot version (drives SSE pushes) --------------------------------- #

def test_bump_version_increments():
    st = State()
    assert st.version == 0
    st.bump_version()
    st.bump_version()
    assert st.version == 2


def test_bump_version_wakes_a_waiter():
    import threading
    import time
    st = State()
    woke = []

    def waiter():
        with st.version_cv:
            st.version_cv.wait_for(lambda: st.version > 0, timeout=2)
            woke.append(st.version)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)  # let the waiter park on the condition
    st.bump_version()
    t.join(2)
    assert woke == [1]


# --- multi-commodity marker expansion (state-level) ----------------------- #
# decode_cargo_from_contract's token split is covered in test_patterns.py; this
# locks the _marker() behaviour built on top of it: a single-destination contract
# carrying several commodities logs a CreateMarker for only dropoff_<phase>_0, and
# the state must fan that lone marker out into one leg per commodity.

def _marker(mid, oid, contract, zone="100", x=1.0, y=2.0, z=3.0):
    return (f"<2026-06-06T00:00:00.000Z> [Notice] <Mission> Creating objective marker: "
            f"missionId [{mid}], generator name [gen], contract [{contract}], "
            f"contractDefinitionId[cdef], objectiveId [{oid}], markerEntityId [12345], "
            f"zoneHostId [{zone}], position [x: {x}, y: {y}, z: {z}]\n")


def test_multicommodity_marker_expands_to_one_leg_per_commodity():
    st = State()
    contract = "HaulCargo_AToB_RefinedOre_Mixed_AluminiumTungstenCorundum_Stanton_x"
    st.feed(_marker("0a-1", "dropoff_1_0", contract, zone="500"))
    legs = st.missions["0a-1"].legs
    assert sorted(legs) == ["dropoff_1_0", "dropoff_1_1", "dropoff_1_2"]
    assert [legs[k].cargo for k in sorted(legs)] == ["Aluminum", "Tungsten", "Corundum"]
    assert all(legs[k].zone_host_id == "500" for k in legs)   # all share the one dropoff zone


def test_singletomulti_marker_is_not_expanded():
    # Multi-DESTINATION contracts have a distinct zone per drop; their per-oid markers
    # already split correctly, so the lone-marker fan-out must NOT fire.
    st = State()
    contract = "HaulCargo_SingleToMulti3_RefinedOre_Mixed_AluminiumTungstenCorundum_Stanton_x"
    st.feed(_marker("0b-2", "dropoff_0", contract, zone="600"))
    assert list(st.missions["0b-2"].legs) == ["dropoff_0"]


# --- award queue gating --------------------------------------------------- #
# Only a completed mission queues for the next "Awarded N aUEC" line; failed /
# abandoned / expired endings must never claim a payout.

def _end(mid, ctype, reason):
    return (f"<2026-06-06T00:00:00.000Z> [Notice] <Mission> Ending mission for player. "
            f"MissionId[{mid}] Player[Foo] z CompletionType[{ctype}] Reason[{reason}]\n")


def test_only_completed_missions_queue_awards():
    st = State()
    st.feed(_end("fa-1", "Failed", "Failed"))
    st.feed(_end("ab-1", "Abandoned", "Abandoned"))
    assert len(st._pending_award) == 0                 # non-completions never queue
    st.feed(_end("dd-1", "Completed", "Success"))
    assert list(st._pending_award) == ["dd-1"]


# --- session-boundary semantics ------------------------------------------- #

def _accept(mid, title="Haul"):
    return (f'<2026-06-06T00:00:00.000Z> [Notice] <SHUDEvent_OnNotification> Added notification '
            f'"Contract Accepted:  {title}: " [1] to queue. New queue size: 1, '
            f'MissionId: [{mid}], ObjectiveId: [] [x]\n')


def _frontend(ts):
    return f'<{ts}> [Notice] <CVS> eCVS_InGame gamerules="SC_Frontend"\n'


def _pu(ts):
    return f'<{ts}> [Notice] <CVS> eCVS_InGame gamerules="SC_Default"\n'


def _shutdown(ts):
    return f'<{ts}> [Notice] <System> CCIGBroker::FastShutdown requested\n'


def _count_resets(st):
    """Wrap st.reset on the instance (which feed() resolves before the class method)
    so the test can count how many times a boundary actually reset the session."""
    seen: list = []
    real = State.reset

    def counting(self, **kw):
        seen.append(kw)
        return real(self, **kw)

    st.reset = types.MethodType(counting, st)
    return seen


def test_frontend_boundary_resets_once_per_timestamp():
    st = State()
    seen = _count_resets(st)
    st.feed(_accept("0a-1"))
    st.feed(_frontend("2026-06-06T01:00:00.000Z"))   # logout -> reset
    st.feed(_frontend("2026-06-06T01:00:00.000Z"))   # same establisher burst -> deduped
    assert len(seen) == 1
    st.feed(_frontend("2026-06-06T02:00:00.000Z"))   # a later, distinct logout -> resets again
    assert len(seen) == 2


def test_relaunch_into_pu_does_not_reset():
    st = State()
    seen = _count_resets(st)
    st.feed(_accept("0a-1"))
    st.feed(_pu("2026-06-06T01:00:00.000Z"))         # SC_Default establisher (login/relaunch)
    assert seen == []                                # missions the game restores must survive
    assert "0a-1" in st.missions
    assert st.logged_in is True


def test_fastshutdown_archives_once_not_twice():
    st = State()
    archived: list = []
    st.on_session_end = lambda s: archived.append(len(s.missions))
    st.feed(_pu("2026-06-06T01:00:00.000Z"))         # logged in
    st.feed(_accept("0a-1"))
    st.feed(_shutdown("2026-06-06T02:00:00.000Z"))   # archive + reset like a logout
    st.feed(_shutdown("2026-06-06T02:00:01.000Z"))   # nothing left -> no double-archive
    assert archived == [1]


def test_game_running_tracks_session_boundaries():
    """game_running is the log-derived 'is the game process up' flag driving the jukebox
    auto-pause: True at the main menu (SC_Frontend) AND in the PU (SC_Default) -- so it stays
    True across a quit-to-menu, unlike logged_in -- and only False on FastShutdown."""
    st = State()
    assert st.game_running is False                   # nothing seen yet
    st.feed(_frontend("2026-06-06T01:00:00.000Z"))   # launched to the main menu
    assert st.game_running is True
    assert st.logged_in is False                      # ...but not yet in the PU
    st.feed(_pu("2026-06-06T01:01:00.000Z"))          # loaded into the universe
    assert st.game_running is True and st.logged_in is True
    st.feed(_frontend("2026-06-06T02:00:00.000Z"))   # quit to menu -> game still running
    assert st.game_running is True and st.logged_in is False
    st.feed(_shutdown("2026-06-06T03:00:00.000Z"))   # quit to desktop -> game gone
    assert st.game_running is False


def test_game_running_resets_on_new_log_file():
    """A full reset (new log file) clears game_running; the next session boundary re-establishes
    it. Guards against a stale True surviving a fresh start before any boundary is parsed."""
    st = State()
    st.feed(_pu("2026-06-06T01:00:00.000Z"))
    assert st.game_running is True
    st.reset(full=True)
    assert st.game_running is False


def test_oversized_line_is_skipped():
    """A line larger than the cap is corrupt/hostile -> skipped before any pattern runs, so it
    can't amplify a regex DoS. A normal line still parses. Locks the audit fix (log-line DoS)."""
    st = State()
    huge = "<2026-06-06T00:00:00.000Z> " + "A" * (State._MAX_LINE + 1) + "\n"
    st.feed(huge)                                    # must not raise / hang / record anything
    assert st.last_event_ts is None
    st.feed(_pu("2026-06-06T01:00:00.000Z"))         # an in-bounds line still works
    assert st.last_event_ts == "2026-06-06T01:00:00.000Z"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
