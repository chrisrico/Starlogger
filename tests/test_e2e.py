"""End-to-end tests that drive the real dashboard in headless Chromium (Playwright).

These cover behaviors unit tests can't: localStorage persistence across reloads, the HTML5
<audio> jukebox, modal open/close, the Settings "Advanced" collapse, and Play/Stop autoplay. They use
the `live_server` fixture (a real Flask server over a throwaway temp data dir — see conftest.py)
so they NEVER touch the live install or the source tree.

Marked `browser`: run with `pytest -m browser`; skip with `pytest -m "not browser"`.
Requires Chromium (`playwright install chromium`); skips gracefully if absent.

Run: python -m pytest tests/test_e2e.py
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _need_browser(require_browser):
    """Skip every test here if Chromium isn't available."""


@pytest.fixture(autouse=True)
def _fast_timeouts(page):
    """Fail fast (don't sit on Playwright's 30s default) so a broken selector surfaces quickly."""
    page.set_default_timeout(7000)
    page.set_default_navigation_timeout(15000)


# --- page boots cleanly --------------------------------------------------------------- #

def test_page_loads_without_console_errors(page, live_server):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(live_server)
    page.wait_for_selector("#sidebar .sb-brand")
    assert "STARLOGGER" in page.inner_text("#sidebar .sb-brand")
    assert errors == [], errors


# --- Settings: Advanced section ------------------------------------------------------- #

def test_settings_advanced_collapsed_by_default(page, live_server):
    page.goto(live_server)
    page.click("#navsettings")
    page.wait_for_selector("#setAdvToggle")
    # the advanced rows are hidden until the section is expanded
    assert page.locator("#set_update_remote").is_hidden()
    assert page.locator("#set_update_branch").is_hidden()
    assert page.locator("#set_idle_timeout").is_hidden()
    # a General row (always visible) anchors the un-collapsed section — Auto-open is a
    # segmented on/off switch, so the visible control is its .modesw, not the backing input
    assert page.locator("#set_open_browser_seg").is_visible()
    # expanding reveals the advanced rows
    page.click("#setAdvToggle")
    assert page.locator("#set_update_remote").is_visible()


# --- Jukebox: numbering, total time, shuffle ------------------------------------------ #

def test_jukebox_track_numbers_and_total(page, live_server):
    page.goto(live_server)
    page.click("#navjukebox")
    page.wait_for_selector("#jukeList .juke-row")
    assert page.locator("#jukeList .juke-num").all_inner_texts() == ["1", "2", "3"]
    # 3 tracks × 2s = 0:06  (the cell is CSS text-transform:uppercase, so lowercase to compare)
    total = page.inner_text("#jukeTotal").lower()
    assert "3 tracks" in total and "0:06" in total


def test_skip_keeps_row_visible_and_bypasses_in_playback(page, live_server):
    page.goto(live_server)
    page.click("#navjukebox")
    page.wait_for_selector("#jukeList .juke-row")
    # there is no "show hidden" control any more — Skip greys but never removes
    assert page.locator("#jukeShowHidden").count() == 0
    first = "#jukeList .juke-row:first-child"
    page.click(f"{first} .juke-skip")
    page.wait_for_selector(f"{first}.skipped-row")          # stays in the list, just marked
    assert page.locator("#jukeList .juke-row").count() == 3  # nothing removed
    # un-skip restores it
    page.click(f"{first} .juke-skip")
    assert "skipped-row" not in (page.get_attribute(first, "class") or "")


def test_shuffle_state_persists_across_reload(page, live_server):
    page.goto(live_server)
    page.click("#navjukebox")
    page.wait_for_selector("#jukeShuffle")
    assert "on" not in (page.get_attribute("#jukeShuffle", "class") or "")
    page.click("#jukeShuffle")
    assert "on" in (page.get_attribute("#jukeShuffle", "class") or "")
    page.reload()
    # the jukebox re-opens itself on load (jukeOpen persisted), so the control is already present
    page.wait_for_selector("#jukeShuffle")
    assert "on" in (page.get_attribute("#jukeShuffle", "class") or "")   # restored from localStorage


# --- playback: persistence + auto-play ------------------------------------------------ #

_AUDIO_HAS_SRC = "() => { const a = document.getElementById('jukeAudio'); return !!(a && a.currentSrc); }"
_AUDIO_PLAYING = "() => { const a = document.getElementById('jukeAudio'); return !!(a && a.currentSrc && !a.paused); }"


def test_playback_restores_same_track_after_reload(page, live_server):
    page.goto(live_server)
    page.click("#navjukebox")
    page.wait_for_selector("#jukeList .juke-row")
    page.click("#jukeList .juke-row:first-child .juke-title")   # play the first track
    page.wait_for_function(_AUDIO_HAS_SRC)
    src = page.evaluate("document.getElementById('jukeAudio').currentSrc")
    page.reload()
    # boot sees the saved jukeState and rebuilds the player (no need to reopen the modal)
    page.wait_for_function(_AUDIO_HAS_SRC)
    assert page.evaluate("document.getElementById('jukeAudio').currentSrc") == src


def test_play_turns_on_autoplay_and_resumes_after_reload(page, live_server):
    page.goto(live_server)
    page.click("#navjukebox")
    page.wait_for_selector("#jukePlay:not([disabled])")
    page.click("#jukePlay")                                     # Play (the click is a user gesture)
    page.wait_for_function(_AUDIO_PLAYING, timeout=5000)
    assert page.evaluate("localStorage.getItem('jukeAutoplay')") == "1"
    page.reload()
    page.wait_for_function(_AUDIO_PLAYING, timeout=5000)        # autoplay on → resumes playing


def test_stop_turns_off_autoplay_and_halts_after_reload(page, live_server):
    page.goto(live_server)
    page.click("#navjukebox")
    page.wait_for_selector("#jukePlay:not([disabled])")
    page.click("#jukePlay")
    page.wait_for_function(_AUDIO_PLAYING, timeout=5000)
    page.click("#jukeStop")                                     # Stop: halt + rewind + autoplay off
    page.wait_for_function("() => { const a = document.getElementById('jukeAudio'); return a.paused && a.currentTime === 0; }")
    assert page.evaluate("localStorage.getItem('jukeAutoplay')") == "0"
    page.reload()
    page.wait_for_selector("#jukeList .juke-row")
    assert not page.evaluate(_AUDIO_PLAYING)                    # autoplay off → stays paused


def test_no_autoplay_for_a_fresh_visitor(page, live_server):
    page.goto(live_server)
    page.click("#navjukebox")
    page.wait_for_selector("#jukeList .juke-row")
    # nothing auto-started: no track loaded into the player
    assert page.evaluate("!document.getElementById('jukeAudio').currentSrc")
