#!/usr/bin/env python3
"""Tests for hooks/destructive-command-gate.py. Zero network: the hook runs as a
subprocess with the JSON payload on stdin, exactly like under the real harness;
gate-stats isolated in a tempdir.

Ported from the original shell acceptance suite, including its BITING test: the
same block payloads are replayed against a stub gate that only exits 0, and the
suite must then see NOTHING blocked. A green suite that stays green with the
gate unplugged proves nothing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "destructive-command-gate.py"

# Commands the gate MUST block with the default families (1, 2, 6).
MUST_BLOCK = [
    "sudo rm -rf /var",
    "sudo apt install nginx",
    "su - root",
    "pkexec systemctl restart app.service",
    "rm -rf /opt/project",
    'find . -name "*.log" -delete',
    "shred -u /etc/app/keyfile",
    "cp -a /var/backups/vault-2026.age $HOME/.secrets.env.age",
    "rm -f $HOME/.secrets.env.age",
    "echo pubkey > $HOME/.ssh/authorized_keys",
]

# Commands that MUST go through untouched.
MUST_PASS = [
    "git status",
    "ls -la",
    "grep -r sudo .",
    'echo "never run rm -rf on this box"',
    "rm -rf /tmp/scratch-x",
    "rm -f /tmp/x.json",
    "cat $HOME/.harness/gate-stats.jsonl",
    "systemctl --user start app.service",
    "sudo rm -rf /var [DESTRUCTIVE-AUTHORIZED reason=cleanup_of_test_fixture]",
]

ENV_KEYS = (
    "HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE",
    "HARNESS_DESTRUCTIVE_COMMAND_FAMILIES",
    "HARNESS_DESTRUCTIVE_COMMAND_SECRET_FILES",
    "HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS",
    "HARNESS_STATE_DIR",
    "HARNESS_GATE_STATS",
)


def run_hook(stdin_text: str, env_extra: dict,
             hook: Path = HOOK) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for leak in ENV_KEYS:  # isolate from whatever the caller's shell carries
        env.pop(leak, None)
    env.update(env_extra)
    return subprocess.run([sys.executable, str(hook)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


def bash_payload(cmd: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})


class TestDestructiveCommandGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}

    def tearDown(self):
        self.tmp.cleanup()

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    # ---- nominal ----------------------------------------------------------

    def test_nominal_commands_pass(self):
        """Reads, listings, /tmp housekeeping, a mention of a destructive verb
        and a disarmed family (5) all go through, and each one is journaled."""
        for cmd in MUST_PASS:
            with self.subTest(cmd=cmd):
                self.stats.unlink(missing_ok=True)
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 0, f"{cmd!r}: {r.stderr}")
                self.assertEqual(r.stderr, "")
                self.assertIn(self.last_stat()["result"], ("pass", "skip-authorized"))

    # ---- block ------------------------------------------------------------

    def test_privilege_escalation_blocks_with_message(self):
        """`sudo rm -rf /var`: exit 2, actionable stderr, one journal line."""
        r = run_hook(bash_payload("sudo rm -rf /var"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (destructive-command-gate)", r.stderr)
        self.assertIn("privilege escalation: sudo", r.stderr)
        self.assertIn("[DESTRUCTIVE-AUTHORIZED reason=", r.stderr)
        self.assertIn("HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE=1", r.stderr)
        line = self.last_stat()
        self.assertEqual(line["result"], "block")
        self.assertIn("sudo", line["pattern"])

    def test_all_armed_family_hits_block(self):
        """Every command of the founding acceptance list is blocked."""
        for cmd in MUST_BLOCK:
            with self.subTest(cmd=cmd):
                self.stats.unlink(missing_ok=True)
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 2, f"{cmd!r} was NOT blocked")
                self.assertEqual(self.last_stat()["result"], "block")

    def test_secret_bearing_files_report_their_family(self):
        """Family 6 names what it protects, so the message is diagnosable."""
        r = run_hook(bash_payload("rm -f $HOME/.secrets.env.age"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("secret-bearing file", r.stderr)
        r2 = run_hook(bash_payload("echo pubkey > $HOME/.ssh/authorized_keys"), self.env)
        self.assertEqual(r2.returncode, 2, r2.stderr)
        self.assertIn("redirection overwriting a secret-bearing file", r2.stderr)

    # ---- escape hatches ---------------------------------------------------

    def test_authorization_tag_lets_the_gesture_through(self):
        """The tag carries a mandatory reason; it is read on the RAW command."""
        cmd = "sudo rm -rf /var [DESTRUCTIVE-AUTHORIZED reason=cleanup_of_test_fixture]"
        r = run_hook(bash_payload(cmd), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-authorized")

    def test_tag_without_a_reason_still_blocks(self):
        """A bare tag is a reflex, not a decision: it does not authorize."""
        r = run_hook(bash_payload("sudo rm -rf /var [DESTRUCTIVE-AUTHORIZED]"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_kill_switch_disarms_for_the_session(self):
        env = dict(self.env, HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE="1")
        r = run_hook(bash_payload("sudo rm -rf /var"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-disabled")

    # ---- configurable perimeter -------------------------------------------

    def test_disarmed_families_block_once_armed(self):
        """Families 3/4/5 are written but off by default; the env arms them."""
        # Note the SQL case is written UNQUOTED: quoted literals are stripped
        # before analysis (that is what keeps `echo "... rm -rf ..."` from
        # firing), so `psql -c 'DROP TABLE x'` is a known blind spot of
        # family 4, documented in docs/gates/destructive-command-gate.md.
        cases = [("systemctl --user stop app.service", "service stop/disable"),
                 ("dd if=/dev/zero of=/dev/sdb bs=1M", "raw disk overwrite"),
                 ("echo DROP TABLE clients | psql app", "destructive DBMS statement")]
        for cmd, label in cases:
            with self.subTest(cmd=cmd):
                off = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(off.returncode, 0, f"{cmd!r} fired while disarmed")
                on = run_hook(bash_payload(cmd),
                              dict(self.env,
                                   HARNESS_DESTRUCTIVE_COMMAND_FAMILIES="1,2,3,4,5,6"))
                self.assertEqual(on.returncode, 2, f"{cmd!r} did not fire once armed")
                self.assertIn(label, on.stderr)

    def test_secret_file_markers_are_configurable(self):
        """The default marker list is an EXAMPLE: a site names its own vault."""
        env = dict(self.env, HARNESS_DESTRUCTIVE_COMMAND_SECRET_FILES=".vaultkey")
        r = run_hook(bash_payload("rm -f /srv/app/.vaultkey"), env)
        self.assertEqual(r.returncode, 2, r.stderr)
        # ... and the replaced defaults no longer fire.
        r2 = run_hook(bash_payload("rm -f /srv/app/backup.age"), env)
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_extra_patterns_from_env_block(self):
        env = dict(self.env,
                   HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS="terraform\\s+destroy")
        r = run_hook(bash_payload("terraform destroy -auto-approve"), env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS", r.stderr)
        self.assertEqual(self.last_stat()["result"], "block")

    def test_invalid_extra_pattern_is_skipped_not_fatal(self):
        """A malformed custom regex must not take the gate down with it."""
        env = dict(self.env,
                   HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS="*[broken(")
        r = run_hook(bash_payload("git status"), env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")

    # ---- fail-open and edges ----------------------------------------------

    def test_fail_open_on_unreadable_stdin(self):
        """Garbage on stdin: never block blindly, and the fail-open is journaled."""
        r = run_hook("this is not json", self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fail-open", r.stderr)
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_edge_mention_is_not_a_gesture(self):
        """The founding false-positive class: naming a destructive verb inside a
        quoted string, a heredoc or a grep argument is TEXT, not a gesture."""
        cases = [
            "grep -r sudo .",
            'echo "never run rm -rf on this box"',
            "git commit -m 'document why sudo is blocked'",
            "cat <<'EOF' > /tmp/notes.txt\nsudo rm -rf /var is forbidden here\nEOF",
        ]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                self.stats.unlink(missing_ok=True)
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 0, f"{cmd!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["result"], "pass")

    def test_edge_tmp_housekeeping_and_non_recursive_rm(self):
        """`rm -r` is judged on its TARGETS: /tmp stays free, and a plain
        `rm -f` is out of the recursive-delete rule entirely."""
        for cmd in ("rm -rf /tmp/scratch-x", "rm -rf /var/tmp/build", "rm -f /etc/app/x.json"):
            with self.subTest(cmd=cmd):
                self.stats.unlink(missing_ok=True)
                r = run_hook(bash_payload(cmd), self.env)
                self.assertEqual(r.returncode, 0, f"{cmd!r}: {r.stderr}")
                self.assertEqual(self.last_stat()["result"], "pass")
        # One single target outside /tmp is enough to block the whole command.
        r = run_hook(bash_payload("rm -rf /tmp/scratch-x /opt/project"), self.env)
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_non_bash_tool_and_empty_command_are_journaled_skips(self):
        """Out of scope is still an execution: it logs, it never blocks."""
        payload = json.dumps({"tool_name": "Write",
                              "tool_input": {"file_path": "/tmp/f",
                                             "content": "sudo rm -rf /var"}})
        r = run_hook(payload, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-not-bash")

        self.stats.unlink(missing_ok=True)
        r2 = run_hook(bash_payload("   "), self.env)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-empty")

    # ---- the biting test ---------------------------------------------------

    def test_suite_bites_when_the_gate_is_unplugged(self):
        """Replay every MUST_BLOCK payload against a stub that only exits 0: all
        of them must come back 0. This proves the block verdicts above come from
        the gate and not from the test runner. The real hook is never touched:
        the stub lives in a tempdir."""
        stub = Path(self.tmp.name) / "stub-gate.py"
        stub.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        still_blocked = [cmd for cmd in MUST_BLOCK
                         if run_hook(bash_payload(cmd), self.env,
                                     hook=stub).returncode != 0]
        self.assertEqual(still_blocked, [],
                         "these payloads block even with the gate unplugged: "
                         "the assertion does not come from the gate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
