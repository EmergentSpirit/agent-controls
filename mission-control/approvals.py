#!/usr/bin/env python3
"""approvals.py -- the queue of operations waiting for a human, and only that.

    python3 approvals.py         the pending queue, as JSON

An execution engine that runs operator scripts eventually meets one it refuses
to run alone. It journals an ESCALATION and stops. This module reads that
journal and turns it into a list a person can act on: what the operation is,
what it would do IN PLAIN LANGUAGE, and whether it can be undone.

THIS MODULE ONLY READS. Recording an approval is a mutation and lives behind an
optional module (see server.py). A panel that can approve by accident is worse
than no panel.

TWO RULES DECIDE WHAT SHOWS UP:

- LATEST WINS, SETTLED CLEARS. The most recent escalation per script is the one
  displayed; any later `ran / blocked / halted / archived / cancelled` for that
  same script removes it. Without the second half, a script that was executed
  out of band stays stuck on the panel forever, and a queue that shows
  finished work stops being read.
- THE SCRIPT MUST EXIST, INSIDE THE ALLOWLIST. See below.

PATH CONFINEMENT, AND WHY IT IS NOT PARANOIA. The script path arrives inside a
journal line. The panel does not hold the key that signed that line, so it
cannot verify it. It then wants to `read_text` that path to explain the
operation. Unconfined, that is a file-disclosure primitive: anything able to
append one line to the journal chooses a file, and the panel renders its
contents. So every path is resolved with `realpath` (symlinks followed,
`..` collapsed) and must land inside HARNESS_OPERATOR_SCRIPT_DIRS. Anything
else is skipped: not read, not hashed, not shown. Fail toward refusing.

Environment: see config.py (HARNESS_MC_EXECUTOR_AUDIT,
HARNESS_OPERATOR_SCRIPT_DIRS).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C                       # noqa: E402
import ingest                            # noqa: E402  -- shares iter_jsonl

MAX_SCRIPT_BYTES = 256 * 1024            # a header parse never needs more

# Header lines a script may carry to describe itself, in the shape
# `# Description: rotates the log files`. Anything else in the file is ignored:
# the panel explains INTENT, it never renders a command line.
HEADER_KEYS = ("description", "scope", "class", "reversible", "backup",
               "rollback")

_TIMESTAMP_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}t?\d{0,4}-", re.IGNORECASE)
_CATEGORY_PREFIX = re.compile(
    r"^(deploy|safe-deploy|restart|fix|hotfix|chore|install|config|migrate|other)-",
    re.IGNORECASE)


def in_allowlist(path) -> bool:
    """True only when `path` resolves to somewhere inside a configured operator
    script directory. Empty, unresolvable or outside: False."""
    if not path:
        return False
    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        return False
    for root in C.operator_script_dirs():
        if resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def script_sha256(path):
    """Hash of the script as it is RIGHT NOW. The panel shows it and an
    approval carries it back, so approving means approving the bytes that were
    on screen -- not whatever the file became in between."""
    if not in_allowlist(path):
        return None
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def readable_name(path) -> str:
    """A human label from the file name, never a technical path. Strips a
    leading timestamp and a leading category so `2026-01-04T0930-safe-deploy-
    log-rotation.sh` reads as `log rotation`."""
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    stem = _TIMESTAMP_PREFIX.sub("", stem)
    stem = _CATEGORY_PREFIX.sub("", stem)
    stem = stem.replace("-", " ").replace("_", " ").strip()
    return stem or "this operation"


def describe(path) -> dict:
    """What the operation does, in a sentence a person can act on.

    NEVER EMPTY, never a question mark. A queue entry whose description is
    blank is an entry nobody can decide on, so a script with no usable header
    falls back to a generic sentence built from its name. The fallback is
    honest -- it says the panel does not know the details -- which beats an
    empty cell that looks like a rendering bug."""
    label = readable_name(path)
    generic = ("Sensitive operation on %s. The script carries no description; "
               "read it before approving." % label)
    out = {"description": generic, "scope": "", "class": "",
           "reversible": None, "backup": None, "rollback": None,
           "impact": generic}
    if not in_allowlist(path) or not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_SCRIPT_BYTES)
    except OSError:
        return out

    def header(key):
        match = re.search(r"^\s*#\s*%s\s*:\s*(.+)$" % key, text,
                          re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else ""

    found = {key: header(key) for key in HEADER_KEYS}
    if found["description"]:
        out["description"] = found["description"]
    out["scope"] = found["scope"]
    out["class"] = found["class"]
    for key in ("reversible", "backup", "rollback"):
        raw = found[key].lower()
        if raw:
            out[key] = raw not in ("no", "false", "0", "none")

    # The impact sentence is what a tired person actually reads at the moment
    # of deciding, so it always states reversibility explicitly rather than
    # leaving it to a column somewhere else on the screen.
    if out["reversible"] is True:
        tail = "Reversible: a rollback path is declared."
    elif out["reversible"] is False:
        tail = "NOT reversible: there is no declared way back."
    else:
        tail = "Reversibility is not declared. Assume there is no way back."
    out["impact"] = "%s %s" % (out["description"], tail)
    return out


def pending(audit_path=None) -> dict:
    """{pending: [...], engine_available: bool}.

    `engine_available` is false when there is no audit journal at all, which is
    the normal state for someone who runs no execution engine. The panel then
    says so instead of showing an empty queue that could also mean "nothing is
    waiting" -- two very different messages."""
    audit_path = audit_path or C.executor_audit()
    if not os.path.isfile(audit_path):
        return {"pending": [], "engine_available": False}

    latest = {}
    for obj in ingest.iter_jsonl(audit_path):
        if not isinstance(obj, dict):
            continue
        entry = obj.get("entry") or obj.get("event") or obj
        if not isinstance(entry, dict):
            continue
        script = entry.get("script") or entry.get("target")
        if not script:
            continue
        result = str(entry.get("result") or entry.get("decision") or "").lower()
        if result in C.EXECUTOR_ESCALATED:
            latest[script] = entry           # a newer escalation replaces one
        elif result in C.EXECUTOR_SETTLED:
            latest.pop(script, None)         # handled: leaves the queue

    items = []
    for script, entry in latest.items():
        script = str(script).strip()
        if not script or not in_allowlist(script) or not os.path.isfile(script):
            continue                          # unprovable: neither read nor shown
        result = str(entry.get("result") or entry.get("decision") or "").lower()
        items.append({
            "script": script,
            "name": readable_name(script),
            "sha256": script_sha256(script),
            "op_class": entry.get("op_class") or "",
            # `frozen`: the engine says no human click may release this one, a
            # written review is required. `key_gated`: a hardware presence
            # proof is required. Both are shown, and both are the ENGINE's
            # call -- the panel never softens them.
            "frozen": result == "escalated-frozen",
            "key_gated": result == "escalated-key-gated",
            "reason": entry.get("reason") or "",
            "ts": entry.get("ts") or "",
            "impact": describe(script),
        })
    items.sort(key=lambda item: item.get("ts") or "", reverse=True)
    return {"pending": items, "engine_available": True}


def main() -> int:
    print(json.dumps(pending(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
