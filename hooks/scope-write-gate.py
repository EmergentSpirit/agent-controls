#!/usr/bin/env python3
"""PreToolUse gate on Write|Edit|MultiEdit|NotebookEdit: an agent writes only
inside its own perimeter.

WHY (production post-mortem): a research-role agent drifted twice in the same
week into execution work that was not its job -- it dropped a hook file on
disk, edited the shared settings file, and hand-fabricated a test transcript,
while its mandate stopped at the written brief handed to the builder role.
Nothing crashed, which is the whole problem: the drift is SILENT. The work
looks done, it lands in a directory nobody owns, and the role that owns the
gesture never reviews it.

Criterion: effectiveness must not depend on the model's good will. A write
outside the perimeter is exit 2 (BLOCK), not a WARN one can talk past.

Environment:
- HARNESS_WRITE_SCOPE        colon-separated write perimeter. Each entry is a
                             directory (everything under it is writable) or an
                             exact file. Default: the session cwd plus the
                             system temp dir -- scratch files are not the drift
                             this gate is about.
                             EXAMPLE for a research role:
                             HARNESS_WRITE_SCOPE=~/work/research:~/.harness/memory:/tmp
- HARNESS_SCOPE_WRITE_STAMP  one-shot bypass stamp written by scope-stamp.py
                             (default: $HARNESS_STATE_DIR/scope-write.stamp)

The stamp is the sanctioned exception: posted AFTER an explicit human GO, it
opens ONE path prefix for 30 minutes, the reason is mandatory, and every
pass-through is journaled as skip-stamp. Missing, expired, corrupt or
non-covering stamp: the block stands (fail-CLOSED on the bypass only).

Disarm (one session): HARNESS_SCOPE_WRITE_GATE_DISABLE=1

Exit codes:
  0  allowed (or out of this gate's scope, or unreadable payload -> fail-open:
     the gate bars drift, it is not a security wall)
  2  BLOCK -- write outside the perimeter
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import STATE_DIR, gate_stat, read_stdin_json
except Exception:
    sys.exit(0)  # no helper, no gate: fail-open before anything else

HOOK = "scope-write"
DISABLE_ENV = "HARNESS_SCOPE_WRITE_GATE_DISABLE"
STAMP_TTL_S = 30 * 60
WATCHED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
PATH_KEYS = ("file_path", "notebook_path", "path")
STAMP_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "scope-stamp.py")


def target_path(payload: dict) -> str:
    """The path the tool is about to write. Empty when the payload carries
    none -- nothing to judge, so nothing to block."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    for key in PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def resolve(path: str, cwd: str = "") -> str:
    """Absolute, symlink-free form of `path`.

    A relative path resolves against the SESSION cwd, and symlinks are
    followed, otherwise the same file reached by a shorter name or through a
    link would walk straight around the perimeter (`../` escapes included).
    """
    p = os.path.expanduser(path)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return os.path.realpath(os.path.normpath(p))


def scope_entries(cwd: str) -> list[str]:
    """The write perimeter, resolved. Default: session cwd + system temp dir."""
    raw = os.environ.get("HARNESS_WRITE_SCOPE")
    if raw:
        items = [i for i in (x.strip() for x in raw.split(":")) if i]
    else:
        items = [cwd or os.getcwd(), tempfile.gettempdir()]
    return [resolve(i, cwd) for i in items]


def covers(prefix: str, target: str) -> bool:
    """True when `target` is `prefix` itself or sits under it. Compares whole
    path segments: `/work/research2` is NOT inside `/work/research`."""
    return target == prefix or target.startswith(prefix.rstrip(os.sep) + os.sep)


def in_scope(path: str, cwd: str) -> bool:
    target = resolve(path, cwd)
    return any(covers(entry, target) for entry in scope_entries(cwd))


def stamp_path() -> str:
    """Path of the one-shot bypass stamp. Overridable FOR TESTS; the gate's
    own activation depends on no variable whatsoever."""
    return os.environ.get("HARNESS_SCOPE_WRITE_STAMP") or os.path.join(
        STATE_DIR, "scope-write.stamp")


def stamp_allows(path: str, cwd: str):
    """The sanctioned bypass: a FRESH stamp (< 30 min) whose allowed prefix
    covers the target. Returns {prefix, reason} or None.

    Any anomaly -- absent, expired, corrupt JSON, missing or relative prefix
    -- returns None: fail-CLOSED, the default BLOCK stays the behavior. A
    stamp is never a global disable: ONE absolute prefix, or nothing.
    """
    try:
        sp = stamp_path()
        if time.time() - os.path.getmtime(sp) > STAMP_TTL_S:
            return None
        with open(sp, encoding="utf-8") as f:
            data = json.load(f)
        prefix = data.get("allowed_prefix")
        if not isinstance(prefix, str) or not prefix.strip().startswith("/"):
            return None
        pref = resolve(prefix.strip(), cwd)
        if covers(pref, resolve(path, cwd)):
            # The stamp is NOT consumed inside its window; in exchange, EVERY
            # pass-through is journaled.
            return {"prefix": pref, "reason": str(data.get("reason", ""))}
        return None
    except Exception:
        return None


def main() -> None:
    if os.environ.get(DISABLE_ENV) == "1":
        gate_stat(HOOK, "skip-disabled")
        sys.exit(0)
    data = read_stdin_json()
    if not data:
        gate_stat(HOOK, "fail-open")
        sys.exit(0)  # unreadable input: never block blindly
    tool = data.get("tool_name", "")
    if tool and tool not in WATCHED_TOOLS:
        gate_stat(HOOK, "skip-out-of-scope")
        sys.exit(0)
    cwd = data.get("cwd") or os.getcwd()
    path = target_path(data)
    if not path:
        gate_stat(HOOK, "skip-no-path")
        sys.exit(0)
    if in_scope(path, cwd):
        gate_stat(HOOK, "pass")
        sys.exit(0)
    allow = stamp_allows(path, cwd)
    if allow:
        gate_stat(HOOK, "skip-stamp", path=path, prefix=allow["prefix"],
                  reason=allow["reason"])
        sys.exit(0)
    gate_stat(HOOK, "block", path=path)
    sys.stderr.write(
        "BLOCKED (scope-write gate): writing OUTSIDE this agent's perimeter.\n"
        f"  target:    {path}\n"
        f"  perimeter: {':'.join(scope_entries(cwd))}\n"
        "Each role owns a perimeter. Writing code or mutating configuration "
        "outside it is another role's gesture, and that drift is silent: it "
        "looks like work done, it lands where nobody reviews it.\n"
        "Normal route: leave a dated deliverable INSIDE your perimeter and "
        "hand the gesture over to the role that owns it.\n"
        "Sanctioned one-shot, only AFTER an explicit human GO:\n"
        f"  python3 {STAMP_CLI} <prefix> --reason '...'\n"
        "(30-minute window, ONE prefix, journaled as skip-stamp.)\n"
        "Widen the perimeter for good instead: HARNESS_WRITE_SCOPE "
        "(colon-separated).\n"
        f"Session kill-switch: {DISABLE_ENV}=1\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # fail-open: a broken gate never blocks the work
