#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for hooks/shell-false-success-gate.py.

Two halves, and both count:
  1. the gate BITES on both patterns, in their REAL shape (the actual files
     that caused the damage, not textbook cases);
  2. it stays SILENT on correct forms and outside a wrapper (zero false
     positives): a gate that blocks healthy work gets unplugged, and then it
     guards nothing at all.

The killer test is `test_neutralized_patterns_go_blind`: it disarms the
patterns and checks the detections collapse. Without it we would not know
whether the gate still guards anything.

Zero network: the hook runs as a subprocess with the JSON payload on stdin,
exactly like under the real harness; gate-stats isolated in a tempdir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "shell-false-success-gate.py"

HEADER = "#!/bin/bash\nset -euo pipefail\n"

# ── The REAL shapes that hurt ───────────────────────────────────────────────
WATCHDOG_V1 = HEADER + (
    'CODE=$(curl -sS -o /dev/null -A "$UA" -w \'%{http_code}\' '
    '--max-time 20 https://example.org 2>/dev/null || echo 000)\n'
    'if [ "$CODE" != "000" ]; then echo ISSUED; fi\n')

INEQUALITY_ONLY = HEADER + (
    'CODE=$(curl -s -o /dev/null -w \'%{http_code}\' --max-time 20 "$U")\n'
    'if [ "$CODE" != "000" ]; then echo ISSUED; fi\n')

CUTOVER_WRAPPER_V1 = HEADER + (
    'PEND=$(sudo grep -rl \'"item_id": *"K\' /var/lib/app/queue '
    '/var/lib/app/commands 2>/dev/null | wc -l)\n'
    'echo "pending: $PEND"\n')

FINAL_VERIFICATION = HEADER + (
    "LEFT=$(sudo grep -rlE 'old-domain\\.example/|@old-domain\\.example' "
    "/opt/app 2>/dev/null | wc -l)\n"
    '[ "$LEFT" -eq 0 ] || exit 1\n')

# ── The FIXED shapes, which must go through ────────────────────────────────
WATCHDOG_V2 = HEADER + (
    'CODE=$(curl -s -o /dev/null -w \'%{http_code}\' --max-time 20 "$U" 2>/dev/null)\n'
    'case "$CODE" in 2??|3??) echo ALIVE ;; esac\n')

CUTOVER_WRAPPER_V2 = HEADER + (
    'PEND=$(sudo grep -rl \'"item_id": *"K\' /var/lib/app/queue '
    '2>/dev/null | wc -l || true)\n')

# ── Outside a wrapper: the same grep with no set -e/pipefail is harmless ───
NO_PIPEFAIL = "#!/bin/bash\nN=$(grep -rl pattern /tmp | wc -l)\necho $N\n"

# The founding false positive: the FIXED watchdog documents the trap in its
# header, and the pattern matched inside the prose.
COMMENTED_FIX = HEADER + (
    '# FIXED BUG: CODE=$(curl -w %{http_code} ... || echo 000) concatenated two\n'
    '# values. CODE held "000000", which is != "000", so the test passed.\n'
    'CODE=$(curl -s -o /dev/null -w \'%{http_code}\' --max-time 20 "$U" 2>/dev/null)\n'
    'case "$CODE" in 2??|3??) echo ALIVE ;; esac\n')

# Disarms the three patterns inside the module, then judges the REAL shapes.
KILL_PROBE = """
import importlib.util, json, re, sys
spec = importlib.util.spec_from_file_location("gate_under_test", sys.argv[1])
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
never = re.compile(r"(?!x)x")   # matches nothing
g.CURL_ECHO_RE = g.CODE_NEQ_RE = g.GREP_WC_RE = never
print(json.dumps([g.judge(s, s)[0] for s in json.loads(sys.argv[2])]))
"""


def run_hook(payload, env_extra: dict) -> subprocess.CompletedProcess:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    env = dict(os.environ, **env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin,
                          capture_output=True, text=True, env=env, timeout=30)


class TestShellFalseSuccessGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.stats = self.dir / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}

    def tearDown(self):
        self.tmp.cleanup()

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def write_payload(self, content: str, name: str = "script.sh") -> dict:
        return {"tool_name": "Write",
                "tool_input": {"file_path": str(self.dir / name),
                               "content": content}}

    # ─── 1. nominal: the fixed / harmless shapes pass ──────────────────────
    def test_nominal_healthy_shapes_pass(self):
        """Fixed watchdog, fixed counter, a counter outside any wrapper, and a
        curl with no http_code: exit 0, no stderr, logged as pass."""
        cases = {
            "fixed watchdog (membership test)": WATCHDOG_V2,
            "fixed counter (|| true)": CUTOVER_WRAPPER_V2,
            "grep|wc OUTSIDE a wrapper (no set -e, no pipefail)": NO_PIPEFAIL,
            "header only": HEADER,
            "curl without http_code": HEADER + 'curl -sf "$U" -o /tmp/f || echo miss\n',
        }
        for label, src in cases.items():
            with self.subTest(case=label):
                r = run_hook(self.write_payload(src), self.env)
                self.assertEqual(r.returncode, 0, f"{label}: {r.stderr}")
                self.assertEqual(r.stderr, "", f"FALSE POSITIVE on: {label}")
                self.assertEqual(self.last_stat()["result"], "pass")

    # ─── 2. block, with the message verified ───────────────────────────────
    def test_curl_echo_concatenation_blocks_with_rewrite(self):
        """The watchdog that announced success on a broken handshake: exit 2,
        stderr names the pattern AND hands the rewrite, stat logged."""
        r = run_hook(self.write_payload(WATCHDOG_V1), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("⛔ shell-false-success-gate: curl-http-code-concatenated",
                      r.stderr)
        self.assertIn("000000", r.stderr)
        self.assertIn('case "$CODE" in 2??|3??)', r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["reason"], "curl-http-code-concatenated")
        self.assertTrue(line["path"].endswith("script.sh"))

    def test_bare_grep_wc_under_pipefail_blocks(self):
        """Both wrapper shapes die on their SUCCESS case; the rewrite hint is
        `|| true`. The final-verification shape is the nastier one: it would
        have failed the wrapper AFTER the mutation went through."""
        for label, src in (("cutover counter", CUTOVER_WRAPPER_V1),
                           ("final verification", FINAL_VERIFICATION)):
            with self.subTest(case=label):
                r = run_hook(self.write_payload(src), self.env)
                self.assertEqual(r.returncode, 2, f"{label} NOT DETECTED")
                self.assertIn("bare-grep-wc-under-pipefail", r.stderr)
                self.assertIn("|| true", r.stderr)
                self.assertEqual(self.last_stat()["result"], "block")

    def test_http_code_tested_by_inequality_blocks(self):
        """Without the `|| echo`, the inequality half of the trap still bites:
        « not the known failure value » is not « a success value »."""
        r = run_hook(self.write_payload(INEQUALITY_ONLY), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("http-code-tested-by-inequality", r.stderr)
        self.assertIn('case "$CODE" in 2??|3??)', r.stderr)

    def test_edit_is_judged_against_the_file_on_disk(self):
        """An Edit only carries a fragment: the `set -e` + `pipefail` context
        comes from the TARGET file. Same fragment, two contexts, two verdicts."""
        fragment = 'PEND=$(grep -rl pattern /var/lib/app/queue | wc -l)\n'
        target = self.dir / "wrapper.sh"

        target.write_text(HEADER + "echo start\n", encoding="utf-8")
        r = run_hook({"tool_name": "Edit",
                      "tool_input": {"file_path": str(target),
                                     "old_string": "echo start",
                                     "new_string": fragment}}, self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("bare-grep-wc-under-pipefail", r.stderr)

        target.write_text("#!/bin/bash\necho start\n", encoding="utf-8")
        r = run_hook({"tool_name": "Edit",
                      "tool_input": {"file_path": str(target),
                                     "old_string": "echo start",
                                     "new_string": fragment}}, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_multiedit_fragments_are_judged(self):
        """MultiEdit: every new_string is inspected, not just the first."""
        target = self.dir / "wrapper.sh"
        target.write_text(HEADER + "echo a\necho b\n", encoding="utf-8")
        r = run_hook({"tool_name": "MultiEdit",
                      "tool_input": {"file_path": str(target), "edits": [
                          {"old_string": "echo a", "new_string": "echo ok"},
                          {"old_string": "echo b",
                           "new_string": 'N=$(grep -rl x /opt/app | wc -l)'}]}},
                     self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("bare-grep-wc-under-pipefail", r.stderr)

    # ─── 3. fail-open ──────────────────────────────────────────────────────
    def test_fail_open_on_garbage_stdin(self):
        """Unreadable input: never block blindly, and never crash."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertFalse(self.stats.exists())

    def test_fail_open_on_malformed_payload(self):
        """tool_input of the wrong type would raise inside the hook: the
        top-level guard turns it into a plain allow (exit 0, no stderr)."""
        r = run_hook({"tool_name": "Write", "tool_input": "not-a-dict"}, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")

    # ─── 4. edge cases ─────────────────────────────────────────────────────
    def test_edge_comment_explaining_the_trap_does_not_fire(self):
        """REAL false positive caught by feeding the day's actual files: the
        FIXED watchdog documents the trap in its header and the pattern matched
        the prose. A gate that punishes explaining a trap pushes people to stop
        documenting it."""
        r = run_hook(self.write_payload(COMMENTED_FIX), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_real_code_still_judged_next_to_comments(self):
        """Control on the fix above: stripping comments must not blind the gate
        to the faulty CODE sitting next to them."""
        src = HEADER + "# a comment explaining something\n" + \
            WATCHDOG_V1.split("\n", 2)[2]
        r = run_hook(self.write_payload(src), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("curl-http-code-concatenated", r.stderr)

    def test_edge_non_shell_file_is_skipped(self):
        """A .py holding those strings is not shell: skipped, never judged."""
        payload = self.write_payload(
            "print('curl %{http_code} || echo 000')\n", name="tool.py")
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-not-shell")

    def test_edge_shebang_without_extension_is_recognized(self):
        """No .sh extension: the shebang alone puts the file in scope."""
        r = run_hook(self.write_payload(WATCHDOG_V1, name="deploy"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("curl-http-code-concatenated", r.stderr)

    def test_edge_empty_write_is_skipped(self):
        """Nothing written = nothing to judge, but the execution is logged."""
        r = run_hook(self.write_payload(""), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-nothing-written")

    # ─── 5. THE KILLER TEST ────────────────────────────────────────────────
    def test_neutralized_patterns_go_blind(self):
        """Disarm the three patterns: the REAL shapes must become invisible.

        This is the only proof that the suite above guards something: a gate
        whose patterns no longer match anything would sail through every
        "silent on healthy code" test unnoticed."""
        sources = [WATCHDOG_V1, INEQUALITY_ONLY, CUTOVER_WRAPPER_V1,
                   FINAL_VERIFICATION]
        probe = subprocess.run(
            [sys.executable, "-c", KILL_PROBE, str(HOOK), json.dumps(sources)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        blind = json.loads(probe.stdout)
        self.assertEqual(blind, [None] * len(sources),
                         "the disarmed gate STILL detects something: the "
                         "patterns under test are not the ones doing the work")
        # and it bites again with the patterns in place
        r = run_hook(self.write_payload(CUTOVER_WRAPPER_V1), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
