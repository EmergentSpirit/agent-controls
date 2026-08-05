#!/usr/bin/env python3
"""store.py -- the signed, append-only event log behind the panel.

    python3 store.py            self-check on a throwaway database

Two guarantees, and they answer two different attacks:

- APPEND-ONLY, enforced by SQLite triggers (see schema.sql). Nothing that goes
  through this connection can rewrite or erase a row. That covers accidents and
  well-behaved code.
- A PER-ROW HMAC, which covers the rest. Someone who edits the file with
  another tool defeats the triggers but not the signature: `verify_all` names
  the row ids that no longer match. The panel shows that verdict per line, so a
  tampered log looks tampered instead of looking clean.

The signature is over a CANONICAL payload with sorted keys and a fixed field
order. Any drift in that serialisation invalidates every existing row, which is
why it lives in one function and nowhere else.

The key is a local file, mode 600, created on first use. It is not a secret
shared with anyone: it exists so that a log cannot be edited by whoever can
reach the disk but not the running process.

Environment: see config.py (HARNESS_MC_DB, HARNESS_MC_HMAC_KEY).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C                       # noqa: E402


def load_key(path=None) -> bytes:
    """Load the signing key, creating a 32-byte one at mode 600 if needed.

    The file is opened with O_CREAT and an explicit 0600 mode, so the key is
    never briefly world-readable between creation and chmod."""
    path = path or C.key_file()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if data:
            return data
    except OSError:
        pass
    key = hashlib.sha256(os.urandom(32)).digest()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return key


def canonical(ts, agent, project, type_, summary, refs) -> str:
    """The exact bytes that get signed. Sorted keys, fixed field set, refs
    already serialised: two rows with the same meaning sign identically, and a
    reordered dict never produces a different signature."""
    refs_json = refs if isinstance(refs, str) else json.dumps(
        refs, sort_keys=True, ensure_ascii=False)
    return json.dumps({"ts": ts, "agent": agent, "project": project,
                       "type": type_, "summary": summary, "refs": refs_json},
                      sort_keys=True, ensure_ascii=False)


def sign(key: bytes, ts, agent, project, type_, summary, refs) -> str:
    payload = canonical(ts, agent, project, type_, summary, refs)
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class EventStore:
    """One instance, one connection. Opening it applies the schema, so a fresh
    machine gets an empty panel rather than `no such table: events` on every
    route until someone thinks to run the ingester first."""

    def __init__(self, db_path=None, key_file=None):
        self.db_path = db_path or C.db_path()
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._key = load_key(key_file)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(C.schema_sql())
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- write --------------------------------------------------------------

    def append(self, ts, agent, project, type, summary, refs=None,
               src_agent=None, src_session=None, src_seq=None):
        """Insert one signed row. Returns its id, or None when the provenance
        triple is already present -- which is what makes re-ingesting a journal
        free instead of duplicating it.

        An unknown `type` is NOT rejected. Forward compatibility beats
        tidiness: the signature covers whatever value actually arrived, and a
        panel that drops events it does not recognise is a panel that lies by
        omission the day an agent learns a new word."""
        refs = [] if refs is None else refs
        refs_json = refs if isinstance(refs, str) else json.dumps(
            refs, sort_keys=True, ensure_ascii=False)
        sig = sign(self._key, ts, agent, project, type, summary, refs_json)
        try:
            cur = self.conn.execute(
                "INSERT INTO events (ts, agent, project, type, summary, refs, "
                "sig, src_agent, src_session, src_seq) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, agent, project, type, summary, refs_json, sig,
                 src_agent, src_session, src_seq))
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None                  # same provenance triple, already in

    # --- read ---------------------------------------------------------------

    def query(self, agent=None, project=None, type=None, since_ts=None,
              search=None, limit=200):
        sql = "SELECT * FROM events WHERE 1=1"
        params = []
        for column, value in (("agent", agent), ("project", project),
                              ("type", type)):
            if value:
                sql += " AND %s = ?" % column
                params.append(value)
        if since_ts:
            sql += " AND ts >= ?"
            params.append(since_ts)
        if search:
            sql += " AND summary LIKE ?"
            params.append("%" + search + "%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def all_rows(self):
        return [dict(r) for r in
                self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]

    def agents(self):
        return [r["agent"] for r in self.conn.execute(
            "SELECT DISTINCT agent FROM events ORDER BY agent").fetchall()]

    def counts_by_type(self, since_ts=None):
        sql = "SELECT type, COUNT(*) n FROM events"
        params = []
        if since_ts:
            sql += " WHERE ts >= ?"
            params.append(since_ts)
        sql += " GROUP BY type"
        return {r["type"]: r["n"] for r in self.conn.execute(sql, params).fetchall()}

    # --- integrity ----------------------------------------------------------

    def verify_sig(self, row) -> bool:
        """True when the stored signature still matches the row's content.
        `compare_digest` rather than `==`: a timing side channel on a local log
        is not much of an attack, but writing the careless comparison teaches
        the careless comparison."""
        expected = sign(self._key, row["ts"], row["agent"], row["project"],
                        row["type"], row["summary"], row["refs"])
        return hmac.compare_digest(expected, row.get("sig") or "")

    def verify_all(self) -> dict:
        tampered = [r["id"] for r in self.all_rows() if not self.verify_sig(r)]
        return {"ok": not tampered, "total": self.count(), "tampered": tampered}


def open_store(db_path=None, key_file=None) -> EventStore:
    return EventStore(db_path=db_path, key_file=key_file)


def _self_check() -> int:
    """Prove the two guarantees on a throwaway database, then delete it."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="mc-store-")
    store = open_store(os.path.join(tmp, "events.db"), os.path.join(tmp, "key"))
    first = store.append(ts="2026-01-01T00:00:00Z", agent="builder",
                         project="demo", type="deliverable", summary="wrote a file",
                         src_agent="builder", src_session="s1", src_seq=1)
    again = store.append(ts="2026-01-01T00:00:00Z", agent="builder",
                         project="demo", type="deliverable", summary="wrote a file",
                         src_agent="builder", src_session="s1", src_seq=1)
    try:
        store.conn.execute("UPDATE events SET summary='rewritten' WHERE id=?", (first,))
        update_refused = False
    except sqlite3.DatabaseError:
        update_refused = True
    print("inserted id       :", first)
    print("duplicate insert  :", again, "(None means the dedup held)")
    print("UPDATE refused    :", update_refused)
    print("integrity         :", store.verify_all())
    store.close()
    for name in os.listdir(tmp):
        os.remove(os.path.join(tmp, name))
    os.rmdir(tmp)
    return 0 if (again is None and update_refused) else 1


if __name__ == "__main__":
    sys.exit(_self_check())
