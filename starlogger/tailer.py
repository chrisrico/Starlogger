"""Follow Game.log, surviving rotation/truncation on game relaunch."""

from __future__ import annotations

import os
import threading
import time

from .state import State


def tail_loop(path: str, state: State, stop: threading.Event) -> None:
    # Rotation key = (device, inode). Both POSIX and modern CPython-on-Windows populate
    # these (NTFS file index), so a key change means the log was replaced. Some
    # filesystems (e.g. SMB shares) report st_ino == 0; there the key never changes, but
    # SC rewrites Game.log from the top on relaunch, so the size-shrink check below still
    # catches rotation. The file is opened in TEXT mode, so CRLF is translated to "\n"
    # before parsing and f.tell()/f.seek() round-trip on the same stream.
    cur_key = None
    pos = 0
    while not stop.is_set():
        try:
            st = os.stat(path)
        except FileNotFoundError:
            time.sleep(1.0)
            continue

        key = (st.st_dev, st.st_ino)
        if cur_key is None or (key != cur_key and st.st_ino) or st.st_size < pos:
            # first pass, new file (relaunch), or truncated -> reparse from the top
            cur_key = key
            pos = 0
            state.reset(full=True)

        if st.st_size > pos:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    for line in f:
                        if not line.endswith("\n") and not stop.is_set():
                            break  # partial trailing line; re-read next tick
                        state.feed(line)
                    pos = f.tell()
            except OSError:
                pass
        # Flush a live archive upsert at most once per read batch (coalesces a burst of
        # completions/trades into one write; no-op unless something finished).
        state.maybe_archive()
        time.sleep(0.5)


def parse_whole_file(path: str, state: State) -> None:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            state.feed(line)
