#!/usr/bin/env python3
"""Tests for the 3-layer shield (shield-inject + shield-reviewer).

Zero network: the judge's verdict is mocked through HARNESS_SHIELD_FAKE_VERDICT,
so no agent CLI is ever launched. Both hooks run as subprocesses with the JSON
payload on stdin, exactly like under the real harness; the state directory and
the gate-stats journal are isolated in a tempdir.

The wiring class is the one that hurts on purpose: unwire either layer from the
example settings and it FAILS. A gate that is no longer wired is a dead gate
that lies about being alive.
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SHIELD = ROOT / "shield"
INJECT = SHIELD / "shield-inject.py"
REVIEWER = SHIELD / "shield-reviewer.py"
LAUNCHERS = ROOT / "launchers"


def run_hook(script: Path, args, payload, env_extra, raw_stdin=None):
    env = dict(os.environ, **env_extra)
    data = raw_stdin if raw_stdin is not None else json.dumps(payload)
    return subprocess.run([sys.executable, str(script)] + list(args),
                          input=data, capture_output=True, text=True,
                          env=env, timeout=60)


def registry_module():
    """shield/_registry.py loaded by path (the shield dir is not a package)."""
    spec = importlib.util.spec_from_file_location("shield_registry",
                                                  SHIELD / "_registry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ShieldBase(unittest.TestCase):
    """One tempdir per test: state directory (holding the marker) and journal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.stats = self.state / "gate-stats.jsonl"
        self.env = {"HARNESS_STATE_DIR": str(self.state),
                    "HARNESS_GATE_STATS": str(self.stats)}
        for var in ("HARNESS_SHIELD_REGISTRY", "HARNESS_SHIELD_RUBRIC",
                    "HARNESS_SHIELD_FAKE_VERDICT", "HARNESS_AGENT"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.tmp.cleanup()

    def marker(self, agent="builder") -> Path:
        return self.state / "shield" / ("%s-trigger.json" % agent)

    def last_stat(self) -> dict:
        lines = self.stats.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1])

    def transcript(self, text: str) -> str:
        path = self.state / "transcript.jsonl"
        path.write_text(json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": text}]}}) + "\n", encoding="utf-8")
        return str(path)

    def arm(self, agent="builder", session="s9", rules=None, ts=None):
        path = self.marker(agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "session_id": session,
            "ts": time.time() if ts is None else ts,
            "rules": rules if rules is not None else ["a-locating-question-gets-a-location"],
        }), encoding="utf-8")


class TestShieldInject(ShieldBase):
    """Layer 1: injects at the moment of risk, arms layer 3, never blocks."""

    def test_t1_trigger_injects_the_rule_and_arms_the_reviewer(self):
        r = run_hook(INJECT, ["--agent", "builder"],
                     {"prompt": "where is the deploy config?",
                      "session_id": "s1"}, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[shield]", r.stdout)
        self.assertIn("Locating question", r.stdout)
        marker = json.loads(self.marker().read_text(encoding="utf-8"))
        self.assertEqual(marker["session_id"], "s1")
        self.assertTrue(marker["rules"])
        stat = self.last_stat()
        self.assertEqual(stat["result"], "warn")
        self.assertTrue(stat["armed"])

    def test_t2_neutral_prompt_stays_silent_and_arms_nothing(self):
        r = run_hook(INJECT, ["--agent", "builder"],
                     {"prompt": "summarize the meeting notes",
                      "session_id": "s1"}, self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.last_stat()["result"], "pass")

    def test_t3_unreadable_registry_is_silence_not_an_error(self):
        env = dict(self.env, HARNESS_SHIELD_REGISTRY="/nonexistent/registry.yaml")
        r = run_hook(INJECT, ["--agent", "builder"],
                     {"prompt": "where is the deploy config?",
                      "session_id": "s1"}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertFalse(self.marker().exists())

    def test_t4_corrupt_stdin_fails_open(self):
        r = run_hook(INJECT, ["--agent", "builder"], None, self.env,
                     raw_stdin="this is not json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_t5_kill_switch_is_logged_never_silent(self):
        env = dict(self.env, HARNESS_SHIELD_INJECT_GATE_DISABLE="1")
        r = run_hook(INJECT, ["--agent", "builder"],
                     {"prompt": "where is the deploy config?",
                      "session_id": "s1"}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertEqual(self.last_stat()["result"], "skip-disabled")


class TestShieldReviewer(ShieldBase):
    """Layer 3: judges only when armed, blocks before display, fails open."""

    def test_t6_not_armed_never_judges(self):
        env = dict(self.env, HARNESS_SHIELD_FAKE_VERDICT='{"violation": true}')
        r = run_hook(REVIEWER, ["--agent", "builder"],
                     {"session_id": "s9",
                      "transcript_path": self.transcript("anything")}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-not-armed")

    def test_t7_loop_guard_stop_hook_active(self):
        self.arm()
        env = dict(self.env, HARNESS_SHIELD_FAKE_VERDICT='{"violation": true}')
        r = run_hook(REVIEWER, ["--agent", "builder"],
                     {"session_id": "s9", "stop_hook_active": True,
                      "transcript_path": self.transcript("anything")}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-loop-guard")

    def test_t8_marker_from_another_session_never_judges(self):
        self.arm(session="another-session")
        env = dict(self.env, HARNESS_SHIELD_FAKE_VERDICT='{"violation": true}')
        r = run_hook(REVIEWER, ["--agent", "builder"],
                     {"session_id": "s9",
                      "transcript_path": self.transcript("anything")}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-not-armed")

    def test_t9_stale_marker_is_dropped_not_judged(self):
        self.arm(ts=time.time() - 10_000)
        env = dict(self.env, HARNESS_SHIELD_FAKE_VERDICT='{"violation": true}',
                   HARNESS_SHIELD_FRESHNESS="60")
        r = run_hook(REVIEWER, ["--agent", "builder"],
                     {"session_id": "s9",
                      "transcript_path": self.transcript("anything")}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-stale-marker")
        self.assertFalse(self.marker().exists())

    def test_t10_violation_is_refused_before_display(self):
        self.arm()
        env = dict(self.env, HARNESS_SHIELD_FAKE_VERDICT=json.dumps({
            "violation": True, "rule": "a-locating-question-gets-a-location",
            "excerpt": "while I was in there I also refactored the module"}))
        r = run_hook(REVIEWER, ["--agent", "builder"],
                     {"session_id": "s9", "transcript_path": self.transcript(
                         "It is in config/deploy.yml. While I was in there I "
                         "also refactored the module.")}, env)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("BLOCKED (shield-reviewer gate)", r.stderr)
        self.assertIn("a-locating-question-gets-a-location", r.stderr)
        self.assertIn("while I was in there I also refactored the module",
                      r.stderr)
        stat = self.last_stat()
        self.assertEqual(stat["result"], "block")
        self.assertIn("duration_ms", stat)

    def test_t11_marker_is_single_use(self):
        self.arm()
        env = dict(self.env, HARNESS_SHIELD_FAKE_VERDICT='{"violation": false}')
        path = self.transcript("It is in config/deploy.yml.")
        first = run_hook(REVIEWER, ["--agent", "builder"],
                         {"session_id": "s9", "transcript_path": path}, env)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.last_stat()["result"], "pass")
        self.assertFalse(self.marker().exists())   # consumed
        env["HARNESS_SHIELD_FAKE_VERDICT"] = '{"violation": true}'
        second = run_hook(REVIEWER, ["--agent", "builder"],
                          {"session_id": "s9", "transcript_path": path}, env)
        self.assertEqual(second.returncode, 0, second.stderr)  # no re-judging
        self.assertEqual(self.last_stat()["result"], "skip-not-armed")

    def test_t12_unparsable_verdict_fails_open(self):
        self.arm()
        env = dict(self.env, HARNESS_SHIELD_FAKE_VERDICT="not json at all")
        r = run_hook(REVIEWER, ["--agent", "builder"],
                     {"session_id": "s9",
                      "transcript_path": self.transcript("anything")}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "fail-open")

    def test_t13_kill_switch_is_logged_never_silent(self):
        self.arm()
        env = dict(self.env, HARNESS_SHIELD_FAKE_VERDICT='{"violation": true}',
                   HARNESS_SHIELD_REVIEWER_GATE_DISABLE="1")
        r = run_hook(REVIEWER, ["--agent", "builder"],
                     {"session_id": "s9",
                      "transcript_path": self.transcript("anything")}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_stat()["result"], "skip-disabled")


class TestRegistryLoader(unittest.TestCase):
    """The registry parses with PyYAML and, identically, without it."""

    def test_t14_example_registry_has_active_triggers(self):
        mod = registry_module()
        triggers = mod.load_triggers()
        self.assertGreaterEqual(len(triggers), 5)
        active = mod.active_triggers()
        self.assertTrue(active)
        self.assertTrue(all(t.get("pattern") and t.get("rule") for t in active))
        self.assertLess(len(active), len(triggers),
                        "the example must show a parked entry (active: false)")

    def test_t15_fallback_parser_matches_pyyaml(self):
        """CI installs pytest and nothing else: the registry must still load
        when PyYAML is absent."""
        mod = registry_module()
        text = Path(mod.registry_path()).read_text(encoding="utf-8")
        fallback = mod._mini_yaml(text)
        self.assertEqual(fallback, mod.load_triggers())
        self.assertIsInstance(fallback[0]["active"], bool)


class TestShieldWiring(unittest.TestCase):
    """Unwire a layer from the example settings and this class FAILS: a gate
    that is no longer wired is a dead gate that lies about being alive."""

    def settings(self, name: str) -> str:
        return (LAUNCHERS / name).read_text(encoding="utf-8")

    def test_t16_layer1_wired_on_user_prompt_submit(self):
        for name in ("settings.example.json",
                     "settings.researcher.example.json"):
            data = json.loads(self.settings(name))
            wired = json.dumps(data["hooks"].get("UserPromptSubmit", []))
            self.assertIn("shield/shield-inject.py", wired,
                          "layer 1 UNWIRED from %s" % name)

    def test_t17_layer3_wired_on_stop(self):
        for name in ("settings.example.json",
                     "settings.researcher.example.json"):
            data = json.loads(self.settings(name))
            wired = json.dumps(data["hooks"].get("Stop", []))
            self.assertIn("shield/shield-reviewer.py", wired,
                          "layer 3 UNWIRED from %s" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
