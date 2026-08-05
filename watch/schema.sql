-- watch/schema.sql -- DDL of the observation panel's DERIVED database.
--
-- The database itself is NEVER published: it holds one operator's sessions.
-- What is published is this schema plus the indexer that fills it, so anyone
-- can rebuild the same panel from their own transcripts and their own journal.
-- Delete the file and re-run `indexer.py --rebuild`: nothing is lost, because
-- the `.jsonl` files are the source of truth and this is only an index of them.
--
-- Message BODIES are deliberately absent. `messages` stores a byte offset and
-- a byte length into the source file; the server re-reads that one line on
-- demand. A panel that copies transcript content into a database is a second
-- place secrets can leak from.

CREATE TABLE IF NOT EXISTS files(
  path        TEXT PRIMARY KEY,   -- absolute path of an indexed source file
  mtime       REAL,               -- mtime seen at the last pass
  size        INTEGER,            -- size seen at the last pass
  byte_offset INTEGER,            -- resume point for the next incremental pass
  kind        TEXT,               -- 'transcript' | 'journal'
  scope       TEXT                -- role (transcript) or scope (journal)
);

CREATE TABLE IF NOT EXISTS sessions(
  id           TEXT PRIMARY KEY,  -- session id = transcript file stem
  agent        TEXT,              -- role name of the source root
  path         TEXT,              -- source transcript, re-read at each request
  title        TEXT,
  first_ts     TEXT,
  last_ts      TEXT,
  n_user       INTEGER DEFAULT 0,
  n_assistant  INTEGER DEFAULT 0,
  n_tool       INTEGER DEFAULT 0,
  n_unreadable INTEGER DEFAULT 0, -- lines the tolerant parser could not read
  models       TEXT               -- JSON array of model ids seen
);

CREATE TABLE IF NOT EXISTS messages(
  session_id  TEXT,
  seq         INTEGER,
  ts          TEXT,
  type        TEXT,               -- 'user' | 'assistant' | 'system'
  tool        TEXT,               -- tool name when the message calls one
  byte_offset INTEGER,            -- where the line starts in the source file
  byte_size   INTEGER,            -- how long it is
  PRIMARY KEY(session_id, seq)
);

CREATE TABLE IF NOT EXISTS gate_events(
  src        TEXT,                -- '<journal path>:<byte offset>', dedup key
  ts         TEXT,
  hook       TEXT,
  result     TEXT,                -- pass | block | deny | warn | skip-* | ...
  tool       TEXT,
  session_id TEXT,
  cwd        TEXT,
  scope      TEXT,
  extra      TEXT                 -- JSON of every other field of the line
);

-- Verdicts of the post-hoc analyst. PROPOSALS, displayed and nothing else:
-- no row here ever arms, blocks or changes anything.
CREATE TABLE IF NOT EXISTS analyses(
  session_id    TEXT PRIMARY KEY,
  ts            TEXT,
  model         TEXT,
  severity      TEXT,             -- info | notice | serious | critical
  summary       TEXT,
  findings      TEXT,             -- JSON array
  gate_proposal TEXT,             -- JSON object or null
  raw           TEXT              -- the judge's raw answer, truncated
);

CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_gates_ts ON gate_events(ts);
CREATE INDEX IF NOT EXISTS idx_gates_session ON gate_events(session_id);
