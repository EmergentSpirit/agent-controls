#!/usr/bin/env python3
"""Tests for mission-control: the live fleet panel and its signed event log.

ZERO network, ZERO third-party dependency, ZERO provider, ZERO key. The only
socket this suite ever opens is a loopback one, opened by the panel itself on
an ephemeral port -- which is also how it proves the bind is 127.0.0.1 and
nothing else. `crontab` and `systemctl` are never really run: the schedule view
is driven through a stand-in for the subprocess module, so the test says the
same thing on a laptop with a full crontab and on a CI runner with no cron
daemon at all.

Every test isolates HARNESS_STATE_DIR, HARNESS_GATE_STATS, the event database,
the signing key, the journals the panel reads and the operator-script allowlist
inside a tempdir. The database is DERIVED and disposable by design, so a test
rebuilds one from forged journals in a few milliseconds and no `.db` file ever
lands in the repository.

The guard gets as much room here as the features, and deliberately: the panel
is unauthenticated and serves command lines, pane titles and pending
operations. A loopback bind stops a stranger on the network. It stops neither
DNS rebinding nor a CSRF POST from the operator's own browser, and it trusts
the entire network behind a reverse proxy.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MC = ROOT / "mission-control"

# The published source files, named one by one on purpose: a test that globs
# `*.py` would also read whatever a previous test deposited in that directory.
SHIPPED_PY = ("server.py", "config.py", "store.py", "ingest.py", "fleet.py",
              "approvals.py")

# Loaded as a BLOCK, in dependency order. The directory name carries a hyphen,
# so nothing here is importable by name, and the modules import each other
# under PLAIN names (`import config as C`). Another suite in the same pytest
# process registers its own `config` and `server` under those same names, so a
# stale sys.modules entry would quietly wire this panel to another module's
# configuration. Reloading the whole block keeps that impossible.
MC_MODULES = ("config", "store", "ingest", "fleet", "approvals", "server")

# One frame of the braille animation an agent CLI paints while it is working.
# Written as a codepoint so this file stays pure ASCII, exactly as fleet.py
# writes its own table.
SPINNER = chr(0x2801)

LOG_ROTATION = "2026-01-04T0930-safe-deploy-log-rotation.sh"
MIGRATION = "2026-01-05T1100-migrate-database-columns.sh"
SILENT = "2026-01-06T0800-chore-silent-script.sh"
SETTLED = "2026-01-07T0900-restart-web-server.sh"

SCRIPT_WITH_HEADER = (
    "#!/bin/sh\n"
    "# Description: rotates the log files on the build host\n"
    "# Scope: the build host only\n"
    "# Class: maintenance\n"
    "# Reversible: yes\n"
    "# Rollback: restore the previous directory from the dated tar\n"
    "echo rotating\n")

SCRIPT_NOT_REVERSIBLE = (
    "#!/bin/sh\n"
    "# Description: drops two columns from the events table\n"
    "# Class: migration\n"
    "# Reversible: no\n"
    "echo migrating\n")

SCRIPT_NO_HEADER = "#!/bin/sh\necho nothing to declare\n"

# A line from /etc/passwd. The confinement tests point a forged journal entry
# at that file and then assert this shape never comes back through the API: a
# path arrives inside a journal the panel cannot verify, and reading it
# unconfined turns the panel into a file-disclosure primitive.
PASSWD_SHAPE = "root:x:0:0"

# TEST STUBS for the optional modules. `optional()` loads them BY PATH, from
# the directory the panel lives in and nowhere else -- which is the point: a
# capability is a file an operator installs, never a flag. So proving both
# states means controlling that directory, and the suite does it by serving a
# COPY of the module from a tempdir. It never writes into mission-control/:
# an operator who has really installed halt.py must still see this suite pass,
# and no test may quietly overwrite the module that pauses their engine.
STUB_MARKER = "TEST STUB, loaded from a copied checkout"

HALT_STUB = '''"""%s"""


def status():
    return {"ok": True, "paused": True, "module": "stub"}


def request(role, token):
    return {"ok": True, "stage": "request", "role": role,
            "confirm_token": "confirm-1"}


def commit(body):
    """One positional argument: the request body.

    Pinned by the suite because server._dispatch_optional calls it exactly
    that way, and an adopter writing this module from a stale signature gets a
    TypeError on the one path that pauses their execution engine."""
    return {"ok": True, "stage": "commit", "reason": body.get("reason", "")}
''' % STUB_MARKER

APPROVE_STUB = '''"""%s"""


def record(script, sha256, approved_by):
    return {"ok": True, "script": script, "sha256": sha256,
            "approved_by": approved_by}
''' % STUB_MARKER


def ts(seconds_ago=0, days_ago=0):
    """ISO-8601 UTC, in the shape the journals and the window filter use."""
    stamp = (datetime.now(timezone.utc)
             - timedelta(seconds=seconds_ago, days=days_ago))
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def short(body, width=180):
    """A refused request that WAS NOT refused answers with the whole log. That
    is the evidence, and it is also four kilobytes of JSON in a CI log, so a
    failure message carries its head and says how much it cut."""
    body = str(body)
    if len(body) <= width:
        return body
    return "%s... [%d more characters]" % (body[:width], len(body) - width)


def free_port():
    """An ephemeral port, released before the panel takes it. Used only where
    the panel has to be a real subprocess (the startup banner)."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def mission_control():
    """The six modules of the panel, in MC_MODULES order."""
    ours = all(
        str(getattr(sys.modules.get(name), "__file__", "") or "").startswith(str(MC))
        for name in MC_MODULES)
    if not ours:
        for name in MC_MODULES:
            spec = importlib.util.spec_from_file_location(name, MC / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
    return tuple(sys.modules[name] for name in MC_MODULES)


class FakeProc:
    """What subprocess.run returns, reduced to what server.py reads."""

    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class FakeSubprocess:
    """Stand-in for the subprocess module inside server.py.

    The schedule view shells out to `crontab` and `systemctl`. Running those
    for real would make the test read the machine it happens to run on, which
    is the opposite of a test. `table` maps argv[0] to a FakeProc or to an
    exception to raise."""

    SubprocessError = subprocess.SubprocessError
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, table):
        self.table = table
        self.calls = []

    def run(self, argv, **_kwargs):
        self.calls.append(list(argv))
        answer = self.table.get(argv[0])
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise OSError("no such binary in this fixture: %s" % argv[0])
        return answer


class MissionControlBase(unittest.TestCase):
    """One tempdir per test: state directory, database, key, journals,
    operator-script allowlist. Nothing reaches the real ~/.harness."""

    VARS = ("HARNESS_STATE_DIR", "HARNESS_GATE_STATS", "HARNESS_MC_PORT",
            "HARNESS_MC_DB", "HARNESS_MC_HMAC_KEY", "HARNESS_MC_PROGRESS_DIRS",
            "HARNESS_MC_EXECUTOR_AUDIT", "HARNESS_MC_HALT_FLAG",
            "HARNESS_MC_ROLES", "HARNESS_MC_ROSTER", "HARNESS_MC_READ_TOKEN",
            "HARNESS_MC_WINDOW_DAYS", "HARNESS_MC_INGEST_INTERVAL",
            "HARNESS_OPERATOR_SCRIPT_DIRS", "HARNESS_LLM_CLI_NAMES")

    # Role names nothing on a developer's machine can accidentally match. The
    # roster is DISCOVERED from the multiplexer's pane titles, so a role called
    # `builder` would make this suite depend on what the person running it
    # happens to have open.
    ROLE_ONE = "zzz-fixture-role-one"
    ROLE_TWO = "zzz-fixture-role-two"

    def setUp(self):
        self.saved = {key: os.environ.pop(key, None) for key in self.VARS}
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / "state"
        self.state.mkdir()
        self.stats = self.state / "gate-stats.jsonl"
        self.progress = base / "progress"
        self.progress.mkdir()
        self.audit = base / "executor" / "audit.jsonl"
        self.audit.parent.mkdir(parents=True)
        self.halt_flag = base / "executor" / "halt"
        self.scripts = base / "operator-scripts"
        (self.scripts / "nested").mkdir(parents=True)
        self.token_file = base / "read-token"
        self.db_file = base / "events.db"
        self.env = {
            "HARNESS_STATE_DIR": str(self.state),
            "HARNESS_GATE_STATS": str(self.stats),
            "HARNESS_MC_DB": str(self.db_file),
            "HARNESS_MC_HMAC_KEY": str(base / "hmac.key"),
            "HARNESS_MC_PROGRESS_DIRS": str(self.progress),
            "HARNESS_MC_EXECUTOR_AUDIT": str(self.audit),
            "HARNESS_MC_HALT_FLAG": str(self.halt_flag),
            "HARNESS_MC_READ_TOKEN": str(self.token_file),
            "HARNESS_MC_ROLES": self.ROLE_ONE + os.pathsep + self.ROLE_TWO,
            "HARNESS_MC_INGEST_INTERVAL": "0",     # no surprise pass mid-test
            "HARNESS_OPERATOR_SCRIPT_DIRS": str(self.scripts),
            "HARNESS_LLM_CLI_NAMES": "claude",
        }
        os.environ.update(self.env)
        (self.config, self.store, self.ingest, self.fleet, self.approvals,
         self.server) = mission_control()
        # THE READ-ONLY CORE IS WHAT THIS SUITE COVERS, so every test starts
        # from the state of a checkout where no optional module is installed --
        # which is the intended default and the only one where the panel is
        # physically unable to act. Pinning the memo rather than reading the
        # directory means an operator who really installed `halt.py` changes
        # none of these answers, and no test ever touches their file.
        self.server._LOADED.clear()
        self.server._LOADED.update(
            {name: None for name in self.server.OPTIONAL_MODULES})

    def tearDown(self):
        self.server._LOADED.clear()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    # --- fixtures -----------------------------------------------------------

    def script_path(self, name):
        return str(self.scripts / name)

    def write_scripts(self):
        """Three legitimate scripts inside the allowlist, plus one symlink
        pointing straight out of it."""
        (self.scripts / LOG_ROTATION).write_text(SCRIPT_WITH_HEADER,
                                                 encoding="utf-8")
        (self.scripts / MIGRATION).write_text(SCRIPT_NOT_REVERSIBLE,
                                              encoding="utf-8")
        (self.scripts / SILENT).write_text(SCRIPT_NO_HEADER, encoding="utf-8")
        (self.scripts / SETTLED).write_text(SCRIPT_NO_HEADER, encoding="utf-8")
        sneaky = self.scripts / "sneaky.sh"
        if not sneaky.exists():
            os.symlink("/etc/passwd", sneaky)

    def write_progress(self):
        """Two agent journals under two roots, one torn line, one event kind
        this panel has never heard of, and one blocker OUTSIDE the alert
        window. Returns the number of readable lines."""
        first = self.progress / "builder"
        second = self.progress / "researcher"
        first.mkdir()
        second.mkdir()
        rows_a = [
            {"ts": ts(90), "agent": "builder", "session": "sess-a", "seq": 1,
             "project": "panel", "type": "deliverable",
             "summary": "wrote the overview view", "refs": ["server.py"]},
            {"ts": ts(85), "agent": "builder", "session": "sess-a", "seq": 2,
             "project": "panel", "type": "dispatch",
             "summary": "dispatched a review"},
            {"ts": ts(80), "agent": "builder", "session": "sess-a", "seq": 3,
             "project": "panel", "type": "mutation",
             "summary": "edited the unit file"},
            {"ts": ts(75), "agent": "builder", "session": "sess-a", "seq": 4,
             "project": "panel", "type": "decision",
             "summary": "chose sqlite over a daemon"},
            {"ts": ts(70), "agent": "builder", "session": "sess-a", "seq": 5,
             "project": "panel", "type": "blocker",
             "summary": "waiting on the signing key"},
            {"ts": ts(65), "agent": "builder", "session": "sess-a", "seq": 6,
             "project": "panel", "type": "pivot",
             "summary": "changed approach on the ingest"},
            {"ts": ts(60), "agent": "builder", "session": "sess-a", "seq": 7,
             "project": "panel", "type": "health",
             "summary": "the disk is fine"},
            {"ts": ts(55), "agent": "builder", "session": "sess-a", "seq": 8,
             "project": "panel", "type": "telepathy",
             "summary": "an event kind this panel has never heard of"},
            {"ts": ts(days_ago=30), "agent": "builder", "session": "sess-a",
             "seq": 9, "project": "panel", "type": "blocker",
             "summary": "an old blocker, outside the alert window"},
        ]
        rows_b = [
            {"ts": ts(50), "agent": "researcher", "session": "sess-b", "seq": 1,
             "project": "study", "type": "deliverable",
             "summary": "read three papers"},
            {"ts": ts(45), "agent": "researcher", "session": "sess-b", "seq": 2,
             "project": "study", "type": "circuit-break",
             "summary": "the loop was cut"},
            {"ts": ts(40), "agent": "researcher", "session": "sess-b", "seq": 3,
             "project": "study", "type": "halt",
             "summary": "paused by hand"},
        ]
        with (first / "sess-a.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows_a[:6]:
                fh.write(json.dumps(row) + "\n")
            fh.write("{ this line was half written when we read it\n")
            for row in rows_a[6:]:
                fh.write(json.dumps(row) + "\n")
        with (second / "sess-b.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows_b:
                fh.write(json.dumps(row) + "\n")
        # A file that is not a journal at all must be walked past in silence.
        (second / "notes.txt").write_text("not a journal\n", encoding="utf-8")
        return len(rows_a) + len(rows_b)

    def write_audit(self):
        """The execution engine's journal, including four FORGED lines whose
        script path lands outside the operator-script allowlist. Returns the
        number of readable lines."""
        rows = [
            # Wrapped shape (engines that hash-chain their audit), superseded
            # by the next line: latest escalation per script wins.
            {"entry": {"ts": ts(600), "result": "escalated", "action_id": "a1",
                       "script": self.script_path(LOG_ROTATION),
                       "op_class": "deploy", "reason": "first pass"}},
            # Flat shape, and the one that must show up in the queue.
            {"ts": ts(500), "result": "escalated", "action_id": "a2",
             "script": self.script_path(LOG_ROTATION), "op_class": "deploy",
             "reason": "the engine will not run this one alone"},
            # FORGED: an absolute path outside the perimeter.
            {"ts": ts(450), "result": "escalated", "action_id": "a3",
             "script": "/etc/passwd", "reason": "forged, outside"},
            # FORGED: a traversal that starts inside the perimeter.
            {"ts": ts(440), "result": "escalated", "action_id": "a4",
             "script": str(self.scripts) + "/../../../etc/passwd",
             "reason": "forged, traversal"},
            {"ts": ts(430), "result": "escalated-frozen", "action_id": "a5",
             "script": self.script_path(MIGRATION),
             "reason": "a written review is required"},
            # FORGED: a symlink inside the perimeter pointing out of it.
            {"ts": ts(420), "result": "escalated", "action_id": "a6",
             "script": self.script_path("sneaky.sh"),
             "reason": "forged, symlink"},
            # LEGITIMATE and full of dots: a path that bounces through `..`
            # and lands back inside must still be served.
            {"ts": ts(410), "result": "escalated", "action_id": "a7",
             "script": str(self.scripts / "nested" / ".." / SILENT),
             "reason": "no header in this one"},
            {"ts": ts(400), "result": "escalated", "action_id": "a8",
             "script": self.script_path(SETTLED), "reason": "waiting"},
            # ... and then the engine ran it out of band: it leaves the queue.
            {"ts": ts(390), "result": "ran", "action_id": "a9",
             "script": self.script_path(SETTLED)},
            # No action_id: the provenance falls back to the natural identity.
            {"ts": ts(380), "result": "verify-pass",
             "summary": "the nightly verification passed"},
            {"ts": ts(370), "result": "halted", "action_id": "a12",
             "summary": "the engine was paused by hand"},
        ]
        with self.audit.open("w", encoding="utf-8") as fh:
            for row in rows[:9]:
                fh.write(json.dumps(row) + "\n")
            fh.write("{ torn engine line\n")
            for row in rows[9:]:
                fh.write(json.dumps(row) + "\n")
        return len(rows)

    def full_fixture(self):
        self.write_scripts()
        progress = self.write_progress()
        executor = self.write_audit()
        return progress, executor

    # --- helpers ------------------------------------------------------------

    def rows(self, sql, args=()):
        conn = sqlite3.connect(str(self.db_file))
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def counts(self):
        store = self.store.open_store()
        try:
            return store.counts_by_type(), store.count()
        finally:
            store.close()


class ServedPanel(MissionControlBase):
    """A real panel on a real loopback socket, driven with raw HTTP so a test
    can forge the headers a browser would send."""

    def serve(self):
        srv = self.server.build_server(port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def raw(self, port, method, path, headers=None, body=None):
        """(status, body, headers). http.client only adds its own Host header
        when the caller supplied none, which is what lets a test forge it."""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            reply = conn.getresponse()
            return reply.status, reply.read().decode("utf-8"), reply.headers
        finally:
            conn.close()

    def assert_hardened(self, headers, context):
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff", context)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer", context)
        self.assertEqual(headers["Cache-Control"], "no-cache", context)
        policy = headers["Content-Security-Policy"] or ""
        for directive in ("default-src 'self'", "object-src 'none'",
                          "base-uri 'none'"):
            self.assertIn(directive, policy, context)


# --- schema and store -------------------------------------------------------

class TestSchemaAndStore(MissionControlBase):
    """The database is DERIVED. What is published is schema.sql plus the
    ingester that fills it, so anyone rebuilds the same panel from their own
    journals."""

    def test_t1_the_database_is_built_from_the_published_schema(self):
        store = self.store.open_store()
        self.addCleanup(store.close)
        ddl = self.config.schema_sql()

        declared_tables = sorted(part.split("(")[0].strip() for part in
                                 ddl.split("CREATE TABLE IF NOT EXISTS ")[1:])
        built_tables = sorted(row[0] for row in self.rows(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"))
        self.assertEqual(built_tables, declared_tables)
        self.assertEqual(built_tables, ["events"])

        declared_triggers = sorted(part.split("\n")[0].strip() for part in
                                   ddl.split("CREATE TRIGGER IF NOT EXISTS ")[1:])
        built_triggers = sorted(row[0] for row in self.rows(
            "SELECT name FROM sqlite_master WHERE type='trigger'"))
        self.assertEqual(built_triggers, declared_triggers)
        self.assertEqual(built_triggers, ["events_no_delete", "events_no_update"])

        declared_indexes = sorted(part.split(" ")[0].strip() for part in
                                  ddl.split("CREATE INDEX IF NOT EXISTS ")[1:])
        built_indexes = sorted(row[0] for row in self.rows(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"))
        self.assertEqual(built_indexes, declared_indexes)

        columns = [c[1] for c in self.rows("PRAGMA table_info(events)")]
        self.assertEqual(columns, ["id", "ts", "agent", "project", "type",
                                   "summary", "refs", "sig", "src_agent",
                                   "src_session", "src_seq"])

        # The file itself never travels: it holds one operator's fleet history.
        self.assertEqual(list(MC.rglob("*.db")), [],
                         "a database file must never be committed")
        self.assertNotIn(str(MC), self.config.db_path(),
                         "the database lives in the state dir, not in the repo")

    def test_t2_append_only_is_enforced_by_the_engine_and_the_key_is_local(self):
        """Two guarantees answering two different attacks: the triggers cover
        anything going through this connection, the per-row HMAC covers
        whoever goes around it."""
        store = self.store.open_store()
        self.addCleanup(store.close)
        first = store.append(ts=ts(10), agent="builder", project="panel",
                             type="deliverable", summary="wrote a file",
                             src_agent="builder", src_session="s1", src_seq=1)
        self.assertIsInstance(first, int)

        # Same provenance triple: the second insert is a no-op, and that is
        # exactly what makes re-ingesting a journal free.
        again = store.append(ts=ts(10), agent="builder", project="panel",
                             type="deliverable", summary="wrote a file",
                             src_agent="builder", src_session="s1", src_seq=1)
        self.assertIsNone(again)
        self.assertEqual(store.count(), 1)

        for sql in ("UPDATE events SET summary='rewritten' WHERE id=?",
                    "DELETE FROM events WHERE id=?"):
            with self.assertRaises(sqlite3.DatabaseError) as caught:
                store.conn.execute(sql, (first,))
            self.assertIn("append-only", str(caught.exception))
        self.assertEqual(store.count(), 1)

        # An UNKNOWN kind is stored as-is rather than dropped: a panel that
        # discards what it does not recognise lies by omission the day an agent
        # learns a new word.
        store.append(ts=ts(5), agent="builder", project="panel",
                     type="telepathy", summary="a brand new kind",
                     src_agent="builder", src_session="s1", src_seq=2)
        self.assertEqual(store.counts_by_type().get("telepathy"), 1)
        self.assertTrue(store.verify_all()["ok"])

        key = Path(self.config.key_file())
        self.assertTrue(key.is_file(), "the signing key is created on first use")
        self.assertEqual(key.stat().st_mode & 0o777, 0o600,
                         "the key file must never be readable by anyone else")

    def test_t3_re_ingesting_the_same_journals_inserts_nothing(self):
        """Idempotent by construction: every line maps to a provenance triple
        the store holds UNIQUE, so a cron, a button and a hand run in the same
        second insert each line once."""
        progress_lines, executor_lines = self.full_fixture()
        first = self.ingest.run()
        self.assertEqual(first["progress_inserted"], progress_lines)
        self.assertEqual(first["executor_inserted"], executor_lines)
        self.assertEqual(first["total_rows"], progress_lines + executor_lines)
        self.assertEqual(first["total_rows"], 23)

        second = self.ingest.run()
        self.assertEqual(second["progress_inserted"], 0)
        self.assertEqual(second["executor_inserted"], 0)
        self.assertEqual(second["total_rows"], first["total_rows"])

        third = self.ingest.run()
        self.assertEqual(third["total_rows"], first["total_rows"])

        # One torn line costs one line, never the file around it.
        self.assertEqual(len(self.rows(
            "SELECT id FROM events WHERE src_session='sess-a'")), 9)
        # A file that is not a `.jsonl` is walked past in silence.
        self.assertEqual(self.rows(
            "SELECT COUNT(*) FROM events WHERE summary LIKE '%not a journal%'"
        )[0][0], 0)

    def test_t4_an_engine_line_dedups_on_a_pinned_sequence_never_on_a_null(self):
        """An engine line has no sequence of its own, so the triple uses its
        natural identity and pins the sequence to 0. SQLite treats every NULL
        as distinct inside a UNIQUE constraint: left NULL, the dedup would be
        silently off and the whole audit would duplicate on every pass."""
        self.write_scripts()
        self.write_audit()
        self.ingest.run()
        self.ingest.run()
        seqs = {row[0] for row in self.rows(
            "SELECT src_seq FROM events WHERE src_agent='executor'")}
        self.assertEqual(seqs, {0}, "an engine row must not carry a NULL sequence")
        naturals = [row[0] for row in self.rows(
            "SELECT src_session FROM events WHERE src_agent='executor' "
            "ORDER BY src_session")]
        self.assertEqual(len(naturals), len(set(naturals)))
        self.assertIn("a1", naturals)
        # The line with no action_id fell back to its timestamp.
        self.assertTrue([n for n in naturals if n.endswith("Z")],
                        "a line with no id falls back to its natural identity")

        # A result the mapping table does not know becomes a `mutation`, which
        # is the conservative reading, not a dropped row.
        kinds = dict(self.rows(
            "SELECT type, COUNT(*) FROM events WHERE src_agent='executor' "
            "GROUP BY type"))
        self.assertEqual(kinds, {"decision": 7, "mutation": 2, "health": 1,
                                 "halt": 1})

    def test_t5_counting_by_type_does_not_truncate(self):
        """The counter and the listing are two different questions. The listing
        is capped (a panel that reads the whole table into memory is a panel
        that dies on a busy month); the count must not be."""
        self.full_fixture()
        self.ingest.run()
        store = self.store.open_store()
        self.addCleanup(store.close)
        for index in range(300):
            store.append(ts=ts(30), agent="builder", project="panel",
                         type="deliverable", summary="bulk row %d" % index,
                         src_agent="bulk", src_session="bulk", src_seq=index)

        by_type = store.counts_by_type()
        self.assertEqual(store.count(), 323)
        self.assertEqual(sum(by_type.values()), 323,
                         "the per-type counts must add up to the whole table")
        self.assertEqual(by_type["deliverable"], 302)
        self.assertEqual(len(store.query()), 200,
                         "the LISTING is capped; the counter is not")
        # Every kind the panel knows, plus the one it does not, and nothing is
        # rounded away.
        self.assertEqual(by_type, {"deliverable": 302, "dispatch": 1,
                                   "mutation": 3, "decision": 8, "blocker": 2,
                                   "pivot": 1, "health": 2, "telepathy": 1,
                                   "circuit-break": 1, "halt": 2})
        for kind in ("deliverable", "dispatch", "mutation", "decision",
                     "blocker", "pivot", "health", "halt", "circuit-break"):
            self.assertIn(kind, self.config.KNOWN_TYPES)


# --- the five views ---------------------------------------------------------

class TestViews(MissionControlBase):
    """The five questions a person actually asks, answered as pure functions
    with no transport in the way."""

    def setUp(self):
        super().setUp()
        self.full_fixture()
        self.ingest.run()

    def test_t6_the_overview_counts_inside_a_window_and_states_two_verdicts(self):
        data = self.server.api_overview({})
        self.assertEqual(sorted(data), ["agents", "alerts", "by_type", "fleet",
                                        "halt", "integrity", "recent",
                                        "total_events", "window_days"])
        self.assertEqual(data["total_events"], 23)
        self.assertEqual(data["window_days"], 7)
        self.assertEqual(data["agents"], ["builder", "executor", "researcher"])
        # WINDOWED: the 30-day-old blocker is in the log and out of the count.
        self.assertEqual(data["alerts"], {"blockers": 1, "health": 2,
                                          "circuit_break": 1, "halts": 2})
        self.assertEqual(sum(data["by_type"].values()), 22)
        self.assertEqual(self.rows(
            "SELECT COUNT(*) FROM events WHERE type='blocker'")[0][0], 2)
        self.assertTrue(data["integrity"]["ok"])
        self.assertEqual(data["integrity"]["total"], 23)
        self.assertEqual(data["integrity"]["tampered"], [])
        self.assertEqual(len(data["recent"]), 20)
        self.assertEqual(data["fleet"]["total"], 2)

        # "Is the engine paused" is a READ, and it works on a panel that
        # installed no ability to act.
        self.assertFalse(data["halt"]["paused"])
        self.assertIs(data["halt"]["module"], False)
        self.halt_flag.write_text("", encoding="utf-8")
        self.assertTrue(self.server.api_overview({})["halt"]["paused"])

        os.environ["HARNESS_MC_WINDOW_DAYS"] = "3650"
        self.assertEqual(self.server.api_overview({})["alerts"]["blockers"], 2)

    def test_t7_the_roster_is_two_sources_and_says_how_each_answer_was_reached(self):
        """The multiplexer answers "alive", the log answers "what happened".
        Neither can answer both, and the panel never hides which of its answers
        came from a guess."""
        empty = self.server.api_agents({})
        self.assertEqual([entry["role"] for entry in empty["fleet"]],
                         [self.ROLE_ONE, self.ROLE_TWO])
        for entry in empty["fleet"]:
            self.assertFalse(entry["alive"])
            self.assertEqual(entry["state"], "dead")
            self.assertEqual(entry["resolved"], "none")
        self.assertEqual(empty["summary"], {"total": 2, "alive": 0, "working": 0,
                                            "idle": 0, "shell": 0, "dead": 2})

        # Now with a multiplexer answering. The listing command is never really
        # run: what is under test is the classification, not tmux.
        panes = {
            "%s:1.0" % self.ROLE_ONE: {"title": SPINNER + " thinking",
                                       "command": "claude", "active": True,
                                       "pid": "111", "cwd": "/tmp/one"},
            "other:2.0": {"title": self.ROLE_TWO + " seat", "command": "claude",
                          "active": False, "pid": "222", "cwd": "/tmp/two"},
            "other:3.0": {"title": self.ROLE_TWO + " again", "command": "bash",
                          "active": False, "pid": "333", "cwd": "/tmp/three"},
        }
        real = self.fleet.list_panes
        self.fleet.list_panes = lambda: panes
        self.addCleanup(setattr, self.fleet, "list_panes", real)

        store = self.store.open_store()
        store.append(ts=ts(1), agent=self.ROLE_ONE, project="panel",
                     type="deliverable", summary="the roster reads the log too",
                     src_agent=self.ROLE_ONE, src_session="s", src_seq=1)
        store.close()

        live = self.server.api_agents({})
        one, two = live["fleet"]
        self.assertTrue(one["alive"])
        self.assertEqual(one["state"], "working")     # a spinner in the title
        self.assertEqual(one["resolved"], "discovered")
        self.assertEqual(one["cwd"], "/tmp/one")
        self.assertEqual(one["last_event"]["summary"],
                         "the roster reads the log too")
        self.assertEqual(two["state"], "idle")        # alive, waiting for a human
        self.assertEqual(two["pane"], "other:2.0",
                         "first match wins and the pane is then taken")
        self.assertIsNone(two["last_event"])
        self.assertEqual(live["summary"], {"total": 2, "alive": 2, "working": 1,
                                           "idle": 1, "shell": 0, "dead": 0})

        # A pane running something that is not an agent CLI is a shell, not a
        # seat that stopped working.
        self.assertEqual(self.fleet.classify(panes["other:3.0"]), "shell")
        self.assertEqual(self.fleet.classify(None), "dead")

        # A pin that points at an address the multiplexer no longer has must
        # say `none`, never a stale address that looks alive.
        os.environ["HARNESS_MC_ROSTER"] = "%s=%%99.99" % self.ROLE_ONE
        pinned = self.fleet.live_fleet()[0]
        self.assertEqual(pinned["resolved"], "none")
        self.assertFalse(pinned["alive"])

    def test_t8_the_schedule_reads_two_sources_fail_soft_and_independently(self):
        """Everything that will run on this machine without a human present. A
        machine with no cron daemon and no user manager is perfectly normal and
        gets two empty tables, not an error page."""
        crontab = ("# a comment line\n"
                   "0 3 * * * /usr/bin/env backup.sh --now\n"
                   "*/5 * * * * /usr/bin/true\n"
                   "notacronline\n")
        timers = json.dumps([
            {"unit": "nightly.timer", "activates": "nightly.service",
             "next": 1767225600000000, "last": 0},
            {"unit": "broken.timer", "activates": "broken.service",
             "next": "", "last": None}])
        fake = FakeSubprocess({"crontab": FakeProc(0, crontab),
                               "systemctl": FakeProc(0, timers)})
        real = self.server.subprocess
        self.server.subprocess = fake
        self.addCleanup(setattr, self.server, "subprocess", real)

        data = self.server.api_schedule({})
        self.assertEqual(sorted(data), ["count", "crons", "timers",
                                        "timers_count"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["crons"][0], {"schedule": "0 3 * * *",
                                            "command": "/usr/bin/env backup.sh --now"})
        self.assertEqual(data["crons"][1]["schedule"], "*/5 * * * *")
        self.assertEqual(data["timers_count"], 2)
        self.assertEqual(data["timers"][0]["unit"], "nightly.timer")
        self.assertTrue(data["timers"][0]["next"],
                        "epoch MICROseconds must render as a date")
        self.assertEqual(data["timers"][0]["last"], "",
                         "a zero field means never, not 1970")
        self.assertEqual(data["timers"][1]["next"], "")
        self.assertEqual(data["timers"][1]["last"], "")

        # No binary at all, a non-zero exit, unreadable output: three ways to
        # get an empty table and none of them is an exception.
        for table in ({}, {"crontab": FakeProc(1, ""), "systemctl": FakeProc(1, "")},
                      {"crontab": FakeProc(0, ""), "systemctl": FakeProc(0, "{{{")},
                      {"crontab": OSError("boom"),
                       "systemctl": subprocess.SubprocessError("boom")}):
            self.server.subprocess = FakeSubprocess(table)
            empty = self.server.api_schedule({})
            self.assertEqual(empty, {"crons": [], "count": 0, "timers": [],
                                     "timers_count": 0}, repr(table))

    def test_t9_the_logs_view_filters_bounds_its_limit_and_verdicts_each_line(self):
        """An integrity check reported only as a global OK tells you something
        is wrong and never which row, which is the one thing you need."""
        data = self.server.api_logs({})
        self.assertEqual(sorted(data), ["count", "events"])
        self.assertEqual(data["count"], 23)
        self.assertTrue(all(row["sig_valid"] for row in data["events"]))
        self.assertTrue(all("sig" in row and "refs" in row
                            for row in data["events"]))

        self.assertEqual(self.server.api_logs({"agent": ["builder"]})["count"], 9)
        self.assertEqual(self.server.api_logs({"project": ["study"]})["count"], 3)
        self.assertEqual(self.server.api_logs({"type": ["blocker"]})["count"], 2)
        self.assertEqual(
            self.server.api_logs({"search": ["signing key"]})["count"], 1)
        self.assertEqual(self.server.api_logs({"agent": ["nobody"]})["count"], 0)
        self.assertEqual(
            self.server.api_logs({"since_ts": [ts(days_ago=1)]})["count"], 22)

        # A malformed `?limit=` used to raise straight through the handler and
        # drop the connection; an unbounded one read the whole table.
        self.assertEqual(self.server._int_or("abc", 200), 200)
        self.assertEqual(self.server._int_or(None, 200), 200)
        self.assertEqual(self.server._int_or("999999999", 200), 1000)
        self.assertEqual(self.server._int_or("0", 200), 1)
        self.assertEqual(self.server._int_or("-5", 200), 1)
        self.assertEqual(self.server.api_logs({"limit": ["1"]})["count"], 1)
        self.assertEqual(self.server.api_logs({"limit": ["abc"]})["count"], 23)

    def test_t10_the_approvals_queue_is_latest_wins_and_settled_clears(self):
        data = self.server.api_approvals({})
        self.assertTrue(data["engine_available"])
        self.assertIs(data["can_approve"], False)
        self.assertIs(data["presence_available"], False)

        names = sorted(item["name"] for item in data["pending"])
        self.assertEqual(names, ["database columns", "log rotation",
                                 "silent script"])
        by_name = {item["name"]: item for item in data["pending"]}

        rotation = by_name["log rotation"]
        self.assertEqual(rotation["reason"],
                         "the engine will not run this one alone",
                         "the LATEST escalation per script is the one shown")
        self.assertEqual(rotation["op_class"], "deploy")
        self.assertFalse(rotation["frozen"])
        self.assertFalse(rotation["key_gated"])
        self.assertEqual(rotation["sha256"], self.approvals.script_sha256(
            self.script_path(LOG_ROTATION)))
        self.assertEqual(rotation["impact"]["description"],
                         "rotates the log files on the build host")
        self.assertIs(rotation["impact"]["reversible"], True)
        self.assertIn("Reversible: a rollback path is declared",
                      rotation["impact"]["impact"])

        migration = by_name["database columns"]
        self.assertTrue(migration["frozen"], "the engine's call, never softened")
        self.assertIs(migration["impact"]["reversible"], False)
        self.assertIn("NOT reversible", migration["impact"]["impact"])

        # NEVER EMPTY: a queue entry nobody can decide on is an entry nobody
        # decides on, so a script with no header falls back to an honest
        # sentence built from its name.
        silent = by_name["silent script"]
        self.assertIn("silent script", silent["impact"]["description"])
        self.assertIn("read it before approving", silent["impact"]["description"])
        self.assertIn("Assume there is no way back", silent["impact"]["impact"])

        # Settled clears: a script the engine ran out of band must not sit on
        # the panel forever, or the queue stops being read.
        self.assertNotIn("web server", names)

        # No engine at all is a different message from an empty queue.
        self.audit.unlink()
        self.assertEqual(self.server.api_approvals({})["engine_available"], False)
        self.assertEqual(self.server.api_approvals({})["pending"], [])


# --- the panel on a socket --------------------------------------------------

class TestServedPanel(ServedPanel):
    """A loopback bind, five views, the static page, and hardened headers on
    every single answer."""

    def setUp(self):
        super().setUp()
        self.full_fixture()
        self.ingest.run()

    def test_t11_the_five_views_answer_over_a_loopback_socket(self):
        srv = self.server.build_server(port=0)
        self.assertEqual(srv.server_address[0], "127.0.0.1")
        srv.server_close()

        port = self.serve()
        shapes = {
            "/api/overview": ("total_events", "alerts", "integrity", "halt"),
            "/api/agents": ("fleet", "summary"),
            "/api/schedule": ("crons", "count", "timers", "timers_count"),
            "/api/logs": ("events", "count"),
            "/api/approvals": ("pending", "engine_available", "can_approve"),
        }
        for path, keys in shapes.items():
            status, body, headers = self.raw(port, "GET", path)
            self.assertEqual(status, 200, "%s -> %s" % (path, short(body)))
            payload = json.loads(body)
            for key in keys:
                self.assertIn(key, payload, path)
            self.assert_hardened(headers, path)
        self.assertEqual(json.loads(self.raw(port, "GET", "/api/logs")[1])["count"],
                         23)

        for path, marker, ctype in (("/", "<title>", "text/html"),
                                    ("/static/index.html", "<title>", "text/html"),
                                    ("/static/app.js", "textContent",
                                     "application/javascript"),
                                    ("/static/style.css", "{", "text/css")):
            status, body, headers = self.raw(port, "GET", path)
            self.assertEqual(status, 200, "%s -> %s" % (path, status))
            self.assertIn(marker, body)
            self.assertIn(ctype, headers["Content-Type"])
            self.assert_hardened(headers, path)

        # The one mutating route of the read-only core mutates nothing outside
        # the panel: it re-reads the journals into the panel's own database.
        status, body, _headers = self.raw(port, "POST", "/api/ingest")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["ingest"]["progress_inserted"], 0)

    def test_t12_every_answer_carries_the_hardened_headers(self):
        """A 403 and a 404 are normal answers of this panel and they are held
        to the same standard: nosniff, no-referrer, no-cache, strict CSP."""
        port = self.serve()
        cases = (
            (200, "GET", "/api/overview", {}),
            (200, "GET", "/static/app.js", {}),
            (404, "GET", "/api/nope", {}),
            (404, "GET", "/nope", {}),
            (403, "GET", "/api/overview", {"Host": "evil.attacker.example"}),
            (404, "POST", "/api/nope", {}),
        )
        for expected, method, path, headers in cases:
            status, body, got = self.raw(port, method, path, headers)
            self.assertEqual(status, expected, "%s %s -> %s" % (method, path, short(body)))
            self.assert_hardened(got, "%s %s" % (method, path))
            self.assertIn("charset=utf-8", got["Content-Type"])

    def test_t13_the_bind_is_a_constant_and_no_wildcard_lives_in_the_sources(self):
        """A host variable is a thing someone eventually sets to a wildcard
        address "just to test from the laptop", and then leaves that way."""
        self.assertEqual(self.config.HOST, "127.0.0.1")
        sources = [(name, (MC / name).read_text(encoding="utf-8"))
                   for name in SHIPPED_PY]
        sources.append(("schema.sql", (MC / "schema.sql").read_text(encoding="utf-8")))
        for name in ("app.js", "index.html", "style.css"):
            sources.append((name, (MC / "static" / name).read_text(encoding="utf-8")))
        for name, text in sources:
            self.assertNotIn("0.0.0.0", text, "%s carries a wildcard address" % name)
            self.assertNotIn("[::]", text, "%s carries a wildcard address" % name)
            # No third-party endpoint is welded in anywhere: the only absolute
            # URLs in this module are the loopback one it prints about itself.
            for found in re.findall(r"https?://[^\s\"'`)]*", text):
                self.assertTrue(found.startswith("http://127.0.0.1")
                                or found.startswith("http://%s"),
                                "%s welds in the endpoint %r" % (name, found))
        # No environment variable can move the bind: there is none to set.
        self.assertNotIn("HARNESS_MC_HOST",
                         (MC / "config.py").read_text(encoding="utf-8"))

        # And the assertion in build_server is real, not a comment about one.
        real = self.config.HOST
        self.config.HOST = "0.0.0.0"
        try:
            with self.assertRaises(AssertionError) as caught:
                self.server.build_server(port=0)
            self.assertIn("localhost ONLY", str(caught.exception))
        finally:
            self.config.HOST = real

        # Journal content is untrusted text. One raw-HTML assignment in the
        # page turns an agent's summary line into script execution.
        for name in ("app.js", "index.html"):
            text = (MC / "static" / name).read_text(encoding="utf-8")
            for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                              "document.write", "eval("):
                self.assertNotIn(forbidden, text, "%s uses %s" % (name, forbidden))

    def test_t14_a_static_request_never_leaves_the_static_directory(self):
        """A static server that trusts its input is how a local panel becomes a
        file-read primitive."""
        port = self.serve()
        for path in ("/static/../server.py", "/static/../../README.md",
                     "/static/..%2fserver.py", "/static/%2e%2e/server.py",
                     "/static/....//server.py", "/static/config.py",
                     "/static/", "/static/nope.js"):
            status, body, _headers = self.raw(port, "GET", path)
            self.assertEqual(status, 404, "%s -> %s" % (path, status))
            self.assertNotIn("import", body, "%s served a source file" % path)
            self.assertNotIn("HARNESS", body)
        self.assertEqual(self.raw(port, "GET", "/static/app.js")[0], 200,
                         "the legitimate case still works")


# --- the request guard ------------------------------------------------------

class TestRequestGuard(ServedPanel):
    """The bind is not the only defense. Two attacks walk past a loopback
    socket, and both arrive through the operator's own browser."""

    def setUp(self):
        super().setUp()
        self.full_fixture()

    def test_t15_a_foreign_host_header_is_refused_dns_rebinding(self):
        """A name the attacker owns, re-resolved to 127.0.0.1 after the page
        loaded: the socket IS loopback, so the bind is satisfied, and the only
        trace of the attack is the Host header."""
        port = self.serve()
        for host in ("evil.attacker.example", "evil.attacker.example:%d" % port,
                     "192.0.2.5", "panel.internal", "127.0.0.1.evil.example",
                     "localhost.evil.example"):
            status, body, _headers = self.raw(port, "GET", "/api/logs",
                                              {"Host": host})
            self.assertEqual(status, 403, "%s -> %s %s" % (host, status, short(body)))
            self.assertIn("does not name loopback", body)
            self.assertNotIn("signing key", body,
                             "a refused request must leak no event at all")
        self.assertFalse(self.db_file.exists(),
                         "the guard runs BEFORE a single row is read")

        for host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "127.0.0.1",
                     "localhost", "LOCALHOST", "[::1]:%d" % port, "::1"):
            status, body, _headers = self.raw(port, "GET", "/api/logs",
                                              {"Host": host})
            self.assertEqual(status, 200, "%s -> %s %s" % (host, status, short(body)))

    def test_t16_a_cross_origin_post_is_refused_csrf(self):
        """Any page open in that browser can fire a POST at the panel, and
        there is no credential to steal because there is none."""
        port = self.serve()
        payload = json.dumps({"stage": "request"})
        for origin in ("https://evil.attacker.example", "http://evil.example:8787",
                       "null", "file://", "http://127.0.0.1.evil.example",
                       "https://localhost.evil.example"):
            status, body, _headers = self.raw(port, "POST", "/api/ingest",
                                              {"Origin": origin}, payload)
            self.assertEqual(status, 403, "%s -> %s %s" % (origin, status, short(body)))
            self.assertIn("not a loopback origin", body)
        self.assertFalse(self.db_file.exists(),
                         "a refused POST must never reach the handler")

        # Fetch Metadata: the browser sets it, page script cannot forge it.
        for site in ("cross-site", "same-site"):
            status, body, _headers = self.raw(
                port, "POST", "/api/ingest",
                {"Origin": "http://127.0.0.1:%d" % port, "Sec-Fetch-Site": site},
                payload)
            self.assertEqual(status, 403, "%s -> %s" % (site, short(body)))
            self.assertIn("cross-site request refused", body)
        self.assertFalse(self.db_file.exists())

        # The panel's own page, a typed address bar, and a plain curl or unit
        # that sends no Origin at all.
        for headers in ({"Origin": "http://127.0.0.1:%d" % port},
                        {"Origin": "http://localhost:%d" % port},
                        {"Origin": "http://[::1]:%d" % port},
                        {"Sec-Fetch-Site": "same-origin"},
                        {"Sec-Fetch-Site": "none"},
                        {}):
            status, body, _headers = self.raw(port, "POST", "/api/ingest",
                                              headers, payload)
            self.assertEqual(status, 200, "%s -> %s %s" % (headers, status, short(body)))
        self.assertTrue(self.db_file.exists(), "the accepted POST did ingest")

    def test_t17_the_guard_functions_are_pure_and_pinned(self):
        """The parsing of a Host header is where this kind of check is usually
        got wrong, so it is pinned case by case rather than end to end."""
        server = self.server
        for good in ("127.0.0.1", "127.0.0.1:8787", "localhost", "LocalHost:9000",
                     "[::1]", "[::1]:8787", "::1", "::ffff:127.0.0.1",
                     "  localhost:8787  "):
            self.assertIsNone(server.host_problem(good), repr(good))
        for bad in (None, "", "evil.example", "evil.example:8787",
                    "127.0.0.1.evil.example", "localhost.evil.example",
                    "192.0.2.5:8787", "0.0.0.0", "127.0.0.2",
                    "xn--localhost.example"):
            self.assertIsNotNone(server.host_problem(bad), repr(bad))

        self.assertIsNone(server.origin_problem(None))    # curl, a unit, a timer
        for good in ("http://127.0.0.1:8787", "https://localhost",
                     "http://[::1]:8787", "  http://localhost:8787  "):
            self.assertIsNone(server.origin_problem(good), repr(good))
        for bad in ("null", "", "file://", "file:///etc/passwd",
                    "http://evil.example", "http://127.0.0.1.evil.example",
                    "https://localhost.evil.example", "data:text/html,x"):
            self.assertIsNotNone(server.origin_problem(bad), repr(bad))

        for marker in server.PROXY_MARKERS:
            self.assertTrue(server.is_proxied({marker: "203.0.113.9"}), marker)
        self.assertFalse(server.is_proxied({}))
        self.assertFalse(server.is_proxied({"x-forwarded-for": ""}))


class TestReadToken(ServedPanel):
    """Put a tunnel or a reverse proxy in front of this and the peer address is
    STILL 127.0.0.1, so the peer address alone would trust the whole network
    behind the proxy."""

    def setUp(self):
        super().setUp()
        self.full_fixture()
        self.ingest.run()

    def test_t18_a_proxied_request_with_no_token_configured_is_refused(self):
        """Somebody put a proxy in front of an unauthenticated panel, and
        guessing that they meant to is how transcripts end up on the internet."""
        self.assertFalse(self.token_file.exists())
        port = self.serve()
        for marker, value in (("X-Forwarded-For", "203.0.113.9"),
                              ("X-Forwarded-Host", "panel.example"),
                              ("X-Real-IP", "203.0.113.9"),
                              ("Forwarded", "for=203.0.113.9;proto=https")):
            status, body, _headers = self.raw(port, "GET", "/api/logs",
                                              {marker: value})
            self.assertEqual(status, 403, "%s -> %s %s" % (marker, status, short(body)))
            self.assertIn("no read token is configured", body)
            self.assertNotIn("signing key", body)
        # Presenting a token nobody configured does not conjure one into being.
        self.assertEqual(self.raw(port, "GET", "/api/logs",
                                  {"X-Forwarded-For": "203.0.113.9",
                                   "X-Panel-Token": "guessed"})[0], 403)
        # Direct loopback needs nothing at all.
        self.assertEqual(self.raw(port, "GET", "/api/logs")[0], 200)

    def test_t19_a_proxied_request_passes_on_the_token_by_header_cookie_or_query(self):
        # A DELIBERATE fake, named so it reads as one. A scanner flagging the
        # lines below is reading them correctly; no real token is in this repo.
        self.token_file.write_text("s3cret-fixture-token\n",  # gitleaks:allow
                                   encoding="utf-8")
        self.assertEqual(self.server.read_token(), "s3cret-fixture-token")
        port = self.serve()
        proxied = {"X-Forwarded-For": "203.0.113.9"}

        for extra, path in (
                ({"X-Panel-Token": "s3cret-fixture-token"}, "/api/logs"),
                ({"Cookie": "panel_token=s3cret-fixture-token"}, "/api/logs"),
                ({"Cookie": "other=1; panel_token=s3cret-fixture-token"},
                 "/api/logs"),
                ({}, "/api/logs?token=s3cret-fixture-token")):
            headers = dict(proxied, **extra)
            status, body, _headers = self.raw(port, "GET", path, headers)
            self.assertEqual(status, 200, "%s -> %s %s" % (headers, status, short(body)))
            self.assertEqual(json.loads(body)["count"], 23)

        for extra, path in (
                ({"X-Panel-Token": "s3cret-fixture-toke"},  # gitleaks:allow
                 "/api/logs"),
                ({"X-Panel-Token": "s3cret-fixture-tokenn"}, "/api/logs"),
                ({"X-Panel-Token": ""}, "/api/logs"),
                ({"Cookie": "panel_token=nope"}, "/api/logs"),
                ({}, "/api/logs?token=nope"),
                ({}, "/api/logs")):
            headers = dict(proxied, **extra)
            status, body, _headers = self.raw(port, "GET", path, headers)
            self.assertEqual(status, 403, "%s -> %s %s" % (headers, status, short(body)))
            self.assertIn("read token", body)
            self.assertNotIn("signing key", body)

        # The token gates the whole surface, not just the API.
        self.assertEqual(self.raw(port, "GET", "/", proxied)[0], 403)
        self.assertEqual(self.raw(port, "GET", "/static/app.js", proxied)[0], 403)
        self.assertEqual(self.raw(port, "POST", "/api/ingest", proxied)[0], 403)


# --- path confinement and tamper detection ----------------------------------

class TestPathConfinement(ServedPanel):
    """The script path arrives inside a journal line the panel cannot verify.
    Unconfined, reading it is a file-disclosure primitive: anything able to
    append one line picks a file and the panel renders its contents."""

    def setUp(self):
        super().setUp()
        self.full_fixture()
        self.ingest.run()

    def test_t20_a_forged_script_path_is_never_read_nor_shown(self):
        port = self.serve()
        status, body, _headers = self.raw(port, "GET", "/api/approvals")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(len(payload["pending"]), 3)
        scripts = [item["script"] for item in payload["pending"]]
        self.assertNotIn("/etc/passwd", scripts)
        for needle in ("/etc/passwd", PASSWD_SHAPE, "sneaky", "/../../.."):
            self.assertNotIn(needle, body,
                             "a forged path reached the screen: %s" % needle)
        # The forged entries were in the journal and were ingested as events:
        # they were SKIPPED at the confinement, not missing from the source.
        self.assertEqual(self.rows(
            "SELECT COUNT(*) FROM events WHERE refs LIKE '%passwd%'")[0][0], 2)

        # The legitimate path full of dots is still served: confinement that
        # refuses ordinary work is confinement that gets switched off.
        self.assertIn("silent script", [item["name"] for item in payload["pending"]])

    def test_t21_the_allowlist_is_pinned_case_by_case(self):
        allow = self.approvals.in_allowlist
        inside = self.script_path(LOG_ROTATION)
        self.assertTrue(allow(inside))
        self.assertTrue(allow(str(self.scripts / "nested" / ".." / LOG_ROTATION)))
        self.assertTrue(allow(str(self.scripts)))
        for outside in ("/etc/passwd", "/etc/shadow", "",
                        None, str(self.scripts) + "/../../../etc/passwd",
                        self.script_path("sneaky.sh"),
                        str(self.scripts) + "-evil/x.sh",
                        str(Path(self.tmp.name) / "events.db")):
            self.assertFalse(allow(outside), repr(outside))

        # Refused paths are not read, not hashed, not described.
        self.assertIsNone(self.approvals.script_sha256("/etc/passwd"))
        self.assertIsNone(self.approvals.script_sha256(
            self.script_path("sneaky.sh")))
        described = self.approvals.describe("/etc/passwd")
        self.assertNotIn(PASSWD_SHAPE, json.dumps(described))
        self.assertIn("read it before approving", described["description"])
        self.assertIsNone(described["reversible"])
        # And the file INSIDE the perimeter is read, so the refusal above is a
        # refusal and not a broken reader.
        self.assertEqual(len(self.approvals.script_sha256(inside)), 64)
        self.assertEqual(self.approvals.describe(inside)["scope"],
                         "the build host only")


class TestTamperDetection(ServedPanel):
    """The triggers stop tampering through the schema. The signature detects
    tampering that went around it."""

    def setUp(self):
        super().setUp()
        self.full_fixture()
        self.ingest.run()

    def tamper(self):
        """Edit the file with another tool, exactly as someone reaching the
        disk but not the running process would: drop the trigger, rewrite one
        row. The panel re-applies the trigger the next time it opens."""
        conn = sqlite3.connect(str(self.db_file))
        try:
            target = conn.execute(
                "SELECT id FROM events WHERE summary LIKE '%signing key%'"
            ).fetchone()[0]
            conn.executescript(
                "DROP TRIGGER events_no_update;\n"
                "UPDATE events SET summary='nothing to see here' WHERE id=%d;\n"
                % target)
            conn.commit()
        finally:
            conn.close()
        return target

    def test_t22_a_row_rewritten_out_of_band_is_reported_tampered(self):
        target = self.tamper()
        store = self.store.open_store()
        self.addCleanup(store.close)
        verdict = store.verify_all()
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["tampered"], [target])
        self.assertEqual(verdict["total"], 23, "no row was lost, one was edited")
        store.close()

        port = self.serve()
        logs = json.loads(self.raw(port, "GET", "/api/logs")[1])
        by_id = {row["id"]: row for row in logs["events"]}
        self.assertFalse(by_id[target]["sig_valid"],
                         "the verdict must sit on the LINE, not only in a total")
        self.assertEqual([i for i, row in by_id.items() if not row["sig_valid"]],
                         [target])
        self.assertEqual(by_id[target]["summary"], "nothing to see here",
                         "the panel shows what is really in the row")

        overview = json.loads(self.raw(port, "GET", "/api/overview")[1])
        self.assertFalse(overview["integrity"]["ok"])
        self.assertEqual(overview["integrity"]["tampered"], [target])

        # Re-ingesting does not launder the row: the provenance triple is
        # already there, so the edited row stays visible as edited.
        self.raw(port, "POST", "/api/ingest")
        overview = json.loads(self.raw(port, "GET", "/api/overview")[1])
        self.assertEqual(overview["integrity"]["tampered"], [target])
        self.assertEqual(overview["total_events"], 23)

        # And the screen has a word for it.
        self.assertIn("TAMPERED",
                      (MC / "static" / "app.js").read_text(encoding="utf-8"))


# --- optional modules -------------------------------------------------------

class TestOptionalModules(ServedPanel):
    """Everything that mutates the world outside the panel lives in an optional
    sibling module. Absent, the route answers 503 and the button never appears;
    present, the module carries its own gate and the panel does not
    re-implement it.

    Both states are served from a COPY of the module in a tempdir, launched as
    its own process. That is the only honest way to test a capability that is a
    FILE: the panel resolves it by path from its own directory, so the test has
    to own that directory -- and it must never be the real one."""

    def setUp(self):
        super().setUp()
        self.full_fixture()
        self.ingest.run()

    def sandbox(self, stubs=()):
        """A pristine checkout: mission-control copied, `hooks/` linked back to
        the real one, and EVERY optional module removed unless this test asked
        for it. Returns the path of the copied server."""
        root = Path(self.tmp.name) / "checkout"
        root.mkdir()
        shutil.copytree(MC, root / "mission-control",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                      "*.db"))
        os.symlink(ROOT / "hooks", root / "hooks")
        wanted = dict(stubs)
        for name in self.server.OPTIONAL_MODULES:
            path = root / "mission-control" / (name + ".py")
            if name in wanted:
                path.write_text(wanted[name], encoding="utf-8")
            elif path.exists():
                path.unlink()
        return root / "mission-control" / "server.py"

    def launch(self, cli):
        """Start the copied panel, read its two banner lines, wait for it to
        actually answer, and hand back (port, banner). Cleanup kills it."""
        port = free_port()
        env = dict(os.environ, **self.env)
        env["HARNESS_MC_PORT"] = str(port)
        proc = subprocess.Popen([sys.executable, str(cli)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        self.addCleanup(self.stop, proc)
        watchdog = threading.Timer(60, proc.kill)
        watchdog.start()
        try:
            banner = (proc.stdout.readline(), proc.stdout.readline())
        finally:
            watchdog.cancel()
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                self.raw(port, "GET", "/api/halt")
                break
            except OSError:
                time.sleep(0.05)
        else:
            self.fail("the copied panel never answered: %r" % (banner,))
        return port, banner

    def stop(self, proc):
        proc.terminate()
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=20)

    def test_t23_an_absent_module_answers_503_and_the_banner_says_none(self):
        port, banner = self.launch(self.sandbox())
        self.assertIn("mission-control: http://127.0.0.1:%d" % port, banner[0])
        self.assertIn("read-only core; optional modules installed: none",
                      banner[1])

        for path in ("/api/halt", "/api/approve", "/api/deposit"):
            status, body, headers = self.raw(port, "POST", path, {},
                                             json.dumps({"stage": "request"}))
            self.assertEqual(status, 503, "%s -> %s %s" % (path, status, short(body)))
            payload = json.loads(body)
            self.assertIs(payload["ok"], False)
            self.assertEqual(payload["module"], path.rsplit("/", 1)[1])
            self.assertIn("module not installed", payload["error"])
            self.assert_hardened(headers, path)
        status, body, _headers = self.raw(port, "POST", "/api/presence/begin",
                                          {}, json.dumps({}))
        self.assertEqual(status, 503, body)
        self.assertIn("presence module not installed", body)
        for path in ("/api/presence", "/api/deposit"):
            self.assertEqual(self.raw(port, "GET", path)[0], 503, path)

        # `status()` for halt is the one exception, and it is a READ: with no
        # module the panel reads the flag file directly, because "is the engine
        # paused" has to work on a panel that installed no ability to act.
        status, body, _headers = self.raw(port, "GET", "/api/halt")
        self.assertEqual(status, 200, body)
        self.assertIs(json.loads(body)["module"], False)
        self.assertIs(json.loads(body)["paused"], False)
        self.halt_flag.write_text("", encoding="utf-8")
        self.assertIs(json.loads(self.raw(port, "GET", "/api/halt")[1])["paused"],
                      True)

        # And the read views say the capability is missing, so the page knows
        # not to draw a button that cannot work.
        approvals = json.loads(self.raw(port, "GET", "/api/approvals")[1])
        self.assertIs(approvals["can_approve"], False)
        self.assertIs(approvals["presence_available"], False)

    def test_t24_an_installed_module_is_picked_up_and_answers_200(self):
        port, banner = self.launch(self.sandbox({"halt": HALT_STUB,
                                                 "approve": APPROVE_STUB}))
        self.assertIn("optional modules installed: halt, approve", banner[1])

        status, body, _headers = self.raw(
            port, "POST", "/api/halt", {},
            json.dumps({"stage": "request", "role": "builder", "token": "t"}))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["confirm_token"], "confirm-1")

        # The commit stage is handed the BODY, as one positional argument.
        status, body, _headers = self.raw(
            port, "POST", "/api/halt", {},
            json.dumps({"stage": "commit", "reason": "the engine is looping"}))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["reason"], "the engine is looping")

        status, body, _headers = self.raw(
            port, "POST", "/api/approve", {},
            json.dumps({"script": "/x.sh", "sha256": "abc", "token": "t"}))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["approved_by"], "t",
                         "the panel hands the token to the module, "
                         "it does not re-implement the gate")

        # The read views see the capability appear, and halt now answers
        # through the module instead of the flag file.
        self.assertIs(json.loads(self.raw(port, "GET", "/api/approvals")[1])
                      ["can_approve"], True)
        halt = json.loads(self.raw(port, "GET", "/api/halt")[1])
        self.assertEqual(halt["module"], "stub")
        self.assertIs(halt["paused"], True)
        self.assertFalse(self.halt_flag.exists(),
                         "the module answered, not the flag file")

        # The guard still applies to a route that can now really act: an
        # installed capability is not a way around the Host check.
        self.assertEqual(self.raw(port, "POST", "/api/halt",
                                  {"Host": "evil.attacker.example"},
                                  json.dumps({"stage": "request"}))[0], 403)
        self.assertEqual(self.raw(port, "POST", "/api/halt",
                                  {"Origin": "https://evil.attacker.example"},
                                  json.dumps({"stage": "request"}))[0], 403)
        # ... and a module that never got installed is still 503 next door.
        self.assertEqual(self.raw(port, "POST", "/api/deposit", {},
                                  json.dumps({}))[0], 503)


if __name__ == "__main__":
    unittest.main(verbosity=2)
