#!/usr/bin/env python3
"""_hook.py — shared helpers for harness hooks.

Design rules:
- Fail-open everywhere: a bug in a hook must never break the agent's flow.
- Every hook execution writes one JSON line to the gate-stats journal,
  whatever the result. That journal is the aliveness signal the sentinel
  cross-checks: a wired gate that never logs is a dead gate that lies.

Environment:
- HARNESS_STATE_DIR  state directory (default: ~/.harness)
- HARNESS_GATE_STATS gate-stats journal path override (tests use a tempdir;
                     never override it in production)
"""
import json
import os
import re
import sys
from datetime import datetime

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("HARNESS_STATE_DIR") or os.path.join(HOME, ".harness")
GATE_STATS = os.environ.get("HARNESS_GATE_STATS") or os.path.join(
    STATE_DIR, "gate-stats.jsonl")


# ─── Session context (observation panel integration) ────────────────────────
# The hook stdin carries session_id/cwd, but stdin can only be read ONCE and
# each hook reads it itself. Rather than editing every wired hook (that many
# silent failure opportunities), we install a TRANSPARENT tap on sys.stdin at
# import time: the hook's own read passes through, the JSON is captured on
# the way, gate_stat() picks it up. design-note: the "magic" is this single
# point; do not "fix" it by dispersing the capture into each hook.
_CTX = {}


def _capture_ctx(raw):
    """Observe one stdin read. Fail-open: never an exception toward the hook."""
    try:
        d = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        if isinstance(d, dict):
            for key in ("session_id", "cwd"):
                v = d.get(key)
                if isinstance(v, str) and v:
                    _CTX[key] = v
    except Exception:
        pass


class _StdinTap:
    """Read-transparent proxy over sys.stdin. Reads NOTHING by itself (lazy)."""

    def __init__(self, raw):
        self._raw = raw

    def read(self, *a):
        data = self._raw.read(*a)
        _capture_ctx(data)
        return data

    def readline(self, *a):
        data = self._raw.readline(*a)
        _capture_ctx(data)
        return data

    def __iter__(self):
        return iter(self._raw)

    def __getattr__(self, name):
        return getattr(self._raw, name)


try:  # armed once, even if the module is re-imported
    if not isinstance(sys.stdin, _StdinTap):
        sys.stdin = _StdinTap(sys.stdin)
except Exception:
    pass

# Secret patterns scrubbed BEFORE anything is written to the journal.
_SECRETS = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{12,}|age1[a-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}"
    r"|Bearer\s+\S+|(?:password|passwd|token|secret|api_key|apikey)\s*[=:]\s*\S+)",
    re.I)


def mask_secrets(text, n=200):
    """Truncate to n chars and scrub secret patterns. For the `target` field."""
    try:
        t = _SECRETS.sub("«secret»", str(text))
        return t[:n] + ("…" if len(t) > n else "")
    except Exception:
        return ""


def gate_stat(hook, result, **kw):
    """One JSON line per hook execution, whatever the result. Fail-open.
    session_id/cwd auto-added when the stdin carried them."""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "hook": hook, "result": result}
        for k, v in _CTX.items():
            rec.setdefault(k, v)
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
        raw = sys.stdin.read()  # goes through the tap -> ctx set on the way
        d = json.loads(raw) if raw.strip() else {}
        if isinstance(d, dict):
            _capture_ctx(raw)
            return d
        return {}
    except Exception:
        return {}


def find_project_root(start):
    """Walk up from `start` to the first directory containing PROJECT.md or
    PROJECT_PLAN.md. Returns (root, sheet_name) or None. This is project
    resolution without git (not every project is a git repo)."""
    try:
        d = os.path.abspath(start or os.getcwd())
    except Exception:
        return None
    while True:
        for name in ("PROJECT.md", "PROJECT_PLAN.md"):
            if os.path.isfile(os.path.join(d, name)):
                return (d, name)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load_transcript(path, limit=400):
    """Returns the list of JSONL events (last `limit`). [] when unreadable."""
    if not path or not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-limit:] if limit else out


def assistant_text_blocks(ev):
    """Concatenate the text (type=text blocks) of one assistant event."""
    msg = ev.get("message", {})
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""
