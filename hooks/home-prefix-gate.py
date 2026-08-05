#!/usr/bin/env python3
"""PreToolUse gate on Bash: blocks any command prefixed with a HOME= assignment.

WHY (production post-mortem): inside the Bash tool, reassigning HOME as a
command prefix makes the output DISAPPEAR while exit stays 0, with no error
at all:

    env HOME=/some/other/home python3 -c "print('hello')"
    → (no output)   exit=0        (measured, including with /usr/bin/python3)

The tool's output capture relies on artifacts under $HOME (including the
shell snapshot re-sourced on every command): moving HOME breaks the capture,
not the program.

The trap is that the false result is SILENT and REPRODUCIBLE, so it has every
appearance of a real measurement. It led an agent to conclude twice in a row
that a healthy gate "was not blocking", right before patching code that
worked perfectly (verified 4/4 afterwards with the proper method).

WORKAROUND to hand the agent: go through a script that writes its verdict to
DISK, then read the file — the output no longer crosses the broken capture
path.

ZERO possible false positives: the construct NEVER works in this tool, so
blocking it costs no legitimate usage.

Disarm (one session): HARNESS_HOME_PREFIX_GATE_DISABLE=1
"""
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import gate_stat
except Exception:
    def gate_stat(*a, **k):
        pass

# Split with shlex, NOT a regex: `sed 's|HOME=/a|HOME=/b|'` produced a false
# positive on the very first trial because a naive split on "|" cuts INSIDE
# the quotes.
SEPARATORS = {"|", "||", "&&", ";", ";;", "&", "(", ")", "\n"}
ASSIGN_ANY = re.compile(r"^[A-Za-z_]\w*\+?=")
# `HOMEBREW_PREFIX=` must not match: the exact name is required
ASSIGN_HOME = re.compile(r"^HOME=")


def segments(cmd: str):
    """Shell segments, quotes respected. None when unparsable."""
    try:
        toks = list(shlex.shlex(cmd, posix=False, punctuation_chars=True))
    except ValueError:
        return None
    segs, cur = [], []
    for t in toks:
        if t in SEPARATORS:
            if cur:
                segs.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def dangerous_segment(seg) -> bool:
    """True when the HEAD of the segment assigns HOME (breaking the capture)."""
    i = 0
    if i < len(seg) and seg[i] == "export":
        i += 1
    elif i < len(seg) and seg[i] == "env":
        i += 1
        while i < len(seg) and seg[i].startswith("-"):
            i += 1
    while i < len(seg):
        if ASSIGN_HOME.match(seg[i]):
            return True
        if ASSIGN_ANY.match(seg[i]):  # FOO=1 HOME=/x cmd
            i += 1
            continue
        return False  # the head is not an assignment: `grep HOME= f` is sane
    return False


def dangerous_command(cmd: str) -> bool:
    segs = segments(cmd)
    if segs is None:
        return False  # unparsable: fail-open. A false negative costs one bad
        # measurement; a false positive would block legitimate work. This is
        # the opposite trade-off from gates where a false negative can freeze
        # a whole production pane.
    return any(dangerous_segment(s) for s in segs)


def main() -> int:
    if os.environ.get("HARNESS_HOME_PREFIX_GATE_DISABLE") == "1":
        gate_stat("home-prefix", "skip-disabled")
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        gate_stat("home-prefix", "fail-open")
        return 0  # unreadable input = never block blindly
    if data.get("tool_name") != "Bash":
        gate_stat("home-prefix", "skip-not-bash")
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not dangerous_command(cmd):
        gate_stat("home-prefix", "pass")
        return 0
    gate_stat("home-prefix", "block", cmd=cmd[:120])
    sys.stderr.write(
        "BLOCKED (HOME= prefix gate): reassigning HOME as a command prefix "
        "BREAKS the Bash tool's output capture — the output comes back EMPTY "
        "with exit=0, even for a plain print. The false result is silent and "
        "reproducible: it looks like a measurement and is not one. "
        "Instead: write a .sh that runs the trial and WRITES ITS VERDICT TO A "
        "FILE, launch it, then read the file. To just read a process's "
        "environment, `tr '\\0' '\\n' < /proc/<pid>/environ`. "
        "Session kill-switch: HARNESS_HOME_PREFIX_GATE_DISABLE=1\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
