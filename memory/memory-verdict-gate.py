#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory-verdict-gate.py — the verdict on top, and an index that agrees.

Born from a real incident: a stale "RELAUNCHED" line in the memory index won
against a decisive "name is taken" verdict buried deep in a note's body. The
index is read first; it must never lie. PreToolUse on Write|Edit: a memory
note is written with a statutory frontmatter, a VERDICT block as the first
line of the body, and an index line that carries the status marker.

Scope: …/memory/*.md, EXCLUDING MEMORY.md and index-*.md (they are indexes,
not notes).
Grace period: an old non-conforming note stays editable IF the edit brings it
into conformance (the gate demands the format in the same gesture, it does
not forbid touching).
Fail-open: any internal error lets the write through (never break the flow
for a hook bug). Nothing is auto-fixed: writing a verdict is a judgment call.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "hooks"))
from _hook import gate_stat

STATUSES = {"active", "discarded", "stale", "superseded", "dormant"}
MARKERS = {"discarded": "⛔", "stale": "⚠", "superseded": "🔁", "dormant": "🌙"}
VERDICT_RE = re.compile(r"^\*\*VERDICT — (active|discarded|stale|superseded|dormant)\b")


def block(problems):
    """ONE error block for ALL the issues (revealing them one at a time costs
    a round-trip each). problems = [(reason, example)]."""
    lines = [f"⛔ memory-verdict-gate: {len(problems)} issue(s)"]
    for i, (reason, example) in enumerate(problems, 1):
        lines.append(f"{i}. {reason}")
        if example:
            lines.append(example)
    print("\n".join(lines), file=sys.stderr)
    gate_stat("memory-verdict", "block",
              reasons=len(problems), reason=problems[0][0][:80])
    sys.exit(2)


def parse_frontmatter(text):
    """Returns (fields_dict, body) or (None, text) when there is no frontmatter.

    Nested-field fallback: some sync tools (e.g. basic-memory) MOVE custom
    fields (status, superseded_by) under `metadata:`, indented. We read them
    nested too; top-level keeps priority. We do NOT demand they come back to
    top-level: you don't win a write war against an automated sync.
    """
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end == -1:
        return None, text
    blk = text[3:end]
    body = text[end + 4:].lstrip("\n")
    fields, nested = {}, {}
    for ln in blk.splitlines():
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", ln)
        n = re.match(r"^\s+(status|superseded_by):\s*(.*)$", ln)
        if m:
            fields[m.group(1)] = m.group(2).strip().strip('"')
        elif n and n.group(1) not in nested:
            nested[n.group(1)] = n.group(2).strip().strip('"')
    return {**nested, **fields}, body


def resulting_content(payload, path):
    """The text the file will carry AFTER the tool runs. None if unresolvable."""
    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input", {}) or {}
    if tool == "Write":
        return inp.get("content")
    if tool == "Edit":
        try:
            current = open(path, encoding="utf-8").read()
        except OSError:
            return None
        old, new = inp.get("old_string", ""), inp.get("new_string", "")
        if not old or old not in current:
            return None  # the Edit will fail on its own; not our problem
        if inp.get("replace_all"):
            return current.replace(old, new)
        return current.replace(old, new, 1)
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    inp = payload.get("tool_input", {}) or {}
    path = inp.get("file_path", "") or ""
    ap = os.path.normpath(os.path.abspath(os.path.expanduser(path)))

    # Scope: a NOTE inside a memory/ directory, never the indexes.
    if "/memory/" not in ap or not ap.endswith(".md"):
        gate_stat("memory-verdict", "skip-out-of-scope")
        return 0
    base = os.path.basename(ap)
    if base == "MEMORY.md" or base.startswith("index-"):
        gate_stat("memory-verdict", "skip-index")
        return 0

    text = resulting_content(payload, ap)
    if text is None:
        gate_stat("memory-verdict", "skip-unresolvable")
        return 0

    # 1. Single hard-stop: no frontmatter at all (checks 2-5 are unverifiable;
    # the minimal example already covers the whole expected format).
    fields, body = parse_frontmatter(text)
    if fields is None:
        block([("the note has no YAML frontmatter (--- … ---).",
                "Minimal example:\n---\nname: my-slug\ndescription: one line\n"
                "status: active\n---\n**VERDICT — active.** What was decided, in one sentence.")])

    # 2+. Everything else ACCUMULATES into a single error block.
    problems = []
    missing = [f for f in ("name", "description", "status") if not fields.get(f)]
    if missing:
        problems.append((f"missing frontmatter field(s): {', '.join(missing)}.",
                         "Add them: name (slug), description (one line), "
                         "status (active|discarded|stale|superseded|dormant)."))
    status = (fields.get("status") or "").lower()
    status_valid = status in STATUSES
    if fields.get("status") and not status_valid:
        problems.append((f"status « {fields['status']} » is not in the closed list.",
                         "Allowed values: active · discarded · stale · superseded · dormant."))

    # 3. The VERDICT block opens the body. Its PRESENCE is checked even without
    # a valid status; agreement, however, requires a valid status.
    first = body.split("\n", 1)[0] if body else ""
    m = VERDICT_RE.match(first)
    if not m:
        expected = status if status_valid else "active|discarded|stale|superseded|dormant"
        problems.append(("the body does not START with the verdict block.",
                         f"Expected first line:\n**VERDICT — {expected}.** "
                         "<what was decided, in one sentence>\n"
                         "(rule: when the verdict contradicts the rest of the body, the verdict wins)"))
    elif status_valid and m.group(1) != status:
        problems.append((f"the verdict says « {m.group(1)} » but the frontmatter says "
                         f"« {status} ».",
                         "Align them: the verdict and the status are the SAME state."))

    # 4. superseded => superseded_by (valid status required, otherwise already listed).
    if status_valid and status == "superseded" and not fields.get("superseded_by"):
        problems.append(("status: superseded without a superseded_by field.",
                         "Add to the frontmatter: superseded_by: <slug-of-the-replacing-note>"))

    # 5. Index agreement: THE point that broke (the founding incident).
    index = os.path.join(os.path.dirname(ap), "MEMORY.md")
    index_line, no_index = None, False
    try:
        for ln in open(index, encoding="utf-8"):
            if base in ln:
                index_line = ln
                break
    except OSError:
        no_index = True

    if no_index:
        # No MEMORY.md: nothing to agree with. But the issues already
        # accumulated do not evaporate.
        if not problems:
            gate_stat("memory-verdict", "skip-no-index")
            return 0
    elif status_valid and status != "active":
        marker = MARKERS[status]
        if index_line is None:
            problems.append((f"« {status} » note MISSING from MEMORY.md: this is "
                             "the index-that-lies incident.",
                             f"First add the line to {index}:\n"
                             f"- [title]({base}) — {marker} {status.upper()} (the reason, short)\n"
                             "then rewrite the note."))
        elif marker not in index_line:
            problems.append((f"the index line for « {base} » does not carry the "
                             f"{marker} marker ({status}).",
                             f"In {index}, the line must show the state AT THE HEAD of its summary:\n"
                             f"- [title]({base}) — {marker} {status.upper()} (short reason)"))

    if problems:
        block(problems)
    gate_stat("memory-verdict", "pass", status=status)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail-open: a gate bug never breaks the flow
        print(f"memory-verdict-gate fail-open: {type(e).__name__}", file=sys.stderr)
        sys.exit(0)
