#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shell-false-success-gate.py — PreToolUse Write|Edit|MultiEdit. BLOCK (exit 2).

Two shell shapes that make a script LIE, both paid for during the same
production day:

(a) `CODE=$(curl ... -w '%{http_code}' ... || echo 000)` — when the connection
    fails, curl ALREADY prints `000` through -w AND exits non-zero, so the
    `|| echo` appends a SECOND one. `CODE` then holds `000000`, which is very
    much != `"000"`, so a success test written as an inequality fires ON A
    FAILURE. A TLS watchdog announced "CERTIFICATE ISSUED" on a broken
    handshake.

(b) `PEND=$(... grep ... | wc -l)` under `set -e` + `pipefail` — a grep that
    finds nothing exits 1, pipefail propagates that through `wc`, and set -e
    kills the script. So it dies ON THE SUCCESS CASE (nothing left to clean
    up is exactly what we wanted). A cutover wrapper died right there; worse,
    the same shape sat in its FINAL VERIFICATION, which would have killed it
    AFTER the mutation and filed the run under `failed/`: mutation done +
    wrapper reported failed = the operator believes nothing moved.

Zero false positives by construction: (b) is judged only when the TARGET file
carries `set -e` AND `pipefail`. Outside a wrapper, the hook says nothing.
Fail-open on any exception.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import gate_stat, read_stdin_json
except Exception:
    sys.exit(0)

HOOK = "shell-false-success"

# (a) a $( ) holding curl + %{http_code} + an `|| echo`.
CURL_ECHO_RE = re.compile(
    r"\$\([^)]*\bcurl\b[^)]*%\{http_code\}[^)]*\|\|\s*echo\b", re.DOTALL)

# (a bis) the inequality test on 000, the other half of the trap.
CODE_NEQ_RE = re.compile(r'!=\s*"?000"?')

# (b) a capture `VAR=$( ... grep ... | wc -l )` with no `|| true` inside the
# substitution.
GREP_WC_RE = re.compile(
    r"=\s*\$\((?P<body>[^()]*\bgrep\b[^()]*\|\s*wc\s+-l[^()]*)\)")

PIPEFAIL_RE = re.compile(r"set\s+-[a-z]*e[a-z]*o?\s+pipefail|set\s+-o\s+pipefail")
SET_E_RE = re.compile(r"set\s+-[a-zA-Z]*e")


def is_shell(path: str, text: str) -> bool:
    if path.endswith((".sh", ".bash")):
        return True
    return text.lstrip().startswith("#!") and "sh" in text.splitlines()[0]


def target_content(tool_input: dict) -> str:
    """The file as it is / will be on disk, to judge the context (pipefail)."""
    path = tool_input.get("file_path") or ""
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
    except Exception:
        pass
    return ""


def written_fragments(tool_input: dict) -> str:
    """What THIS call writes: content (Write) or the new_string (Edit/MultiEdit)."""
    chunks = []
    if tool_input.get("content"):
        chunks.append(tool_input["content"])
    if tool_input.get("new_string"):
        chunks.append(tool_input["new_string"])
    for edit in tool_input.get("edits") or []:
        if edit.get("new_string"):
            chunks.append(edit["new_string"])
    return "\n".join(chunks)


def strip_comments(text: str) -> str:
    """Drop whole-line comments.

    Born from a false positive of the gate ON ITSELF: the fixed watchdog
    DOCUMENTS the trap in its header (« CODE held 000000, which is != "000" »)
    and the pattern matched inside the prose. A gate that punishes explaining a
    trap pushes people to stop documenting it: the exact opposite of the goal.
    Inline fixtures never caught it, only the REAL files of the session did."""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def judge(written: str, context: str):
    """-> (reason, explanation) or (None, None). `context` = the whole file."""
    written = strip_comments(written)
    context = strip_comments(context)
    if CURL_ECHO_RE.search(written):
        return ("curl-http-code-concatenated",
                "`curl -w '%{http_code}'` followed by an `|| echo`: when the "
                "connection fails, curl ALREADY prints 000 through -w AND exits "
                "non-zero, so the `|| echo` appends a SECOND one. The variable "
                "holds `000000`, which is != \"000\": a success test written as "
                "an inequality passes ON A FAILURE.\n"
                "   Rewrite: drop the `|| echo`, then test MEMBERSHIP:\n"
                "     CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \"$U\")\n"
                "     case \"$CODE\" in 2??|3??) ... ;; esac")

    if CODE_NEQ_RE.search(written) and "http_code" in (written + context):
        return ("http-code-tested-by-inequality",
                "an HTTP code tested with `!= \"000\"`: « not the one known "
                "failure value » is NOT « a success value » (curl can return a "
                "concatenated 000, or 404, or 500...). Test membership instead: "
                "`case \"$CODE\" in 2??|3??)`.")

    whole = context or written
    if PIPEFAIL_RE.search(whole) or (SET_E_RE.search(whole) and "pipefail" in whole):
        for m in GREP_WC_RE.finditer(written):
            if "|| true" in m.group("body") or "||true" in m.group("body"):
                continue
            return ("bare-grep-wc-under-pipefail",
                    "a `grep ... | wc -l` counter with NO `|| true`, inside a "
                    "script running `set -e` + `pipefail`. A grep that finds "
                    "NOTHING exits 1, pipefail propagates it through `wc`, and "
                    "set -e kills the script: it therefore dies on the SUCCESS "
                    "CASE (nothing to find was what we wanted).\n"
                    "   Rewrite: `VAR=$(... | wc -l || true)`")
    return (None, None)


def main():
    data = read_stdin_json()
    if not data:
        sys.exit(0)
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    written = written_fragments(tool_input)
    if not written:
        gate_stat(HOOK, "skip-nothing-written")
        sys.exit(0)

    context = target_content(tool_input)
    if not is_shell(path, written + context):
        gate_stat(HOOK, "skip-not-shell", path=path)
        sys.exit(0)

    reason, explanation = judge(written, context)
    if not reason:
        gate_stat(HOOK, "pass", path=path)
        sys.exit(0)

    gate_stat(HOOK, "block", reason=reason, path=path)
    sys.stderr.write(
        f"⛔ shell-false-success-gate: {reason}\n\n   {explanation}\n\n"
        "   This gate was born from TWO real losses on the same day: a wrapper "
        "that died on its success case (the operator believed for a whole day "
        "that the work was done), and a watchdog that reported a certificate as "
        "issued on a broken handshake.\n"
        "   Pattern miscalibrated? Say so to the operator, do not route around "
        "it silently (the gate-stats journal sees everything).\n")
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)   # fail-open: a broken gate never blocks the work
