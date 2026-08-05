#!/usr/bin/env python3
"""audit.py -- recurring audit of the LIVE gates, BY EXCEPTION.

    python3 governor/audit.py          # weekly, from a timer or a cron entry

A gate doing its job is SILENCE. The operator only receives the lines where a
decision is actually pending, in plain verbs (retire / keep / review). This is
the difference between an audit a human can sustain and a report nobody opens:
auditing everything every week is auditing nothing.

Deterministic, and no LLM anywhere. The judges exist for NEW proposals; the
existing battery is judged on its own lived numbers, read from the gate-stats
journal (the same journal every gate writes to on every execution).

Exceptions detected:

    SILENT  a wired script whose stem produced no journal event in the window.
            Dead weight or broken gate: both deserve a word. A gate that is
            wired and mute is worse than no gate, because it is believed.
    NOISY   >= threshold `block`/`deny` in the window -> a false-positive review,
            with the 3 most recent samples pasted in, so the decision is made on
            evidence and not on a feeling.

Output: `$HARNESS_STATE_DIR/governor/audit-decisions.md`, DELETED when there is
nothing to decide (the silence has to be true, a stale page is a lie). Every run
appends one aliveness line to `audit-log.jsonl`: an audit timer that stops
firing is itself a dead gate, and the sentinel can see it in that file.

Environment:
- HARNESS_GOVERNOR_SETTINGS     colon-separated settings.json files whose hooks
                                are audited (default: ~/.claude/settings.json)
- HARNESS_GOVERNOR_WINDOW_DAYS  observation window, days (default 30)
- HARNESS_GOVERNOR_NOISY_MIN    block/deny count that triggers a review (10)
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from _state import (GOVERNOR_DIR, cutoff_iso, ensure_dir, gate_stat,  # noqa: E402
                    gov_path, int_env, mask_secrets, now_iso, read_journal)

HOOK = "governor-audit"
PAGE = gov_path("audit-decisions.md")
LOG = gov_path("audit-log.jsonl")
DEFAULT_SETTINGS = "~/.claude/settings.json"
SCRIPT_IN_COMMAND = re.compile(r"(/\S+\.(?:py|sh))")


def settings_files():
    raw = os.environ.get("HARNESS_GOVERNOR_SETTINGS") or DEFAULT_SETTINGS
    return [os.path.expanduser(p) for p in raw.split(os.pathsep) if p.strip()]


def wired_scripts():
    """Absolute paths of the .py/.sh scripts wired in the audited settings."""
    seen = set()
    for path in settings_files():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue                 # absent or broken settings: not our call
        for groups in (data.get("hooks") or {}).values():
            for group in groups if isinstance(groups, list) else []:
                for hook in group.get("hooks", []):
                    found = SCRIPT_IN_COMMAND.search(hook.get("command", ""))
                    if found:
                        seen.add(found.group(1))
    return seen


def is_alive(path: str, alive_stems) -> bool:
    """A wired script is ALIVE when a stem OBSERVED in the journal appears in
    its source text.

    Guessing the stem from the file name, or from a regex over the code, wrongly
    accused 4 healthy gates in 2 runs: naming conventions vary from one hook to
    the next. The textual presence of an observed stem does not vary. The
    residual error is to MISS a truly mute gate (a common word inside a
    comment), never to accuse a live one -- and that is the right way round: an
    audit that cries wolf gets ignored, and then it protects nothing.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError:
        return True                  # unreadable: never accuse without proof
    return any(stem in source for stem in alive_stems)


def sample_line(rec: dict) -> str:
    """One journal record as evidence: timestamp + the payload minus the
    routine fields, secrets scrubbed by the shared helper."""
    extra = {k: v for k, v in rec.items()
             if k not in ("ts", "hook", "result", "session_id", "cwd")}
    return "  - %s %s" % (str(rec.get("ts", ""))[:16],
                          mask_secrets(json.dumps(extra, ensure_ascii=False), 120))


def exceptions_report(records, wired):
    """The lines worth a human decision, and nothing else."""
    alive_stems = {str(r.get("hook")) for r in records if r.get("hook")}
    blocked = [r for r in records if r.get("result") in ("block", "deny")]
    counts = {}
    for rec in blocked:
        counts[str(rec.get("hook"))] = counts.get(str(rec.get("hook")), 0) + 1
    noisy_min = int_env("HARNESS_GOVERNOR_NOISY_MIN", 10)
    days = int_env("HARNESS_GOVERNOR_WINDOW_DAYS", 30)

    lines = []
    for path in sorted(wired):
        if not is_alive(path, alive_stems):
            lines.append(
                "- SILENT **%s**: wired, but no gate-stats event in %d days. "
                "Dead weight or broken gate. Decision: retire / keep"
                % (os.path.basename(path), days))
    for hook, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if count < noisy_min:
            continue
        samples = [r for r in blocked if str(r.get("hook")) == hook][-3:]
        rendered = "\n".join(sample_line(r) for r in reversed(samples))
        lines.append(
            "- NOISY **%s**: %d block/deny in %d days. The last 3:\n%s\n"
            "  If those refusals are mostly legitimate: keep. Otherwise: "
            "review the trigger. Decision: keep / review"
            % (hook, count, days, rendered))
    return lines


def main() -> int:
    os.makedirs(GOVERNOR_DIR, exist_ok=True)
    days = int_env("HARNESS_GOVERNOR_WINDOW_DAYS", 30)
    records = read_journal(since=cutoff_iso(days))
    wired = wired_scripts()
    lines = exceptions_report(records, wired)
    stamp = now_iso()

    if lines:
        ensure_dir()
        with open(PAGE, "w", encoding="utf-8") as fh:
            fh.write("# Gate audit -- %s -- decisions pending\n\n"
                     "By exception: a gate doing its job is not listed here. "
                     "Window: %d days, %d journal events, %d wired scripts.\n\n"
                     % (stamp[:10], days, len(records), len(wired)))
            fh.write("\n".join(lines) + "\n")
        print("%d decision(s) to make -> %s" % (len(lines), PAGE))
    else:
        if os.path.exists(PAGE):
            os.remove(PAGE)   # the silence must be true: no stale page survives
        print("audit: nothing to decide, every gate is doing its job")

    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": stamp, "exceptions": len(lines),
                                 "events": len(records), "wired": len(wired),
                                 "window_days": days}) + "\n")
    except OSError:
        pass
    gate_stat(HOOK, "warn" if lines else "pass",
              exceptions=len(lines), events=len(records), wired=len(wired))
    return 0


if __name__ == "__main__":
    sys.exit(main())
