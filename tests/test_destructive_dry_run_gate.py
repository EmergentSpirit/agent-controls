#!/usr/bin/env python3
"""Tests for hooks/destructive-dry-run-gate.py. Zero network: the hook runs as
a subprocess with the JSON payload on stdin, exactly like under the real
harness; gate-stats and the operator script directory isolated in a tempdir."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "destructive-dry-run-gate.py"

GUARDED_HEADER = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "DRYRUN=1\n"
    "for a in \"$@\"; do case \"$a\" in --go) DRYRUN=0 ;; esac; done\n"
)


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("HARNESS_DESTRUCTIVE_DRY_RUN_GATE_DISABLE", None)  # isolate from caller
    env.pop("HARNESS_OPERATOR_SCRIPT_DIRS", None)
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


class TestDestructiveDryRunGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scripts = Path(self.tmp.name) / "operator-scripts"
        (self.scripts / "done").mkdir(parents=True)
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats),
                    "HARNESS_OPERATOR_SCRIPT_DIRS": str(self.scripts)}

    def tearDown(self):
        self.tmp.cleanup()

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def payload(self, name: str, content: str, tool: str = "Write") -> str:
        """Write payload targeting a file inside the operator script tree."""
        path = name if os.path.isabs(name) else str(self.scripts / name)
        return json.dumps({"tool_name": tool,
                           "tool_input": {"file_path": path, "content": content}})

    # --- nominal pass ----------------------------------------------------

    def test_nominal_harmless_script_passes(self):
        """A script that destroys no disk needs no safety net at all."""
        body = ("#!/usr/bin/env bash\nset -euo pipefail\n"
                "rsync -a \"$SRC/\" \"$DST/\"\ndf -h\n")
        r = run_hook(self.payload("backup.sh", body), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        line = self.last_stat()
        self.assertEqual(line["result"], "pass")
        self.assertNotIn("guarded", line)

    def test_guarded_destructive_script_passes(self):
        """Dry run ON BY DEFAULT plus an explicit gesture: the net is there,
        the wipe is allowed through and journaled as an examined pass."""
        body = GUARDED_HEADER + (
            "[ \"$DRYRUN\" -eq 1 ] && { echo 'NOTHING TOUCHED'; exit 0; }\n"
            "sudo wipefs -a /dev/sdb\nsudo mkfs.ext4 /dev/sdb1\n")
        r = run_hook(self.payload("done/format-key.sh", body), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        line = self.last_stat()
        self.assertEqual(line["result"], "pass")
        self.assertTrue(line["guarded"])
        self.assertEqual(line["n"], 2)

    # --- block with a verified message -----------------------------------

    def test_unguarded_wipe_blocks_with_message(self):
        """No net at all: blocked, exit 2, both missing pieces named, every
        offending command echoed back, stat logged."""
        body = ("#!/usr/bin/env bash\nset -euo pipefail\n"
                "sudo wipefs -a /dev/sdb\nsudo mkfs.ext4 /dev/sdb1\n")
        r = run_hook(self.payload("format-key.sh", body), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (destructive-dry-run gate)", r.stderr)
        self.assertIn("sudo wipefs", r.stderr)
        self.assertIn("sudo mkfs.ext4", r.stderr)
        self.assertIn("a dry run ON BY DEFAULT", r.stderr)
        self.assertIn("an explicit gesture to destroy", r.stderr)
        self.assertIn(" AND ", r.stderr)
        self.assertIn("HARNESS_DESTRUCTIVE_DRY_RUN_GATE_DISABLE=1", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertEqual(line["n"], 2)
        self.assertTrue(line["path"].endswith("format-key.sh"))

    def test_half_a_net_still_blocks_and_names_only_what_is_missing(self):
        """`--go` parsed but DRYRUN defaulting to 0: the destruction is still
        the default path. Only the missing half is reported."""
        body = ("#!/usr/bin/env bash\nDRYRUN=0\n"
                "for a in \"$@\"; do case \"$a\" in --go) DRYRUN=0 ;; esac; done\n"
                "shred -n1 /dev/sdb\n")
        r = run_hook(self.payload("shred-disk.sh", body), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("a dry run ON BY DEFAULT", r.stderr)
        self.assertNotIn("an explicit gesture to destroy", r.stderr)
        self.assertNotIn(" AND ", r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_edit_payload_on_new_string_blocks_too(self):
        """An Edit slipping the wipe in through `new_string` is the same
        gesture as a Write: the gate reads both fields."""
        path = str(self.scripts / "patch-me.sh")
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": path, "old_string": "echo hi",
                           "new_string": "blkdiscard /dev/nvme0n1\n"}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("blkdiscard", r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    # --- fail-open --------------------------------------------------------

    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly; the fail-open is logged."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_kill_switch_disarms_for_the_session(self):
        """The kill-switch lets even a bare unguarded wipe through, traced."""
        env = dict(self.env, HARNESS_DESTRUCTIVE_DRY_RUN_GATE_DISABLE="1")
        r = run_hook(self.payload("format-key.sh", "wipefs -a /dev/sdb\n"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.last_stat()["result"], "skip-disabled")

    # --- edge cases -------------------------------------------------------

    def test_edge_read_only_forms_and_comments_pass(self):
        """Zero false positives by construction: listing forms are not wipes,
        and a destructive command QUOTED in a comment was never run."""
        cases = {
            "inspect.sh": "wipefs /dev/sdb\nsfdisk --dump /dev/sdb\n"
                          "sgdisk -p /dev/sdb\n",
            "documented.sh": "# careful: sudo wipefs -a /dev/sdb erases it all\n"
                             "#   mkfs.ext4 /dev/sdb1\n"
                             "echo 'read the comments above'\n",
        }
        for name, body in cases.items():
            with self.subTest(script=name):
                r = run_hook(self.payload(name, body), self.env)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_out_of_perimeter_and_non_shell_are_skipped(self):
        """Scope is the operator's own script tree, and only shell in it: a
        wipe written anywhere else is out of this gate's mandate, and every
        skip is still journaled (a silent gate cannot be proven alive)."""
        outside = str(Path(self.tmp.name) / "elsewhere" / "format-key.sh")
        cases = [
            (self.payload(outside, "wipefs -a /dev/sdb\n"), "skip-out-of-perimeter"),
            (self.payload("notes.md", "wipefs -a /dev/sdb\n"), "skip-not-shell"),
            (self.payload("empty.sh", ""), "skip-not-shell"),
            (json.dumps({"tool_name": "Bash",
                         "tool_input": {"command": "wipefs -a /dev/sdb"}}),
             "skip-out-of-scope"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                r = run_hook(payload, self.env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertEqual(self.last_stat()["result"], expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
