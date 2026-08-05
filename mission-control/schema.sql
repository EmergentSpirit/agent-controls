-- mission-control/schema.sql -- DDL of the panel's signed event database.
--
-- The database itself is NEVER published: it holds one operator's fleet
-- history. What is published is this schema plus the ingester that fills it,
-- so anyone can rebuild the same panel from their own journals. Delete the
-- file and re-run `ingest.py`: nothing is lost, because the `.jsonl` journals
-- are the source of truth and this is only a signed index of them.
--
-- APPEND-ONLY IS ENFORCED BY THE ENGINE, not by application discipline. The
-- two triggers below refuse UPDATE and DELETE outright, so a bug, a stray
-- `sqlite3` prompt or a future contributor cannot quietly rewrite history.
--
-- SIGNATURE: each row carries an HMAC-SHA256 over its canonical fields. The
-- triggers stop tampering through this schema; the signature DETECTS tampering
-- that went around it (a raw file edit, a copy restored from elsewhere). The
-- key lives in a mode-600 file and never travels with the schema. This is
-- integrity and non-repudiation, NOT confidentiality: rows are not encrypted.

CREATE TABLE IF NOT EXISTS events(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,          -- ISO-8601 UTC, as written by the source
  agent   TEXT NOT NULL,          -- role that produced the event
  project TEXT NOT NULL,          -- what it was working on
  type    TEXT NOT NULL,          -- deliverable | dispatch | mutation | ...
  summary TEXT NOT NULL,          -- one human sentence
  refs    TEXT NOT NULL DEFAULT '[]',   -- JSON, sorted keys, part of the signature
  sig     TEXT NOT NULL,          -- HMAC-SHA256 of the canonical payload

  -- Provenance triple, and the whole reason re-ingesting is free: the same
  -- journal line always lands on the same (source, session, sequence) and the
  -- UNIQUE constraint turns the second insert into a no-op. Events with no
  -- journal behind them (a halt pressed in the panel) leave these NULL.
  src_agent   TEXT,
  src_session TEXT,
  src_seq     INTEGER,
  UNIQUE(src_agent, src_session, src_seq)
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events is append-only: UPDATE rejected');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events is append-only: DELETE rejected');
END;

CREATE INDEX IF NOT EXISTS idx_events_agent   ON events(agent);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
