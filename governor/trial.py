#!/usr/bin/env python3
"""trial.py -- observation trials: open one, and close the ones that are due.

    python3 governor/trial.py                      # close every due trial
    python3 governor/trial.py --open <stem> [--days 7]

A new gate approved by the operator does NOT start armed. It starts in
OBSERVATION: it detects exactly as it will once armed, journals
`result: "observe"`, and exits 0. It blocks nothing, refuses nothing, costs one
journal line per hit. A trial file
`$HARNESS_STATE_DIR/governor/trials/<stem>.json` carries
`{"stem", "start", "end"}` and is what the gate reads to know it is in trial:

    if os.path.exists(trial_file):          # EXAMPLE snippet, inside the gate
        gate_stat(STEM, "observe", why=reason)
        sys.exit(0)                         # trial: we note, we do not block
    gate_stat(STEM, "block", why=reason)
    sys.exit(2)

At the end of the window this script compiles what the gate WOULD have blocked
and files the review in `awaiting-operator/`. The operator then judges lived
catches, never an idea: "arm it" or "throw it away" is a decision about a list
of real events, which is a decision a human can actually make in ten seconds.

Arming (removing the observe branch) stays a separate human-approved gesture.
This script never performs it. Opening a trial is not arming either: a gate in
observation cannot refuse anything.

Exit code: 0 always (a closing pass never breaks a session).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _state import (ensure_dir, gate_stat, gov_path, ledger_append,  # noqa: E402
                    now_iso, read_journal)

HOOK = "governor-trial"
DEFAULT_DAYS = 7
MAX_SAMPLES = 5


def trial_path(stem: str) -> str:
    return gov_path("trials", "%s.json" % stem)


def open_trial(stem: str, days: int) -> int:
    """Create the observation window. The gate reads this file to stay mute."""
    ensure_dir("trials")
    start = datetime.now()
    record = {"stem": stem,
              "start": start.isoformat(timespec="seconds"),
              "end": (start + timedelta(days=days)).isoformat(timespec="seconds")}
    path = trial_path(stem)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1)
    gate_stat(HOOK, "observe", stem=stem, days=days, action="open")
    print("trial opened: %s -> %s (%d days, observation only)"
          % (record["start"][:16], record["end"][:16], days))
    print("the gate must journal `observe` and exit 0 while %s exists" % path)
    return 0


def observations(stem: str, start: str, end: str):
    """The `observe` records of THIS gate inside the window. A record outside
    the window belongs to another trial of the same gate, not to this one."""
    return [r for r in read_journal(since=start, until=end, results=("observe",))
            if str(r.get("hook")) == stem]


def review(record: dict, hits) -> str:
    lines = ["# trial review: %s" % record["stem"],
             "Period: %s -> %s, observation only."
             % (record["start"][:10], record["end"][:10]),
             "Would have blocked: %d time(s)." % len(hits)]
    for hit in hits[:MAX_SAMPLES]:
        extra = {k: v for k, v in hit.items()
                 if k not in ("ts", "hook", "result", "session_id", "cwd")}
        lines.append("  - %s %s" % (str(hit.get("ts", ""))[:16],
                                    json.dumps(extra, ensure_ascii=False)[:120]))
    if len(hits) > MAX_SAMPLES:
        lines.append("  - (+%d more)" % (len(hits) - MAX_SAMPLES))
    lines.append("If those catches are good: arm it. Otherwise: throw it away, "
                 "zero debt, the trial cost nothing but journal lines.")
    lines.append("**Your word: arm / discard**")
    return "\n".join(lines) + "\n"


def close_due() -> int:
    """Close every trial whose end date has passed. Returns the count."""
    folder = ensure_dir("trials")
    now = now_iso()
    closed = 0
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue                      # .closed files stay as the archive
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            stem, start, end = record["stem"], record["start"], record["end"]
        except (OSError, ValueError, KeyError, TypeError):
            print("trial file unreadable, left in place: %s" % path)
            continue                      # never destroy what we cannot read
        if now < end:
            continue                      # still running
        hits = observations(stem, start, end)
        out = os.path.join(ensure_dir("awaiting-operator"), "trial-%s.md" % stem)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(review(record, hits))
        os.replace(path, path + ".closed")
        entry = ledger_append(stem, "trial-closed", out, kind="gate-trial")
        gate_stat(HOOK, "pass", stem=stem, observations=len(hits),
                  action="close", ledger=entry)
        print("trial closed -> %s (%d observation(s), ledger %s)"
              % (out, len(hits), entry))
        closed += 1
    if not closed:
        gate_stat(HOOK, "pass", action="close", closed=0)
        print("trial: nothing due")
    return closed


def main(argv) -> int:
    if "--open" in argv:
        try:
            stem = argv[argv.index("--open") + 1].strip()
        except IndexError:
            stem = ""
        if not stem or stem.startswith("-"):
            print("usage: trial.py --open <stem> [--days N]")
            return 1
        days = DEFAULT_DAYS
        if "--days" in argv:
            try:
                raw = argv[argv.index("--days") + 1]
                days = int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_DAYS
            except IndexError:
                pass
        return open_trial(stem, days)
    close_due()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:              # a closing pass never breaks anything
        gate_stat(HOOK, "fail-open", error=str(exc)[:120])
        sys.exit(0)
