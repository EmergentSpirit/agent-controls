#!/usr/bin/env python3
"""Tests for hooks/scope-write-gate.py and its companion hooks/scope-stamp.py.
Fails if the gate is unwired, mute, or too permissive.

Zero network: the hook runs as a subprocess with the JSON payload on stdin,
exactly like under the real harness. Gate-stats, the write perimeter AND the
bypass stamp are isolated in a tempdir -- never the real journal, never the
real perimeter, and never a leftover real stamp that would silently turn every
BLOCK case green."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "scope-write-gate.py"
STAMP_CLI = HERE.parent / "hooks" / "scope-stamp.py"

LEAKY_ENV = ("HARNESS_WRITE_SCOPE", "HARNESS_SCOPE_WRITE_STAMP",
             "HARNESS_SCOPE_WRITE_GATE_DISABLE", "HARNESS_STATE_DIR",
             "HARNESS_GATE_STATS")


def run(script: Path, args: list[str], stdin_text: str,
        env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for leak in LEAKY_ENV:
        env.pop(leak, None)  # isolate from the caller's own harness
    env.update(env_extra)
    return subprocess.run([sys.executable, str(script)] + args,
                          input=stdin_text, capture_output=True, text=True,
                          env=env, timeout=30)


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    return run(HOOK, [], stdin_text, env_extra)


def run_cli(args: list[str], env_extra: dict) -> subprocess.CompletedProcess:
    return run(STAMP_CLI, args, "", env_extra)


class TestScopeWriteGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.work = root / "agent-work"            # directory entry
        self.decoy = root / "agent-work-2"         # same prefix, NOT in scope
        self.shared = root / "shared"              # another role's territory
        self.exact = root / "extra-config.json"    # exact file entry
        for d in (self.work / "reports", self.decoy, self.shared / ".claude"):
            d.mkdir(parents=True)
        self.stats = root / "stats.jsonl"
        self.stamp = root / "scope-write.stamp"    # deliberately absent
        self.root = root
        self.cwd = str(root)
        self.env = {
            "HARNESS_GATE_STATS": str(self.stats),
            "HARNESS_STATE_DIR": str(root / "state"),
            "HARNESS_WRITE_SCOPE": f"{self.work}:{self.exact}",
            "HARNESS_SCOPE_WRITE_STAMP": str(self.stamp),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, path: str, tool: str = "Write", key: str = "file_path",
                cwd: str = "") -> str:
        return json.dumps({"tool_name": tool,
                           "tool_input": {key: path, "content": "x\n"},
                           "cwd": cwd or self.cwd})

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def write_stamp(self, body, age_s: float = 0.0) -> None:
        text = body if isinstance(body, str) else json.dumps(body)
        self.stamp.write_text(text + "\n", encoding="utf-8")
        if age_s:
            when = os.path.getmtime(self.stamp) - age_s
            os.utime(self.stamp, (when, when))

    # -- nominal --------------------------------------------------------
    def test_nominal_writes_inside_perimeter_pass(self):
        """Everything under a perimeter directory, plus the exact file entry."""
        for path in (f"{self.work}/notes.md",
                     f"{self.work}/reports/2026-01-01-brief.md",
                     f"{self.work}/deep/nested/new.txt",
                     str(self.exact)):
            with self.subTest(path=path):
                r = run_hook(self.payload(path), self.env)
                self.assertEqual(r.returncode, 0, f"{path!r}: {r.stderr}")
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    # -- block ----------------------------------------------------------
    def test_outside_perimeter_blocks_with_message(self):
        """The founding gesture: exit 2, actionable stderr, stat journaled."""
        target = f"{self.shared}/.claude/settings.json"
        r = run_hook(self.payload(target, "Edit"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (scope-write gate)", r.stderr)
        self.assertIn(target, r.stderr)
        self.assertIn(str(self.work), r.stderr)  # the perimeter is spelled out
        self.assertIn("scope-stamp.py", r.stderr)
        self.assertIn("HARNESS_WRITE_SCOPE", r.stderr)
        self.assertIn("HARNESS_SCOPE_WRITE_GATE_DISABLE=1", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["path"], target)

    def test_every_write_tool_outside_blocks(self):
        """All four mutating tools, including the notebook path key."""
        cases = [(f"{self.shared}/hooks/guard.py", "Write", "file_path"),
                 (f"{self.shared}/canon/rule.md", "Edit", "file_path"),
                 (f"{self.shared}/app/main.py", "MultiEdit", "file_path"),
                 (f"{self.shared}/nb/run.ipynb", "NotebookEdit",
                  "notebook_path")]
        for path, tool, key in cases:
            with self.subTest(tool=tool):
                r = run_hook(self.payload(path, tool, key), self.env)
                self.assertEqual(r.returncode, 2, f"{path!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["result"], "block")

    # -- fail-open ------------------------------------------------------
    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly; the fail-open is journaled."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    # -- edge cases -----------------------------------------------------
    def test_edge_traversal_out_of_the_perimeter_blocks(self):
        """A path that STARTS inside the perimeter and walks out with `../`
        is outside: the target is normalized before matching."""
        target = f"{self.work}/../shared/sneaky.md"
        r = run_hook(self.payload(target), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_edge_relative_path_resolves_against_session_cwd(self):
        """The same file reached by a shorter name is the same file."""
        r = run_hook(self.payload("agent-work/report.md"), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")
        r = run_hook(self.payload("shared/report.md"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_edge_same_prefix_is_not_inside_the_perimeter(self):
        """`agent-work-2` shares a string prefix with `agent-work` and is a
        different directory: matching is on whole path segments."""
        r = run_hook(self.payload(f"{self.decoy}/notes.md"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_edge_exact_file_entry_does_not_open_its_directory(self):
        """A FILE entry grants that file only, not its siblings."""
        r = run_hook(self.payload(f"{self.root}/extra-config.local.json"),
                     self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_out_of_scope_tool_is_journaled_and_allowed(self):
        """Read and friends are out of scope, and the pass-through logs."""
        payload = json.dumps({"tool_name": "Read",
                              "tool_input": {"file_path": "/etc/hosts"},
                              "cwd": self.cwd})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-out-of-scope")

    def test_payload_without_a_path_is_journaled_and_allowed(self):
        """No path in the payload: nothing to judge, nothing to block."""
        payload = json.dumps({"tool_name": "Write",
                              "tool_input": {"content": "x"},
                              "cwd": self.cwd})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-no-path")

    def test_kill_switch_disarms_for_the_session(self):
        """The kill-switch lets even a real hit through, journaled as
        skip-disabled: routing around the gate stays visible."""
        env = dict(self.env, HARNESS_SCOPE_WRITE_GATE_DISABLE="1")
        r = run_hook(self.payload(f"{self.shared}/.claude/settings.json"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-disabled")

    def test_default_perimeter_is_the_session_cwd(self):
        """With no HARNESS_WRITE_SCOPE the session cwd is the perimeter."""
        env = {k: v for k, v in self.env.items() if k != "HARNESS_WRITE_SCOPE"}
        r = run_hook(self.payload(f"{self.work}/notes.md",
                                  cwd=str(self.work)), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")
        r = run_hook(self.payload("/etc/agent-config/settings.json",
                                  cwd=str(self.work)), env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    # -- stamped bypass -------------------------------------------------
    def test_fresh_stamp_turns_the_block_into_a_traced_allow(self):
        """Sanctioned one-shot: a stamp younger than 30 minutes covering the
        target lets the write through, journaled as skip-stamp with reason."""
        self.write_stamp({"allowed_prefix": str(self.shared),
                          "reason": "human GO: land the hook here"})
        target = f"{self.shared}/hooks/guard.py"
        r = run_hook(self.payload(target), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "skip-stamp")
        self.assertEqual(line["path"], target)
        self.assertEqual(line["reason"], "human GO: land the hook here")

    def test_stamp_covers_one_prefix_only(self):
        """Outside the stamped prefix, the block is unchanged: a stamp is
        never a global disable."""
        self.write_stamp({"allowed_prefix": f"{self.shared}/hooks",
                          "reason": "human GO: land the hook here"})
        r = run_hook(self.payload(f"{self.shared}/.claude/settings.json"),
                     self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_stale_stamp_still_blocks(self):
        """Past the 30-minute window the stamp is worthless."""
        self.write_stamp({"allowed_prefix": str(self.shared),
                          "reason": "human GO"}, age_s=31 * 60)
        r = run_hook(self.payload(f"{self.shared}/x.md"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_malformed_stamp_fails_closed(self):
        """Corrupt JSON, no prefix, or a relative prefix: BLOCK stands. The
        bypass is the only fail-CLOSED part of this gate."""
        for body in ("{ not json at all",
                     {"reason": "human GO"},
                     {"allowed_prefix": "shared", "reason": "human GO"},
                     {"allowed_prefix": "", "reason": "human GO"}):
            with self.subTest(body=body):
                self.write_stamp(body)
                r = run_hook(self.payload(f"{self.shared}/x.md"), self.env)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(self.last_stat()["result"], "block")

    # -- the companion CLI ----------------------------------------------
    def test_stamp_cli_posts_a_stamp_the_gate_honors(self):
        """End to end: the CLI writes the stamp and journals the gesture, then
        the gate turns the same write into a traced allow."""
        r = run_cli([str(self.shared), "--reason", "human GO: drop the hook"],
                    self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stamp posted", r.stdout)
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        self.assertEqual(stamp["allowed_prefix"], str(self.shared))
        self.assertEqual(stamp["reason"], "human GO: drop the hook")
        line = self.last_stat()
        self.assertEqual(line["hook"], "scope-write")
        self.assertEqual(line["result"], "observe")
        self.assertEqual(line["event"], "stamp-posted")

        r = run_hook(self.payload(f"{self.shared}/hooks/guard.py"), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-stamp")

    def test_stamp_cli_refuses_a_relative_prefix_or_an_empty_reason(self):
        """No stamp file is created when the CLI refuses: nothing to honor."""
        for args in (["shared", "--reason", "human GO"],
                     [str(self.shared), "--reason", "   "]):
            with self.subTest(args=args):
                r = run_cli(args, self.env)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("REFUSED", r.stderr)
                self.assertFalse(self.stamp.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
