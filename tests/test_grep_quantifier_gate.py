#!/usr/bin/env python3
"""Tests for hooks/grep-quantifier-gate.py. Zero network: the hook runs as a
subprocess with the JSON payload on stdin, exactly like under the real
harness; gate-stats isolated in a tempdir."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "grep-quantifier-gate.py"

KILLER = ".{0,20}X.{0,20}"          # the measured 2 GB pattern (ERE form)
KILLER_BRE = r".\{0,20\}X.\{0,20\}"  # same pattern, escaped BRE form


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("HARNESS_GREP_QUANTIFIER_GATE_DISABLE", None)  # isolate from caller
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


def bash_payload(cmd: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})


class TestGrepQuantifierGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}

    def tearDown(self):
        self.tmp.cleanup()

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def test_nominal_grep_passes(self):
        """An ordinary grep, with no bounded quantifier, goes through."""
        r = run_hook(bash_payload("grep -rn 'def main' hooks/ | head -20"),
                     self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_two_bounded_quantifiers_block_with_message(self):
        """The measured killer pattern: blocked, exit 2, actionable stderr,
        stat logged with the truncated command."""
        cmd = "grep -E '%s' notes.txt" % KILLER
        r = run_hook(bash_payload(cmd), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (ugrep quantifier gate", r.stderr)
        self.assertIn("add -P", r.stderr)
        self.assertIn("command grep", r.stderr)
        self.assertIn("HARNESS_GREP_QUANTIFIER_GATE_DISABLE=1", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertIn("grep -E", line["cmd"])

    def test_escaped_bre_and_pipeline_variants_block(self):
        """The escaped BRE form kills too, and a grep in any pipeline segment,
        eval, backtick substitution or quoted head is the same shadow
        function."""
        for cmd in ("grep '%s' notes.txt" % KILLER_BRE,
                    "cat notes.txt | grep -E '%s'" % KILLER,
                    "echo ok && grep -E '%s' notes.txt" % KILLER,
                    "eval \"grep -E '%s' notes.txt\"" % KILLER,
                    "out=`grep -E '%s' notes.txt`" % KILLER,
                    "'grep' -E '%s' notes.txt" % KILLER,
                    "time grep -E '%s' notes.txt" % KILLER,
                    "ugrep -E '%s' notes.txt" % KILLER):
            with self.subTest(cmd=cmd):
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 2, f"{cmd!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["result"], "block")

    def test_proven_innocent_paths_pass(self):
        """Measured innocents that must NOT be blocked: the -P and -F escape
        hatches, execvp wrappers and absolute paths (GNU grep from PATH), and
        child scripts (shell functions are not exported)."""
        for cmd in ("grep -P '%s' notes.txt" % KILLER,
                    "grep -F '%s' notes.txt" % KILLER,
                    "grep --perl-regexp '%s' notes.txt" % KILLER,
                    "command grep -E '%s' notes.txt" % KILLER,
                    "sudo grep -E '%s' /var/log/syslog" % KILLER,
                    "xargs grep -E '%s'" % KILLER,
                    "/usr/bin/grep -E '%s' notes.txt" % KILLER,
                    "bash scan.sh"):
            with self.subTest(cmd=cmd):
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 0, f"{cmd!r}: {r.stderr}")
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly; the fail-open is logged."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_edge_below_threshold_or_single_quantifier_passes(self):
        """The blast is 2^min(N,M) with TWO overlapping bounded quantifiers:
        one quantifier alone, or two small ones, stay under the threshold."""
        for cmd in ("grep -E '.{0,20}X' notes.txt",
                    "grep -E 'a{0,3}b{0,3}' notes.txt",
                    "grep -E '[0-9]{4}-[0-9]{2}' notes.txt"):
            with self.subTest(cmd=cmd):
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 0, f"{cmd!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_unparsable_line_blocks_conservatively(self):
        """Unclosed quote = shlex ValueError = coarse whole-line rule. Opposite
        trade-off from most gates, on purpose: a false positive costs one
        rewrite, a false negative costs a production pane."""
        r = run_hook(bash_payload("grep -E '%s notes.txt" % KILLER), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (ugrep quantifier gate", r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_kill_switch_disarms_for_the_session(self):
        """HARNESS_GREP_QUANTIFIER_GATE_DISABLE=1 lets even a real hit through."""
        env = dict(self.env, HARNESS_GREP_QUANTIFIER_GATE_DISABLE="1")
        r = run_hook(bash_payload("grep -E '%s' notes.txt" % KILLER), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "kill-switch")

    def test_non_bash_tool_ignored(self):
        """Other tools are out of scope: exit 0, no journal line. Writing the
        killer pattern INTO a script file is legitimate."""
        payload = json.dumps({"tool_name": "Write",
                              "tool_input": {"file_path": "/tmp/scan.sh",
                                             "content": "grep -E '%s' f\n"
                                                        % KILLER}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.stats.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
