#!/usr/bin/env python3
"""PreToolUse gate on Write|Edit|MultiEdit: a SUBSTANTIAL edit is never made
without a preparation step proved on disk.

ONE interlock for ONE predicate: before a big write, the agent must have taken
a preparation step, and that step must be PROVED by a state file inside a
validity window. Two doors open the same lock:

  decomp_done_ts -- a decomposition step: the work written out as a JSON list
                    of steps (step / action / pass_fail_criteria /
                    evidence_expected);
  ack_done_ts    -- a mission ack: the inventory of the files to touch plus
                    the risks, at least 5 non-empty lines.

The stamp is posted by the companion CLI `hooks/interlock-stamp.py`, which
demands a REAL artifact -- a valid steps JSON or a substantial ack -- never a
bare `touch`.

WHY (production post-mortem): the ancestor of this gate carried a WARN mode
plus two activation environment variables. Neither variable was ever exported,
so for months an inventory listed a guardrail that had never blocked a single
gesture. That is the classic dead-flag failure: documentation that lies, and a
team that believes it is protected. Hence the deliberate design here: NO
activation variable, NO kill-switch, WARN mode removed. The gate is armed the
moment it is wired, and the assumed way to stand it down is to remove its line
from the settings file -- a visible, reviewable gesture.

Anti-false-positive calibration (every exemption is JOURNALED, never mute):
  .md / .txt          -- a handoff or a memory note is not a build to
                         decompose; without this, every end-of-session write
                         would be a guaranteed block;
  scratch directories -- ephemeral work and deliverables, and also where the
                         proof artifacts live, so there is no chicken and egg.

Fail-open is journaled and covers hook errors only (unreadable stdin, crash).
A MISSING or CORRUPT state file is NOT a fail-open: above the threshold with
no proof is a block, otherwise corrupting the state would pick the lock.

Thresholds: new-file Write over 30 lines; Python AST diff of 2 or more new
structural nodes; non-Python over 30 added lines; 3 or more distinct code
files inside 10 minutes. Preparation-step window: 60 minutes.

Environment:
- HARNESS_STATE_DIR              state directory (default: ~/.harness); the
                                 session state is
                                 $HARNESS_STATE_DIR/interlock-<session>.json
- HARNESS_INTERLOCK_SCRATCH_DIRS colon-separated exempt scratch directories
                                 (default: the system temp dir, /tmp,
                                 /var/tmp). EXAMPLE, adding a deliverable
                                 directory:
                                 HARNESS_INTERLOCK_SCRATCH_DIRS=/tmp:/var/tmp:~/deliverables
"""
from __future__ import annotations

import ast
import fcntl
import json
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import STATE_DIR, gate_stat, read_stdin_json
except Exception:
    sys.exit(0)  # no helper, no gate: fail-open before anything else

HOOK = "interlock"
STAMP_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "interlock-stamp.py")

GATE_TOOLS = ("Write", "Edit", "MultiEdit")
STRUCTURAL_NODE_TYPES = frozenset({
    "If", "While", "For", "FunctionDef", "ClassDef", "AsyncFunctionDef",
})
THRESHOLD_NEW_NODES = 2
THRESHOLD_MULTI_FILE = 3
THRESHOLD_NEW_FILE_LINES = 30
THRESHOLD_NON_PYTHON_LINES_ADDED = 30
MULTI_FILE_WINDOW_S = 600
STEP_WINDOW_S = 3600
DOC_EXTS = {".md", ".txt"}


def scratch_prefixes() -> tuple[str, ...]:
    """Directories whose content is exempt: scratch, ephemeral, deliverables.

    They also hold the proof artifacts, so writing an ack or a steps file is
    never itself blocked by the gate that asks for it.
    """
    raw = os.environ.get("HARNESS_INTERLOCK_SCRATCH_DIRS")
    items = raw.split(":") if raw else ["/tmp", "/var/tmp",
                                        tempfile.gettempdir()]
    out = set()
    for item in items:
        item = item.strip()
        if not item:
            continue
        p = os.path.normpath(os.path.expanduser(item))
        out.add(p if p.endswith(os.sep) else p + os.sep)
    return tuple(sorted(out))


def state_path(session_id: str) -> Path:
    """One state file per session, under the shared harness state directory."""
    return Path(STATE_DIR) / f"interlock-{session_id}.json"


def load_state(session_id: str) -> dict:
    default = {"recent_edits": [], "decomp_done_ts": 0.0, "ack_done_ts": 0.0}
    if not session_id:
        return dict(default)
    sp = state_path(session_id)
    if not sp.exists():
        return dict(default)
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(default)
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except (OSError, ValueError):
        return dict(default)


def save_state(session_id: str, state: dict) -> None:
    """Locked and atomic: several tool calls can land at the same moment."""
    if not session_id:
        return
    sp = state_path(session_id)
    lock_file = sp.with_suffix(".json.lock")
    tmp_file = sp.with_suffix(".json.tmp")
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_file, "w", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                tmp_file.write_text(json.dumps(state), encoding="utf-8")
                os.replace(str(tmp_file), str(sp))
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def step_age(state: dict) -> float | None:
    """Age in seconds of the most recent stamp, whichever door posted it.
    None when no door has been stamped at all."""
    try:
        ts = max(float(state.get("decomp_done_ts", 0) or 0),
                 float(state.get("ack_done_ts", 0) or 0))
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return time.time() - ts


def count_structural_nodes(content: str) -> Counter | None:
    """None when the source does not parse; empty content gives an empty
    Counter (an unparsable side means the AST diff cannot be trusted)."""
    if not content:
        return Counter()
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return None
    return Counter(
        n.__class__.__name__
        for n in ast.walk(tree)
        if n.__class__.__name__ in STRUCTURAL_NODE_TYPES
    )


def count_new_structural_nodes(before: str, after: str) -> int | None:
    nodes_after = count_structural_nodes(after)
    nodes_before = count_structural_nodes(before)
    if nodes_after is None or nodes_before is None:
        return None
    delta = 0
    for t in STRUCTURAL_NODE_TYPES:
        diff = nodes_after.get(t, 0) - nodes_before.get(t, 0)
        if diff > 0:
            delta += diff
    return delta


def _apply_edit(content: str, old: str, new: str, replace_all: bool) -> str:
    if old and old in content:
        return content.replace(old, new) if replace_all else content.replace(old, new, 1)
    return content + new


def content_after(tool: str, tool_input: dict, before: str) -> str:
    """The file content as it WOULD be once the tool call lands."""
    if tool == "Write":
        return tool_input.get("content", "") or ""
    if tool == "MultiEdit":
        content = before
        for e in (tool_input.get("edits") or []):
            if isinstance(e, dict):
                content = _apply_edit(content, e.get("old_string", "") or "",
                                      e.get("new_string", "") or "",
                                      e.get("replace_all") is True)
        return content
    return _apply_edit(before, tool_input.get("old_string", "") or "",
                       tool_input.get("new_string", "") or "",
                       tool_input.get("replace_all") is True)


def _norm(file_path: str) -> str:
    try:
        return os.path.normpath(os.path.abspath(os.path.expanduser(file_path)))
    except Exception:
        return file_path


def decide(payload: dict) -> tuple[bool, str, str]:
    """(block, journal result, human detail). Reads the state, never writes it."""
    tool = payload.get("tool_name", "")
    if tool not in GATE_TOOLS:
        return (False, "skip-out-of-scope", tool or "?")
    inp = payload.get("tool_input") or {}
    if not isinstance(inp, dict):
        return (False, "fail-open", "tool_input is not an object")
    file_path = inp.get("file_path", "") or ""
    if not file_path:
        return (False, "skip-no-path", "no file_path in the payload")
    ap = _norm(file_path)

    if os.path.splitext(ap)[1].lower() in DOC_EXTS:
        return (False, "skip-doc", ap)
    if ap.startswith(scratch_prefixes()):
        return (False, "skip-scratch-path", ap)
    if tool == "Edit" and inp.get("replace_all") is True:
        return (False, "skip-replace-all", ap)

    session_id = payload.get("session_id", "") or ""
    state = load_state(session_id)
    age = step_age(state)
    if age is not None and age < STEP_WINDOW_S:
        return (False, "skip-recent-step", f"preparation step {int(age)}s ago")

    fp = Path(ap)
    if tool == "Write":
        before = ""
    else:
        try:
            before = fp.read_text(encoding="utf-8") if fp.exists() else ""
        except (OSError, UnicodeDecodeError):
            before = ""
    after = content_after(tool, inp, before)

    if tool == "Write" and not fp.exists():
        lines = after.count("\n") + (1 if after else 0)
        if lines > THRESHOLD_NEW_FILE_LINES:
            return (True, "block",
                    f"new file Write of {lines} lines "
                    f"(threshold >{THRESHOLD_NEW_FILE_LINES})")

    if ap.endswith(".py"):
        new_nodes = count_new_structural_nodes(before, after)
        if new_nodes is not None and new_nodes >= THRESHOLD_NEW_NODES:
            return (True, "block",
                    f"Python AST diff: {new_nodes} new structural nodes "
                    f"(threshold >={THRESHOLD_NEW_NODES})")
    else:
        added = after.count("\n") - before.count("\n")
        if added > THRESHOLD_NON_PYTHON_LINES_ADDED:
            return (True, "block",
                    f"non-Python +{added} lines "
                    f"(threshold >{THRESHOLD_NON_PYTHON_LINES_ADDED})")

    now = time.time()
    recent = [
        e for e in state.get("recent_edits", [])
        if isinstance(e, dict) and (now - float(e.get("ts", 0) or 0)) < MULTI_FILE_WINDOW_S
    ]
    touched = {e.get("file", "") for e in recent if e.get("file")}
    touched.add(ap)
    if len(touched) >= THRESHOLD_MULTI_FILE:
        return (True, "block",
                f"{len(touched)} distinct code files in {MULTI_FILE_WINDOW_S}s "
                f"(threshold >={THRESHOLD_MULTI_FILE})")

    return (False, "pass", "under every threshold")


def remember_edit(payload: dict) -> None:
    """Trace the edit AFTER the decision. Docs and scratch paths never count:
    this is a lock on CODE, so the multi-file threshold only counts code."""
    session_id = payload.get("session_id", "") or ""
    inp = payload.get("tool_input") or {}
    file_path = inp.get("file_path", "") if isinstance(inp, dict) else ""
    if not session_id or not file_path:
        return
    if payload.get("tool_name", "") not in GATE_TOOLS:
        return
    ap = _norm(file_path)
    if os.path.splitext(ap)[1].lower() in DOC_EXTS or ap.startswith(scratch_prefixes()):
        return
    state = load_state(session_id)
    now = time.time()
    edits = state.get("recent_edits", [])
    if not isinstance(edits, list):
        edits = []
    edits = [
        e for e in edits
        if isinstance(e, dict) and (now - float(e.get("ts", 0) or 0)) < MULTI_FILE_WINDOW_S
    ]
    edits.append({"file": ap, "ts": now})
    state["recent_edits"] = edits
    save_state(session_id, state)


def main() -> int:
    data = read_stdin_json()
    if not data:
        gate_stat(HOOK, "fail-open")
        return 0  # unreadable input: never block blindly
    blocked, result, detail = decide(data)
    remember_edit(data)
    if not blocked:
        gate_stat(HOOK, result, why=detail[:100])
        return 0
    session_id = data.get("session_id") or "<session-id>"
    inp = data.get("tool_input") or {}
    path = inp.get("file_path", "") if isinstance(inp, dict) else ""
    gate_stat(HOOK, "block", why=detail[:100], path=path)
    sys.stderr.write(
        f"BLOCKED (interlock gate): substantial edit with no preparation "
        f"step -- {detail}.\n"
        f"  file: {path}\n"
        "This is an andon cord, not a wall. Two doors open the same lock; put "
        "the artifact in a scratch directory, never in the tree you are about "
        "to edit:\n"
        "  1. DECOMPOSE the work -- a JSON list of steps, each one carrying "
        "step / action / pass_fail_criteria / evidence_expected, then\n"
        f"       python3 {STAMP_CLI} --session {session_id} "
        "--decomp <steps.json>\n"
        "  2. ACK the mission -- the inventory of the files you will touch "
        "plus the risks, at least 5 non-empty lines, then\n"
        f"       python3 {STAMP_CLI} --session {session_id} --ack <ack.md>\n"
        "Window: 60 minutes, then re-anchor. Threshold miscalibrated for this "
        "case? Say so to the operator instead of routing around it in "
        "silence: every execution lands in the gate-stats journal.\n"
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # fail-open on a hook error only, always journaled
        try:
            gate_stat(HOOK, "fail-open", why=("crash: " + str(exc))[:100])
        except Exception:
            pass
        sys.exit(0)
