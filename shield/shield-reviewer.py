#!/usr/bin/env python3
"""shield-reviewer.py -- layer 3 of the shield: the judge before display.

Stop hook. It runs ONLY when layer 1 armed the marker in the SAME session and
recently. On the vast majority of turns it exits in a couple of milliseconds
having done nothing but write its journal line: a trigger armed is a review
armed, and nothing else pays.

When it is armed, it reads the answer about to be shown (the last assistant
message of the transcript), judges it against the rubric (the armed rules plus
the standing invariants), and refuses it on a violation -- exit 2, so the
operator never sees the faulty version, only the rewrite.

The judge is ISOLATED from what it judges, and this is the point of the design:

- it runs the agent CLI headless (`-p`) in an EMPTY temporary directory, so it
  inherits no project instruction file and sees none of the files around the
  work it is grading. An instrument that reads the answer sheet measures
  nothing (that lesson cost about 38 USD of quota to learn once).
- its environment is purged of every ANTHROPIC_* variable, so a review can
  never silently fall back onto a metered API key.
- it receives the rubric and the output, nothing else. It does not know what
  the operator asked, who wrote the answer, or which project this is.

FAIL-OPEN everywhere: missing CLI, timeout, non-zero exit, unparsable verdict,
unreadable transcript -- all of it exits 0 and journals. A reviewer that breaks
must never freeze a session; the worst it may do is fail to catch a violation.

Environment:
- HARNESS_SHIELD_REGISTRY              trigger registry path (see _registry.py)
- HARNESS_SHIELD_RUBRIC                rubric file appended to the armed rules
                                       (default: reviewer-rubric.example.md
                                       next to this file)
- HARNESS_SHIELD_FRESHNESS             marker lifetime in seconds (default 2700)
- HARNESS_SHIELD_TIMEOUT               hard timeout of the judge call, seconds
                                       (default 12: a cold headless call under
                                       5 s does not exist)
- HARNESS_SHIELD_MODEL                 model alias for the judge (default haiku)
- HARNESS_LLM_CLI_NAMES                colon-separated agent CLI binaries; the
                                       first one is the judge (default claude)
- HARNESS_STATE_DIR                    state directory holding the marker
- HARNESS_AGENT                        role name, also settable with --agent
- HARNESS_SHIELD_REVIEWER_GATE_DISABLE=1  session kill-switch
- HARNESS_SHIELD_FAKE_VERDICT          TEST ONLY: a JSON verdict that short-
                                       circuits the CLI call. Never set it in
                                       production, it replaces the judge.

Exit codes:
  0  allowed, or not armed, or any fail-open path
  2  BLOCK -- the answer violates an armed rule and is refused before display
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
try:
    from _hook import (STATE_DIR, assistant_text_blocks, gate_stat,
                       load_transcript, read_stdin_json)
    from _registry import load_triggers
except Exception:
    sys.exit(0)   # no helper, no layer: fail-open before anything else

HOOK = "shield-reviewer"
DISABLE_ENV = "HARNESS_SHIELD_REVIEWER_GATE_DISABLE"
RUBRIC_DEFAULT = os.path.join(HERE, "reviewer-rubric.example.md")
FRESHNESS_DEFAULT = 2700          # 45 minutes
TIMEOUT_DEFAULT = 12
MODEL_DEFAULT = "haiku"
MAX_JUDGED_CHARS = 6000

INSTRUCTION = (
    "\nYou are a reviewer. The OUTPUT below is about to be displayed to the "
    "operator. Judge ONLY whether it violates a rule of the rubric. A "
    "violation requires the output to ADD something that was not asked for; "
    "when in doubt, violation=false. Answer with STRICT JSON on a single "
    "line, nothing else: "
    '{"violation": true|false, "rule": "<slug or empty>", '
    '"excerpt": "<the exact offending quote, or empty>"}\n'
    "\n--- OUTPUT UNDER REVIEW ---\n"
)


def int_env(name: str, default: int) -> int:
    """Positive integer from the environment; anything else is ignored, so a
    typo cannot silently disarm a timeout or a freshness window."""
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


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
    return os.path.join(STATE_DIR, "shield", "%s-trigger.json" % agent.lower())


def judge_cli() -> str:
    raw = os.environ.get("HARNESS_LLM_CLI_NAMES") or "claude"
    names = [n.strip() for n in raw.split(":") if n.strip()]
    return names[0] if names else "claude"


def last_assistant_text(path) -> str:
    """Text of the LAST assistant message of the JSONL transcript. Empty when
    the transcript is missing or unreadable (load_transcript fails open)."""
    if not isinstance(path, str) or not path:
        return ""
    for ev in reversed(load_transcript(path)):
        if not isinstance(ev, dict):
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        return assistant_text_blocks(ev)
    return ""


def standing_invariants() -> str:
    """The rubric file: the rules that hold on EVERY turn, as opposed to the
    ones layer 1 armed for this one. Absent file = no invariants, not a crash."""
    path = os.path.expanduser(
        os.environ.get("HARNESS_SHIELD_RUBRIC") or RUBRIC_DEFAULT)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def rubric(slugs) -> str:
    """Stable header first (armed rules, then invariants): the constant part of
    the prompt sits at the front, where a prompt cache can reach it."""
    lines = []
    for trigger in load_triggers():
        slug = str(trigger.get("memory") or "")
        if slugs and slug not in slugs:
            continue
        lines.append("- %s: %s" % (slug or "rule", trigger.get("rule") or ""))
    return ("RUBRIC (the rules to check):\n" + "\n".join(lines) + "\n"
            + standing_invariants())


def judge(slugs, output: str):
    """Verdict dict, or None when the judge could not be trusted to speak."""
    fake = os.environ.get("HARNESS_SHIELD_FAKE_VERDICT")
    if fake:
        try:
            verdict = json.loads(fake)
        except Exception:
            return None
        return verdict if isinstance(verdict, dict) else None

    prompt = rubric(slugs) + INSTRUCTION + output[-MAX_JUDGED_CHARS:]
    # Never the metered API: the judge runs on the interactive plan or not at all.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    try:
        with tempfile.TemporaryDirectory(prefix="shield-judge-") as neutral:
            # cwd = an EMPTY directory: the instrument inherits no project
            # instruction file and sees none of the files it is grading.
            proc = subprocess.run(
                [judge_cli(), "-p", "--model",
                 os.environ.get("HARNESS_SHIELD_MODEL") or MODEL_DEFAULT,
                 prompt],
                capture_output=True, text=True, env=env, cwd=neutral,
                timeout=int_env("HARNESS_SHIELD_TIMEOUT", TIMEOUT_DEFAULT))
        if proc.returncode != 0:
            return None
        raw = proc.stdout.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        verdict = json.loads(raw[start:end + 1])
        return verdict if isinstance(verdict, dict) else None
    except Exception:
        return None


def armed_rules(agent: str, session_id: str):
    """The rules layer 1 armed for THIS session, or None when the reviewer must
    stay asleep. The marker is consumed here: one arming, one review."""
    path = marker_path(agent)
    try:
        with open(path, encoding="utf-8") as f:
            marker = json.load(f)
    except Exception:
        return None, "skip-not-armed"
    if not isinstance(marker, dict) or marker.get("session_id") != session_id:
        return None, "skip-not-armed"
    try:
        age = time.time() - float(marker.get("ts") or 0)
    except (TypeError, ValueError):
        age = None
    if age is None or age > int_env("HARNESS_SHIELD_FRESHNESS",
                                    FRESHNESS_DEFAULT):
        _unlink(path)
        return None, "skip-stale-marker"
    _unlink(path)   # single use: an armed turn is reviewed once
    rules = marker.get("rules")
    return ([str(r) for r in rules] if isinstance(rules, list) else []), ""


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def main() -> int:
    if os.environ.get(DISABLE_ENV) == "1":
        gate_stat(HOOK, "skip-disabled")
        return 0
    agent = agent_name(sys.argv)
    data = read_stdin_json()
    if not data:
        gate_stat(HOOK, "fail-open", agent=agent)
        return 0   # unreadable input: never block blindly

    # Loop guard: this turn is already a retry driven by a Stop hook. Blocking
    # in a loop would freeze the pane.
    if data.get("stop_hook_active"):
        gate_stat(HOOK, "skip-loop-guard", agent=agent)
        return 0

    rules, why = armed_rules(agent, str(data.get("session_id") or ""))
    if rules is None:
        gate_stat(HOOK, why, agent=agent)
        return 0

    output = last_assistant_text(data.get("transcript_path"))
    if not output.strip():
        gate_stat(HOOK, "skip-no-text", agent=agent)
        return 0   # nothing to judge

    started = time.time()
    verdict = judge(rules, output)
    duration_ms = int((time.time() - started) * 1000)

    if verdict is None:
        gate_stat(HOOK, "fail-open", agent=agent, duration_ms=duration_ms)
        return 0
    if not verdict.get("violation"):
        gate_stat(HOOK, "pass", agent=agent, duration_ms=duration_ms)
        return 0

    rule = str(verdict.get("rule") or "?")[:80]
    excerpt = str(verdict.get("excerpt") or "")[:200]
    gate_stat(HOOK, "block", agent=agent, duration_ms=duration_ms, rule=rule)
    sys.stderr.write(
        "BLOCKED (shield-reviewer gate): this answer violates \"%s\".\n"
        "Offending excerpt: %s\n"
        "Rewrite the answer WITHOUT that passage: answer the question that was\n"
        "asked, nothing else. The rule was injected at the top of this turn by\n"
        "layer 1 -- apply it.\n"
        "Session kill-switch: %s=1\n" % (rule, excerpt, DISABLE_ENV)
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # fail-open: a broken judge never freezes a session
