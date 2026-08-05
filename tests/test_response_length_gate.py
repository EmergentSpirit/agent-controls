#!/usr/bin/env python3
"""Tests for hooks/response-length-gate.py. Zero network: the hook runs as a
subprocess with the JSON payload on stdin, exactly like under the real harness;
gate-stats isolated in a tempdir.

Ported from the internal shell suite, whose founding constraint stays: this
suite MUST fail if the gate is unplugged (file missing, or gate letting
everything through). Otherwise it is a dead gate that lies."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "response-length-gate.py"


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for k in ("HARNESS_RESPONSE_LENGTH_GATE_DISABLE", "HARNESS_MAX_RESPONSE_WORDS"):
        env.pop(k, None)   # isolate from the caller's session
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


def words_payload(n: int, **extra) -> str:
    d = {"assistant_text": " ".join(["word"] * n)}
    d.update(extra)
    return json.dumps(d)


class TestResponseLengthGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}

    def tearDown(self):
        self.tmp.cleanup()

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def test_short_answer_passes(self):
        """A normal short answer goes through untouched and is journalled."""
        r = run_hook(words_payload(12), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        line = self.last_stat()
        self.assertEqual(line["result"], "pass")
        self.assertEqual(line["words"], 12)

    def test_long_answer_blocks_with_message(self):
        """The real production case (900 words): blocked, exit 2, actionable
        stderr, stat logged with the count and the ceiling."""
        r = run_hook(words_payload(900), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (response-length gate): 900 words of prose", r.stderr)
        self.assertIn("ceiling 350", r.stderr)
        self.assertIn("hand back its", r.stderr)
        self.assertIn("HARNESS_RESPONSE_LENGTH_GATE_DISABLE=1", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["words"], 900)
        self.assertEqual(line["ceiling"], 350)

    def test_ceiling_boundary_is_inclusive(self):
        """350 exactly passes, 351 blocks: the boundary is not fuzzy."""
        r = run_hook(words_payload(350), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")
        r = run_hook(words_payload(351), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_code_tables_and_urls_are_free(self):
        """Evidence is not chatter: fenced code, markdown tables, inline code
        and URLs do not count toward the ceiling."""
        code = "```\n" + "\n".join("command output line %d" % i
                                   for i in range(300)) + "\n```"
        table = "\n".join("| column alpha | column beta | column gamma |"
                          for _ in range(200))
        urls = " ".join("https://example.invalid/very/long/path/%d" % i
                        for i in range(200))
        inline = " ".join("`token_%d`" % i for i in range(200))
        for label, body in (("code", code), ("table", table),
                            ("urls", urls), ("inline", inline)):
            with self.subTest(kind=label):
                payload = json.dumps({"assistant_text": "Short verdict.\n" + body})
                r = run_hook(payload, self.env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_loop_guard_lets_a_retry_through(self):
        """stop_hook_active means the turn is already a Stop-hook retry:
        blocking in a loop would freeze the pane."""
        r = run_hook(words_payload(900, stop_hook_active=True), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-loop-guard")

    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly; the fail-open is logged."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_nothing_to_measure_is_a_skip(self):
        """Empty text, missing transcript, unreadable transcript: nothing to
        measure, so nothing to block."""
        for label, payload in (
                ("empty-text", json.dumps({"assistant_text": ""})),
                ("missing-transcript",
                 json.dumps({"transcript_path": "/does/not/exist/transcript.jsonl"})),
                ("no-field", json.dumps({})),
        ):
            with self.subTest(case=label):
                r = run_hook(payload, self.env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "skip-no-text")

    def test_blocks_through_the_transcript_path(self):
        """The runtime path: no inline text, the words are read from the last
        assistant event of the JSONL transcript."""
        transcript = Path(self.tmp.name) / "transcript.jsonl"
        with transcript.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n")
            f.write(json.dumps({"message": {"role": "assistant", "content": [
                {"type": "text", "text": " ".join(["word"] * 900)}]}}) + "\n")
        r = run_hook(json.dumps({"transcript_path": str(transcript)}), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("900 words of prose", r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_transcript_reads_the_LAST_assistant_message(self):
        """An old long answer must not block a new short one."""
        transcript = Path(self.tmp.name) / "transcript2.jsonl"
        with transcript.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"message": {"role": "assistant", "content": [
                {"type": "text", "text": " ".join(["word"] * 900)}]}}) + "\n")
            f.write(json.dumps({"message": {"role": "user", "content": "shorter"}}) + "\n")
            f.write(json.dumps({"message": {"role": "assistant",
                                            "content": "Done. Here is the path."}}) + "\n")
        r = run_hook(json.dumps({"transcript_path": str(transcript)}), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_ceiling_is_configurable(self):
        """HARNESS_MAX_RESPONSE_WORDS retargets the gate; garbage values fall
        back to the default instead of disarming it."""
        env = dict(self.env, HARNESS_MAX_RESPONSE_WORDS="100")
        r = run_hook(words_payload(300), env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("ceiling 100", r.stderr)
        for bad in ("0", "-5", "lots"):
            with self.subTest(value=bad):
                env = dict(self.env, HARNESS_MAX_RESPONSE_WORDS=bad)
                r = run_hook(words_payload(400), env)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("ceiling 350", r.stderr)

    def test_kill_switch_disarms_for_the_session(self):
        """The kill-switch is deliberate and never silent: logged skip-disabled."""
        env = dict(self.env, HARNESS_RESPONSE_LENGTH_GATE_DISABLE="1")
        r = run_hook(words_payload(900), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-disabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
