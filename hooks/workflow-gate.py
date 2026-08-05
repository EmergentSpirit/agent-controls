#!/usr/bin/env python3
"""workflow-gate.py -- PreToolUse gate on the `Workflow` tool. BLOCK (exit 2).

WHY (production post-mortem, one single session): an agent (1) delegated an `ls`
over 416 files to a sub-agent -- a DETERMINISTIC task handed to an LLM: costly,
slow, non-deterministic, single point of failure -- and (2) launched a fan-out of
416 sub-agents WITHOUT ever validating the mechanism on 3 to 5 items first. Both
mistakes had already been written down in a memory note, and both were repeated
inside that same session. The operator's verdict: "a memory depends on your good
will, carve a HOOK". This gate is deterministic.

Same criterion as the other gates here: effectiveness must not depend on the
model's good will. A red flag is exit 2 (BLOCK), not an ignorable WARN.

Three guards:
  A. A sub-agent whose LABEL or PROMPT smells of a deterministic task (list,
     count, parse, ls, glob, grep, wc, write a data file...) -> BLOCK. Do it in
     Bash/Python in the main loop and pass the result to the workflow via `args`.
  C. A sub-agent that PERSISTS DATA with the Write tool -> BLOCK. Workflow
     agents return data through their SCHEMA; the harness journals it, the main
     loop harvests the journal and writes the files.
  B. A fan-out (parallel/pipeline) with NO validation attestation -> BLOCK. The
     script must carry an explicit marker in a comment:
       @small-run      -> this IS the validation run (small sample, ~10 items)
       @sample-tested  -> big run, mechanism ALREADY validated on a sample
     It forces the "test on 3 to 5 first" reflex.

Environment:
- HARNESS_WORKFLOW_GATE_DISABLE=1  session kill-switch

Fail-open: unreadable payload / other tool / no script -> exit 0. This gate bars
drift, it is not a security wall.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import gate_stat
except Exception:
    sys.exit(0)   # fail-open: a broken helper must never block the work

HOOK = "workflow"
DISABLE_ENV = "HARNESS_WORKFLOW_GATE_DISABLE"
WATCHED_TOOL = "Workflow"

# A -- an agent label that gives away a deterministic task
MECH_LABEL = re.compile(
    r"""label\s*:\s*['"]\s*"""
    r"""(list|ls|glob|grep|count|parse|format|dedup|wc|write|copy|rename|sort)""",
    re.I,
)
# A -- an agent prompt that orders purely mechanical shell work
MECH_PROMPT = re.compile(
    r"(list\s+(all\s+)?(the\s+)?files"
    r"|(with|via|using)\s+the\s+bash\s+tool.{0,40}\b(ls|list|glob)\b"
    r"|\bls\s+-\w"
    r"|wc\s+-l"
    r"|glob\b.{0,20}files)",
    re.I,
)

# C -- DATA persisted through an agent Write (instead of schema + journal harvest)
AGENT_WRITE = re.compile(
    r"((with|using)\s+the\s+write\s+tool"
    r"|\bwrite\s+.{0,40}\b(to|into)\s+(a\s+)?(file|disk)\b"
    r"|\bwrite\s+.{0,25}\.(json|txt|csv|md)\b)",
    re.I,
)

# B -- fan-out and its attestation
FANOUT = re.compile(r"\b(parallel|pipeline)\s*\(", re.I)
ATTEST = re.compile(r"@(small-run|sample-tested)", re.I)


def script_of(payload: dict) -> str:
    """The workflow script, inline or read from `scriptPath`. Empty when absent."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    script = tool_input.get("script")
    if isinstance(script, str) and script.strip():
        return script
    path = tool_input.get("scriptPath")
    if isinstance(path, str) and path.strip():
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""
    return ""


def main() -> int:
    if os.environ.get(DISABLE_ENV) == "1":
        gate_stat(HOOK, "skip-disabled")
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        gate_stat(HOOK, "fail-open")
        return 0   # unreadable input: never block blindly
    if not isinstance(payload, dict):
        gate_stat(HOOK, "fail-open")
        return 0
    if payload.get("tool_name", "") != WATCHED_TOOL:
        gate_stat(HOOK, "skip-out-of-scope")
        return 0

    script = script_of(payload)
    if not script:
        gate_stat(HOOK, "skip-no-script")
        return 0

    # Check A -- deterministic task delegated to an agent
    hit = MECH_LABEL.search(script) or MECH_PROMPT.search(script)
    if hit:
        gate_stat(HOOK, "block", check="A", match=hit.group(0).strip()[:70])
        sys.stderr.write(
            "BLOCKED (workflow gate, check A): deterministic task delegated to an\n"
            "agent -- \"%s\".\n"
            "List / count / parse / write a file = CODE (Bash, Python), never an\n"
            "LLM agent: costly, slow, non-deterministic, single point of failure.\n"
            "Do it in the main loop and pass the result to the workflow via `args`.\n"
            "Session kill-switch: %s=1\n"
            % (hit.group(0).strip()[:70], DISABLE_ENV)
        )
        return 2

    # Check C -- an agent persists DATA through Write
    hit = AGENT_WRITE.search(script)
    if hit:
        gate_stat(HOOK, "block", check="C", match=hit.group(0).strip()[:70])
        sys.stderr.write(
            "BLOCKED (workflow gate, check C): an agent persists DATA through the\n"
            "Write tool -- \"%s\".\n"
            "Workflow agents do NOT persist through Write: one skipped or\n"
            "hallucinated call and the data is gone (a single production run lost\n"
            "144 extractions exactly that way). Return the data VIA THE SCHEMA, the\n"
            "harness journals it; then harvest the journal and write the files from\n"
            "the main loop.\n"
            "Session kill-switch: %s=1\n"
            % (hit.group(0).strip()[:70], DISABLE_ENV)
        )
        return 2

    # Check B -- fan-out with no prior-validation attestation
    if FANOUT.search(script) and not ATTEST.search(script):
        gate_stat(HOOK, "block", check="B")
        sys.stderr.write(
            "BLOCKED (workflow gate, check B): fan-out (parallel/pipeline) with no\n"
            "attestation.\n"
            "Before a big run, VALIDATE the mechanism on 3 to 5 items. Then declare\n"
            "one of these markers in a comment inside the script:\n"
            "  // @small-run      -> this IS the validation run (small sample)\n"
            "  // @sample-tested  -> big run, mechanism already validated on a sample\n"
            "Check the design BEFORE launching, not after.\n"
            "Session kill-switch: %s=1\n"
            % DISABLE_ENV
        )
        return 2

    gate_stat(HOOK, "pass")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # fail-open: a broken gate never blocks the work
