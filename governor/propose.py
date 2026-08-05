#!/usr/bin/env python3
"""propose.py -- the conveyor: parse a proposal, have it judged, route it.

    python3 governor/propose.py <proposal.md>

A gate proposal is five bounded human fields plus a free block for the judges.
The bound is the point: a pitch that cannot be stated in five short lines is a
pitch nobody reads, and a governance nobody reads is theatre.

    # proposal: slug-in-kebab
    touches: technical | life          (optional, default: technical)
    what: one sentence, no jargon
    incident: the lived event that justifies it, dated
    blocks: a concrete example of what would have been caught
    error_cost: what it costs when it is wrong
    trial: 7 days, observation only
    detail:
    (free: exact trigger, pseudo-code, adjacent existing gates...)

Each of the five fields is capped at 200 characters, refused at the source. A
field that overflows is not truncated: the author rewrites it.

Routing, from the two independent verdicts:

    one `rejected`          -> archive/           + ledger `rejected-by-judges`
                               (silence toward the operator: a killed proposal
                               is auditable, not reportable)
    a judge did not speak   -> pending-judge/     + ledger `judge-unavailable`
                               (NEVER a default yes -- this is the whole point)
    two `viable`, technical -> to-build/          + ledger `viable-to-build`
    two `viable`, life      -> awaiting-operator/ + ledger `awaiting-operator`
                               (money, public surface, irreversible, personal
                               data: the human word stays the only key)

A rejection is checked BEFORE unavailability: a judge that killed the proposal
killed it whether or not the other one answered. Unavailability decides the
fate of the proposals nobody rejected.

This script ARMS NOTHING. It writes files and ledger lines. Turning a proposal
into a live gate is a separate human-approved gesture, and that is the one step
of the pipeline a poisoned text cannot walk through.

Exit codes: 0 routed, 1 usage error or malformed proposal.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import judges                                                     # noqa: E402
from _state import (FOLDERS, ensure_dir, gate_stat, ledger_append,  # noqa: E402
                    now_iso)

HOOK = "governor-propose"
FIELDS = ("what", "incident", "blocks", "error_cost", "trial")
MAX_FIELD = 200


def parse(text: str):
    """(slug, fields), or SystemExit naming the exact missing/oversized field."""
    head = re.search(r"^#\s*proposal:\s*([a-z0-9][a-z0-9-]*)\s*$", text, re.M)
    if not head:
        raise SystemExit("proposal without a `# proposal: slug-in-kebab` header")
    fields = {}
    for name in FIELDS:
        found = re.search(r"^%s:\s*(.+)$" % name, text, re.M)
        if not found:
            raise SystemExit("missing field: %s" % name)
        value = found.group(1).strip()
        if len(value) > MAX_FIELD:
            raise SystemExit(
                "field %s too long (%d > %d): the human pitch protects itself "
                "at the source" % (name, len(value), MAX_FIELD))
        fields[name] = value
    touches = re.search(r"^touches:\s*(technical|life)\s*$", text, re.M)
    fields["touches"] = touches.group(1) if touches else "technical"
    return head.group(1), fields


def pitch(slug: str, fields: dict, v1, why1: str, v2, why2: str) -> str:
    """The five bounded lines, the judges' line, and the question. Nothing
    else, ever: what reaches the operator is capped by construction."""
    lines = ["# %s" % slug,
             "What: %s" % fields["what"],
             "Incident: %s" % fields["incident"],
             "What it blocks: %s" % fields["blocks"],
             "Cost when it is wrong: %s" % fields["error_cost"],
             "Trial: %s" % fields["trial"]]
    judge_line = "Judges: judge-1 %s" % judges.label(v1, why1)
    if v1 and v1.get("reservations"):
        judge_line += " (reservations: %s)" % " | ".join(v1["reservations"][:2])
    judge_line += " / judge-2 %s" % judges.label(v2, why2)
    if v2 and v2.get("reservations"):
        judge_line += " (reservations: %s)" % " | ".join(v2["reservations"][:2])
    lines.append(judge_line)
    if v1 is None or v2 is None:
        lines.append("Held: a judge did not speak, so nothing was concluded. "
                     "Configure the missing judge and run this proposal again.")
    elif fields["touches"] == "life":
        lines.append("Your word: GO trial / no "
                     "(life class: money, public, irreversible, personal data)")
    else:
        lines.append("Governor decision: to build, then 7 days of "
                     "observation-only trial before anything is armed")
    return "\n".join(lines) + "\n"


def route(v1, v2, touches: str) -> str:
    if (v1 and v1["verdict"] == "rejected") or (v2 and v2["verdict"] == "rejected"):
        return "rejected-by-judges"
    if v1 is None or v2 is None:
        return "judge-unavailable"          # never a default yes
    return "awaiting-operator" if touches == "life" else "viable-to-build"


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    try:
        with open(argv[1], encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise SystemExit("unreadable proposal: %s" % exc)
    slug, fields = parse(text)
    stamp = now_iso()

    # The two judges are called independently, and NEITHER receives the other's
    # verdict. Do not "optimize" this into a single call carrying both opinions:
    # a judge shown a previous verdict anchors on it, and two anchored judges
    # are one judge you paid for twice.
    print("judge 1 (local agent CLI) on `%s`..." % slug)
    v1, why1 = judges.judge_local(text)
    print("  -> %s" % judges.label(v1, why1))
    print("judge 2 (configured endpoint)...")
    v2, why2 = judges.judge_http(text)
    print("  -> %s" % judges.label(v2, why2))

    verdict_path = os.path.join(ensure_dir("verdicts"),
                                "%s-%s.json" % (slug, stamp))
    with open(verdict_path, "w", encoding="utf-8") as fh:
        json.dump({"slug": slug, "ts": stamp, "touches": fields["touches"],
                   "judge1": v1, "judge1_why": why1,
                   "judge2": v2, "judge2_why": why2,
                   "judge2_host": judges.judge2_host()},
                  fh, ensure_ascii=False, indent=1)

    status = route(v1, v2, fields["touches"])
    pitch_path = os.path.join(ensure_dir(FOLDERS[status]), "%s.md" % slug)
    with open(pitch_path, "w", encoding="utf-8") as fh:
        fh.write(pitch(slug, fields, v1, why1, v2, why2))
        if status == "rejected-by-judges":
            fh.write("\n## Why the judges killed it\n")
            for name, verdict in (("judge-1", v1), ("judge-2", v2)):
                if verdict and verdict["verdict"] == "rejected":
                    for reason in verdict["reasons"]:
                        fh.write("- %s: %s\n" % (name, reason))

    entry = ledger_append(slug, status, verdict_path)
    gate_stat(HOOK, "warn" if status == "judge-unavailable" else "pass",
              slug=slug, status=status, ledger=entry)
    print("%s -> %s (ledger %s)" % (status, pitch_path, entry))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
