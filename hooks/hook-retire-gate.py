#!/usr/bin/env python3
"""PreToolUse gate on Bash: a LIVE hook is never renamed, only copied.

WHY (production post-mortem): while shipping a reworked end-of-mission hook,
an agent rewired the settings file and then `git mv`-ed the two old hooks to
`.bak`. Already-open panes read the settings of their OWN BOOT: they were
still calling the old paths. The operator's production pane locked itself up
— a `UserPromptSubmit` hook whose file no longer existed exits non-zero, so
EVERY prompt came back blocked. Diagnosis and restore took a second agent.

Rule: any `mv` / `rm` / `git mv` whose SOURCE is a file at the top level of a
live hooks directory → BLOCK exit 2. The safe path is the COPY: `cp` to a
dated `.bak`, the original stays in place until every pane has been
restarted. Things that never block: `*.bak*` (cleaning up those copies),
anything under `tests/`, and `cp` itself.

Orphan cleanup sanctioned by a human: a fresh settings-GO stamp (< 30 min,
the same lock the protected-settings gate uses, and journaled) turns the
block into a traced allow.

Design: deterministic, no activation environment variable — the gate is on
as soon as it is wired. Reversible: remove the line from the settings file
(with a stamp) or restore the day's backup.

Environment:
- HARNESS_HOOK_DIRS       colon-separated live hook directories
                          (default: this file's own directory + ~/.claude/hooks)
- HARNESS_SETTINGS_STAMP  human-GO stamp path
                          (default: $HARNESS_STATE_DIR/settings-go.stamp)
"""
from __future__ import annotations

import os
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import STATE_DIR, gate_stat, read_stdin_json
except Exception:
    sys.exit(0)  # no helper, no gate: fail-open before anything else

TTL_S = 30 * 60
SEPARATORS = {"&&", "||", ";", "|"}
PREFIXES = {"sudo", "command", "env", "nice", "timeout"}


def hook_dirs() -> set[str]:
    """The directories whose top-level files are LIVE hooks.

    Default: the directory this gate lives in (a harness ships its hooks
    together) plus the agent's own `~/.claude/hooks`. Override with
    HARNESS_HOOK_DIRS when hooks live elsewhere, e.g.
    `HARNESS_HOOK_DIRS=~/work/agent-a/hooks:~/work/agent-b/hooks`.
    """
    raw = os.environ.get("HARNESS_HOOK_DIRS")
    if raw:
        candidates = raw.split(":")
    else:
        candidates = [os.path.dirname(os.path.abspath(__file__)),
                      os.path.expanduser("~/.claude/hooks")]
    return {os.path.normpath(os.path.expanduser(p)) for p in candidates if p}


def stamp_path() -> str:
    """Path of the human-GO stamp. Overridable FOR TESTS; the ACTIVATION of
    the gate itself depends on no variable whatsoever."""
    return os.environ.get("HARNESS_SETTINGS_STAMP") or os.path.join(
        STATE_DIR, "settings-go.stamp")


def mv_rm_sources(cmd: str) -> list[str]:
    """The SOURCES of the mv / rm / git mv in the command. Simple,
    deterministic parse: split on shell separators, strip the usual command
    prefixes and inline VAR=value assignments, then read the arguments."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    segments: list[list[str]] = [[]]
    for t in toks:
        if t in SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(t)
    sources: list[str] = []
    for s in segments:
        i = 0
        while i < len(s) and (s[i] in PREFIXES or ("=" in s[i] and not s[i].startswith("/"))):
            i += 1
        if i >= len(s):
            continue
        prog, args = os.path.basename(s[i]), s[i + 1:]
        if prog == "git" and args and args[0] == "mv":
            prog, args = "mv", args[1:]
        if prog not in ("mv", "rm", "unlink"):
            continue
        cand = [a for a in args if not a.startswith("-")]
        if prog == "mv" and len(cand) >= 2:
            cand = cand[:-1]  # the last argument is the destination
        sources.extend(cand)
    return sources


def targets_live_hook(path: str, cwd: str) -> bool:
    """True when `path` resolves to a file sitting at the top level of a live
    hooks directory. Backup copies, `tests/` and `test*` files are never
    live: retiring those is exactly the cleanup we want to stay possible."""
    p = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    base = os.path.basename(p)
    if ".bak" in base or "/tests/" in p or base.startswith("test"):
        return False
    return os.path.dirname(p) in hook_dirs()


def stamp_is_fresh() -> bool:
    try:
        return (time.time() - os.path.getmtime(stamp_path())) < TTL_S
    except OSError:
        return False


def main() -> None:
    data = read_stdin_json()
    if not data:
        gate_stat("hook-retire", "fail-open")
        sys.exit(0)  # unreadable input: never block blindly
    if data.get("tool_name", "Bash") != "Bash":
        sys.exit(0)  # other tools are out of scope
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cwd = data.get("cwd") or os.getcwd()
    if cmd and any(k in cmd for k in ("mv", "rm", "unlink")):
        targets = [s for s in mv_rm_sources(cmd) if targets_live_hook(s, cwd)]
    else:
        targets = []
    if not targets:
        gate_stat("hook-retire", "pass")
        sys.exit(0)
    if stamp_is_fresh():
        gate_stat("hook-retire", "skip-stamp", targets=targets)
        sys.exit(0)
    gate_stat("hook-retire", "block", targets=targets)
    sys.stderr.write(
        "BLOCKED (hook-retire gate): you are renaming/deleting a hook that a "
        f"LIVE pane may still be calling: {', '.join(targets)}\n"
        "Settings are read AT BOOT: rewiring the config does NOT free the "
        "file. A production pane froze exactly like this — every prompt "
        "blocked by a hook path that had stopped existing.\n"
        "Safe path: cp to a dated .bak, the original STAYS in place until "
        "every pane has been restarted.\n"
        "Orphan cleanup sanctioned by a human: after an explicit GO, refresh "
        "the stamp\n"
        "  touch \"${HARNESS_SETTINGS_STAMP:-$HOME/.harness/settings-go.stamp}\"\n"
        "then redo the gesture (30-minute window, journaled as skip-stamp).\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
