"""Live archive upsert: a finished contract/trade marks the session dirty, and
maybe_archive() flushes at most once per call (coalescing a batch).

Run: python3 -m pytest tests/test_live_archive.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger.state import State

MID = "aaaaaaaa-0000-0000-0000-00000000000"


def _accept(n: int) -> str:
    return (f'<2026-06-05T01:00:0{n}.000Z> [Notice] <SHUDEvent_OnNotification> Added notification '
            f'"Contract Accepted:  Haul {n}: " [1] to queue. New queue size: 1, '
            f'MissionId: [{MID}{n}], ObjectiveId: [] [x]\n')


def _complete(n: int) -> str:
    return (f'<2026-06-05T01:05:0{n}.000Z> [Notice] <SHUDEvent_OnNotification> Added notification '
            f'"Contract Complete:  Haul {n}: " [2] to queue. New queue size: 1, '
            f'MissionId: [{MID}{n}], ObjectiveId: [] [x]\n')


def _recorder():
    calls = []
    st = State()
    st.on_archive = lambda s: calls.append(
        sum(1 for m in s.missions.values() if m.status == "completed"))
    return st, calls


def test_accept_does_not_flush_only_completion_does():
    st, calls = _recorder()
    st.feed(_accept(1)); st.maybe_archive()
    assert calls == []                       # active mission: nothing finished
    st.feed(_complete(1)); st.maybe_archive()
    assert calls == [1]                      # completion flushes
    st.feed("<2026-06-05T01:06:00.000Z> [Notice] <Noise> irrelevant\n"); st.maybe_archive()
    assert calls == [1]                      # nothing new -> no extra write


def test_batch_of_completions_coalesces_to_one_write():
    st, calls = _recorder()
    for n in (1, 2, 3):
        st.feed(_accept(n))
        st.feed(_complete(n))
    st.maybe_archive()                       # one flush for the whole batch
    assert calls == [3]


def test_reset_does_not_flush_empty_session():
    st, calls = _recorder()
    st.feed(_accept(1)); st.feed(_complete(1))
    # a logout boundary archives + clears the session (via on_session_end, not on_archive)
    st.feed('<2026-06-05T02:00:00.000Z> [Notice] <CVS> eCVS_InGame gamerules="SC_Frontend"\n')
    st.maybe_archive()
    assert calls == []                       # the empty, just-reset session is never upserted
    st.feed("<2026-06-05T02:00:01.000Z> [Notice] <Noise> irrelevant\n"); st.maybe_archive()
    assert calls == []                       # and the signature doesn't "snap back" to dirty


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
