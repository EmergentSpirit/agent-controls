#!/usr/bin/env python3
"""Tests for hooks/settings-go-gate.py. Fails if the gate is unwired or mute.

Zero network: the hook runs as a subprocess with the JSON payload on stdin,
exactly like under the real harness. Gate-stats, the protected perimeter AND
the GO stamp are isolated in a tempdir -- never the real journal, never the
real settings file, and never a leftover real stamp that would silently turn
every BLOCK case green."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "settings-go-gate.py"


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for leak in ("HARNESS_PROTECTED_SETTINGS", "HARNESS_SETTINGS_STAMP",
                 "HARNESS_SETTINGS_GO_GATE_DISABLE", "HARNESS_STATE_DIR",
                 "HARNESS_GATE_STATS"):
        env.pop(leak, None)  # isolate from the caller's own harness
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


class TestSettingsGoGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.conf = root / "agent-home" / ".claude"      # file entry
        self.extra = root / "agent-home" / "conf-extra"  # directory entry
        self.conf.mkdir(parents=True)
        self.extra.mkdir(parents=True)
        self.stats = root / "stats.jsonl"
        self.stamp = root / "settings-go.stamp"
        self.env = {
            "HARNESS_GATE_STATS": str(self.stats),
            "HARNESS_STATE_DIR": str(root / "state"),
            "HARNESS_PROTECTED_SETTINGS": f"{self.conf}/settings.json:{self.extra}/",
            "HARNESS_SETTINGS_STAMP": str(self.stamp),  # deliberately absent
        }
        self.cwd = str(root)

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, path: str, tool: str = "Write") -> str:
        return json.dumps({"tool_name": tool,
                           "tool_input": {"file_path": path,
                                          "content": "{}\n"},
                           "cwd": self.cwd})

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def touch_stamp(self, age_s: float = 0.0) -> None:
        self.stamp.write_text("go: wire the end-of-mission hook\n",
                              encoding="utf-8")
        if age_s:
            when = os.path.getmtime(self.stamp) - age_s
            os.utime(self.stamp, (when, when))

    # -- nominal --------------------------------------------------------
    def test_nominal_writes_pass(self):
        """Edits that touch no protected settings file go through, journaled."""
        for path in (f"{self.cwd}/notes.md",
                     f"{self.cwd}/project/config.json",
                     f"{self.conf}/hooks/guard.py",
                     f"{self.conf}/agents/reviewer.md"):
            with self.subTest(path=path):
                r = run_hook(self.payload(path), self.env)
                self.assertEqual(r.returncode, 0, f"{path!r}: {r.stderr}")
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    # -- block ----------------------------------------------------------
    def test_protected_settings_blocks_with_message(self):
        """The founding gesture: exit 2, actionable stderr, stat journaled."""
        target = f"{self.conf}/settings.json"
        r = run_hook(self.payload(target), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (settings-go gate)", r.stderr)
        self.assertIn("EVERY pane", r.stderr)
        self.assertIn("TELL the operator", r.stderr)
        self.assertIn("HARNESS_SETTINGS_STAMP", r.stderr)
        self.assertIn("HARNESS_SETTINGS_GO_GATE_DISABLE=1", r.stderr)
        self.assertIn(target, r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["path"], target)

    def test_every_settings_form_and_tool_blocks(self):
        """The whole settings family, through any of the three edit tools:
        the siblings are read at boot exactly like the main file."""
        cases = [
            (f"{self.conf}/settings.json", "Write"),
            (f"{self.conf}/settings.local.json", "Edit"),
            (f"{self.conf}/reviewer-settings.json", "MultiEdit"),
            (f"{self.extra}/settings.json", "Edit"),
            (f"{self.extra}/settings.local.json", "Write"),
        ]
        for path, tool in cases:
            with self.subTest(path=path, tool=tool):
                r = run_hook(self.payload(path, tool), self.env)
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
    def test_edge_same_name_outside_perimeter_passes(self):
        """A file NAMED settings.json outside the protected perimeter is an
        ordinary file: blocking it would cost legitimate work for nothing."""
        for path in (f"{self.cwd}/some-app/settings.json",
                     f"{self.cwd}/settings.local.json"):
            with self.subTest(path=path):
                r = run_hook(self.payload(path), self.env)
                self.assertEqual(r.returncode, 0, f"{path!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_relative_path_resolves_against_cwd(self):
        """The same file reached by a shorter name is the same file: a
        relative path is resolved against the session cwd before matching."""
        payload = json.dumps({"tool_name": "Write",
                              "tool_input": {"file_path": ".claude/settings.json",
                                             "content": "{}\n"},
                              "cwd": str(self.conf.parent)})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_fresh_stamp_turns_the_block_into_a_traced_allow(self):
        """Sanctioned edit: a stamp younger than the 30-minute window lets the
        gesture through, journaled as skip-stamp."""
        self.touch_stamp()
        r = run_hook(self.payload(f"{self.conf}/settings.json"), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "skip-stamp")
        self.assertEqual(line["path"], f"{self.conf}/settings.json")

    def test_stale_stamp_still_blocks(self):
        """Past the 30-minute window the stamp is worthless: block again."""
        self.touch_stamp(age_s=31 * 60)
        r = run_hook(self.payload(f"{self.conf}/settings.json"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_kill_switch_disarms_for_the_session(self):
        """The kill-switch lets even a real hit through, journaled as
        skip-disabled: routing around the gate stays visible."""
        env = dict(self.env, HARNESS_SETTINGS_GO_GATE_DISABLE="1")
        r = run_hook(self.payload(f"{self.conf}/settings.json"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-disabled")

    def test_out_of_scope_tool_is_journaled_and_allowed(self):
        """Other tools are out of scope, and the pass-through still logs."""
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command":
                                             f"cat {self.conf}/settings.json"},
                              "cwd": self.cwd})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-out-of-scope")

    def test_default_perimeter_when_env_is_unset(self):
        """With no HARNESS_PROTECTED_SETTINGS the default perimeter applies:
        ~/.claude/settings.json, and nothing else in the tempdir."""
        env = {k: v for k, v in self.env.items()
               if k != "HARNESS_PROTECTED_SETTINGS"}
        env["HOME"] = str(Path(self.tmp.name) / "agent-home")
        r = run_hook(self.payload(f"{self.conf}/settings.json"), env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")
        r = run_hook(self.payload(f"{self.extra}/settings.json"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")


if __name__ == "__main__":
    unittest.main(verbosity=2)
