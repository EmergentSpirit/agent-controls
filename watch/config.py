#!/usr/bin/env python3
"""config.py -- what the observation panel reads, and what it refuses to read.

Everything is resolved LAZILY from the environment: a test, a launcher or a
systemd unit can point the panel somewhere else without touching this file,
and nothing is frozen at import time.

Three source families:

- TRANSCRIPTS: directories holding agent session files (one `.jsonl` per
  session). Each root carries a ROLE name, which is what the panel displays.
- JOURNALS: gate-stats journals (one JSON line per hook execution). Each
  carries a SCOPE name, so a panel watching several agents can tell them
  apart. The default is the journal `_hook` writes to.
- The DERIVED database. It is derived on purpose: the `.jsonl` files stay the
  source of truth, the database only holds metadata and byte OFFSETS, and it
  can be thrown away and rebuilt at any time. The DDL lives in `schema.sql`,
  which is the published artifact; the database file itself never is.

THE DELIBERATE BLIND SPOT: `HARNESS_WATCH_EXCLUDE` lists patterns whose
TRANSCRIPTS are never indexed. The journals of an excluded role are still
indexed. An observation panel that can see everything is a surveillance tool;
a role whose sessions are private stays private, and the panel still proves
its gates fire. See docs/watch.md.

Environment:
- HARNESS_WATCH_DB           derived database path
                             (default: $HARNESS_STATE_DIR/watch/watch.db)
- HARNESS_WATCH_TRANSCRIPTS  colon-separated `[<role>=]<directory>` entries
                             (default: every subdirectory of ~/.claude/projects,
                             role = directory name)
- HARNESS_WATCH_JOURNALS     colon-separated `[<scope>=]<file>` entries
                             (default: the `_hook` gate-stats journal)
- HARNESS_WATCH_EXCLUDE      colon-separated fnmatch patterns; a transcript
                             root, file or role matching one is NEVER indexed
- HARNESS_WATCH_PORT         local port of the panel (default: 8815)
- HARNESS_STATE_DIR          state directory (read through `_hook`)
- HARNESS_GATE_STATS         gate-stats journal path (read through `_hook`)
"""
from __future__ import annotations

import fnmatch
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
import _hook   # noqa: E402  -- same state dir and journal as every gate

# The panel binds HERE and nowhere else. Transcripts carry command outputs;
# this is a local instrument, never a service. There is no host variable on
# purpose: a variable is a thing someone eventually sets to a wildcard address
# "just to test from the laptop", and then leaves that way.
HOST = "127.0.0.1"
PORT_DEFAULT = 8815
PAGE_SIZE = 100                       # messages per trajectory page
TRANSCRIPTS_DEFAULT = "~/.claude/projects"
SCHEMA = os.path.join(HERE, "schema.sql")


def state_dir() -> str:
    return os.environ.get("HARNESS_STATE_DIR") or _hook.STATE_DIR


def default_journal() -> str:
    return os.environ.get("HARNESS_GATE_STATS") or _hook.GATE_STATS


def db_path() -> str:
    raw = (os.environ.get("HARNESS_WATCH_DB") or "").strip()
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(state_dir(), "watch", "watch.db")


def port() -> int:
    raw = (os.environ.get("HARNESS_WATCH_PORT") or "").strip()
    return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else PORT_DEFAULT


def _pairs(raw: str, default_label: str):
    """`[<label>=]<path>` entries, colon-separated. The label is optional
    because most people have one of each and should not have to name it."""
    out = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        label, sep, path = item.partition("=")
        if not sep:
            label, path = "", item
        path = os.path.expanduser(path.strip())
        label = label.strip() or os.path.basename(path.rstrip(os.sep)) or default_label
        out.append((label, path))
    return out


def transcript_sources():
    """[(role, root)] -- explicit configuration wins; otherwise every
    subdirectory of the default projects directory, named after itself."""
    raw = (os.environ.get("HARNESS_WATCH_TRANSCRIPTS") or "").strip()
    if raw:
        return _pairs(raw, "agent")
    base = os.path.expanduser(TRANSCRIPTS_DEFAULT)
    out = []
    try:
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if os.path.isdir(full):
                out.append((name, full))
    except OSError:
        pass                          # no projects directory: nothing to index
    return out


def journal_sources():
    """[(scope, file)] -- gate-stats journals. Default: the one `_hook` writes."""
    raw = (os.environ.get("HARNESS_WATCH_JOURNALS") or "").strip()
    if raw:
        return _pairs(raw, "gates")
    return [("default", default_journal())]


def exclusions():
    """Patterns whose TRANSCRIPTS are never indexed. Empty by default: the
    blind spot is a decision someone takes, not one they inherit."""
    raw = os.environ.get("HARNESS_WATCH_EXCLUDE") or ""
    return [p.strip() for p in raw.split(os.pathsep) if p.strip()]


def is_excluded(role: str, path: str) -> bool:
    """True when this role or this path is inside the deliberate blind spot.
    Matched against the role, the full path and the basename, so
    `HARNESS_WATCH_EXCLUDE=private` and `.../private-role/*` both work."""
    patterns = exclusions()
    if not patterns:
        return False
    candidates = [str(role or ""), str(path or ""), os.path.basename(str(path or ""))]
    for pattern in patterns:
        for candidate in candidates:
            if candidate and fnmatch.fnmatch(candidate, pattern):
                return True
    return False


def schema_sql() -> str:
    """The published DDL. The database is derived and never shipped; this file
    is what travels, so anyone can rebuild the panel from their own journals."""
    with open(SCHEMA, encoding="utf-8") as fh:
        return fh.read()
