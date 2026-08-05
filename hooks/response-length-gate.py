#!/usr/bin/env python3
"""response-length-gate.py -- Stop hook: refuses an answer that buries the
operator under pages of prose. BLOCK (exit 2).

WHY (production post-mortem): six answers between 600 and 900 words in a single
session, until the operator cut in -- "stop bombarding me with pages of filler,
it is impossible to digest". A memory note asking for short answers ALREADY
existed and said exactly that. It did not hold.

Empirical proof from the same session: a soft validator running alongside fired
four "too verbose" classifications DURING that session. It SAW the fault, it
warned, and the model kept going anyway.

That is the whole argument for a hard gate: a WARN depends on the model's good
will, so it does not count. This one blocks (exit 2).

Rule: beyond HARNESS_MAX_RESPONSE_WORDS words of PROSE in the last assistant
message, the answer is refused. If there is more to say, it goes into a dated
file and the answer hands back the path.

NOT counted (these are evidence, not chatter):
  - fenced code blocks ``` ```
  - inline code `...`
  - markdown table rows
  - URLs

Loop guard: when the payload carries stop_hook_active, the turn is already a
retry driven by a Stop hook. Blocking in a loop would freeze the pane, so the
turn is let through and journalled.

Environment:
- HARNESS_MAX_RESPONSE_WORDS              prose ceiling (default: 350)
- HARNESS_RESPONSE_LENGTH_GATE_DISABLE=1  session kill-switch

Exit codes:
  0  allow (including every fail-open path: this gate guards against drift, it
     is not a security wall)
  2  BLOCK -- answer too long, condense it or move the detail to a file
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import assistant_text_blocks, gate_stat, load_transcript
except Exception:
    sys.exit(0)   # fail-open: a broken helper must never block the work

HOOK = "response-length"
DISABLE_ENV = "HARNESS_RESPONSE_LENGTH_GATE_DISABLE"
MAX_WORDS_ENV = "HARNESS_MAX_RESPONSE_WORDS"
MAX_WORDS_DEFAULT = 350

CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]*`")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
URL_RE = re.compile(r"https?://\S+")
WORD_RE = re.compile(r"[^\s]+")


def max_words() -> int:
    """Ceiling from the environment; anything not a positive int is ignored."""
    raw = os.environ.get(MAX_WORDS_ENV, "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return MAX_WORDS_DEFAULT


def prose_words(text: str) -> int:
    """Word count of PROSE only: code, table rows and URLs are excluded."""
    t = CODE_BLOCK_RE.sub(" ", text)
    t = INLINE_CODE_RE.sub(" ", t)
    t = TABLE_ROW_RE.sub(" ", t)
    t = URL_RE.sub(" ", t)
    return len(WORD_RE.findall(t))


def last_assistant_text(path) -> str:
    """Text of the LAST assistant message of the JSONL transcript. Empty string
    when the transcript is missing or unreadable (load_transcript fails open)."""
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


def main() -> int:
    if os.environ.get(DISABLE_ENV) == "1":
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

    # Loop guard: never re-block a turn that a Stop hook already triggered.
    if data.get("stop_hook_active"):
        gate_stat(HOOK, "skip-loop-guard")
        return 0

    # The text arrives either inline (tests, wrappers) or through the
    # transcript (real runtime).
    text = data.get("assistant_text")
    if not isinstance(text, str) or not text.strip():
        text = last_assistant_text(data.get("transcript_path"))
    if not text.strip():
        gate_stat(HOOK, "skip-no-text")
        return 0   # nothing to measure

    ceiling = max_words()
    words = prose_words(text)
    if words <= ceiling:
        gate_stat(HOOK, "pass", words=words)
        return 0

    gate_stat(HOOK, "block", words=words, ceiling=ceiling)
    sys.stderr.write(
        "BLOCKED (response-length gate): %d words of prose, ceiling %d.\n"
        "The operator reads a terminal pane, not a report. Condense: the verdict\n"
        "first, then only what changes a decision.\n"
        "Code blocks, inline code, table rows and URLs are NOT counted, so\n"
        "evidence is free. What costs is the prose around it.\n"
        "If there is genuinely more to say, write a dated file and hand back its\n"
        "path in one line.\n"
        "Raise the ceiling: %s=<n>. Session kill-switch: %s=1\n"
        % (words, ceiling, MAX_WORDS_ENV, DISABLE_ENV)
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # fail-open: a broken gate never blocks the work
