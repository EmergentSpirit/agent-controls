#!/usr/bin/env python3
"""Tests for hooks/hook-retire-gate.py. Fails if the gate is unwired or mute.

Zero network: the hook runs as a subprocess with the JSON payload on stdin,
exactly like under the real harness. Gate-stats AND the settings-GO stamp are
isolated in a tempdir — never the real journal, and never a leftover real
stamp that would silently turn every BLOCK case green."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "hook-retire-gate.py"


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for leak in ("HARNESS_HOOK_DIRS", "HARNESS_SETTINGS_STAMP",
                 "HARNESS_STATE_DIR", "HARNESS_GATE_STATS"):
        env.pop(leak, None)  # isolate from the caller's own harness
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


class TestHookRetireGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.hooks_a = root / "agent-a" / "hooks"
        self.hooks_b = root / "agent-b" / "hooks"
        (self.hooks_a / "tests").mkdir(parents=True)
        self.hooks_b.mkdir(parents=True)
        self.stats = root / "stats.jsonl"
        self.env = {
            "HARNESS_GATE_STATS": str(self.stats),
            "HARNESS_STATE_DIR": str(root / "state"),
            "HARNESS_HOOK_DIRS": f"{self.hooks_a}:{self.hooks_b}",
            "HARNESS_SETTINGS_STAMP": str(root / "no-such-stamp"),
        }
        self.cwd = str(root)

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, cmd: str) -> str:
        return json.dumps({"tool_name": "Bash",
                           "tool_input": {"command": cmd}, "cwd": self.cwd})

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    # ── nominal ──────────────────────────────────────────────────────────
    def test_nominal_commands_pass(self):
        """Gestures that touch no live hook go through and are journaled."""
        for cmd in (f"ls -la {self.hooks_a}/",
                    "rm /tmp/some-scratch-file.py",
                    f"cp {self.hooks_a}/guard.py /tmp/guard.bak",
                    f"mv /tmp/new-hook.py {self.hooks_a}/dropped.py"):
            with self.subTest(cmd=cmd):
                r = run_hook(self.payload(cmd), self.env)
                self.assertEqual(r.returncode, 0, f"{cmd!r}: {r.stderr}")
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    # ── block ────────────────────────────────────────────────────────────
    def test_mv_of_a_live_hook_blocks_with_message(self):
        """The founding gesture: exit 2, actionable stderr, stat journaled."""
        target = f"{self.hooks_a}/guard.py"
        r = run_hook(self.payload(f"mv {target} /tmp/x.py"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (hook-retire gate)", r.stderr)
        self.assertIn("Settings are read AT BOOT", r.stderr)
        self.assertIn("cp to a dated .bak", r.stderr)
        self.assertIn("HARNESS_SETTINGS_STAMP", r.stderr)
        self.assertIn(target, r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["targets"], [target])

    def test_every_retire_form_blocks(self):
        """git mv, rm, unlink, and any of them after a shell separator or a
        command prefix — the same retirement, blocked the same way."""
        for cmd in (f"git mv {self.hooks_b}/scan.py other.py",
                    f"rm {self.hooks_b}/pretool-guard.py",
                    f"unlink {self.hooks_a}/notify.py",
                    f"cd /tmp && rm {self.hooks_a}/notify.sh",
                    f"sudo rm -f {self.hooks_a}/guard.py"):
            with self.subTest(cmd=cmd):
                r = run_hook(self.payload(cmd), self.env)
                self.assertEqual(r.returncode, 2, f"{cmd!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["result"], "block")

    # ── fail-open ────────────────────────────────────────────────────────
    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly; the fail-open is journaled."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    # ── edge cases ───────────────────────────────────────────────────────
    def test_edge_backups_and_tests_stay_removable(self):
        """Cleaning up the .bak copies and moving test files must stay free,
        otherwise the safe path itself becomes a dead end."""
        for cmd in (f"rm {self.hooks_a}/end-of-mission.py.bak-20240115",
                    f"mv {self.hooks_a}/tests/test_x.py /tmp/",
                    f"rm {self.hooks_a}/test_helper.py"):
            with self.subTest(cmd=cmd):
                r = run_hook(self.payload(cmd), self.env)
                self.assertEqual(r.returncode, 0, f"{cmd!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_mv_destination_is_not_a_source(self):
        """Dropping a NEW hook into the directory is not retiring one: only
        the sources of an mv count, never its last argument."""
        r = run_hook(self.payload(f"mv -f /tmp/new.py {self.hooks_a}/new.py"),
                     self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_nested_subdirectory_is_not_top_level(self):
        """Only the TOP level of a hooks directory holds wired hooks."""
        r = run_hook(self.payload(f"rm {self.hooks_a}/data/cache.json"), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_fresh_stamp_turns_the_block_into_a_traced_allow(self):
        """Sanctioned cleanup: a stamp younger than the 30-minute window lets
        the gesture through, journaled as skip-stamp."""
        stamp = Path(self.tmp.name) / "settings-go.stamp"
        stamp.write_text("go\n", encoding="utf-8")
        env = dict(self.env, HARNESS_SETTINGS_STAMP=str(stamp))
        r = run_hook(self.payload(f"rm {self.hooks_a}/guard.py"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-stamp")

    def test_stale_stamp_still_blocks(self):
        """Past the 30-minute window the stamp is worthless: block again."""
        stamp = Path(self.tmp.name) / "settings-go.stamp"
        stamp.write_text("go\n", encoding="utf-8")
        old = os.path.getmtime(stamp) - 31 * 60
        os.utime(stamp, (old, old))
        env = dict(self.env, HARNESS_SETTINGS_STAMP=str(stamp))
        r = run_hook(self.payload(f"rm {self.hooks_a}/guard.py"), env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_non_bash_tool_ignored(self):
        """Other tools are out of scope: exit 0, allowed, and STILL journaled.
        A gate that stays silent on a path cannot be told apart from a
        gate that is dead, which is what the coverage check looks for."""
        payload = json.dumps({"tool_name": "Write",
                              "tool_input": {"file_path": f"{self.hooks_a}/x.py",
                                             "content": "rm -f hook"}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = json.loads(
            self.stats.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(line["hook"], "hook-retire")
        self.assertEqual(line["result"], "skip-not-bash")


if __name__ == "__main__":
    unittest.main(verbosity=2)
