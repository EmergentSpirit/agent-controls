#!/usr/bin/env python3
"""Tests for the governor: two adversarial judges, routing, audit, trials.

ZERO network, ZERO API key, ZERO provider. Both judges are injected through
HARNESS_GOVERNOR_FAKE_JUDGE1/2 (same technique as the shield suite injects its
reviewer verdict), and judge 2's endpoint is deliberately left unconfigured in
the test that matters most: the one proving that a judge who does not speak
produces an EXPLICIT status and never a default yes.

Each test isolates HARNESS_STATE_DIR and HARNESS_GATE_STATS in a tempdir, and
runs the CLIs as subprocesses, exactly as a timer would.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GOVERNOR = ROOT / "governor"
PROPOSE = GOVERNOR / "propose.py"
AUDIT = GOVERNOR / "audit.py"
TRIAL = GOVERNOR / "trial.py"
EXAMPLE = GOVERNOR / "proposal.example.md"

VIABLE = json.dumps({"verdict": "viable", "reasons": ["the trigger is decidable"],
                     "blind_spots": [], "reservations": ["cap the sample size"]})
REJECTED = json.dumps({"verdict": "rejected",
                       "reasons": ["the friction cost exceeds the gain"],
                       "blind_spots": ["an existing gate already covers it"],
                       "reservations": []})


def run(script: Path, args, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ, **env_extra)
    return subprocess.run([sys.executable, str(script)] + [str(a) for a in args],
                          capture_output=True, text=True, env=env, timeout=90)


def judges_module():
    """governor/judges.py loaded by path (governor/ is not a package)."""
    spec = importlib.util.spec_from_file_location("governor_judges",
                                                  GOVERNOR / "judges.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(delta_days: float = 0.0) -> str:
    return (datetime.now() - timedelta(days=delta_days)).isoformat(
        timespec="seconds")


class GovernorBase(unittest.TestCase):
    """One tempdir per test: state directory, journal, proposals."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.stats = self.state / "gate-stats.jsonl"
        self.env = {"HARNESS_STATE_DIR": str(self.state),
                    "HARNESS_GATE_STATS": str(self.stats)}
        # No inherited configuration may leak into a test: an API key or an
        # endpoint picked up from the developer's shell would turn this suite
        # into a paying, networked, non-deterministic thing.
        for var in ("HARNESS_JUDGE2_URL", "HARNESS_JUDGE2_MODEL",
                    "HARNESS_JUDGE2_API_KEY_ENV", "HARNESS_JUDGE1_MODEL",
                    "HARNESS_GOVERNOR_FAKE_JUDGE1", "HARNESS_GOVERNOR_FAKE_JUDGE2",
                    "HARNESS_GOVERNOR_SETTINGS", "HARNESS_GOVERNOR_LEDGER",
                    "HARNESS_GOVERNOR_WINDOW_DAYS", "HARNESS_GOVERNOR_NOISY_MIN"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.tmp.cleanup()

    # --- helpers ------------------------------------------------------------
    def gov(self, *parts) -> Path:
        return self.state.joinpath("governor", *parts)

    def ledger(self):
        text = self.gov("ledger.jsonl").read_text(encoding="utf-8").strip()
        return [json.loads(line) for line in text.splitlines()]

    def stats_lines(self):
        text = self.stats.read_text(encoding="utf-8").strip()
        return [json.loads(line) for line in text.splitlines()]

    def journal(self, records):
        with self.stats.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def proposal(self, slug="example-gate", touches="technical", what=None):
        path = Path(self.tmp.name) / ("%s.md" % slug)
        path.write_text(
            "# proposal: %s\n"
            "touches: %s\n"
            "what: %s\n"
            "incident: 2026-01-14: it happened once, here is the date.\n"
            "blocks: the exact command that would have been caught.\n"
            "error_cost: one extra round-trip on a legitimate gesture.\n"
            "trial: 7 days, observation only.\n"
            "detail:\nfree block, written for the judges.\n"
            % (slug, touches, what or "One sentence, no jargon."),
            encoding="utf-8")
        return path

    def verdict_file(self) -> dict:
        files = sorted(self.gov("verdicts").glob("*.json"))
        self.assertEqual(len(files), 1, "exactly one raw verdict expected")
        return json.loads(files[0].read_text(encoding="utf-8"))


class TestProposeRouting(GovernorBase):
    """proposal -> two independent judges -> silent rejection, held, or a
    bounded pitch. The governor arms nothing: it writes files and ledger lines."""

    def test_t1_two_viable_technical_goes_to_the_build_queue(self):
        r = run(PROPOSE, [self.proposal()],
                dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=VIABLE,
                     HARNESS_GOVERNOR_FAKE_JUDGE2=VIABLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        pitch = self.gov("to-build", "example-gate.md").read_text(encoding="utf-8")
        self.assertIn("Judges: judge-1 viable", pitch)
        self.assertIn("judge-2 viable", pitch)
        self.assertIn("cap the sample size", pitch)      # reservations surface
        self.assertIn("observation-only trial before anything is armed", pitch)
        self.assertEqual(len(pitch.strip().splitlines()), 8,
                         "the pitch stays bounded: 5 fields + title + judges + decision")
        entry = self.ledger()[-1]
        self.assertEqual(entry["status"], "viable-to-build")
        self.assertEqual(entry["title"], "example-gate")
        self.assertEqual(entry["route"], "governor")
        raw = self.verdict_file()
        self.assertEqual(raw["judge1"]["verdict"], "viable")
        self.assertEqual(raw["judge2"]["verdict"], "viable")
        self.assertEqual(self.stats_lines()[-1]["result"], "pass")

    def test_t2_one_rejection_kills_it_into_the_archive(self):
        """A single `rejected` is enough, and the reasons are kept: a killed
        proposal is auditable afterwards, it is not reported to the human."""
        r = run(PROPOSE, [self.proposal()],
                dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=REJECTED,
                     HARNESS_GOVERNOR_FAKE_JUDGE2=VIABLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.gov("to-build").exists())
        killed = self.gov("archive", "example-gate.md").read_text(encoding="utf-8")
        self.assertIn("Why the judges killed it", killed)
        self.assertIn("judge-1: the friction cost exceeds the gain", killed)
        self.assertEqual(self.ledger()[-1]["status"], "rejected-by-judges")

    def test_t3_judge_two_unavailable_is_explicit_never_a_default_yes(self):
        """THE HARD RULE. Judge 2 is not configured at all (no URL, no key, no
        fake): judge 1 alone can NOT carry a proposal through. The status says
        so out loud, and the proposal waits."""
        r = run(PROPOSE, [self.proposal()],
                dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=VIABLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.gov("to-build").exists(),
                         "an absent judge must never route a proposal to build")
        self.assertFalse(self.gov("awaiting-operator").exists())
        held = self.gov("pending-judge", "example-gate.md").read_text(encoding="utf-8")
        self.assertIn("judge-2 UNAVAILABLE (not-configured)", held)
        self.assertIn("a judge did not speak", held)
        self.assertEqual(self.ledger()[-1]["status"], "judge-unavailable")
        raw = self.verdict_file()
        self.assertIsNone(raw["judge2"])
        self.assertEqual(raw["judge2_why"], "not-configured")
        self.assertEqual(raw["judge2_host"], "")
        last = self.stats_lines()[-1]
        self.assertEqual(last["result"], "warn")
        self.assertEqual(last["status"], "judge-unavailable")

    def test_t4_judge_one_unavailable_is_held_too_no_judge_is_optional(self):
        """Symmetry: the rule is not "judge 2 is the optional one". Either judge
        missing holds the proposal."""
        r = run(PROPOSE, [self.proposal()],
                dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1="unavailable",
                     HARNESS_GOVERNOR_FAKE_JUDGE2=VIABLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        held = self.gov("pending-judge", "example-gate.md").read_text(encoding="utf-8")
        self.assertIn("judge-1 UNAVAILABLE (fake-unavailable)", held)
        self.assertEqual(self.ledger()[-1]["status"], "judge-unavailable")

    def test_t5_an_unreadable_verdict_is_not_a_vote(self):
        """Prose instead of JSON, or a verdict word outside the closed list: in
        both cases the judge did not judge, and the proposal is held rather than
        being credited with an opinion nobody can read."""
        prose = run(PROPOSE, [self.proposal()],
                    dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=VIABLE,
                         HARNESS_GOVERNOR_FAKE_JUDGE2="Looks good to me, ship it."))
        self.assertEqual(prose.returncode, 0, prose.stderr)
        held = self.gov("pending-judge", "example-gate.md").read_text(encoding="utf-8")
        self.assertIn("judge-2 UNAVAILABLE (unreadable-verdict)", held)

        off_list = run(PROPOSE, [self.proposal(slug="other-gate")],
                       dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=VIABLE,
                            HARNESS_GOVERNOR_FAKE_JUDGE2='{"verdict": "maybe"}'))
        self.assertEqual(off_list.returncode, 0, off_list.stderr)
        self.assertTrue(self.gov("pending-judge", "other-gate.md").exists())
        self.assertFalse(self.gov("to-build").exists())

    def test_t6_life_class_waits_for_the_human_word(self):
        """Money, public surface, irreversible, personal data: two `viable` are
        not a green light, they are a right to ask the question."""
        r = run(PROPOSE, [self.proposal(slug="pay-gate", touches="life")],
                dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=VIABLE,
                     HARNESS_GOVERNOR_FAKE_JUDGE2=VIABLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.gov("to-build").exists())
        pitch = self.gov("awaiting-operator", "pay-gate.md").read_text(encoding="utf-8")
        self.assertIn("Your word: GO trial / no", pitch)
        self.assertEqual(self.ledger()[-1]["status"], "awaiting-operator")

    def test_t7_the_five_fields_are_bounded_at_the_source(self):
        """An over-long field is refused, not truncated, and nothing is routed:
        a pitch that does not fit is rewritten by its author."""
        long_one = self.proposal(slug="verbose-gate", what="x" * 201)
        r = run(PROPOSE, [long_one],
                dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=VIABLE,
                     HARNESS_GOVERNOR_FAKE_JUDGE2=VIABLE))
        self.assertEqual(r.returncode, 1)
        self.assertIn("field what too long (201 > 200)", r.stderr)
        self.assertFalse(self.gov().exists(), "a refused proposal routes nothing")

        truncated = Path(self.tmp.name) / "no-incident.md"
        truncated.write_text("# proposal: x-gate\nwhat: one line\n", encoding="utf-8")
        missing = run(PROPOSE, [truncated],
                      dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=VIABLE,
                           HARNESS_GOVERNOR_FAKE_JUDGE2=VIABLE))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("missing field: incident", missing.stderr)

    def test_t8_the_shipped_example_parses_and_routes(self):
        """The example that ships with the module is the format's own test: if
        it stops parsing, the documentation started lying."""
        r = run(PROPOSE, [EXAMPLE],
                dict(self.env, HARNESS_GOVERNOR_FAKE_JUDGE1=VIABLE,
                     HARNESS_GOVERNOR_FAKE_JUDGE2=VIABLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(
            self.gov("to-build", "example-commit-after-piped-tests.md").exists())


class TestJudgesAdapter(unittest.TestCase):
    """Judge 2 is a configurable adapter, not a welded dependency."""

    def setUp(self):
        self.mod = judges_module()
        self.saved = {k: os.environ.pop(k, None) for k in
                      ("HARNESS_JUDGE2_URL", "HARNESS_JUDGE2_MODEL",
                       "HARNESS_JUDGE2_API_KEY_ENV", "HARNESS_GOVERNOR_FAKE_JUDGE2")}

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_t9_no_endpoint_no_call_and_an_explicit_reason(self):
        verdict, why = self.mod.judge_http("# proposal: x")
        self.assertIsNone(verdict)
        self.assertEqual(why, "not-configured")   # no socket was ever opened

        os.environ["HARNESS_JUDGE2_URL"] = "endpoint-placeholder"
        self.assertEqual(self.mod.judge_http("x")[1], "no-model-configured")

        os.environ["HARNESS_JUDGE2_MODEL"] = "some-model-id"
        os.environ["HARNESS_JUDGE2_API_KEY_ENV"] = "A_KEY_VAR_THAT_IS_EMPTY"
        os.environ.pop("A_KEY_VAR_THAT_IS_EMPTY", None)
        self.assertEqual(self.mod.judge_http("x")[1], "api-key-absent")

    def test_t10_verdict_parsing_is_strict_and_absence_is_loud(self):
        wrapped = 'Here is my call:\n{"verdict": "rejected", "reasons": ["a"]}\nthanks'
        parsed = self.mod.normalize(self.mod.extract_json(wrapped))
        self.assertEqual(parsed["verdict"], "rejected")
        self.assertEqual(parsed["reasons"], ["a"])
        self.assertEqual(parsed["blind_spots"], [])
        self.assertIsNone(self.mod.normalize({"verdict": "probably"}))
        self.assertIsNone(self.mod.normalize("rejected"))
        self.assertIsNone(self.mod.extract_json("no json here"))
        self.assertEqual(self.mod.label(None, "timeout"), "UNAVAILABLE (timeout)")
        self.assertEqual(self.mod.label({"verdict": "viable"}, ""), "viable")

    def test_t11_no_provider_and_no_key_is_hard_coded_anywhere(self):
        """The second family is a deployment choice. A URL literal in this code
        would silently re-weld it, and a key literal would be a leak."""
        for path in sorted(GOVERNOR.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("://", source, "%s hard-codes an endpoint" % path.name)
            self.assertNotIn("sk-", source, "%s looks like it carries a key" % path.name)
        judges_source = (GOVERNOR / "judges.py").read_text(encoding="utf-8")
        for var in ("HARNESS_JUDGE2_URL", "HARNESS_JUDGE2_MODEL",
                    "HARNESS_JUDGE2_API_KEY_ENV"):
            self.assertIn(var, judges_source)


class TestAudit(GovernorBase):
    """Weekly audit BY EXCEPTION: a gate doing its job is never mentioned."""

    def wire(self, *scripts) -> str:
        path = Path(self.tmp.name) / "settings.json"
        path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python3 %s" % s} for s in scripts]}]}}),
            encoding="utf-8")
        return str(path)

    def gate_script(self, name: str, body: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_t12_silent_gate_and_noisy_gate_are_the_only_lines(self):
        alive = self.gate_script("alive-gate.py", 'gate_stat("alive-gate", "pass")\n')
        noisy = self.gate_script("noisy-gate.py", 'gate_stat("noisy-gate", "block")\n')
        mute = self.gate_script("mute-gate.py", 'print("this one never journals")\n')
        self.journal(
            [{"ts": iso(1), "hook": "alive-gate", "result": "pass"}]
            + [{"ts": iso(0.5), "hook": "noisy-gate", "result": "block",
                "why": "sample-%d" % i} for i in range(4)]
            # outside the window: an old flood must not make a gate noisy today
            + [{"ts": iso(40), "hook": "quiet-gate", "result": "block",
                "why": "ancient-%d" % i} for i in range(20)])
        env = dict(self.env, HARNESS_GOVERNOR_SETTINGS=self.wire(alive, noisy, mute),
                   HARNESS_GOVERNOR_NOISY_MIN="3", HARNESS_GOVERNOR_WINDOW_DAYS="30")
        r = run(AUDIT, [], env)
        self.assertEqual(r.returncode, 0, r.stderr)
        page = self.gov("audit-decisions.md").read_text(encoding="utf-8")
        self.assertIn("SILENT **mute-gate.py**", page)
        self.assertIn("Decision: retire / keep", page)
        self.assertNotIn("alive-gate.py**", page)      # alive: never mentioned
        self.assertIn("NOISY **noisy-gate**: 4 block/deny", page)
        self.assertIn("sample-3", page)                # the 3 most recent
        self.assertIn("sample-1", page)
        self.assertNotIn("sample-0", page)             # the oldest is not pasted
        self.assertNotIn("quiet-gate", page)           # out of window, invisible
        self.assertEqual(self.stats_lines()[-1]["result"], "warn")

    def test_t13_silence_must_be_true_the_page_is_deleted(self):
        """A stale decision page is a lie: when nothing is pending, the file
        goes away instead of being left behind to be re-read forever."""
        mute = self.gate_script("mute-gate.py", 'print("nothing")\n')
        alive = self.gate_script("alive-gate.py", 'gate_stat("alive-gate", "pass")\n')
        self.journal([{"ts": iso(1), "hook": "alive-gate", "result": "pass"}])
        noisy_env = dict(self.env, HARNESS_GOVERNOR_SETTINGS=self.wire(alive, mute))
        run(AUDIT, [], noisy_env)
        self.assertTrue(self.gov("audit-decisions.md").exists())

        clean = run(AUDIT, [], dict(self.env,
                                    HARNESS_GOVERNOR_SETTINGS=self.wire(alive)))
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertIn("nothing to decide", clean.stdout)
        self.assertFalse(self.gov("audit-decisions.md").exists())
        log = self.gov("audit-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(log), 2, "every run leaves an aliveness line")
        self.assertEqual(json.loads(log[-1])["exceptions"], 0)

    def test_t14_absent_settings_and_absent_journal_stay_silent(self):
        """Nothing wired, nothing journalled: the audit reports nothing and
        does not crash. A governance tool that dies on a fresh install is a
        governance tool nobody installs."""
        r = run(AUDIT, [], dict(self.env,
                                HARNESS_GOVERNOR_SETTINGS="/nonexistent/settings.json"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to decide", r.stdout)
        self.assertFalse(self.gov("audit-decisions.md").exists())


class TestTrial(GovernorBase):
    """The trial logs `observe` and blocks NOTHING; its review is lived facts."""

    def due_trial(self, stem: str, start_days: float, end_days: float):
        path = self.gov("trials", "%s.json" % stem)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stem": stem, "start": iso(start_days),
                                    "end": iso(end_days)}), encoding="utf-8")
        return path

    def test_t15_open_then_close_compiles_what_it_would_have_blocked(self):
        opened = run(TRIAL, ["--open", "new-gate", "--days", "7"], self.env)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        record = json.loads(self.gov("trials", "new-gate.json").read_text(
            encoding="utf-8"))
        self.assertEqual(record["stem"], "new-gate")
        self.assertGreater(record["end"], record["start"])

        # Make it due, then journal what the gate saw during the window.
        self.due_trial("new-gate", start_days=8, end_days=1)
        self.journal([
            {"ts": iso(5), "hook": "new-gate", "result": "observe", "why": "caught-a"},
            {"ts": iso(3), "hook": "new-gate", "result": "observe", "why": "caught-b"},
            {"ts": iso(3), "hook": "new-gate", "result": "pass", "why": "not-a-catch"},
            {"ts": iso(3), "hook": "other-gate", "result": "observe", "why": "not-mine"},
            {"ts": iso(0), "hook": "new-gate", "result": "observe", "why": "after-the-end"},
        ])
        closed = run(TRIAL, [], self.env)
        self.assertEqual(closed.returncode, 0, closed.stderr)
        review = self.gov("awaiting-operator", "trial-new-gate.md").read_text(
            encoding="utf-8")
        self.assertIn("Would have blocked: 2 time(s).", review)
        self.assertIn("caught-a", review)
        self.assertIn("caught-b", review)
        self.assertNotIn("not-a-catch", review)      # only `observe` counts
        self.assertNotIn("not-mine", review)         # only THIS gate
        self.assertNotIn("after-the-end", review)    # only inside the window
        self.assertIn("**Your word: arm / discard**", review)
        self.assertTrue(self.gov("trials", "new-gate.json.closed").exists())
        self.assertFalse(self.gov("trials", "new-gate.json").exists())
        self.assertEqual(self.ledger()[-1]["status"], "trial-closed")
        self.assertEqual(self.ledger()[-1]["type"], "gate-trial")

    def test_t16_a_running_trial_is_left_alone_and_a_closed_one_is_not_redone(self):
        self.due_trial("running-gate", start_days=1, end_days=-6)   # ends later
        first = run(TRIAL, [], self.env)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("nothing due", first.stdout)
        self.assertTrue(self.gov("trials", "running-gate.json").exists())
        self.assertFalse(self.gov("awaiting-operator").exists())

        self.due_trial("done-gate", start_days=8, end_days=1)
        run(TRIAL, [], self.env)
        self.assertTrue(self.gov("awaiting-operator", "trial-done-gate.md").exists())
        again = run(TRIAL, [], self.env)
        self.assertIn("nothing due", again.stdout)

    def test_t17_an_unreadable_trial_file_is_left_in_place(self):
        """Never destroy what cannot be read: a corrupt trial file is reported
        and kept, so the human can look at it."""
        path = self.gov("trials", "broken-gate.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        r = run(TRIAL, [], self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unreadable", r.stdout)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
