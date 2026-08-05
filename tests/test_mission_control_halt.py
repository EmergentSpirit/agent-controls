#!/usr/bin/env python3
"""Tests for mission-control/halt.py -- the optional halt module.

Zero network beyond a loopback socket the test opens itself. Every path the
module touches is redirected into a tempdir through the environment, so a run
can never reach the operator's real state directory: the whole point of this
module is a flag file, and a suite that writes the real one would pause the
real engine.

The module is loaded BY PATH, the way server.py loads it, and reloaded from
scratch where a test needs to prove the state lives on the disk rather than in
this process.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(os.path.dirname(HERE), "mission-control")
HALT_PY = os.path.join(PANEL, "halt.py")


def load_panel_config():
    """Install THIS panel's config under the bare name `config`.

    halt.py does `import config`, which resolves through sys.path. Another
    module in this repo also ships a `config.py`, so whichever suite ran first
    would win and the panel would quietly wire itself to a different module's
    configuration. Loading by path and pinning sys.modules makes the order of
    the suites irrelevant.
    """
    current = sys.modules.get("config")
    if str(getattr(current, "__file__", "") or "").startswith(str(PANEL)):
        return current
    spec = importlib.util.spec_from_file_location(
        "config", os.path.join(PANEL, "config.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["config"] = module
    spec.loader.exec_module(module)
    return module


def load_panel_module(name):
    """Load one of THIS panel's modules by path, pinning sys.modules.

    Same reason as load_panel_config: server.py pulls in config, store, ingest,
    fleet and approvals by bare name, and another module in this repo ships
    files with those names. Whichever suite ran first would otherwise decide
    which code this test exercises.
    """
    load_panel_config()
    current = sys.modules.get(name)
    if str(getattr(current, "__file__", "") or "").startswith(str(PANEL)):
        return current
    for dep in ("store", "ingest", "fleet", "approvals", name):
        have = sys.modules.get(dep)
        if str(getattr(have, "__file__", "") or "").startswith(str(PANEL)):
            continue
        spec = importlib.util.spec_from_file_location(
            dep, os.path.join(PANEL, dep + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[dep] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def load_halt(name="mc_halt_under_test"):
    """Load halt.py exactly as server.optional() does: by path, into a fresh
    module object. Calling it twice is how a test simulates a panel restart."""
    load_panel_config()
    spec = importlib.util.spec_from_file_location(name, HALT_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(root):
    """Every path under `root`, with the size of each file. A snapshot fine
    enough that "this call mutated nothing" is a real assertion and not a
    hopeful one."""
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs:
            out[os.path.relpath(os.path.join(base, name), root)] = "dir"
        for name in files:
            path = os.path.join(base, name)
            out[os.path.relpath(path, root)] = os.path.getsize(path)
    return out


class HaltCase(unittest.TestCase):
    """Shared fixture: a tempdir state directory and a freshly loaded module."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-halt-")
        self._env = dict(os.environ)
        os.environ.update({
            "HARNESS_STATE_DIR": self.tmp,
            "HARNESS_MC_HALT_FLAG": os.path.join(self.tmp, "executor", "halt"),
            "HARNESS_MC_HALT_TOKEN": os.path.join(self.tmp, "halt-token"),
            "HARNESS_MC_DB": os.path.join(self.tmp, "events.db"),
            "HARNESS_MC_HMAC_KEY": os.path.join(self.tmp, "hmac.key"),
            "HARNESS_GATE_STATS": os.path.join(self.tmp, "gate-stats.jsonl"),
        })
        self.halt = load_halt()
        self.flag = os.environ["HARNESS_MC_HALT_FLAG"]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers ------------------------------------------------------------

    def token(self):
        return self.halt.load_token()

    def two_step(self, role=None, reason="", token=None):
        """The honest gesture, both halves of it."""
        token = self.token() if token is None else token
        first = self.halt.request(role, token)
        self.assertTrue(first.get("ok"), first)
        return self.halt.commit({"role": role, "token": token,
                                 "confirm_token": first["confirm_token"],
                                 "issued_at": first["issued_at"],
                                 "reason": reason})

    def events(self):
        db = os.environ["HARNESS_MC_DB"]
        if not os.path.isfile(db):
            return []
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT type, summary, refs FROM events "
                                "ORDER BY id").fetchall()
        finally:
            conn.close()
        return [{"type": r[0], "summary": r[1], "refs": json.loads(r[2])}
                for r in rows]

    def journalled_events(self):
        return [row["refs"].get("event") for row in self.events()]


class TestTwoStepGesture(HaltCase):
    """The gesture is in two steps, and one step alone does nothing."""

    def test_two_steps_pause_the_engine(self):
        result = self.two_step(reason="maintenance window")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["role"], "executor")
        self.assertTrue(result["engine"])
        self.assertTrue(os.path.isfile(self.flag))
        state = self.halt.status()
        self.assertTrue(state["paused"])
        self.assertEqual(state["reason"], "maintenance window")

    def test_request_alone_halts_nothing(self):
        """Step one is the step that can be fired by accident, so step one is
        the step that must be inert."""
        before = tree(self.tmp)
        first = self.halt.request(None, self.token())
        self.assertTrue(first["ok"], first)
        self.assertIn("confirm_token", first)
        self.assertFalse(os.path.exists(self.flag))
        self.assertFalse(self.halt.status()["paused"])
        # The only files a request may leave behind are this module's own token
        # and its journal. No flag, anywhere.
        created = set(tree(self.tmp)) - set(before)
        self.assertFalse([p for p in created if "halt" in os.path.basename(p)
                          and "token" not in p],
                         "a request created a halt flag: %s" % sorted(created))

    def test_commit_without_a_request_is_refused(self):
        """A caller who skips step one has no confirm token to present, and a
        plausible-looking one is not enough: it is an HMAC under the secret."""
        result = self.halt.commit({"role": "executor", "token": self.token(),
                                   "confirm_token": "0" * 32,
                                   "issued_at": int(time.time())})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid confirm token")
        self.assertFalse(os.path.exists(self.flag))
        self.assertFalse(self.halt.status()["paused"])

    def test_confirm_token_is_bound_to_the_role(self):
        """Confirming a halt for one role must not commit a halt for another,
        or the confirmation sentence on screen would be describing the wrong
        target."""
        first = self.halt.request("builder", self.token())
        result = self.halt.commit({"role": "executor", "token": self.token(),
                                   "confirm_token": first["confirm_token"],
                                   "issued_at": first["issued_at"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid confirm token")
        self.assertFalse(os.path.exists(self.flag))

    def test_confirm_token_expires(self):
        """A tab left open overnight confirms nothing."""
        first = self.halt.request(None, self.token())
        stale = first["issued_at"] - self.halt.CONFIRM_TTL_S - 1
        result = self.halt.commit({
            "role": None, "token": self.token(),
            "confirm_token": self.halt._confirm_token("executor", stale,
                                                      self.token()),
            "issued_at": stale})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "confirm token expired")
        self.assertFalse(os.path.exists(self.flag))

    def test_malformed_issued_at_is_refused(self):
        first = self.halt.request(None, self.token())
        result = self.halt.commit({"role": None, "token": self.token(),
                                   "confirm_token": first["confirm_token"],
                                   "issued_at": "not-a-number"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid issued_at")
        self.assertFalse(os.path.exists(self.flag))


class TestTokenGate(HaltCase):
    """The module owns its own gate, and it fails toward refusing."""

    def test_wrong_token_is_refused_at_both_steps(self):
        first = self.halt.request(None, "not-the-token")
        self.assertFalse(first["ok"])
        self.assertEqual(first["error"], "invalid token")

        good = self.halt.request(None, self.token())
        result = self.halt.commit({"role": None, "token": "not-the-token",
                                   "confirm_token": good["confirm_token"],
                                   "issued_at": good["issued_at"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid token")
        self.assertFalse(os.path.exists(self.flag))

    def test_empty_token_never_matches(self):
        """An empty presented value must not compare equal to anything, which
        is the shape of a client that simply forgot to send the field."""
        for empty in (None, "", 0):
            self.assertFalse(self.halt.request(None, empty)["ok"])
        self.assertFalse(os.path.exists(self.flag))

    def test_token_file_is_private(self):
        """Mode 600, created on demand. The one lever that stops the fleet is
        not readable by every account on the machine."""
        path = self.halt.token_file()
        self.assertFalse(os.path.exists(path))
        self.token()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_token_survives_and_is_not_regenerated(self):
        first = self.token()
        self.assertEqual(first, load_halt("mc_halt_second").load_token())


class TestFlagOnDisk(HaltCase):
    """The halt is a file, not a variable in the panel's memory."""

    def test_flag_survives_a_panel_restart(self):
        """A panel that restarts must not release a halt somebody set an hour
        ago. Loading the module from scratch is exactly that restart."""
        self.assertTrue(self.two_step(reason="overnight")["ok"])
        restarted = load_halt("mc_halt_after_restart")
        state = restarted.status()
        self.assertTrue(state["paused"])
        self.assertEqual(state["reason"], "overnight")
        self.assertEqual(state["flag"], self.flag)

    def test_a_flag_written_by_hand_is_a_halt(self):
        """Presence is the state. Content is only the explanation, and an
        unreadable one must never read as "not paused"."""
        os.makedirs(os.path.dirname(self.flag), exist_ok=True)
        with open(self.flag, "w", encoding="utf-8") as fh:
            fh.write("halted by hand, not json")
        self.assertTrue(self.halt.status()["paused"])

    def test_halting_a_role_leaves_the_engine_flag_alone(self):
        """Stopping one worker must never stop the fleet by side effect."""
        result = self.two_step(role="builder")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["engine"])
        self.assertTrue(result["advisory"])
        self.assertTrue(os.path.isfile(result["flag"]))
        self.assertFalse(os.path.exists(self.flag))
        state = self.halt.status()
        self.assertFalse(state["paused"])
        self.assertIn("builder", state["roles"])
        self.assertTrue(state["roles"]["builder"]["advisory"])

    def test_a_role_name_cannot_choose_a_path(self):
        """The target arrives in a request body. It gets to name a flag, never
        a location."""
        result = self.two_step(role="../../etc/passwd")
        self.assertTrue(result["ok"], result)
        written = os.path.realpath(result["flag"])
        self.assertTrue(written.startswith(os.path.realpath(self.tmp)), written)
        self.assertEqual(os.path.basename(written), "etcpasswd")

    def test_release_clears_the_flag_and_is_not_a_panel_route(self):
        self.assertTrue(self.two_step()["ok"])
        released = self.halt.release(reason="engine back")
        self.assertTrue(released["ok"])
        self.assertTrue(released["removed"])
        self.assertFalse(self.halt.status()["paused"])
        # Releasing is the dangerous direction: the panel routes status,
        # request and commit, and nothing that resumes an engine.
        with open(os.path.join(PANEL, "server.py"), encoding="utf-8") as fh:
            server_src = fh.read()
        self.assertNotIn("release", server_src.split("OPTIONAL_MODULES")[0])


class TestStatusIsAPureRead(HaltCase):
    def test_status_mutates_nothing(self):
        """It answers for a caller holding no token at all, so it may not
        create the token file, the journal, or a directory on the way."""
        before = tree(self.tmp)
        state = self.halt.status()
        self.assertFalse(state["paused"])
        self.assertTrue(state["module"])
        self.assertEqual(tree(self.tmp), before)
        self.assertFalse(os.path.exists(self.halt.token_file()))

    def test_status_mutates_nothing_while_paused(self):
        self.assertTrue(self.two_step(role="builder")["ok"])
        self.assertTrue(self.two_step()["ok"])
        before = tree(self.tmp)
        for _ in range(3):
            self.assertTrue(self.halt.status()["paused"])
        self.assertEqual(tree(self.tmp), before)

    def test_status_agrees_with_the_core_fallback(self):
        """With no module installed, server.py reads the flag itself. The two
        answers have to mean the same thing, or the panel contradicts itself
        depending on which files an operator copied."""
        core_config = load_panel_config()
        self.assertEqual(self.halt.status()["flag"], core_config.halt_flag())
        self.assertTrue(self.two_step()["ok"])
        self.assertEqual(self.halt.status()["paused"],
                         os.path.exists(core_config.halt_flag()))


class TestJournal(HaltCase):
    """Every outcome lands in the signed log, refusals included."""

    def test_every_outcome_is_journalled(self):
        self.halt.request(None, "wrong")                      # refused request
        first = self.halt.request(None, self.token())         # accepted request
        self.halt.commit({"role": None, "token": self.token(),
                          "confirm_token": "0" * 32,
                          "issued_at": first["issued_at"]})   # refused commit
        result = self.halt.commit({"role": None, "token": self.token(),
                                   "confirm_token": first["confirm_token"],
                                   "issued_at": first["issued_at"],
                                   "reason": "power work"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["journaled"])
        self.halt.release(reason="done")

        seen = self.journalled_events()
        for expected in ("halt-refused", "halt-requested", "halt-committed",
                         "halt-released"):
            self.assertIn(expected, seen, seen)
        self.assertEqual(seen.count("halt-refused"), 2, seen)
        self.assertTrue(all(row["type"] == "halt" for row in self.events()))
        committed = [row for row in self.events()
                     if row["refs"].get("event") == "halt-committed"][0]
        self.assertIn("power work", committed["summary"])

    def test_the_journal_is_signed_and_append_only(self):
        """The rows this module writes get the same treatment as ingested ones:
        the store refuses to rewrite them and the signature covers them."""
        self.assertTrue(self.two_step(reason="checked")["ok"])
        sys.path.insert(0, PANEL)
        import store as core_store
        store = core_store.open_store()
        try:
            self.assertTrue(store.verify_all()["ok"])
            with self.assertRaises(sqlite3.DatabaseError):
                store.conn.execute("UPDATE events SET summary='rewritten'")
        finally:
            store.close()

    def test_a_broken_journal_does_not_lose_the_halt(self):
        """Fail-soft on the trace, never on the act -- and never silently: the
        result says the trace was lost instead of claiming a clean record."""
        os.environ["HARNESS_MC_DB"] = os.path.join(self.tmp, "nope", "x.db")
        os.makedirs(os.path.join(self.tmp, "nope"), exist_ok=True)
        with open(os.path.join(self.tmp, "nope", "x.db"), "w") as fh:
            fh.write("this is not a database")
        result = self.two_step(reason="journal down")
        self.assertTrue(result["ok"], result)
        self.assertTrue(os.path.isfile(self.flag))
        self.assertFalse(result["journaled"])


class TestThroughThePanel(HaltCase):
    """The contract, exercised the way the panel actually calls it."""

    def setUp(self):
        super().setUp()
        server_mod = load_panel_module("server")
        self.server_mod = server_mod
        server_mod._LOADED.pop("halt", None)          # load ours, not a cached one
        self.httpd = server_mod.build_server(port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.server_mod._LOADED.pop("halt", None)
        super().tearDown()

    def call(self, path, payload=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_panel_drives_both_steps_and_never_the_gate(self):
        token = self.token()
        code, refused = self.call("/api/halt", {"stage": "request",
                                                "role": "executor",
                                                "token": "wrong"})
        self.assertEqual(code, 400, refused)
        self.assertEqual(refused["error"], "invalid token")

        code, first = self.call("/api/halt", {"stage": "request",
                                              "role": "executor",
                                              "token": token})
        self.assertEqual(code, 200, first)
        code, state = self.call("/api/halt")
        self.assertEqual(code, 200)
        self.assertFalse(state["paused"], "step one halted the engine")

        code, done = self.call("/api/halt", {
            "stage": "commit", "role": "executor", "token": token,
            "confirm_token": first["confirm_token"],
            "issued_at": first["issued_at"], "reason": "via the panel"})
        self.assertEqual(code, 200, done)
        self.assertTrue(os.path.isfile(self.flag))

        code, state = self.call("/api/halt")
        self.assertTrue(state["paused"])
        self.assertTrue(state["module"], "the panel answered from the flag, "
                                         "not from the module")
        self.assertEqual(state["reason"], "via the panel")


if __name__ == "__main__":
    unittest.main()
