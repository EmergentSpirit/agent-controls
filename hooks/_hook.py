#!/usr/bin/env python3
"""_hook.py — minimal shared helpers for harness hooks.

Design rules:
- Fail-open everywhere: a bug in a hook must never break the agent's flow.
- Every hook execution writes one JSON line to the gate-stats journal,
  whatever the result. That journal is the aliveness signal the sentinel
  cross-checks: a wired gate that never logs is a dead gate that lies.

This is the batch-0 core (what memory-verdict-gate needs). The full helper
(stdin context tap, transcript reader, project-root resolution) lands with
the core-hooks batch.

Environment:
- HARNESS_STATE_DIR  state directory (default: ~/.harness)
- HARNESS_GATE_STATS gate-stats journal path override (tests use a tempdir;
                     never override it in production)
"""
import json
import os
import sys
from datetime import datetime

STATE_DIR = os.environ.get("HARNESS_STATE_DIR") or os.path.join(
    os.path.expanduser("~"), ".harness")
GATE_STATS = os.environ.get("HARNESS_GATE_STATS") or os.path.join(
    STATE_DIR, "gate-stats.jsonl")


def gate_stat(hook, result, **kw):
    """One JSON line per hook execution, whatever the result. Fail-open."""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "hook": hook, "result": result}
        rec.update(kw)
        os.makedirs(os.path.dirname(GATE_STATS), exist_ok=True)
        with open(GATE_STATS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_stdin_json():
    """Claude Code hook contract arrives as JSON on stdin.
    Returns {} when stdin is empty or unreadable (fail-open)."""
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        d = json.loads(raw) if raw.strip() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}
