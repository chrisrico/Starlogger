"""Hauler rank-progression summary, derived from the raw logs.

The archive (sessions.json) doesn't store a mission's generator org, and a
contract's rank lives only in its title text — so the per-hauler, per-rank
progression is reconstructed by scanning the live log + every logbackup. A
rank's contracts only appear once you've reached that standing tier, so the
first time each rank shows up doubles as the (approximate) promotion date —
the closest thing to a reputation history the logs allow.

Result is cached by the set of (path, mtime) of the scanned logs, and only
recomputed when a log changes (throttled), since a full reparse isn't free.
"""

from __future__ import annotations

import glob
import os
import re
import time
from collections import Counter, defaultdict

from . import patterns
from .config import find_log, find_log_backups

_RANK = re.compile(r"\b([A-Za-z]+)\s+Rank\b", re.I)
# canonical Covalex/hauling rank ladder; anything else is appended in seen order
_RANK_ORDER = ["Rookie", "Junior", "Member", "Experienced", "Senior", "Veteran"]

_cache: dict = {"key": None, "data": None, "at": 0.0}
_MIN_RECOMPUTE_S = 15  # don't reparse more than this often, even as the live log grows


def _friendly_org(org: str) -> str:
    """`Covalex_Hauling` -> `Covalex`; `LingFamilyHauling_Hauling` -> `Ling Family`."""
    s = (org or "").replace("_Hauling", "").replace("_Generator", "").replace("_", " ")
    s = re.sub(r"\bHauling\b", "", s).strip()
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)  # split CamelCase: LingFamily -> Ling Family
    return re.sub(r"\s+", " ", s).strip() or "Unknown"


def _log_files() -> list[str]:
    log = find_log()
    if not log:
        return []
    return [log] + find_log_backups(log)


def _scan(files: list[str]) -> dict:
    """Build {mid: {org,title,rank,accept,complete}} across all logs (min ts wins)."""
    miss: dict = defaultdict(lambda: {"org": "", "title": "", "rank": None,
                                       "accept": None, "complete": None})

    def setmin(r, k, ts):
        if ts and (r[k] is None or ts < r[k]):
            r[k] = ts

    for fp in files:
        try:
            fh = open(fp, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                tsm = patterns.TS.search(line)
                ts = tsm.group("ts") if tsm else None
                m = patterns.MARKER.search(line)
                if m:
                    r = miss[m.group("mid")]
                    r["org"] = r["org"] or m.group("gen")
                    setmin(r, "accept", ts)
                    continue
                m = patterns.ACCEPTED.search(line)
                if m:
                    r = miss[m.group("mid")]
                    t = patterns.clean_title(m.group("title"))
                    if t and not r["title"]:
                        r["title"] = t
                    setmin(r, "accept", ts)
                    continue
                m = patterns.COMPLETE_NOTE.search(line)
                if m:
                    r = miss[m.group("mid")]
                    t = patterns.clean_title(m.group("title"))
                    if t and not r["title"]:
                        r["title"] = t
                    setmin(r, "complete", ts)
                    continue
                m = patterns.END_MISSION.search(line)
                if m and patterns.classify_end(m.group("ctype"), m.group("reason")) == "completed":
                    setmin(miss[m.group("mid")], "complete", ts)
                    continue
                m = patterns.MISSION_ENDED.search(line)
                if m and patterns.classify_end(m.group("state")) == "completed":
                    setmin(miss[m.group("mid")], "complete", ts)

    for r in miss.values():
        mr = _RANK.search(r["title"] or "")
        r["rank"] = mr.group(1).title() if mr else None
    return miss


def _build(miss: dict) -> dict:
    completed = [r for r in miss.values() if r["complete"]]

    # hauling orgs = those with at least one rank-titled completion (excludes bounty etc.)
    org_completed: Counter = Counter()
    for r in completed:
        if r["rank"]:
            org_completed[r["org"]] += 1
    haulers = [{"org": _friendly_org(o), "raw": o, "completed": n}
               for o, n in org_completed.most_common()]

    if not org_completed:
        return {"haulers": [], "primary": None}

    primary_raw = org_completed.most_common(1)[0][0]
    pri = [r for r in completed if r["org"] == primary_raw]

    def datespan(rows):
        ds = sorted(x["complete"][:10] for x in rows if x["complete"])
        return (ds[0], ds[-1]) if ds else (None, None)

    seen_ranks = {r["rank"] for r in pri if r["rank"]}
    rank_order = [x for x in _RANK_ORDER if x in seen_ranks] + \
                 sorted(seen_ranks - set(_RANK_ORDER))

    ranks = []
    for rk in rank_order:
        rows = [r for r in pri if r["rank"] == rk]
        first, last = datespan(rows)
        ranks.append({"rank": rk, "completed": len(rows), "first": first, "last": last})
    untitled = sum(1 for r in pri if not r["rank"])

    # promotion milestones: first time each rank's contracts appear (any status)
    pri_all = [r for r in miss.values() if r["org"] == primary_raw and r["rank"]]
    milestones = []
    for rk in rank_order:
        rows = [r for r in pri_all if r["rank"] == rk]
        firsts = [r["accept"] for r in rows if r["accept"]]
        if firsts:
            milestones.append({"rank": rk, "ts": min(firsts)})
    milestones.sort(key=lambda x: x["ts"])

    by_day = Counter(r["complete"][:10] for r in pri if r["complete"])
    days = [{"day": d, "count": by_day[d]} for d in sorted(by_day)]

    return {
        "haulers": haulers,
        "primary": {
            "org": _friendly_org(primary_raw),
            "total": len(pri),
            "ranks": ranks,
            "untitled": untitled,
            "milestones": milestones,
            "by_day": days,
        },
    }


def rank_progression() -> dict:
    """Cached hauler progression summary (see module docstring)."""
    files = _log_files()
    try:
        key = tuple(sorted((f, os.path.getmtime(f)) for f in files))
    except OSError:
        key = tuple(files)
    now = time.time()
    if _cache["key"] == key:
        return _cache["data"]
    if _cache["data"] is not None and (now - _cache["at"]) < _MIN_RECOMPUTE_S:
        return _cache["data"]  # logs changed but throttle a fresh full reparse
    data = _build(_scan(files))
    _cache.update(key=key, data=data, at=now)
    return data
