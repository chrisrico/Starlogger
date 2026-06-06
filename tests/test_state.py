"""Multicrew ship detection: when the local player boards ANOTHER player's ship as crew,
the boarded ship is detected from its comms-channel join ("You have joined channel
'<Ship> : <Owner>'", Owner != you) and cleared again when they re-board/pilot their own.

Disk-isolated: shipcargo's cargo-DB lookups are monkeypatched, so no ships_cargo.json read.

Run: python3 -m pytest tests/test_state.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import shipcargo
from starlogger.state import State

KNOWN = {"Hermes", "Ironclad", "Freelancer MAX"}   # all named as officially marketed


def _chan(verb, ship, owner):
    # verb: "joined channel" | "left the channel"
    return (f'<2026-06-06T00:00:00.000Z> [Notice] <SHUDEvent_OnNotification> Added '
            f'notification "You have {verb} \'{ship} : {owner}\'." [1] to queue.\n')


def _clear_driver(ent):
    return (f"<2026-06-06T00:00:01.000Z> [Notice] <Vehicle Control Flow> "
            f"CVehicleMovementBase::ClearDriver: Local client node [204772152312] "
            f"releasing control token for '{ent}_267739545027' [267739545027] [Team_CGP4][Vehicle]\n")


def _st(monkeypatch, player="WonkoTheSane1"):
    monkeypatch.setattr(shipcargo, "known_ship_names", lambda db=None: set(KNOWN))
    monkeypatch.setattr(shipcargo, "ship_display_name", lambda ent, db=None: None)  # force mfr-split fallback
    st = State()
    st.player = player
    return st


def test_board_other_players_ship(monkeypatch):
    st = _st(monkeypatch)
    st.feed(_chan("joined channel", "Drake Ironclad", "caged-danimal"))
    assert st.boarded_ship == "Ironclad"
    assert st.boarded_owner == "caged-danimal"


@pytest.mark.xfail(reason="cargo DB extraction names the Crusader Spirit 'Spirit C1'; the "
                          "official (and comms-channel) name is 'C1 Spirit'. Pending a DB "
                          "name fix by a separate agent — resolve_ship_name will then match "
                          "exactly. Uses the real ships_cargo.json on purpose.",
                   strict=False)
def test_boarding_spirit_uses_official_name():
    st = State()
    st.player = "WonkoTheSane1"
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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
