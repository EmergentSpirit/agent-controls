#!/usr/bin/env python3
"""shield-inject.py -- layer 1 of the shield: the rule arrives WITH the risk.

UserPromptSubmit hook. It matches the incoming prompt against the active
patterns of the trigger registry. On the first match it prints the matching
rule (one or two lines, prefixed `[shield]`) into the turn's context, and it
ARMS layer 3 by writing a marker under the state directory.

WHY (production post-mortem): the rules this enforces were already written
down, loaded, and agreed to. They still broke, repeatedly, because a standing
instruction competes with everything else in the context window. Injected at
the exact moment the prompt shows the risk, the same sentence lands in the
foreground of the turn that is about to violate it. Same words, different
position, different outcome.

The marker is what makes layer 3 free at rest: the reviewer runs an LLM call
ONLY on the turns layer 1 flagged as risky. Ordinary turns cost one journal
line and nothing else.

Invariants:
- ALWAYS exit 0. This hook runs on UserPromptSubmit, where exit 2 would ERASE
  the operator's prompt. Whatever happens, the prompt goes through.
- stdout carries the injection block and nothing else: it is appended to the
  model's context verbatim.
- An unreadable or empty registry is silence, not an error (fail-open).
- One journal line per execution, whatever the outcome.

Environment:
- HARNESS_SHIELD_REGISTRY            trigger registry path (see _registry.py)
- HARNESS_STATE_DIR                  state directory holding the marker
                                     (default: ~/.harness)
- HARNESS_AGENT                      role name, also settable with --agent
- HARNESS_SHIELD_INJECT_GATE_DISABLE=1  session kill-switch

Exit codes:
  0  always (this layer injects, it never blocks)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
try:
    from _hook import STATE_DIR, gate_stat, read_stdin_json
    from _registry import active_triggers
except Exception:
    sys.exit(0)   # no helper, no layer: fail-open before anything else

HOOK = "shield-inject"
DISABLE_ENV = "HARNESS_SHIELD_INJECT_GATE_DISABLE"
PREFIX = "[shield]"


def agent_name(argv) -> str:
    """Role name: --agent <name>, else HARNESS_AGENT, else `agent`."""
    if "--agent" in argv:
        try:
            name = argv[argv.index("--agent") + 1].strip()
            if name:
                return name
        except IndexError:
            pass
    return (os.environ.get("HARNESS_AGENT") or "agent").strip() or "agent"


def marker_path(agent: str) -> str:
    """One marker per role: two panes sharing a state directory must not arm
    each other's reviewer."""
    return os.path.join(STATE_DIR, "shield", "%s-trigger.json" % agent.lower())


def matches(prompt: str) -> list:
    """Active triggers whose pattern fires on this prompt."""
    hits = []
    for trigger in active_triggers():
        try:
            if re.search(str(trigger.get("pattern")), prompt, re.IGNORECASE):
                hits.append(trigger)
        except re.error:
            continue   # a broken pattern skips its entry, never the prompt
    return hits


def arm(agent: str, session_id: str, hits: list) -> bool:
    """Write the single-use marker layer 3 reads. False when it could not be
    written: the injection still happened, only the review is lost."""
    try:
        path = marker_path(agent)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "session_id": session_id,
                "ts": time.time(),
                "rules": [str(t.get("memory") or t.get("rule") or "")[:80]
                          for t in hits],
            }, f)
        return True
    except Exception:
        return False


def main() -> int:
    if os.environ.get(DISABLE_ENV) == "1":
        gate_stat(HOOK, "skip-disabled")
        return 0
    agent = agent_name(sys.argv)
    data = read_stdin_json()
    if not data:
        gate_stat(HOOK, "fail-open", agent=agent)
        return 0   # unreadable input: the prompt goes through untouched
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        gate_stat(HOOK, "skip-no-prompt", agent=agent)
        return 0

    hits = matches(prompt)
    if not hits:
        gate_stat(HOOK, "pass", agent=agent)
        return 0

    # The injection itself: the rule of the moment, short and hard. One line
    # per match, and nothing else on stdout.
    for trigger in hits:
        sys.stdout.write("%s %s\n" % (PREFIX, trigger.get("rule")))

    armed = arm(agent, str(data.get("session_id") or ""), hits)
    gate_stat(HOOK, "warn", agent=agent, armed=armed,
              rules=[str(t.get("memory") or "?") for t in hits])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # fail-open: a broken layer never eats a prompt
