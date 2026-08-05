#!/usr/bin/env python3
"""interlock-stamp.py -- prove the preparation step the interlock gate demands.

Two doors, one lock. The state file
`$HARNESS_STATE_DIR/interlock-<session>.json` is MERGED, never overwritten:
the edit trail and the other door keep their values.

  --decomp <steps.json>  a non-empty JSON list of objects, each carrying
                         step / action / pass_fail_criteria /
                         evidence_expected            -> decomp_done_ts
  --ack <file>           at least 5 non-empty lines: the inventory of the
                         files to touch plus the risks -> ack_done_ts

The artifact is VALIDATED: no stamp on an empty file, and no stamp from a bare
`touch`. Its path is kept in the state (decomp_done_file / ack_done_file), so
what was actually decomposed or acked stays auditable after the fact. Every run is
journaled to gate-stats under the `interlock` hook as `observe`, posted or
refused alike.

Usage:
  python3 hooks/interlock-stamp.py --session <id> --decomp <steps.json>
  python3 hooks/interlock-stamp.py --session <id> --ack <notes.md>

Environment:
- HARNESS_STATE_DIR  state directory (default: ~/.harness)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import STATE_DIR, gate_stat
except Exception:
    print("interlock-stamp: _hook helper unavailable, cannot post a stamp.",
          file=sys.stderr)
    sys.exit(1)

HOOK = "interlock"
WINDOW_MIN = 30
REQUIRED_STEP_KEYS = {"step", "action", "pass_fail_criteria",
                      "evidence_expected"}
MIN_ACK_LINES = 5


def refuse(why: str) -> int:
    gate_stat(HOOK, "observe", event="stamp-refused", why=why[:200])
    print(f"interlock-stamp REFUSED: {why}", file=sys.stderr)
    return 1


def check_decomposition(text: str) -> str:
    """Empty string when the steps artifact holds, else the reason it does not."""
    try:
        steps = json.loads(text)
    except ValueError as exc:
        return f"invalid steps JSON ({exc})"
    if not isinstance(steps, list) or not steps:
        return "steps: a non-empty JSON list is expected"
    for s in steps:
        if not isinstance(s, dict) or not REQUIRED_STEP_KEYS.issubset(s):
            return ("every step must carry step, action, pass_fail_criteria, "
                    "evidence_expected")
    return ""


def check_ack(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < MIN_ACK_LINES:
        return (f"ack too thin ({len(lines)} non-empty lines, "
                f"{MIN_ACK_LINES} expected): inventory plus risks")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="post the interlock preparation-step stamp "
                    f"({WINDOW_MIN}-minute window)")
    ap.add_argument("--session", required=True,
                    help="the session id printed by the gate")
    door = ap.add_mutually_exclusive_group(required=True)
    door.add_argument("--decomp", metavar="STEPS_JSON",
                      help="JSON list of steps: the decomposition door")
    door.add_argument("--ack", metavar="FILE",
                      help="inventory plus risks, 5 non-empty lines minimum")
    args = ap.parse_args()

    session = args.session.strip()
    if not session:
        return refuse("empty session id")
    mode = "decomp" if args.decomp else "ack"
    path = args.decomp or args.ack
    if not os.path.isfile(path):
        return refuse(f"artifact not found: {path}")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        return refuse(f"artifact unreadable: {exc}")

    problem = check_decomposition(text) if mode == "decomp" else check_ack(text)
    if problem:
        return refuse(problem)

    ts_key = f"{mode}_done_ts"
    file_key = f"{mode}_done_file"
    sp = Path(STATE_DIR) / f"interlock-{session}.json"
    state: dict = {}
    try:
        if sp.exists():
            loaded = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded  # merge: the edit trail and the other door stay
    except (OSError, ValueError):
        state = {}
    state[ts_key] = time.time()
    state[file_key] = os.path.abspath(path)
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        tmp = sp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(str(tmp), str(sp))
    except OSError as exc:
        return refuse(f"cannot write the state file: {exc}")

    label = "decomposition" if mode == "decomp" else "mission ack"
    gate_stat(HOOK, "observe", event="stamp-posted", door=mode,
              artifact=os.path.abspath(path))
    print(f"interlock: {label} stamped for session {session} "
          f"({WINDOW_MIN}-minute window).")
    print(f"state: {sp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
