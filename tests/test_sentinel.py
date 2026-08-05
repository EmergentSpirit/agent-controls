#!/usr/bin/env python3
"""Tests for the sentinel (sentinel/sentinel.py).

Zero network, zero dependency beyond pytest+stdlib. Every case builds a
throwaway harness in a tempdir -- its own settings file, its own hooks
directory, its own gate-stats journal -- and runs the sentinel as a
SUBPROCESS, exactly like a timer would. Nothing here reads the machine's real
configuration: --settings and --hook-dirs are always pointed at the tempdir,
so the suite cannot pass or fail because of what the host happens to wire.

The class that matters is TestCoverage: it pins the check the whole module
exists for -- a gate that is wired, present and syntactically perfect, and has
left NO trace in the journal.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SENTINEL = ROOT / "sentinel" / "sentinel.py"

VALID_HOOK = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
BROKEN_HOOK = "#!/usr/bin/env python3\ndef broken(:\n    pass\n"


def sentinel_module():
    """sentinel/sentinel.py loaded by path, for the pure-function cases."""
    spec = importlib.util.spec_from_file_location("sentinel_mod", SENTINEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SentinelBase(unittest.TestCase):
    """One tempdir per test: hooks, settings, journal, report directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hooks = self.root / "hooks"
        self.hooks.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.stats = self.state / "gate-stats.jsonl"
        self.env = {"HARNESS_STATE_DIR": str(self.state),
                    "HARNESS_GATE_STATS": str(self.stats)}
        for var in ("HARNESS_SENTINEL_SETTINGS", "HARNESS_SENTINEL_EXEMPT",
                    "HARNESS_SENTINEL_PROBES", "HARNESS_SENTINEL_PROBE_ALLOW",
                    "HARNESS_SENTINEL_ACTIVITY_PATHS", "HARNESS_HOOK_DIRS",
                    "HARNESS_SENTINEL_COVERAGE_DAYS", "HARNESS_SENTINEL_REPORT_DIR",
                    "HARNESS_SENTINEL_FRESHNESS_HOURS"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.tmp.cleanup()

    # --- fixture builders ---------------------------------------------------

    def hook_file(self, name, body=VALID_HOOK):
        path = self.hooks / name
        path.write_text(body, encoding="utf-8")
        return path

    def settings(self, commands, name="settings.json", event="PreToolUse"):
        """A minimal Claude Code settings file wiring the given commands."""
        data = {"hooks": {event: [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": c} for c in commands]}]}}
        path = self.root / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def journal(self, entries):
        """entries: [(hook name, age in hours)]."""
        lines = []
        for name, age_h in entries:
            stamp = datetime.now() - timedelta(hours=age_h)
            lines.append(json.dumps({"ts": stamp.isoformat(timespec="seconds"),
                                     "hook": name, "result": "pass"}))
        self.stats.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.stats

    def run_sentinel(self, args=(), env_extra=None):
        env = dict(os.environ, **self.env, **(env_extra or {}))
        base = ["--hook-dirs", str(self.hooks)]
        return subprocess.run([sys.executable, str(SENTINEL)] + base + list(args),
                              capture_output=True, text=True, env=env, timeout=120)

    # --- assertions ---------------------------------------------------------

    def verdict_of(self, out):
        line = [x for x in out.splitlines() if x.startswith("VERDICT")][-1]
        return line.split()[1]

    def lines_of(self, out, family):
        return [x for x in out.splitlines() if x.split()[1:2] == [family]]

    def last_stat(self):
        rows = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(rows[-1])


class TestNominal(SentinelBase):
    """Everything wired, present, and logging: the only path to a clean OK."""

    def test_t1_wired_present_and_logging_is_the_only_ok(self):
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 1)])
        r = self.run_sentinel(["--settings", str(cfg)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.verdict_of(r.stdout), "OK", r.stdout)
        self.assertIn("1 hooks wired", r.stdout)
        self.assertIn("present, syntax OK", r.stdout)
        self.assertIn("trace(s) in the journal", r.stdout)
        report = self.state / "sentinel" / (datetime.now().strftime("%Y-%m-%d") + ".txt")
        self.assertTrue(report.exists(), "the daily report was not written")
        self.assertIn("VERDICT OK", report.read_text(encoding="utf-8"))

    def test_t2_the_sentinel_journals_its_own_run(self):
        """A sentinel that stops logging is exactly the failure it hunts."""
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 1)])
        self.run_sentinel(["--settings", str(cfg)])
        stat = self.last_stat()
        self.assertEqual(stat["hook"], "sentinel")
        self.assertEqual(stat["result"], "observe")
        self.assertEqual(stat["verdict"], "OK")
        self.assertEqual(stat["hooks"], 1)

    def test_t3_enumerate_derives_the_inventory_and_checks_nothing(self):
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py"),
                             "bash -c 'echo inline'"])
        r = self.run_sentinel(["--settings", str(cfg), "--enumerate"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("alpha-gate.py", r.stdout)
        self.assertIn("inline", r.stdout)
        self.assertIn("total: 2 hooks wired across 1 settings file(s)", r.stdout)
        self.assertNotIn("VERDICT", r.stdout)
        self.assertFalse((self.state / "sentinel").exists())


class TestScripts(SentinelBase):
    """Presence and syntax of what the settings files actually point at."""

    def test_t4_wired_script_missing_from_disk_is_a_fail(self):
        cfg = self.settings(["python3 %s" % (self.hooks / "ghost-gate.py")])
        self.journal([("ghost", 1)])
        r = self.run_sentinel(["--settings", str(cfg)])
        self.assertEqual(self.verdict_of(r.stdout), "FAIL", r.stdout)
        self.assertIn("MISSING on disk", r.stdout)
        self.assertIn("ghost-gate.py", r.stdout)

    def test_t5_broken_python_is_caught_by_the_compile_check(self):
        self.hook_file("broken-gate.py", BROKEN_HOOK)
        cfg = self.settings(["python3 %s" % (self.hooks / "broken-gate.py")])
        self.journal([("broken", 1)])
        r = self.run_sentinel(["--settings", str(cfg)])
        self.assertEqual(self.verdict_of(r.stdout), "FAIL", r.stdout)
        syntax = [x for x in r.stdout.splitlines()
                  if x.startswith("FAIL script") and "syntax" in x]
        self.assertTrue(syntax, r.stdout)
        # the compile check writes NOTHING next to the inspected file
        self.assertFalse((self.hooks / "__pycache__").exists())

    def test_t6_unresolved_variable_is_reported_never_silently_skipped(self):
        cfg = self.settings(["python3 $NOT_SET_ANYWHERE/hooks/x-gate.py"])
        self.journal([("x", 1)])
        r = self.run_sentinel(["--settings", str(cfg)])
        self.assertIn("unresolved variable", r.stdout)
        self.assertIn("SKIP script", r.stdout)


class TestOrphans(SentinelBase):
    """A hook on disk that no settings file wires."""

    def test_t7_script_on_disk_wired_nowhere_warns(self):
        self.hook_file("alpha-gate.py")
        self.hook_file("forgotten-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 1)])
        r = self.run_sentinel(["--settings", str(cfg)])
        self.assertEqual(self.verdict_of(r.stdout), "WARN", r.stdout)
        orphans = self.lines_of(r.stdout, "orphan")
        self.assertTrue(any("forgotten-gate.py" in x for x in orphans), orphans)
        self.assertFalse(any("alpha-gate.py" in x and x.startswith("WARN")
                             for x in orphans), orphans)

    def test_t8_exempt_list_silences_a_deliberate_unwired_tool(self):
        self.hook_file("alpha-gate.py")
        self.hook_file("by-hand.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 1)])
        r = self.run_sentinel(["--settings", str(cfg)],
                              {"HARNESS_SENTINEL_EXEMPT": "by-hand.py"})
        self.assertEqual(self.verdict_of(r.stdout), "OK", r.stdout)
        self.assertIn("SKIP orphan", r.stdout)
        self.assertIn("exempt", r.stdout)


class TestCoverage(SentinelBase):
    """The reason the module exists: wired is not alive, logging is."""

    def test_t9_wired_gate_with_no_journal_trace_is_flagged(self):
        self.hook_file("alpha-gate.py")
        self.hook_file("silent-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py"),
                             "python3 %s" % (self.hooks / "silent-gate.py")])
        self.journal([("alpha", 1)])
        r = self.run_sentinel(["--settings", str(cfg)])
        self.assertEqual(self.verdict_of(r.stdout), "WARN", r.stdout)
        coverage = self.lines_of(r.stdout, "coverage")
        dead = [x for x in coverage if "silent-gate.py" in x]
        self.assertTrue(dead, coverage)
        self.assertTrue(dead[0].startswith("WARN"), dead)
        self.assertIn("NO trace in the journal", dead[0])
        alive = [x for x in coverage if "alpha-gate.py" in x]
        self.assertTrue(alive[0].startswith("OK"), alive)

    def test_t10_a_trace_older_than_the_window_does_not_count(self):
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 24 * 30)])       # last run a month ago
        r = self.run_sentinel(["--settings", str(cfg), "--coverage-days", "7"])
        dead = [x for x in self.lines_of(r.stdout, "coverage")
                if "alpha-gate.py" in x]
        self.assertTrue(dead[0].startswith("WARN"), dead)
        self.assertIn("over 7 days", dead[0])

    def test_t11_no_journal_means_undecidable_never_a_pass(self):
        """Absent noise is not zero noise: with no journal the sentinel
        REFUSES to declare any gate alive."""
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        r = self.run_sentinel(["--settings", str(cfg)])
        self.assertEqual(self.verdict_of(r.stdout), "FAIL", r.stdout)
        self.assertIn("nothing proves any gate is alive", r.stdout)
        coverage = self.lines_of(r.stdout, "coverage")
        self.assertEqual(len(coverage), 1, coverage)
        self.assertTrue(coverage[0].startswith("SKIP"), coverage)
        self.assertIn("UNDECIDABLE", coverage[0])


class TestJournal(SentinelBase):
    """The aliveness signal of the whole harness, checked on its own."""

    def test_t12_silent_journal_alone_is_a_warn_not_a_fail(self):
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 80)])
        old = time.time() - 80 * 3600
        os.utime(self.stats, (old, old))
        r = self.run_sentinel(["--settings", str(cfg), "--coverage-days", "30"])
        journal = self.lines_of(r.stdout, "journal")
        self.assertTrue(journal[0].startswith("WARN"), journal)
        self.assertIn("either no session ran", journal[0])

    def test_t13_silent_journal_while_a_session_ran_is_a_fail(self):
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 80)])
        old = time.time() - 80 * 3600
        os.utime(self.stats, (old, old))
        activity = self.root / "activity"
        activity.mkdir()
        (activity / "session.jsonl").write_text("{}\n", encoding="utf-8")
        r = self.run_sentinel(["--settings", str(cfg), "--coverage-days", "30"],
                              {"HARNESS_SENTINEL_ACTIVITY_PATHS": str(activity)})
        journal = self.lines_of(r.stdout, "journal")
        self.assertTrue(journal[0].startswith("FAIL"), journal)
        self.assertIn("the wired gates are mute", journal[0])

    def test_t14_corrupt_tail_line_is_a_fail(self):
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 1)])
        with open(self.stats, "a", encoding="utf-8") as f:
            f.write("this line is not json\n")
        r = self.run_sentinel(["--settings", str(cfg)])
        self.assertEqual(self.verdict_of(r.stdout), "FAIL", r.stdout)
        self.assertIn("last line is not JSON", r.stdout)


class TestRobustness(SentinelBase):
    """A health check that crashes is a health check that lies."""

    def test_t15_unreadable_settings_file_is_reported_and_never_raises(self):
        bad = self.root / "settings.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        self.journal([("alpha", 1)])
        r = self.run_sentinel(["--settings", str(bad)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.verdict_of(r.stdout), "FAIL", r.stdout)
        self.assertIn("unreadable JSON", r.stdout)

    def test_t16_missing_settings_file_is_a_fail_not_an_empty_audit(self):
        r = self.run_sentinel(["--settings", str(self.root / "nowhere.json")])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("file not found", r.stdout)

    def test_t17_strict_turns_a_fail_verdict_into_exit_1(self):
        cfg = self.settings(["python3 %s" % (self.hooks / "ghost-gate.py")])
        self.journal([("ghost", 1)])
        plain = self.run_sentinel(["--settings", str(cfg)])
        self.assertEqual(plain.returncode, 0, plain.stderr)
        strict = self.run_sentinel(["--settings", str(cfg), "--strict"])
        self.assertEqual(strict.returncode, 1, strict.stdout)

    def test_t18_probe_outside_the_allowlist_is_skipped_with_its_reason(self):
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 1)])
        probes = self.root / "probes.txt"
        probes.write_text("# a comment\ntest -d %s\nnot-allowed --wipe\n" % self.root,
                          encoding="utf-8")
        r = self.run_sentinel(["--settings", str(cfg), "--probes", str(probes)])
        probe_lines = self.lines_of(r.stdout, "probe")
        self.assertTrue(any(x.startswith("OK") and "test -d" in x
                            for x in probe_lines), probe_lines)
        skipped = [x for x in probe_lines if x.startswith("SKIP")]
        self.assertTrue(skipped, probe_lines)
        self.assertIn("not in the allowlist", skipped[0])


class TestProbeExecution(SentinelBase):
    """The probe file is CONFIG, read by a job that runs unattended from a
    timer. Allowlisting the first word of a line that is then handed whole to a
    shell protects nothing, and one malformed line must cost one line."""

    def probe_run(self, probe_lines, args=()):
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 1)])
        probes = self.root / "probes.txt"
        probes.write_text("".join(line + "\n" for line in probe_lines),
                          encoding="utf-8")
        return self.run_sentinel(["--settings", str(cfg), "--probes", str(probes)]
                                 + list(args))

    def skips(self, out):
        return [x for x in self.lines_of(out, "probe") if x.startswith("SKIP")]

    def test_t21_an_allowlisted_head_never_smuggles_a_second_command(self):
        """The measured bypass: `test` passed the allowlist, then the WHOLE
        line ran under `bash -c` and the half after the `;` executed."""
        loot = self.root / "PWNED"
        for line in ("test -d /tmp; echo PWNED > %s" % loot,
                     "test -d /tmp && echo PWNED > %s" % loot,
                     "test -d /tmp | tee %s" % loot,
                     "test -d $(echo /tmp)",
                     "test -d `echo /tmp`"):
            r = self.probe_run([line])
            self.assertFalse(loot.exists(),
                             "the smuggled command RAN for %r:\n%s" % (line, r.stdout))
            skipped = self.skips(r.stdout)
            self.assertTrue(skipped, "%r was not skipped:\n%s" % (line, r.stdout))
            self.assertIn("metacharacter", skipped[0])
        # SKIP never moves the verdict, and the legitimate line still runs.
        r = self.probe_run(["test -d /tmp; echo PWNED > %s" % loot, "test -d /tmp"])
        self.assertEqual(self.verdict_of(r.stdout), "OK", r.stdout)
        self.assertTrue(any(x.startswith("OK") and "test -d /tmp" in x
                            for x in self.lines_of(r.stdout, "probe")), r.stdout)

    def test_t22_the_allowlist_is_a_binary_not_a_word_and_carries_its_rules(self):
        """A planted binary that merely SHARES the name does not pass, and an
        allowlisted binary is still confined to read-only use."""
        planted = self.hooks / "test"
        planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        planted.chmod(0o755)
        loot = self.root / "LOOT"
        r = self.probe_run(["%s -d /tmp" % planted,
                            "systemctl --user stop example-service.service",
                            "curl -fsS --max-time 2 -o %s http://127.0.0.1:1/x" % loot,
                            "curl -fsS --max-time 2 -T /etc/hostname http://127.0.0.1:1/x"])
        reasons = " | ".join(self.skips(r.stdout))
        self.assertIn("non-canonical path", reasons)
        self.assertIn("systemctl: verb is not read-only", reasons)
        self.assertIn("-o pointing somewhere other than /dev/null", reasons)
        self.assertIn("forbidden option: -T", reasons)
        self.assertFalse(loot.exists(), r.stdout)
        self.assertEqual(len(self.skips(r.stdout)), 4, r.stdout)

    def test_t23_one_malformed_probe_line_does_not_kill_the_whole_report(self):
        """The parse used to sit outside the loop's try: an unclosed quote
        raised to the module's fail-open handler, so NO dated report was
        written and not one other family ran."""
        r = self.probe_run(['test -f "/etc/hostname', "test -d /tmp"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("unexpected error", r.stderr)
        report = self.state / "sentinel" / (datetime.now().strftime("%Y-%m-%d") + ".txt")
        self.assertTrue(report.exists(),
                        "no dated report was written:\n%s\n%s" % (r.stdout, r.stderr))
        self.assertIn("VERDICT", report.read_text(encoding="utf-8"))
        skipped = self.skips(r.stdout)
        self.assertTrue(skipped, r.stdout)
        self.assertIn("unparsable line", skipped[0])
        # the malformed line costs itself, and NOTHING else
        self.assertTrue(any(x.startswith("OK") and "test -d /tmp" in x
                            for x in self.lines_of(r.stdout, "probe")), r.stdout)
        for family in ("settings", "script", "journal", "coverage"):
            self.assertTrue(self.lines_of(r.stdout, family),
                            "family %s never ran:\n%s" % (family, r.stdout))
        self.assertEqual(self.last_stat()["result"], "observe", self.last_stat())

    def test_t24_the_probe_parser_is_the_same_gate_as_recalls_check_field(self):
        """Pure functions, and the twin of recall.run_check by construction: a
        second implementation of the same rule is a second thing to get wrong."""
        mod = sentinel_module()
        allow = set(mod.DEFAULT_PROBE_ALLOW)
        argv, refusal = mod.probe_argv("test -d /tmp", allow)
        self.assertEqual((argv, refusal), (["test", "-d", "/tmp"], None))
        for bad in ("test -d /tmp; rm -rf /", "test -d /tmp > /x", "test -d ~",
                    "test -d /tmp || bash"):
            self.assertIn("metacharacter", mod.probe_argv(bad, allow)[1], bad)
        self.assertIn("unparsable line", mod.probe_argv('test -f "/x', allow)[1])
        self.assertIn("not in the allowlist", mod.probe_argv("bash -c x", allow)[1])
        self.assertIn("nothing left after parsing", mod.probe_argv("   ", allow)[1])
        self.assertIsNone(mod.probe_argv(
            "curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8080/health",
            allow)[1])
        self.assertIsNone(mod.probe_argv(
            "systemctl --user is-active --quiet example.service", allow)[1])


class TestMatching(unittest.TestCase):
    """Pure functions: how a script name is matched against a journaled one."""

    def test_t19_journal_key_normalizes_stem_and_gate_suffix(self):
        mod = sentinel_module()
        self.assertEqual(mod.journal_key("home-prefix-gate.py"), "home-prefix")
        self.assertEqual(mod.journal_key("/a/b/scope_write_gate.py"), "scope-write")
        self.assertEqual(mod.journal_key("home-prefix"), "home-prefix")
        self.assertTrue(mod.keys_match(mod.journal_key("home-prefix-gate.py"),
                                       mod.journal_key("home-prefix")))
        self.assertFalse(mod.keys_match(mod.journal_key("alpha-gate.py"),
                                        mod.journal_key("beta-gate.py")))

    def test_t20_script_of_ignores_interpreters_and_data_arguments(self):
        mod = sentinel_module()
        self.assertEqual(mod.script_of("/opt/venv/bin/python3 /srv/hooks/a-gate.py"),
                         ("/srv/hooks/a-gate.py", "script"))
        self.assertEqual(mod.script_of("bash -c 'echo hi'"), (None, "inline"))
        self.assertEqual(mod.script_of("python3 $UNSET_VAR/hooks/a-gate.py"),
                         (None, "unresolved"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
