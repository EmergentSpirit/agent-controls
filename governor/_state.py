#!/usr/bin/env python3
"""_state.py -- shared paths, ledger and journal reader for the governor.

The governor produces FILES and LEDGER LINES, nothing else. It never arms a
gate, never edits a hook, never touches a settings file. Everything it writes
lives under `$HARNESS_STATE_DIR/governor/`:

    proposals routed to        to-build/            (viable, technical class)
                               awaiting-operator/   (viable, life class + trial reviews)
                               pending-judge/       (a judge did not speak)
                               archive/             (killed by a judge, auditable)
    raw verdicts               verdicts/<slug>-<ts>.json
    observation trials         trials/<stem>.json
    audit page                 audit-decisions.md   (deleted when nothing is due)
    aliveness of the audit     audit-log.jsonl
    one line per decision      ledger.jsonl

State paths come from `_hook` (HARNESS_STATE_DIR / HARNESS_GATE_STATS): they
are never recomputed here, so a test that isolates the state directory
isolates the governor with it.

Environment:
- HARNESS_GOVERNOR_LEDGER   ledger path override (default: <governor>/ledger.jsonl)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))

from _hook import GATE_STATS, STATE_DIR, gate_stat, mask_secrets  # noqa: E402,F401

GOVERNOR_DIR = os.path.join(STATE_DIR, "governor")
LEDGER = os.path.expanduser(
    os.environ.get("HARNESS_GOVERNOR_LEDGER") or os.path.join(GOVERNOR_DIR,
                                                              "ledger.jsonl"))

# Routing folders. The status vocabulary is closed: a status that is not in
# this table has no folder, which makes a silent new status impossible.
FOLDERS = {
    "viable-to-build": "to-build",
    "awaiting-operator": "awaiting-operator",
    "judge-unavailable": "pending-judge",
    "rejected-by-judges": "archive",
}


def gov_path(*parts: str) -> str:
    return os.path.join(GOVERNOR_DIR, *parts)


def ensure_dir(*parts: str) -> str:
    path = gov_path(*parts)
    os.makedirs(path, exist_ok=True)
    return path


def int_env(name: str, default: int) -> int:
    """Positive integer from the environment; anything else is ignored, so a
    typo cannot silently widen a window or disarm a threshold."""
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def cutoff_iso(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def read_journal(since: str = "", until: str = "", results=None):
    """Records of the gate-stats journal, filtered. Fail-open: an unreadable or
    absent journal yields [] and the caller reports zero, never an error.

    Timestamps are compared as strings: `datetime.isoformat(timespec="seconds")`
    is fixed-width, so lexical order IS chronological order.
    """
    out = []
    try:
        with open(GATE_STATS, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue          # a corrupt line is skipped, not fatal
                if not isinstance(rec, dict):
                    continue
                ts = str(rec.get("ts") or "")
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue
                if results and rec.get("result") not in results:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


def ledger_append(title: str, status: str, reference: str,
                  kind: str = "gate-proposal") -> str:
    """One line per governance decision, appended. Returns the assigned id.

    The id is `<day>-NN`, NN being the next free number of the day, so two runs
    on the same day never collide. Every field a human decision will need later
    exists from the start and stays empty until it is filled: an entry that has
    to be re-shaped later is an entry nobody updates.
    """
    day = date.today().isoformat()
    seen = 0
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            for line in fh:
                found = re.search(r'"id": "%s-(\d+)"' % day, line)
                if found:
                    seen = max(seen, int(found.group(1)))
    except OSError:
        pass
    rec = {"id": "%s-%02d" % (day, seen + 1), "type": kind, "title": title,
           "reference": reference, "date": day, "status": status,
           "decided_by": "", "decided_on": "", "route": "governor",
           "verified_on": "", "verified_proof": ""}
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return rec["id"]
