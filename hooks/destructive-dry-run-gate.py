#!/usr/bin/env python3
"""PreToolUse gate on Write|Edit|MultiEdit: a disk-destroying script handed to
a human must run DRY BY DEFAULT.

WHY (production post-mortem): an agent handed the operator a
`format-rescue-key.sh` that wiped a USB stick, with no dry-run mode and
without a single one of its guards ever having been exercised. The operator
was the one who caught it -- "re-read your script, it does a lot of acting in
there, I would not want you to erase something important". The forced re-read
found a name collision that could have closed the operator's encrypted volume
(`rescue` vs `secure`), and a window where `/dev/sda` could drift onto a
different disk between the check and the wipe.

Rule: a script handed to a human that DESTROYS a disk must execute dry BY
DEFAULT. Destroying takes an explicit gesture (`--go`), never the reverse.
It is the cheapest protection of the lot: it makes a wrong-target mistake
impossible and costs nothing when everything goes right.

Zero false positives by construction: it looks ONLY at `.sh` files written
inside the operator's own script directories (the scripts a human launches by
hand), ignores comment lines, lets read-only forms through (`wipefs` without
-a/-f, `sfdisk --dump`), and demands the safety net only when a disk-
destroying command is actually present.

Environment:
- HARNESS_OPERATOR_SCRIPT_DIRS  colon-separated directories holding scripts a
                                human launches by hand
                                (default: ~/operator-scripts)

Disarm (one session): HARNESS_DESTRUCTIVE_DRY_RUN_GATE_DISABLE=1
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import gate_stat, read_stdin_json
except Exception:
    sys.exit(0)  # no helper, no gate: fail-open before anything else

HOOK = "destructive-dry-run"

# Commands that destroy the contents of a disk with no way back.
DESTRUCTIVE_RE = re.compile(
    r"""(?:^|[;&|(\s])(?:sudo\s+)?(?:
          wipefs\b(?=[^\n]*\s-\w*[af])   # wipefs that ERASES (-a/-f), not the listing
        | mkfs\.\w+\b
        | mkfs\s+-t\b
        | cryptsetup\s+luksFormat\b
        | sfdisk\b(?![^\n]*--dump)       # sfdisk that writes, not --dump
        | sgdisk\b(?![^\n]*(?:--print|-p)\b)
        | shred\b
        | dd\b[^\n]*\bof=/dev/
        | parted\b[^\n]*\bmklabel\b
        | blkdiscard\b
    )""",
    re.VERBOSE | re.MULTILINE,
)

# The safety net: a dry-run mode ON BY DEFAULT + an explicit gesture to destroy.
DEFAULT_ON_RE = re.compile(r"^\s*DRY[_-]?RUN\s*=\s*(?:1|true|yes)\b",
                           re.IGNORECASE | re.MULTILINE)
EXPLICIT_GESTURE_RE = re.compile(r"--go\b|--for-real\b|--execute\b|--confirm\b")


def operator_script_dirs() -> list[str]:
    """Directories holding the scripts a HUMAN launches by hand.

    Those are the only ones in scope: a script the agent runs itself is
    reviewed by the agent, a script handed over is run blind. Override with
    HARNESS_OPERATOR_SCRIPT_DIRS when they live elsewhere, e.g.
    `HARNESS_OPERATOR_SCRIPT_DIRS=~/bin/ops:~/handoff-scripts`.
    """
    raw = os.environ.get("HARNESS_OPERATOR_SCRIPT_DIRS")
    candidates = raw.split(":") if raw else ["~/operator-scripts"]
    return [os.path.normpath(os.path.expanduser(p)) for p in candidates if p]


def under_operator_scripts(path: str) -> bool:
    """True when `path` sits anywhere inside an operator script directory
    (at any depth: those trees carry done/ and archive/ subdirectories)."""
    p = os.path.normpath(os.path.expanduser(path or ""))
    return any(p == d or p.startswith(d + os.sep) for d in operator_script_dirs())


def strip_comment_lines(text: str) -> str:
    """A wipefs quoted in an explanation is not a wipefs being run."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def written_content(data: dict) -> tuple[str, str]:
    """The text being written and the file it goes to. ("", path) when the
    payload carries no content (a plain read, a rename, a deletion)."""
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ""
    for key in ("content", "new_string"):
        v = ti.get(key)
        if isinstance(v, str) and v:
            return v, path
    return "", path


def main() -> None:
    if os.environ.get("HARNESS_DESTRUCTIVE_DRY_RUN_GATE_DISABLE") == "1":
        gate_stat(HOOK, "skip-disabled")
        sys.exit(0)
    data = read_stdin_json()
    if not data:
        gate_stat(HOOK, "fail-open")
        sys.exit(0)  # unreadable input: never block blindly
    if data.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        gate_stat(HOOK, "skip-out-of-scope")
        sys.exit(0)
    text, path = written_content(data)
    if not text or not path.endswith(".sh"):
        gate_stat(HOOK, "skip-not-shell")
        sys.exit(0)
    if not under_operator_scripts(path):
        gate_stat(HOOK, "skip-out-of-perimeter")
        sys.exit(0)

    code = strip_comment_lines(text)
    hits = [" ".join(m.group(0).split()) for m in DESTRUCTIVE_RE.finditer(code)]
    if not hits:
        gate_stat(HOOK, "pass", path=path)
        sys.exit(0)
    if DEFAULT_ON_RE.search(code) and EXPLICIT_GESTURE_RE.search(code):
        gate_stat(HOOK, "pass", path=path, n=len(hits), guarded=True)
        sys.exit(0)

    missing = []
    if not DEFAULT_ON_RE.search(code):
        missing.append("a dry run ON BY DEFAULT (e.g. `DRYRUN=1` at the top)")
    if not EXPLICIT_GESTURE_RE.search(code):
        missing.append("an explicit gesture to destroy (e.g. `--go`)")

    gate_stat(HOOK, "block", path=path, n=len(hits))
    sys.stderr.write(
        "BLOCKED (destructive-dry-run gate): script that destroys a disk with "
        "NO dry run by default.\n\n"
        f"  file    : {path}\n"
        "  disk-destroying command(s):\n"
        + "".join(f"    {h}\n" for h in dict.fromkeys(hits))
        + "  missing : " + " AND ".join(missing) + "\n\n"
        "An operator once had to catch this by hand: the script wiped a USB "
        "key with none of its guards ever exercised. The forced re-read found "
        "a name collision that could have closed the operator's encrypted "
        "volume, and a window where /dev/sda drifted onto another disk between "
        "the check and the wipe. Destroying must take a gesture; destroying "
        "nothing must be the default.\n\n"
        "Expected shape:\n"
        "  DRYRUN=1\n"
        "  for a in \"$@\"; do case \"$a\" in --go) DRYRUN=0 ;; esac; done\n"
        "  ...all the checks...\n"
        "  [ \"$DRYRUN\" -eq 1 ] && { echo 'NOTHING TOUCHED, rerun with --go'; "
        "exit 0; }\n\n"
        "Session kill-switch: HARNESS_DESTRUCTIVE_DRY_RUN_GATE_DISABLE=1\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
