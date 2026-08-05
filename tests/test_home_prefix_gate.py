#!/usr/bin/env python3
"""Tests for hooks/home-prefix-gate.py. Zero network: the hook runs as a
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
HOOK = HERE.parent / "hooks" / "home-prefix-gate.py"


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("HARNESS_HOME_PREFIX_GATE_DISABLE", None)  # isolate from the caller
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


def bash_payload(cmd: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})


class TestHomePrefixGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}

    def tearDown(self):
        self.tmp.cleanup()

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def test_nominal_command_passes(self):
        """A plain command without any HOME= prefix goes through untouched."""
        r = run_hook(bash_payload("echo hello && ls -la /tmp"), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_home_prefix_blocks_with_message(self):
        """Direct HOME= prefix: blocked, exit 2, actionable stderr, stat logged."""
        cmd = "HOME=/tmp/other-home python3 -c \"print('hello')\""
        r = run_hook(bash_payload(cmd), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (HOME= prefix gate)", r.stderr)
        self.assertIn("WRITES ITS VERDICT TO A FILE", r.stderr)
        self.assertIn("HARNESS_HOME_PREFIX_GATE_DISABLE=1", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertIn("HOME=/tmp/other-home", line["cmd"])

    def test_env_and_export_variants_block(self):
        """`env [flags] HOME=` and `export HOME=` are the same trap."""
        for cmd in ("env HOME=/tmp/x python3 -V",
                    "env -i HOME=/tmp/x python3 -V",
                    "export HOME=/tmp/x",
                    "FOO=1 HOME=/tmp/x ./run.sh",
                    "echo ok && HOME=/tmp/x python3 -V"):
            with self.subTest(cmd=cmd):
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 2, f"{cmd!r}: {r.stderr}")

    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly; the fail-open is logged."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_edge_home_inside_quotes_or_argument_passes(self):
        """Founding false positive: HOME= inside quotes or as a plain argument
        is NOT a prefix assignment (naive splitting on | cut inside quotes)."""
        for cmd in ("sed 's|HOME=/a|HOME=/b|' config.txt",
                    "grep HOME= /etc/profile",
                    "HOMEBREW_PREFIX=/opt/homebrew brew list"):
            with self.subTest(cmd=cmd):
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 0, f"{cmd!r}: {r.stderr}")
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_unparsable_command_fails_open(self):
        """Unclosed quote = shlex ValueError = fail-open (pass, no block)."""
        r = run_hook(bash_payload('echo "unclosed HOME=/tmp/x'), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_kill_switch_disarms_for_the_session(self):
        """HARNESS_HOME_PREFIX_GATE_DISABLE=1 lets even a real hit through."""
        env = dict(self.env, HARNESS_HOME_PREFIX_GATE_DISABLE="1")
        r = run_hook(bash_payload("HOME=/tmp/x python3 -V"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-disabled")

    def test_non_bash_tool_ignored(self):
        """Other tools are out of scope: exit 0, no journal line."""
        payload = json.dumps({"tool_name": "Write",
                              "tool_input": {"file_path": "/tmp/f",
                                             "content": "HOME=/tmp/x"}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.stats.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
