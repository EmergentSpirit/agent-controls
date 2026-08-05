#!/usr/bin/env python3
"""isolated-llm-measure-gate.py -- PreToolUse Write|Edit|MultiEdit. BLOCK (exit 2).

An LLM you MEASURE must run in a NEUTRAL directory. The agent CLI inherits the
context of its current working directory: it loads the project instruction file
and it can see the files sitting around it. Point it at the benchmark folder and
the instrument reads the answer sheet it is supposed to be graded against.

Measured on one identical prompt, same model, same day:

    launched from the benchmark directory : 59k tokens of context
    launched from an empty directory      : 18k tokens of context

The run from the benchmark directory opened its answer by ANNOUNCING that the
prompt had the exact shape of an item from the corpus it was being fed. The
model knew it was being tested while it was being tested. The score came back
perfect and meant nothing, and about 38 USD of quota equivalent was spent to
learn that.

The rule this enforces: any code that automates the agent CLI in headless mode
(`-p` / `--print`) must pass an explicit `cwd=`. A neutral temporary directory
costs one line.

Zero false positives by construction: only Python `subprocess` calls are judged,
and only inside `.py` files. A `claude -p` typed by hand in a terminal, a Bash
command, a doc, a comment or a commit message is never in scope.

Environment:
- HARNESS_ISOLATED_LLM_MEASURE_GATE_DISABLE=1  session kill-switch
- HARNESS_LLM_CLI_NAMES  colon-separated CLI binaries treated as an LLM under
                         measurement (default: `claude`)
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

HOOK = "isolated-llm-measure"

# A subprocess call launching the agent CLI in headless mode. BOTH markers are
# required inside the SAME call: the binary name AND the `-p` / `--print` flag.
SUBPROC_RE = re.compile(
    r"subprocess\.(?:run|Popen|check_output|call|check_call)\s*\((?P<body>.*?)\)\s*(?:$|\n)",
    re.DOTALL,
)
HEADLESS_RE = re.compile(r"""["'](?:-p|--print)["']""")
CWD_RE = re.compile(r"\bcwd\s*=")


def cli_pattern():
    """Quoted CLI binary names, from HARNESS_LLM_CLI_NAMES (default `claude`)."""
    raw = os.environ.get("HARNESS_LLM_CLI_NAMES") or "claude"
    names = [n.strip() for n in raw.split(":") if n.strip()]
    if not names:
        names = ["claude"]
    return re.compile(r"""["'](?:%s)["']""" % "|".join(re.escape(n) for n in names))


def unisolated_calls(text):
    """Excerpts of subprocess calls launching the CLI headless with no `cwd=`."""
    cli_re = cli_pattern()
    found = []
    for m in SUBPROC_RE.finditer(text):
        body = m.group("body")
        if not (cli_re.search(body) and HEADLESS_RE.search(body)):
            continue
        if CWD_RE.search(body):
            continue                     # isolated: this is what we want
        found.append(" ".join(m.group(0).split())[:140])
    return found


def written_content(data):
    """The text the tool is about to write, whichever tool it is."""
    tool_input = data.get("tool_input") or {}
    for key in ("content", "new_string"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v, tool_input.get("file_path", "")
    return "", tool_input.get("file_path", "")


def main():
    if os.environ.get("HARNESS_ISOLATED_LLM_MEASURE_GATE_DISABLE") == "1":
        gate_stat(HOOK, "skip-disabled")
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        gate_stat(HOOK, "fail-open")
        return 0   # unreadable input: never block blindly
    if not isinstance(data, dict):
        gate_stat(HOOK, "fail-open")
        return 0
    if data.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        gate_stat(HOOK, "skip-not-write")
        return 0

    text, path = written_content(data)
    if not text:
        gate_stat(HOOK, "skip-nothing-written")
        return 0
    # Only Python files carry executable subprocess calls.
    if path and not path.endswith(".py"):
        gate_stat(HOOK, "skip-not-python", path=path)
        return 0

    offenders = unisolated_calls(text)
    if not offenders:
        gate_stat(HOOK, "pass", path=path)
        return 0

    gate_stat(HOOK, "block", path=path, n=len(offenders))
    sys.stderr.write(
        "⛔ isolated-llm-measure-gate: automated `claude -p` with NO cwd=.\n\n"
        "An LLM you MEASURE must run in a NEUTRAL directory. Otherwise the agent\n"
        "CLI inherits the context of the current directory: it reads the project\n"
        "instruction file and sees the files around it. Measured on one identical\n"
        "prompt:\n"
        "  from the benchmark directory : 59k tokens, and the model ANNOUNCES it\n"
        "                                 recognizes an item of the corpus fed to it\n"
        "  from an empty directory      : 18k tokens, no recognition at all\n"
        "-> about 38 USD of quota equivalent burnt, and a perfect score that meant\n"
        "   nothing.\n\n"
        "Fix: hand it an empty temporary directory.\n"
        "  subprocess.run(cmd, capture_output=True, text=True, cwd=tempfile.mkdtemp())\n\n"
        "Offending call(s):\n" + "".join("  %s\n" % f for f in offenders) +
        "\nSession kill-switch: HARNESS_ISOLATED_LLM_MEASURE_GATE_DISABLE=1\n"
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # fail-open: a broken gate never blocks the work
