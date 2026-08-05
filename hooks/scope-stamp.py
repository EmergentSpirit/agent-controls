#!/usr/bin/env python3
"""scope-stamp.py -- post the one-shot bypass stamp of the scope-write gate.

Run this ONLY after an explicit human GO in the session. The stamp opens ONE
path prefix for 30 minutes; the reason is mandatory and journaled (gate-stats:
scope-write / observe). Posting it again restarts the window. Removing it: rm
the file.

The gate never treats a stamp as a global disable: a stamp without an ABSOLUTE
prefix is worthless, and a target outside that prefix is blocked exactly as
before. The trade-off is deliberate -- the hard criterion becomes "good will
plus a dated trace plus a short window" rather than "good will" alone.

Usage:
  python3 scope-stamp.py /allowed/path/prefix --reason "human GO: ..."

Environment:
- HARNESS_SCOPE_WRITE_STAMP  stamp path (default: $HARNESS_STATE_DIR/scope-write.stamp)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import STATE_DIR, gate_stat
except Exception:
    print("scope-stamp: _hook helper unavailable, cannot post a stamp.",
          file=sys.stderr)
    sys.exit(1)

HOOK = "scope-write"
TTL_MIN = 30


def stamp_path() -> str:
    """Same contract as the gate: env override, else $HARNESS_STATE_DIR."""
    return os.environ.get("HARNESS_SCOPE_WRITE_STAMP") or os.path.join(
        STATE_DIR, "scope-write.stamp")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=f"post the one-shot scope-write bypass stamp ({TTL_MIN} min)")
    ap.add_argument("prefix", help="the ONE absolute path prefix to allow")
    ap.add_argument("--reason", required=True,
                    help="mandatory: the human GO and the why, both journaled")
    args = ap.parse_args()

    reason = args.reason.strip()
    if not reason:
        print("stamp REFUSED: the reason is mandatory (the human GO, in words).",
              file=sys.stderr)
        return 1
    prefix = os.path.expanduser(args.prefix)
    if not os.path.isabs(prefix):
        print(f"stamp REFUSED: prefix is not absolute ({args.prefix}).",
              file=sys.stderr)
        return 1
    prefix = os.path.normpath(prefix)

    now = datetime.now()
    stamp = {"ts": now.isoformat(timespec="seconds"),
             "allowed_prefix": prefix,
             "reason": reason}
    path = stamp_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(stamp, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"stamp REFUSED: cannot write {path} ({exc}).", file=sys.stderr)
        return 1

    gate_stat(HOOK, "observe", event="stamp-posted", prefix=prefix,
              reason=reason)
    until = (now + timedelta(minutes=TTL_MIN)).strftime("%H:%M")
    print(f"stamp posted: {prefix} allowed for {TTL_MIN} min (until ~{until}).")
    print(f"file: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
