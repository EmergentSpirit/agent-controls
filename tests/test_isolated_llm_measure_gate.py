#!/usr/bin/env python3
"""Tests for hooks/isolated-llm-measure-gate.py. Zero network: the hook runs as
a subprocess with the JSON payload on stdin, exactly like under the real
harness; gate-stats isolated in a tempdir.

design-note: the offending call is NEVER spelled out as a literal in this file.
Fixtures are templates whose CLI name and headless flag are substituted at
runtime. Spelled out literally, this very test file would be a Python file
containing an unisolated call, and the gate would block the writing of its own
test suite. The same dodge is what a user needs, and it is documented in
docs/gates/isolated-llm-measure-gate.md."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "isolated-llm-measure-gate.py"

CLI = "claude"        # binary the gate watches by default
FLAG = "-p"           # headless flag


def fixture(template: str, cli: str = CLI) -> str:
    """Render a fixture: %(cli)s / %(flag)s become QUOTED literals."""
    return template % {"cli": '"%s"' % cli, "flag": '"%s"' % FLAG}


# The measurement harness as it must NOT be written: the CLI inherits the
# current directory, so the judge reads the answer it is meant to grade.
UNISOLATED = fixture(
    "import subprocess\n"
    "\n"
    "def judge(prompt):\n"
    "    out = subprocess.run([%(cli)s, %(flag)s, prompt], "
    "capture_output=True, text=True)\n"
    "    return out.stdout\n"
)

# The same harness, isolated in a neutral temporary directory.
ISOLATED = fixture(
    "import subprocess\n"
    "import tempfile\n"
    "\n"
    "def judge(prompt):\n"
    "    out = subprocess.run([%(cli)s, %(flag)s, prompt], capture_output=True,\n"
    "                         text=True, cwd=tempfile.mkdtemp())\n"
    "    return out.stdout\n"
)


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for leaked in ("HARNESS_ISOLATED_LLM_MEASURE_GATE_DISABLE",
                   "HARNESS_LLM_CLI_NAMES"):
        env.pop(leaked, None)          # isolate from the caller's environment
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


def write_payload(path: str, content: str) -> str:
    return json.dumps({"tool_name": "Write",
                       "tool_input": {"file_path": path, "content": content}})


class TestIsolatedLlmMeasureGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}
        self.target = str(Path(self.tmp.name) / "bench.py")

    def tearDown(self):
        self.tmp.cleanup()

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def test_nominal_isolated_call_passes(self):
        """An automated headless call carrying an explicit cwd= goes through."""
        r = run_hook(write_payload(self.target, ISOLATED), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_unisolated_call_blocks_with_message(self):
        """No cwd=: blocked, exit 2, actionable stderr, one journal line."""
        r = run_hook(write_payload(self.target, UNISOLATED), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("isolated-llm-measure-gate", r.stderr)
        self.assertIn("NEUTRAL directory", r.stderr)
        self.assertIn("cwd=tempfile.mkdtemp()", r.stderr)
        self.assertIn("HARNESS_ISOLATED_LLM_MEASURE_GATE_DISABLE=1", r.stderr)
        self.assertIn("Offending call(s):", r.stderr)
        self.assertIn(CLI, r.stderr)          # the excerpt names the culprit
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["n"], 1)
        self.assertEqual(line["path"], self.target)

    def test_multiline_call_and_edit_tool_block(self):
        """A call spread over several lines, and the Edit tool's new_string,
        are the same trap."""
        multiline = fixture(
            "import subprocess\n"
            "proc = subprocess.Popen(\n"
            "    [%(cli)s, %(flag)s, prompt],\n"
            "    stdout=subprocess.PIPE,\n"
            ")\n"
        )
        r = run_hook(write_payload(self.target, multiline), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

        edit = json.dumps({"tool_name": "Edit",
                           "tool_input": {"file_path": self.target,
                                          "old_string": "pass\n",
                                          "new_string": UNISOLATED}})
        r = run_hook(edit, self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly; the fail-open is logged."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_edge_non_subprocess_mentions_pass(self):
        """Zero false positives by construction: a shell string, a comment, or a
        call without the headless flag are not automated measurements."""
        cases = {
            "shell string": ("import os\n"
                             "CMD = \"%s -p grade-this\"\n" % CLI +
                             "os.system(CMD)\n"),
            "comment only": fixture(
                "import subprocess\n"
                "# never call subprocess.run([%(cli)s, %(flag)s, x]) "
                "without an explicit directory\n"),
            "no headless flag": fixture(
                "import subprocess\n"
                "subprocess.run([%(cli)s, \"--version\"], capture_output=True)\n"),
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                r = run_hook(write_payload(self.target, content), self.env)
                self.assertEqual(r.returncode, 0, "%s: %s" % (label, r.stderr))
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_non_python_file_is_out_of_scope(self):
        """A doc that QUOTES the trap must stay writable: punishing the people
        who document a trap is the fastest way to stop them documenting it."""
        doc = str(Path(self.tmp.name) / "notes.md")
        r = run_hook(write_payload(doc, "Never do this:\n\n    " + UNISOLATED),
                     self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-not-python")

    def test_custom_cli_names_are_honoured(self):
        """HARNESS_LLM_CLI_NAMES retargets the gate at another agent CLI."""
        content = fixture(
            "import subprocess\n"
            "subprocess.run([%(cli)s, %(flag)s, prompt], capture_output=True)\n",
            cli="othercli")
        r = run_hook(write_payload(self.target, content), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)   # unknown binary: ignored
        self.assertEqual(self.last_stat()["result"], "pass")
        env = dict(self.env, HARNESS_LLM_CLI_NAMES="%s:othercli" % CLI)
        r = run_hook(write_payload(self.target, content), env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_kill_switch_disarms_for_the_session(self):
        """The kill-switch lets a real hit through, and says so in the journal."""
        env = dict(self.env, HARNESS_ISOLATED_LLM_MEASURE_GATE_DISABLE="1")
        r = run_hook(write_payload(self.target, UNISOLATED), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-disabled")

    def test_other_tools_are_logged_and_ignored(self):
        """A Bash command is out of scope, and the execution is still logged."""
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": "%s -p hello" % CLI}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-not-write")


if __name__ == "__main__":
    unittest.main(verbosity=2)
