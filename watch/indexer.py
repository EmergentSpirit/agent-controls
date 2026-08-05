#!/usr/bin/env python3
"""indexer.py -- incremental index: transcripts + gate journals -> SQLite.

    python3 indexer.py            incremental pass (the normal one, sub-second
                                  when nothing changed)
    python3 indexer.py --rebuild  throw the database away and re-index everything

Principles, in the order they matter:

- The database is DERIVED. The `.jsonl` files remain the source of truth. Only
  metadata and byte OFFSETS are stored; message bodies are re-read from the
  source file on demand. Losing the database costs one rebuild, never data.
- Incremental by (mtime, size, offset). A file that grew is resumed at its
  offset; a file that shrank, or that is new, is re-indexed from zero; a file
  that disappeared is purged.
- TOLERANT parser. The transcript format is not a contract: a line that will
  not parse is COUNTED (`n_unreadable`) and never fatal. An indexer that dies
  on one malformed line is an indexer that shows nothing on the day it matters.
- The exclusion list is enforced HERE, at the only place data enters. Adding a
  pattern also PURGES what was indexed before it existed, so the blind spot is
  retroactive rather than aspirational.

This module writes to its own database and to nothing else.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
import config as C                      # noqa: E402

try:
    from _hook import gate_stat         # noqa: E402
except Exception:                       # pragma: no cover -- helper absent
    def gate_stat(*_a, **_kw):
        return None

HOOK = "watch-indexer"
JOURNAL_FIELDS = {"ts", "hook", "result", "tool", "session_id", "cwd"}


# --- database ---------------------------------------------------------------

def open_db(path=None):
    """Open (and create when missing) the derived database. The DDL comes from
    schema.sql: the schema is the published artifact, the database is not."""
    path = path or C.db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(C.schema_sql())
    return db


def file_state(db, path):
    return db.execute(
        "SELECT mtime, size, byte_offset FROM files WHERE path=?", (path,)).fetchone()


def remember_file(db, path, mtime, size, byte_offset, kind, scope):
    db.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, byte_offset, kind, scope) "
        "VALUES(?,?,?,?,?,?)", (path, mtime, size, byte_offset, kind, scope))


def forget_file(db, path):
    db.execute("DELETE FROM files WHERE path=?", (path,))


# --- transcripts ------------------------------------------------------------

def index_transcript(db, path, agent, from_offset, from_seq):
    """Read `path` from `from_offset`. Returns (new_offset, last_seq)."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    counts = {"user": 0, "assistant": 0, "tool": 0, "unreadable": 0}
    models, title = set(), None
    first_ts = last_ts = None
    seq = from_seq
    batch = []
    with open(path, "rb") as fh:
        fh.seek(from_offset)
        while True:
            offset = fh.tell()
            line = fh.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except Exception:
                counts["unreadable"] += 1
                continue
            kind = event.get("type")
            ts = event.get("timestamp") or ""
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if kind == "ai-title":
                title = event.get("title") or event.get("text") or title
            if kind not in ("user", "assistant", "system"):
                continue
            tool = None
            message = event.get("message")
            if isinstance(message, dict):
                if message.get("model"):
                    models.add(message["model"])
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool = block.get("name")
                            counts["tool"] += 1
            if kind in ("user", "assistant"):
                counts[kind] += 1
            seq += 1
            batch.append((session_id, seq, ts, kind, tool, offset, len(line)))
        end = fh.tell()

    if batch:
        db.executemany(
            "INSERT OR REPLACE INTO messages(session_id, seq, ts, type, tool, "
            "byte_offset, byte_size) VALUES(?,?,?,?,?,?,?)", batch)

    # Merge with the existing session row (incremental resume).
    row = db.execute(
        "SELECT first_ts, n_user, n_assistant, n_tool, n_unreadable, models, title, "
        "last_ts FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row:
        first_ts = row[0] or first_ts
        counts["user"] += row[1]
        counts["assistant"] += row[2]
        counts["tool"] += row[3]
        counts["unreadable"] += row[4]
        try:
            models |= set(json.loads(row[5] or "[]"))
        except ValueError:
            pass
        title = title or row[6]
        # A resumed pass that read no timestamped line must not blank the one
        # already known: an empty append would make the session look undated.
        last_ts = last_ts or row[7]
    db.execute(
        "INSERT OR REPLACE INTO sessions(id, agent, path, title, first_ts, last_ts, "
        "n_user, n_assistant, n_tool, n_unreadable, models) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, agent, path, title, first_ts, last_ts, counts["user"],
         counts["assistant"], counts["tool"], counts["unreadable"],
         json.dumps(sorted(models))))
    return end, seq


def purge_session(db, path):
    """Remove a transcript from the index. Used when the file shrank, when it
    disappeared, and when it entered the exclusion list."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    db.execute("DELETE FROM analyses WHERE session_id=?", (session_id,))


# --- gate journals ----------------------------------------------------------

def index_journal(db, path, scope, from_offset):
    batch = []
    with open(path, "rb") as fh:
        fh.seek(from_offset)
        while True:
            offset = fh.tell()
            line = fh.readline()
            if not line:
                break
            try:
                rec = json.loads(line)
            except Exception:
                continue                # tolerant: a torn line is not fatal
            if not isinstance(rec, dict):
                continue
            extra = {k: v for k, v in rec.items() if k not in JOURNAL_FIELDS}
            batch.append(("%s:%d" % (path, offset), rec.get("ts"), rec.get("hook"),
                          rec.get("result"), rec.get("tool"), rec.get("session_id"),
                          rec.get("cwd"), scope,
                          json.dumps(extra, ensure_ascii=False) if extra else None))
        end = fh.tell()
    if batch:
        db.executemany(
            "INSERT INTO gate_events(src, ts, hook, result, tool, session_id, cwd, "
            "scope, extra) VALUES(?,?,?,?,?,?,?,?,?)", batch)
    return end


# --- the pass ---------------------------------------------------------------

def scan(db):
    """One incremental pass. Returns counters. Writes nothing outside the
    database, and never touches a source file."""
    stats = {"files_seen": 0, "files_read": 0, "excluded": 0, "sessions": 0}

    for role, root in C.transcript_sources():
        if not os.path.isdir(root):
            continue
        if C.is_excluded(role, root):
            stats["excluded"] += 1
            continue
        for name in sorted(os.listdir(root)):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            if C.is_excluded(role, path):
                stats["excluded"] += 1
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            stats["files_seen"] += 1
            known = file_state(db, path)
            if known and known[0] == st.st_mtime and known[1] == st.st_size:
                continue                # nothing new
            from_offset, seq = 0, 0
            if known and st.st_size >= known[1]:
                from_offset = known[2]  # the file grew: resume at the offset
                seq = db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM messages WHERE session_id=?",
                    (os.path.splitext(name)[0],)).fetchone()[0]
            else:
                purge_session(db, path)  # shrank or unknown: start clean
            offset, _ = index_transcript(db, path, role, from_offset, seq)
            remember_file(db, path, st.st_mtime, st.st_size, offset, "transcript", role)
            stats["files_read"] += 1

    for scope, path in C.journal_sources():
        if not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        stats["files_seen"] += 1
        known = file_state(db, path)
        if known and known[0] == st.st_mtime and known[1] == st.st_size:
            continue
        from_offset = known[2] if (known and st.st_size >= known[1]) else 0
        if from_offset == 0:
            db.execute("DELETE FROM gate_events WHERE src LIKE ?", (path + ":%",))
        offset = index_journal(db, path, scope, from_offset)
        remember_file(db, path, st.st_mtime, st.st_size, offset, "journal", scope)
        stats["files_read"] += 1

    # Purge what left the perimeter: file gone from disk, or role newly excluded.
    for path, scope in db.execute(
            "SELECT path, scope FROM files WHERE kind='transcript'").fetchall():
        if not os.path.isfile(path) or C.is_excluded(scope, path):
            purge_session(db, path)
            forget_file(db, path)

    stats["sessions"] = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    return stats


def expected_sessions():
    """How many transcripts SHOULD be indexed, counted on disk. The integrity
    assert of the pass: an indexer that quietly drops files is worse than one
    that crashes, because the panel keeps looking complete."""
    total = 0
    for role, root in C.transcript_sources():
        if not os.path.isdir(root) or C.is_excluded(role, root):
            continue
        for name in os.listdir(root):
            if name.endswith(".jsonl") and not C.is_excluded(
                    role, os.path.join(root, name)):
                total += 1
    return total


def main():
    path = C.db_path()
    if "--rebuild" in sys.argv and os.path.exists(path):
        os.remove(path)
    started = time.time()
    db = open_db(path)
    stats = scan(db)
    db.commit()

    expected = expected_sessions()
    real = stats["sessions"]
    verdict = "OK" if real == expected else "MISMATCH (%d expected)" % expected
    n_msg = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    n_gates = db.execute("SELECT COUNT(*) FROM gate_events").fetchone()[0]
    db.close()
    print("watch-index: %d sessions [%s] - %d messages - %d gate events - "
          "%d/%d files read - %d excluded - %.1fs"
          % (real, verdict, n_msg, n_gates, stats["files_read"],
             stats["files_seen"], stats["excluded"], time.time() - started))
    # One aliveness line per CLI run, like every other organ. `observe`: the
    # indexer blocks nothing, it only looks. The server's own incremental pass
    # does NOT journal, or a busy panel would drown its own journal.
    gate_stat(HOOK, "observe" if real == expected else "warn",
              sessions=real, expected=expected, excluded=stats["excluded"])
    return 0 if real == expected else 1


if __name__ == "__main__":
    sys.exit(main())
