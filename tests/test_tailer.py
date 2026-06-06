"""tailer.tail_loop / parse_whole_file: the file-follow entry point.

The tailer had no tests despite owning the rotation/truncation logic that decides
when to reparse Game.log from the top. These drive it against a real temp file (in a
thread, polling for the async read) and assert: initial read, incremental append, and
reparse-on-truncation (game relaunch rewrites the log shorter). A bare State() has no
archive/session callbacks wired, so feeding lines never touches disk.

Run: python -m pytest tests/test_tailer.py
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlogger import tailer
from starlogger.state import State

# Two complete, real trade lines (newline-terminated) -- feeding each registers one
# entry in State.trades, a simple observable for "this line was parsed".
BUY = ('<2026-06-01T16:20:49.543Z> [Notice] <CEntityComponentCommodityUIProvider::SendCommodityBuyRequest> '
       'shopName[SCShop_x] kioskId[111] price[1067040.000000] '
       'resourceGUID[35121003-f1af-481a-b16f-7f48d8af0efb] '
       'quantity[28800.000000 cSCU] Cargo Box Data: boxSize[16.000000] | unitAmount[18]\n')
SELL = ('<2026-06-01T03:46:57.282Z> [Notice] <CEntityComponentCommodityUIProvider::SendCommoditySellRequest> '
        'shopName[SCShop_Admin] kioskId[222] amount[793520.000000] '
        'resourceGUID[9e65a7bd-adcf-4129-9ef5-26f4fe13f85b] '
        'Cargo Box Data:  [boxSize[16] | unitAmount[14]]\n')


def _wait(cond, timeout=3.0) -> bool:
    """Poll until cond() is truthy (the loop reads on a 0.5s tick), then return it."""
    deadline = time.time() + timeout
    while time.time() < deadline and not cond():
        time.sleep(0.02)
    return cond()


def _run(path, state):
    stop = threading.Event()
    t = threading.Thread(target=tailer.tail_loop, args=(str(path), state, stop), daemon=True)
    t.start()
    return stop, t


def test_parse_whole_file(tmp_path):
    p = tmp_path / "Game.log"
    p.write_text(BUY + SELL)
    st = State()
    tailer.parse_whole_file(str(p), st)
    assert len(st.trades) == 2


def test_tail_loop_reads_initial_then_appended(tmp_path):
    p = tmp_path / "Game.log"
    p.write_text(BUY)
    st = State()
    stop, t = _run(p, st)
    try:
        assert _wait(lambda: len(st.trades) == 1), "initial content not read"
        with open(p, "a", encoding="utf-8") as f:
            f.write(SELL)
        assert _wait(lambda: len(st.trades) == 2), "appended line not picked up"
    finally:
        stop.set()
        t.join(timeout=2)


def test_tail_loop_reparses_on_truncation(tmp_path):
    p = tmp_path / "Game.log"
    p.write_text(BUY + SELL)
    st = State()
    stop, t = _run(p, st)
    try:
        assert _wait(lambda: len(st.trades) == 2)
        # Game relaunch rewrites Game.log from the top -> size shrinks below pos,
        # which must trigger a full reset + reparse (not append onto stale state).
        p.write_text(BUY)
        assert _wait(lambda: len(st.trades) == 1), "truncation did not trigger reparse"
    finally:
        stop.set()
        t.join(timeout=2)


def test_tail_loop_holds_partial_trailing_line(tmp_path):
    p = tmp_path / "Game.log"
    p.write_text("")
    st = State()
    stop, t = _run(p, st)
    try:
        # a line without its terminating newline must NOT be parsed yet
        with open(p, "a", encoding="utf-8") as f:
            f.write(BUY.rstrip("\n"))
        assert not _wait(lambda: len(st.trades) >= 1, timeout=1.0), "partial line parsed too early"
    finally:
        stop.set()
        t.join(timeout=2)
