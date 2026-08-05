#!/usr/bin/env python3
"""PreToolUse gate on Write|Edit|MultiEdit: a STANDING settings file is never
edited without an explicit human GO.

WHY (production post-mortem): an agent wired a hook into the shared settings
file on its own initiative. The vendor's built-in auto-mode classifier did
stop it that day, but that classifier is not ours: it does not bite in
accept-edits mode, and it can be recalibrated at any time without notice.
Mutating a global settings file is not a local edit -- it changes the
STANDING behavior of every pane on that machine at its next boot. That call
belongs to the human operator.

Rule: Write / Edit / MultiEdit on a protected settings file -> BLOCK exit 2,
unless a fresh GO stamp (< 30 min) was posted AFTER a real human GO. Same
family of lock as the other stamped gates: short window, auditable trace.
The stamp is never forged without a real GO: the gesture is journaled
(gate-stats plus the stamp's own content), so a forged GO is visible after
the fact.

Environment:
- HARNESS_PROTECTED_SETTINGS  colon-separated protected config paths
                              (default: ~/.claude/settings.json). An entry
                              ending with "/" (or naming an existing
                              directory) protects the settings FAMILY inside
                              that directory.
- HARNESS_SETTINGS_STAMP      human-GO stamp path
                              (default: $HARNESS_STATE_DIR/settings-go.stamp)

Disarm (one session): HARNESS_SETTINGS_GO_GATE_DISABLE=1
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import STATE_DIR, gate_stat, read_stdin_json
except Exception:
    sys.exit(0)  # no helper, no gate: fail-open before anything else

HOOK = "settings-go"
TTL_S = 30 * 60
DEFAULT_PROTECTED = "~/.claude/settings.json"
# Inside a protected directory, the whole settings family is protected: the
# sibling files are read at boot exactly like the main one.
FAMILY_EXACT = ("settings.json", "settings.local.json")
FAMILY_SUFFIX = "-settings.json"


def protected_targets() -> tuple[set[str], set[str]]:
    """(exact files, directories) drawn from HARNESS_PROTECTED_SETTINGS.

    Each colon-separated entry is either a FILE (protected as itself, and its
    directory joins the settings-family scan) or a DIRECTORY -- an entry
    ending with "/" or naming an existing directory -- whose settings family
    is protected. Example:
    `HARNESS_PROTECTED_SETTINGS=~/.claude/settings.json:~/.config/agent/`
    """
    raw = os.environ.get("HARNESS_PROTECTED_SETTINGS") or DEFAULT_PROTECTED
    files: set[str] = set()
    dirs: set[str] = set()
    for item in raw.split(":"):
        item = item.strip()
        if not item:
            continue
        p = os.path.normpath(os.path.expanduser(item))
        if item.endswith("/") or os.path.isdir(p):
            dirs.add(p)
        else:
            files.add(p)
            dirs.add(os.path.dirname(p))
    return files, dirs


def is_protected(path: str, cwd: str = "") -> bool:
    """True only for a protected settings file: zero false positives elsewhere.

    A relative path is resolved against the session cwd, otherwise the same
    file reached by a shorter name would walk straight through the gate.
    """
    if not path:
        return False
    p = os.path.normpath(os.path.join(cwd or os.getcwd(),
                                      os.path.expanduser(path)))
    files, dirs = protected_targets()
    if p in files:
        return True
    if os.path.dirname(p) not in dirs:
        return False
    base = os.path.basename(p)
    return base in FAMILY_EXACT or base.endswith(FAMILY_SUFFIX)


def stamp_path() -> str:
    """Path of the human-GO stamp, shared with the other stamped gates."""
    return os.environ.get("HARNESS_SETTINGS_STAMP") or os.path.join(
        STATE_DIR, "settings-go.stamp")


def stamp_is_fresh() -> bool:
    try:
        return (time.time() - os.path.getmtime(stamp_path())) < TTL_S
    except OSError:
        return False


def main() -> None:
    if os.environ.get("HARNESS_SETTINGS_GO_GATE_DISABLE") == "1":
        gate_stat(HOOK, "skip-disabled")
        sys.exit(0)
    data = read_stdin_json()
    if not data:
        gate_stat(HOOK, "fail-open")
        sys.exit(0)  # unreadable input: never block blindly
    if data.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        gate_stat(HOOK, "skip-out-of-scope")
        sys.exit(0)
    path = (data.get("tool_input") or {}).get("file_path") or ""
    cwd = data.get("cwd") or os.getcwd()
    if not is_protected(path, cwd):
        gate_stat(HOOK, "pass")
        sys.exit(0)
    if stamp_is_fresh():
        gate_stat(HOOK, "skip-stamp", path=path)
        sys.exit(0)
    gate_stat(HOOK, "block", path=path)
    sys.stderr.write(
        "BLOCKED (settings-go gate): editing a STANDING settings file without "
        "an explicit human GO.\n"
        f"  file: {path}\n"
        "A shared settings file changes the behavior of EVERY pane on this "
        "machine at its next boot: that call belongs to the human operator, "
        "not to the model. Do not lean on the vendor's auto-mode classifier "
        "either -- it does not bite in accept-edits mode and can be "
        "recalibrated at any time.\n"
        "1. TELL the operator what you want to wire and why;\n"
        "2. after an explicit GO, refresh the stamp:\n"
        "     touch \"${HARNESS_SETTINGS_STAMP:-$HOME/.harness/"
        "settings-go.stamp}\"\n"
        "3. redo the edit (30-minute window, journaled as skip-stamp).\n"
        "NEVER forge the stamp without a real GO: every gesture lands in the "
        "gate-stats journal.\n"
        "Session kill-switch: HARNESS_SETTINGS_GO_GATE_DISABLE=1\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
