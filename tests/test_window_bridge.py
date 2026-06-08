"""Static guard for the frontend's `window` bridge.

The dashboard JS is loaded as ES modules (`<script type="module">`), so top-level
function declarations are module-scoped, NOT global. Inline HTML handlers
(`onclick="editMission(…)"`, and the interpolated `onclick="${fn}(…)"` built by
`tabBar`) resolve names against `window`, so every handler-reachable function must be
re-exposed via `Object.assign(window, { … })`.

This test parses the served JS and fails if any inline-handler-referenced name is not
in the bridge — catching the easy-to-miss "added a handler, forgot to expose it" bug
without needing a browser. It globs web/*.js so it keeps working after app.js is split
into modules (the bridge may live in any one of them).
"""
from __future__ import annotations

import glob
import os
import re

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")

# JS keywords / globals that can legally lead an inline-handler expression but are not
# functions we own (e.g. onmousedown="event.preventDefault()").
_NOT_OURS = {
    "if", "this", "event", "window", "return", "true", "false", "null", "for", "while",
    "function", "var", "let", "const", "new", "typeof", "void", "delete", "do", "else",
    "switch", "case", "break", "continue", "throw", "try", "catch", "document",
}


def _all_js() -> str:
    return "\n".join(
        open(p, encoding="utf-8").read()
        for p in sorted(glob.glob(os.path.join(WEB_DIR, "*.js")))
        # cargogrid.js is a classic global IIFE (exposes its own window.* api); not bridged
        if os.path.basename(p) != "cargogrid.js"
    )


def _referenced_names(js: str) -> set[str]:
    # 1. directly-invoked handler names:  onclick="editMission(...)"
    direct = set(re.findall(r'\son[a-z]+="([A-Za-z_$][\w$]*)\(', js))
    # 2. interpolated handler names passed to tabBar(items, active, "fnName", ...)
    tabbar = set(re.findall(r'tabBar\([^,]*,[^,]*,\s*"([A-Za-z_$][\w$]*)"', js))
    return (direct | tabbar) - _NOT_OURS


def _bridged_names(js: str) -> set[str]:
    names: set[str] = set()
    # union every Object.assign(window, { ... }) block's keys
    for body in re.findall(r"Object\.assign\(\s*window\s*,\s*\{(.*?)\}\s*\)", js, re.S):
        # keys are bare identifiers (shorthand properties), possibly across comment lines
        body = re.sub(r"//[^\n]*", "", body)  # strip line comments
        names |= set(re.findall(r"\b([A-Za-z_$][\w$]*)\b", body))
    return names


def test_every_inline_handler_is_window_bridged():
    js = _all_js()
    referenced = _referenced_names(js)
    bridged = _bridged_names(js)
    missing = sorted(referenced - bridged)
    assert not missing, (
        "Inline-handler function(s) not exposed on window (add to the Object.assign "
        f"bridge): {missing}"
    )
