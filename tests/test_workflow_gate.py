#!/usr/bin/env python3
"""Tests for hooks/workflow-gate.py. Zero network: the hook runs as a subprocess
with the JSON payload on stdin, exactly like under the real harness; gate-stats
isolated in a tempdir.

Ported from the internal suite, which drove the three checks A / C / B off the
payloads of the founding session."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "workflow-gate.py"


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("HARNESS_WORKFLOW_GATE_DISABLE", None)   # isolate from the caller
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


def workflow_payload(script: str) -> str:
    return json.dumps({"tool_name": "Workflow", "tool_input": {"script": script}})


class TestWorkflowGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}

    def tearDown(self):
        self.tmp.cleanup()

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    # --- Check A: a deterministic task handed to an LLM agent ---------------

    def test_a_mechanical_label_blocks_with_message(self):
        """The founding case: an `ls` over hundreds of files delegated to a
        sub-agent. Blocked, exit 2, actionable stderr, stat carries check A."""
        script = ("phase('L'); const l = await agent(pr, "
                  "{label:'list', model:'small', schema:S})")
        r = run_hook(workflow_payload(script), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (workflow gate, check A)", r.stderr)
        self.assertIn("deterministic task delegated to an", r.stderr)
        self.assertIn("via `args`", r.stderr)
        self.assertIn("HARNESS_WORKFLOW_GATE_DISABLE=1", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["check"], "A")
        self.assertIn("label:'list", line["match"])

    def test_a_mechanical_label_variants_block(self):
        """Every label naming a job that CODE does better is the same trap."""
        for label in ("count", "parse", "glob", "grep", "wc", "dedup",
                      "rename", "sort", "ls"):
            with self.subTest(label=label):
                script = "const c = await agent(p, {label:'%s', schema:S})" % label
                r = run_hook(workflow_payload(script), self.env)
                self.assertEqual(r.returncode, 2, f"{label!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["check"], "A")

    def test_a_mechanical_prompt_blocks(self):
        """The label can be innocent; the prompt gives the shell work away."""
        for script in (
            "const pr='With the Bash tool, list all the files under /x';"
            " await agent(pr,{label:'x'})",
            "await agent('run ls -la and report', {label:'x'})",
            "await agent('give me wc -l for each entry', {label:'x'})",
        ):
            with self.subTest(script=script[:40]):
                r = run_hook(workflow_payload(script), self.env)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(self.last_stat()["check"], "A")

    # --- Check C: an agent persisting data through Write --------------------

    def test_c_agent_write_blocks_with_message(self):
        """The 144 lost extractions: agents persisting data with the Write tool."""
        script = ("const p='THEN write the result with the Write tool into '+out;"
                  " await agent(p,{label:'ext',schema:S})")
        r = run_hook(workflow_payload(script), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (workflow gate, check C)", r.stderr)
        self.assertIn("VIA THE SCHEMA", r.stderr)
        self.assertIn("144 extractions", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["check"], "C")

    def test_c_agent_write_to_a_data_file_blocks(self):
        """A data-file extension right after a write is the same persistence."""
        script = ("await agent('extract then write to ideas/x.json', "
                  "{label:'e', schema:S})")
        r = run_hook(workflow_payload(script), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["check"], "C")

    # --- Check B: fan-out without attestation -------------------------------

    def test_b_fanout_without_attestation_blocks(self):
        """A fan-out launched before the mechanism was validated on a sample."""
        for script in (
            "const r = await parallel(items.map(i=>()=>agent(judge(i),"
            "{label:'judge',schema:S})))",
            "const r = await pipeline(items, s1, s2)",
        ):
            with self.subTest(script=script[:30]):
                r = run_hook(workflow_payload(script), self.env)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("BLOCKED (workflow gate, check B)", r.stderr)
                self.assertIn("@small-run", r.stderr)
                self.assertIn("@sample-tested", r.stderr)
                self.assertEqual(self.last_stat()["check"], "B")

    def test_b_attested_fanout_passes(self):
        """Both attestation markers unlock the fan-out; the corrected design
        (schema-based extraction, Read only, no Write) goes through."""
        for script in (
            "// @small-run\nconst r = await parallel(items.map(i=>()=>"
            "agent(judge(i),{label:'judge'})))",
            "// @sample-tested\nconst r = await pipeline(items, extract, score)",
            "// @sample-tested\nconst r = await parallel(items.map(i=>()=>"
            "agent('Read the transcript at: '+i.path,{schema:EXT})))",
        ):
            with self.subTest(script=script[:30]):
                r = run_hook(workflow_payload(script), self.env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    # --- Fail-open, scope, kill-switch, edge cases --------------------------

    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly; the fail-open is logged."""
        r = run_hook("this is not json {", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_other_tools_are_out_of_scope(self):
        """A Bash command holding the same words is not this gate's business."""
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": "ls -la"}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-out-of-scope")

    def test_workflow_without_script_is_a_skip(self):
        """No script to judge: nothing to block."""
        payload = json.dumps({"tool_name": "Workflow", "tool_input": {"name": "foo"}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-no-script")

    def test_edge_script_path_is_read_from_disk(self):
        """The script can arrive as a path: the gate reads the file, and an
        unreadable path fails open instead of blocking."""
        script_file = Path(self.tmp.name) / "wf.js"
        script_file.write_text("const r = await pipeline(items, s1, s2)\n",
                               encoding="utf-8")
        payload = json.dumps({"tool_name": "Workflow",
                              "tool_input": {"scriptPath": str(script_file)}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["check"], "B")

        payload = json.dumps({"tool_name": "Workflow",
                              "tool_input": {"scriptPath": "/does/not/exist.js"}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-no-script")

    def test_kill_switch_disarms_for_the_session(self):
        """The kill-switch is deliberate and never silent: logged skip-disabled."""
        env = dict(self.env, HARNESS_WORKFLOW_GATE_DISABLE="1")
        r = run_hook(workflow_payload("const l = await agent(p, {label:'list'})"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-disabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
