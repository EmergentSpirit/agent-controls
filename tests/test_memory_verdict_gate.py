#!/usr/bin/env python3
"""Tests for memory-verdict-gate.py (all issues at once, one single error
block). Zero network: the hook runs as a subprocess with the JSON payload on
stdin, exactly like under the real harness; gate-stats isolated in a tempdir."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "memory" / "memory-verdict-gate.py"


def run_hook(payload: dict, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ, **env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=30)


class TestMemoryVerdictGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memdir = Path(self.tmp.name) / "memory"
        self.memdir.mkdir()
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}

    def tearDown(self):
        self.tmp.cleanup()

    def payload_write(self, name: str, note: str) -> dict:
        return {"tool_name": "Write",
                "tool_input": {"file_path": str(self.memdir / name), "content": note}}

    def test_t1_two_issues_one_single_block(self):
        """Status outside the closed list + body without a VERDICT block:
        BOTH reasons, ONE block."""
        note = "---\nname: x\ndescription: d\nstatus: bogus\n---\nNo verdict here.\n"
        r = run_hook(self.payload_write("t1.md", note), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("2 issue", r.stderr)
        self.assertIn("not in the closed list", r.stderr)
        self.assertIn("does not START with the verdict block", r.stderr)
        self.assertEqual(r.stderr.count("⛔ memory-verdict-gate"), 1)
        line = json.loads(self.stats.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["reasons"], 2)
        self.assertIn("reason", line)  # stats schema compatibility

    def test_t2_conforming_note(self):
        note = ("---\nname: x\ndescription: d\nstatus: active\n---\n"
                "**VERDICT — active.** It holds.\n")
        r = run_hook(self.payload_write("t2.md", note), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")

    def test_t3_index_that_lies_regression(self):
        """Non-active status MISSING from MEMORY.md: always blocked (this is
        the founding incident: the index is read first, it must never lie)."""
        (self.memdir / "MEMORY.md").write_text("# index\n- [other](other.md)\n",
                                               encoding="utf-8")
        note = ("---\nname: x\ndescription: d\nstatus: stale\n---\n"
                "**VERDICT — stale.** Done with.\n")
        r = run_hook(self.payload_write("t3.md", note), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("MISSING from MEMORY.md", r.stderr)

    def test_t4_issues_without_index_still_block(self):
        """Without MEMORY.md, the accumulated issues do not evaporate."""
        note = "---\nname: x\ndescription: d\nstatus: stale\n---\nNo verdict.\n"
        r = run_hook(self.payload_write("t4.md", note), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("does not START with the verdict block", r.stderr)

    def test_t5_t6_t7_fields_nested_by_sync_tools(self):
        """Some sync tools (e.g. basic-memory) nest status/superseded_by under
        metadata: (indented). T5: status ONLY nested → passes. T6:
        superseded_by nested with a superseded status → passes (no false
        « superseded without superseded_by »). T7: status nowhere → still
        blocked (the fallback invents nothing). Three cases, one method:
        same fallback class."""
        # T5 — status ONLY under metadata:, verdict in agreement.
        note5 = ("---\nname: x\ndescription: d\nmetadata:\n  type: project\n"
                 "  status: active\n---\n**VERDICT — active.** It holds.\n")
        r5 = run_hook(self.payload_write("t5.md", note5), self.env)
        self.assertEqual(r5.returncode, 0, r5.stderr)
        self.assertEqual(r5.stderr, "")
        # T6 — superseded_by nested, superseded status nested too.
        note6 = ("---\nname: x\ndescription: d\nmetadata:\n  status: superseded\n"
                 "  superseded_by: new-note\n---\n"
                 "**VERDICT — superseded.** Superseded by new-note.\n")
        r6 = run_hook(self.payload_write("t6.md", note6), self.env)
        self.assertEqual(r6.returncode, 0, r6.stderr)
        # T7 — status nowhere: blocked, the fallback invented nothing.
        note7 = ("---\nname: x\ndescription: d\nmetadata:\n  type: project\n---\n"
                 "**VERDICT — active.** It holds.\n")
        r7 = run_hook(self.payload_write("t7.md", note7), self.env)
        self.assertEqual(r7.returncode, 2, r7.stderr)
        self.assertIn("status", r7.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
