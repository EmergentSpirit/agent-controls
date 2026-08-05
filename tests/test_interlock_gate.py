#!/usr/bin/env python3
"""Tests for hooks/interlock-gate.py and its companion hooks/interlock-stamp.py.

Zero network: both scripts run as subprocesses with the JSON payload on stdin,
exactly like under the real harness. Gate-stats AND the state directory are
isolated in a tempdir -- never the real journal, never the real state.

TRAP, learned the hard way: a tempdir usually lives under the system temp dir,
which this gate exempts by default. A suite that leaves the default in place
turns every block case green for the wrong reason. So the exempt scratch
directory is pinned to ONE sub-directory of the tempdir, and the default
exemption gets its own dedicated test.

A missing or corrupt state file is NOT a fail-open: above the threshold with no
proof is a block, otherwise corrupting the state would pick the lock. The
fail-open covers hook errors only (unreadable stdin).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "interlock-gate.py"
STAMP_CLI = HERE.parent / "hooks" / "interlock-stamp.py"
EXAMPLE_SETTINGS = HERE.parent / "launchers" / "settings.example.json"

LEAKY_ENV = ("HARNESS_STATE_DIR", "HARNESS_GATE_STATS",
             "HARNESS_INTERLOCK_SCRATCH_DIRS")
SESSION = "test-session"


def run(script: Path, args: list[str], stdin_text: str,
        env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for leak in LEAKY_ENV:
        env.pop(leak, None)  # isolate from the caller's own harness
    env.update(env_extra)
    return subprocess.run([sys.executable, str(script)] + args,
                          input=stdin_text, capture_output=True, text=True,
                          env=env, timeout=30)


class TestInterlockGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.proj = root / "proj"          # judged: this is the "code tree"
        self.scratch = root / "scratch"    # exempt: artifacts and throwaways
        for d in (self.proj, self.scratch):
            d.mkdir(parents=True)
        self.stats = root / "stats.jsonl"
        self.state_dir = root / "state"
        self.env = {
            "HARNESS_GATE_STATS": str(self.stats),
            "HARNESS_STATE_DIR": str(self.state_dir),
            "HARNESS_INTERLOCK_SCRATCH_DIRS": str(self.scratch),
        }

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def run_hook(self, payload, env_extra: dict = None):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return run(HOOK, [], raw, env_extra if env_extra is not None else self.env)

    def run_stamp(self, args: list[str], env_extra: dict = None):
        return run(STAMP_CLI, args, "",
                   env_extra if env_extra is not None else self.env)

    def write_payload(self, path, lines: int, session: str = SESSION) -> dict:
        return {"tool_name": "Write", "session_id": session,
                "tool_input": {"file_path": str(path),
                               "content": "# line\n" * lines}}

    def stats_text(self) -> str:
        try:
            return self.stats.read_text(encoding="utf-8")
        except OSError:
            return ""

    def last_stat(self) -> dict:
        return json.loads(self.stats_text().strip().splitlines()[-1])

    def state_file(self, session: str = SESSION) -> Path:
        return self.state_dir / f"interlock-{session}.json"

    def steps_artifact(self, name: str = "steps.json") -> str:
        p = self.scratch / name
        p.write_text(json.dumps([{"step": 1, "action": "write the module",
                                  "pass_fail_criteria": "tests green",
                                  "evidence_expected": "unittest output"}]),
                     encoding="utf-8")
        return str(p)

    def ack_artifact(self, name: str = "ack.md") -> str:
        p = self.scratch / name
        p.write_text("mission: port the gate\nfiles: a.py, b.py\n"
                     "risk: thresholds\nplan: 2 steps\nverify: unittest\n",
                     encoding="utf-8")
        return str(p)

    # -- I1: above the threshold, no proof -------------------------------
    def test_i1_above_threshold_without_a_step_blocks(self):
        """The founding gesture: exit 2, both doors spelled out, journaled."""
        r = self.run_hook(self.write_payload(self.proj / "big.py", 60))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (interlock gate)", r.stderr)
        self.assertIn("interlock-stamp.py", r.stderr)
        self.assertIn("--decomp", r.stderr)
        self.assertIn("--ack", r.stderr)
        self.assertIn("new file Write of 61 lines", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["hook"], "interlock")
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["path"], str(self.proj / "big.py"))

    # -- I2 / I3: the two doors ------------------------------------------
    def test_i2_fresh_decomposition_stamp_opens_the_lock(self):
        s = self.run_stamp(["--session", SESSION, "--decomp",
                            self.steps_artifact()])
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertIn("stamped", s.stdout)
        r = self.run_hook(self.write_payload(self.proj / "big.py", 60))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-recent-step")

    def test_i3_fresh_ack_stamp_opens_the_same_lock(self):
        """Second door, same lock: the mission ack is worth a decomposition."""
        s = self.run_stamp(["--session", SESSION, "--ack", self.ack_artifact()])
        self.assertEqual(s.returncode, 0, s.stderr)
        r = self.run_hook(self.write_payload(self.proj / "big.py", 60))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-recent-step")

    # -- I4: the window closes -------------------------------------------
    def test_i4_stale_stamp_blocks_again(self):
        """Past the 30-minute window the proof is worthless."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text(
            json.dumps({"recent_edits": [],
                        "decomp_done_ts": time.time() - 3600}),
            encoding="utf-8")
        r = self.run_hook(self.write_payload(self.proj / "big.py", 60))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    # -- I5: no false positives (the most important test) ----------------
    def test_i5_nominal_small_edits_and_exemptions_pass(self):
        cases = [(self.proj / "small.py", 20, "pass"),
                 (self.proj / "notes.md", 80, "skip-doc"),
                 (self.proj / "handoff.txt", 80, "skip-doc"),
                 (self.scratch / "probe.py", 80, "skip-scratch-path")]
        for path, lines, expected in cases:
            with self.subTest(path=str(path)):
                r = self.run_hook(self.write_payload(path, lines))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], expected)

    def test_i5_default_scratch_dirs_exempt_the_system_temp_dir(self):
        """With no override, the system temp dir is scratch: a probe file
        written there is never a build to decompose."""
        env = {k: v for k, v in self.env.items()
               if k != "HARNESS_INTERLOCK_SCRATCH_DIRS"}
        r = self.run_hook(self.write_payload(self.proj / "probe.py", 80), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-scratch-path")

    def test_i5_multi_file_counts_code_only(self):
        """Three distinct CODE files inside the window blocks; the docs written
        in between do not count toward it."""
        for name in ("a.py", "b.py"):
            r = self.run_hook(self.write_payload(self.proj / name, 5))
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")
            self.assertEqual(self.last_stat()["result"], "pass")
        for name in ("x.md", "y.md", "z.md"):
            self.run_hook(self.write_payload(self.proj / name, 5))
        r = self.run_hook(self.write_payload(self.proj / "c.py", 5))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("3 distinct code files", r.stderr)

    def test_ast_diff_two_new_structural_nodes_blocks(self):
        """An Edit that is short in lines but adds two functions is a build."""
        target = self.proj / "mod.py"
        target.write_text("x = 1\n", encoding="utf-8")
        payload = {"tool_name": "Edit", "session_id": SESSION,
                   "tool_input": {"file_path": str(target),
                                  "old_string": "x = 1",
                                  "new_string": "def a():\n    return 1\n\n"
                                                "def b():\n    return 2\n"}}
        r = self.run_hook(payload)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("2 new structural nodes", r.stderr)

    # -- I6: fail-open, and what is NOT a fail-open ----------------------
    def test_i6_fail_open_on_unreadable_stdin(self):
        for raw in ("{ not json at all", ""):
            with self.subTest(raw=raw):
                r = self.run_hook(raw)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_i6_corrupt_state_below_threshold_passes_without_crashing(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text("{ corrupt", encoding="utf-8")
        r = self.run_hook(self.write_payload(self.proj / "small.py", 10))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_i6_corrupt_state_above_threshold_still_blocks(self):
        """Corrupting the state must NOT pick the lock: no proof is no proof."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file().write_text("{ corrupt", encoding="utf-8")
        r = self.run_hook(self.write_payload(self.proj / "big.py", 60))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_out_of_scope_tool_is_journaled_and_allowed(self):
        payload = {"tool_name": "Read", "session_id": SESSION,
                   "tool_input": {"file_path": str(self.proj / "big.py")}}
        r = self.run_hook(payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-out-of-scope")

    # -- I7: the gate is actually WIRED ----------------------------------
    @unittest.skipUnless(EXAMPLE_SETTINGS.is_file(),
                         "launchers/settings.example.json not shipped yet")
    def test_i7_wired_in_the_example_settings(self):
        """A gate nobody wires is a gate that lies. The shipped example must
        call it on Write|Edit|MultiEdit."""
        text = EXAMPLE_SETTINGS.read_text(encoding="utf-8")
        self.assertIn("interlock-gate.py", text)
        wired = False
        cfg = json.loads(text)
        for block in cfg.get("hooks", {}).get("PreToolUse", []):
            matcher = set((block.get("matcher") or "").split("|"))
            for h in block.get("hooks", []):
                if ("interlock-gate.py" in (h.get("command") or "")
                        and {"Write", "Edit", "MultiEdit"} <= matcher):
                    wired = True
        self.assertTrue(wired, "matcher must cover Write|Edit|MultiEdit")

    # -- I8: the founding principle --------------------------------------
    def test_i8_no_activation_and_no_kill_switch_environment_variable(self):
        """A gate armed by an environment variable nobody ever exports is a
        dead gate that lies. This one has neither an activation variable nor a
        kill-switch, and that is load-bearing: do not add one."""
        for script in (HOOK, STAMP_CLI):
            with self.subTest(script=script.name):
                src = script.read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(r"[A-Z][A-Z0-9_]*_(BLOCK|DISABLE)\b", src),
                    "an activation or kill-switch variable crept in")
        env_reads = set(re.findall(r"os\.environ\.get\(\"([A-Z_]+)\"",
                                   HOOK.read_text(encoding="utf-8")))
        self.assertEqual(env_reads, {"HARNESS_INTERLOCK_SCRATCH_DIRS"},
                         "the gate reads exactly one environment variable, "
                         "and it is not an on/off switch")

    # -- the companion CLI ------------------------------------------------
    def test_stamp_cli_refuses_hollow_artifacts(self):
        empty = self.scratch / "empty.json"
        empty.write_text("[]", encoding="utf-8")
        thin = self.scratch / "thin.md"
        thin.write_text("ok\n", encoding="utf-8")
        broken = self.scratch / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        cases = [["--session", SESSION, "--decomp", str(empty)],
                 ["--session", SESSION, "--decomp", str(broken)],
                 ["--session", SESSION, "--ack", str(thin)],
                 ["--session", SESSION, "--ack", str(self.scratch / "nope")]]
        for args in cases:
            with self.subTest(args=args):
                s = self.run_stamp(args)
                self.assertEqual(s.returncode, 1, s.stdout)
                self.assertIn("REFUSED", s.stderr)
                self.assertFalse(self.state_file().exists(),
                                 "a refused stamp writes no state")
                self.assertEqual(self.last_stat()["event"], "stamp-refused")

    def test_stamp_cli_merges_and_journals(self):
        """The stamp keeps the edit trail and the other door, and the gesture
        is journaled as observe."""
        self.run_hook(self.write_payload(self.proj / "a.py", 5))
        before = json.loads(self.state_file().read_text(encoding="utf-8"))
        self.assertEqual(len(before["recent_edits"]), 1)
        s = self.run_stamp(["--session", SESSION, "--ack", self.ack_artifact()])
        self.assertEqual(s.returncode, 0, s.stderr)
        after = json.loads(self.state_file().read_text(encoding="utf-8"))
        self.assertEqual(after["recent_edits"], before["recent_edits"])
        self.assertGreater(after["ack_done_ts"], 0)
        self.assertTrue(after["ack_done_file"].endswith("ack.md"))
        line = self.last_stat()
        self.assertEqual(line["hook"], "interlock")
        self.assertEqual(line["result"], "observe")
        self.assertEqual(line["event"], "stamp-posted")
        self.assertEqual(line["door"], "ack")


if __name__ == "__main__":
    unittest.main(verbosity=2)
