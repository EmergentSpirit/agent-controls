#!/usr/bin/env python3
"""recall-inject.py -- "it already exists, do not rebuild it", at prompt time.

UserPromptSubmit hook. It hands the operator's prompt to the recall engine,
which matches it against the name and aliases of the curated catalog. On a
precise match it prints the bounded "already built" block into the turn's
context: what exists, its status, how to bring it back, and whether it is still
there RIGHT NOW. No match: silence, which is a correct answer and not a gap.

WHY here and not in a standing instruction: the agent does not rebuild an
existing artifact out of carelessness, it rebuilds it because nothing in the
window says the artifact exists. A catalog nobody reads changes nothing. The
answer has to arrive in the same turn as the risk, and the prompt is where the
risk first becomes visible.

Deterministic, zero LLM, hard timeout. The engine runs as a SUBPROCESS on
purpose: an engine that hangs must never freeze the operator's prompt.

Invariants:
- ALWAYS exit 0. On UserPromptSubmit, exit 2 would ERASE the prompt.
- stdout carries the injection block and nothing else: it is appended verbatim
  to the model's context.
- One journal line per execution, whatever the outcome.

Environment:
- HARNESS_RECALL_CATALOG                 curated catalog (see recall.py)
- HARNESS_AGENT                          role name, also settable with --agent
- HARNESS_RECALL_INJECT_GATE_DISABLE=1   session kill-switch

Exit codes:
  0  always (this hook injects, it never blocks)
"""
import datetime
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
try:
    from _hook import gate_stat, read_stdin_json
except Exception:
    sys.exit(0)   # no helper, no hook: fail-open before anything else

HOOK = "recall-inject"
DISABLE_ENV = "HARNESS_RECALL_INJECT_GATE_DISABLE"
ENGINE = os.path.join(HERE, "recall.py")
MIN_PROMPT_CHARS = 4
TIMEOUT_S = 3


def agent_name(argv):
    """Role name: --agent <name>, else HARNESS_AGENT, else `agent`."""
    if "--agent" in argv:
        try:
            name = argv[argv.index("--agent") + 1].strip()
            if name:
                return name
        except IndexError:
            pass
    return (os.environ.get("HARNESS_AGENT") or "agent").strip() or "agent"


def engine_match(prompt):
    """The engine's block for this prompt, or None when it could not answer.

    The date is INJECTED here: the engine reads no clock, so that a missing
    date shows up as "staleness unverified" instead of quietly passing.
    """
    env = dict(os.environ)
    env["HARNESS_RECALL_TODAY"] = datetime.date.today().isoformat()
    try:
        proc = subprocess.run([sys.executable, ENGINE, "match", prompt],
                              capture_output=True, text=True, env=env,
                              timeout=TIMEOUT_S)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def main():
    if os.environ.get(DISABLE_ENV) == "1":
        gate_stat(HOOK, "skip-disabled")
        return 0
    agent = agent_name(sys.argv)
    if not os.path.isfile(ENGINE):
        gate_stat(HOOK, "skip-no-engine", agent=agent)
        return 0
    data = read_stdin_json()
    if not data:
        gate_stat(HOOK, "fail-open", agent=agent)
        return 0   # unreadable input: the prompt goes through untouched
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or len(prompt.strip()) < MIN_PROMPT_CHARS:
        gate_stat(HOOK, "skip-short-prompt", agent=agent)
        return 0

    block = engine_match(prompt)
    if block is None:
        gate_stat(HOOK, "fail-open", agent=agent)
        return 0   # engine broken or too slow: never hold up a prompt for it
    if not block:
        gate_stat(HOOK, "pass", agent=agent)
        return 0   # nothing known about this: silence

    sys.stdout.write(block + "\n")
    gate_stat(HOOK, "warn", agent=agent, hits=block.count("\n* "))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # fail-open: a broken hook never eats a prompt
