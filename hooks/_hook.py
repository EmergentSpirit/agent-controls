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
# This is a filter, not a guarantee: it catches the shapes a command line
# actually carries. Treat the journal as readable by anyone who can read the
# state directory, and keep payloads out of it.
#
# design-note: every quantifier here is BOUNDED and the alternatives do not
# overlap. An earlier version wrapped the keyword alternation in two greedy
# `[A-Za-z0-9_-]*`, which backtracks quadratically: 40 kB of one repeated
# keyword took 400+ seconds. This text can come from a transcript, which the
# project itself treats as hostile input, so a masker that backtracks is a
# remote stall. Keep the bounds, and measure before widening.
_SECRETS = re.compile(
    # Vendor-shaped tokens, recognizable on their own.
    r"sk-[A-Za-z0-9_-]{8,80}|AKIA[A-Z0-9]{12,30}|age1[a-z0-9]{20,80}"
    r"|gh[pousr]_[A-Za-z0-9]{20,80}|xox[abprs]-[A-Za-z0-9-]{10,80}"
    r"|eyJ[A-Za-z0-9_-]{10,200}\.[A-Za-z0-9_-]{10,400}\.[A-Za-z0-9_-]{10,200}"
    # Authorization headers.
    r"|Bearer\s+\S{1,400}"
    # A credential inside a URL: scheme://user:secret@host
    r"|://[^\s/:@]{1,80}:[^\s/@]{1,200}@"
    # NAME=value / NAME: value, where the NAME says it holds a secret. The
    # name is matched as one bounded token, not as two greedy runs around an
    # alternation.
    # `auth` is deliberately NOT a keyword here: it turned `--author=alice`,
    # `AUTHORS:` and `auth: refactor` into masked noise, and the credentials
    # it would have caught (AUTH_TOKEN, Authorization: Bearer) already match
    # on `token` and on the Bearer branch. Over-masking costs a journal you
    # can no longer read back, which is the whole reason to keep one.
    r"|[A-Za-z0-9_-]{0,40}(?:password|passwd|secret|token|api[_-]?key|apikey"
    r"|access[_-]?key|private[_-]?key|credential)"
    r"[A-Za-z0-9_-]{0,40}\s{0,4}[=:]\s{0,4}\S{1,400}"
    # Same names in the flag-then-space form: --api-key VALUE
    r"|--[A-Za-z0-9-]{0,40}(?:password|secret|token|api-?key|access-?key"
    r"|private-?key|credential)[A-Za-z0-9-]{0,40}\s{1,4}\S{1,400}",
    re.I)


def mask_secrets(text, n=200):
    """Scrub secret patterns from at most n characters of `text`.

    Truncation happens FIRST, on purpose. It bounds the work whatever the
    caller hands over, and it costs nothing in coverage: a secret past the
    cut was never going to appear in the output anyway.
    """
    try:
        raw = str(text)
        clipped = raw[:n]
        return _SECRETS.sub("«secret»", clipped) + ("…" if len(raw) > n else "")
    except Exception:
        return ""


def gate_stat(hook, result, **kw):
    """One JSON line per hook execution, whatever the result. Fail-open.
    session_id/cwd auto-added when the stdin carried them.

    Every string value is scrubbed on the way in. Gates journal the command
    or the path that tripped them, and a command can carry a token: without
    this, the journal quietly becomes the least protected copy of a secret
    on the machine. Scrubbing belongs here, not in each caller, because the
    one caller that forgets is the one that leaks.
    """
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "hook": hook, "result": result}
        for k, v in _CTX.items():
            rec.setdefault(k, v)
        for k, v in kw.items():
            kw[k] = mask_secrets(v, 500) if isinstance(v, str) else v
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
