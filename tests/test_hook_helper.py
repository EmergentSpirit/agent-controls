#!/usr/bin/env python3
"""Tests for hooks/_hook.py, the shared helper every gate imports.

The journal is the one artifact every gate writes to, so what lands in it is
a security property, not a formatting detail: gates journal the command that
tripped them, and a command can carry a credential.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER = HERE.parent / "hooks" / "_hook.py"


def helper(journal: Path):
    """Load _hook.py fresh with the journal pointed at a tempdir."""
    os.environ["HARNESS_GATE_STATS"] = str(journal)
    spec = importlib.util.spec_from_file_location("harness_hook_probe", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMaskSecrets(unittest.TestCase):
    """Shapes a real command line carries. This is a filter, not a guarantee,
    but every shape listed here was measured passing through in the clear."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "gate-stats.jsonl"
        self.mod = helper(self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def test_credential_shapes_are_masked(self):
        # Every value below is a DELIBERATE fake. They are fixtures proving
        # the scrubber bites; no real credential enters this repository.
        cases = {
            "env var naming itself":
                "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCY",  # gitleaks:allow
            "flag then space":
                "aws configure --secret-access-key wJalrXUtnFEMIK7MDENG",
            "credential inside a URL":
                "psql postgresql://admin:SuperSecret99@db:5432/prod",
            "authorization header":
                "curl -H 'Authorization: Bearer abcdef123456789'",
            "vendor token":
                "git push https://ghp_AAAABBBBCCCCDDDDEEEE1111 origin",
            "signed token":
                "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.dBjftJeZ4CVP",
        }
        for label, raw in cases.items():
            with self.subTest(label):
                masked = self.mod.mask_secrets(raw, 500)
                self.assertIn("secret", masked.lower(),
                              "%s: nothing was masked" % label)
                self.assertNotIn(raw, masked, "%s: passed through" % label)

    def test_ordinary_strings_survive_intact(self):
        """The control. A masker that masks everything protects nothing: it
        just makes the journal useless for reading back what happened."""
        for raw in ("rm -rf /srv/app/releases",
                    "grep -rn 'token' src/",
                    "/home/user/projects/app/config.yaml",
                    "the secret sauce is documentation",
                    "deploy --verbose --dry-run",
                    # `auth` as a keyword turned all three into masked noise.
                    "git log --author=alice --since=2024-01-01",
                    "git commit -m 'fix: auth: refactor the login path'",
                    "echo 'AUTHORS: see AUTHORS.md'"):
            with self.subTest(raw):
                self.assertEqual(self.mod.mask_secrets(raw, 500), raw)

    def test_cost_stays_linear_on_hostile_input(self):
        """Masking runs on transcript text, which this project treats as
        hostile input. A backtracking pattern is therefore a remote stall,
        not a slow function: one earlier version took over 400 seconds on
        40 kB of a repeated keyword. Truncation happens first, so the work
        is bounded whatever a caller hands over."""
        for label, raw in (("repeated keyword", "token" * 2000),
                           ("base64url run", "aB3-_" * 4000),
                           ("hex run", "deadbeef" * 3750),
                           ("keyword then noise", "secret=" + "x" * 40000)):
            with self.subTest(label):
                start = time.perf_counter()
                self.mod.mask_secrets(raw, 500)
                elapsed = time.perf_counter() - start
                self.assertLess(elapsed, 0.2,
                                "%s took %.3fs: the pattern backtracks"
                                % (label, elapsed))

    def test_truncation_happens_before_masking(self):
        """The bound is what makes the cost predictable, so it is pinned."""
        out = self.mod.mask_secrets("a" * 5000, 100)
        self.assertEqual(len(out), 101)          # 100 characters + the ellipsis
        self.assertTrue(out.endswith("…"))


class TestGateStatScrubs(unittest.TestCase):
    """gate_stat() scrubs on the way in, so a caller cannot forget to."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "gate-stats.jsonl"
        self.mod = helper(self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def last(self) -> dict:
        return json.loads(
            self.journal.read_text(encoding="utf-8").strip().splitlines()[-1])

    def test_string_values_are_scrubbed_by_the_writer(self):
        self.mod.gate_stat("probe", "block",
                           cmd="deploy --api-key HUNTER2SECRETVALUE")
        line = self.last()
        self.assertNotIn("HUNTER2SECRETVALUE", json.dumps(line))
        self.assertEqual(line["result"], "block")

    def test_non_string_values_are_left_alone(self):
        self.mod.gate_stat("probe", "pass", n=42, ok=True, ratio=0.5)
        line = self.last()
        self.assertEqual(line["n"], 42)
        self.assertIs(line["ok"], True)
        self.assertEqual(line["ratio"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
