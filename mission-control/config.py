#!/usr/bin/env python3
"""config.py -- what the fleet panel reads, and where it binds.

Everything is resolved LAZILY from the environment, so a test, a launcher or a
systemd unit can point the panel somewhere else without touching this file and
nothing is frozen at import time.

Four source families:

- The EVENT database. Append-only, HMAC-signed, and DERIVED: it is filled by
  `ingest.py` from journals that stay the source of truth. The DDL lives in
  `schema.sql`, which is the published artifact; the database file never is.
- PROGRESS journals: one `.jsonl` per agent session, written by the agents
  themselves (deliverable / dispatch / mutation / decision / blocker / pivot).
- The EXECUTOR audit: the journal of an external execution engine, if you run
  one. The panel only READS it. No engine is required, and none is named.
- The PANES of a terminal multiplexer, which is how the panel knows an agent is
  alive right now rather than alive last time it wrote a line.

THE BIND IS NOT CONFIGURABLE. There is no host variable on purpose: a variable
is a thing someone eventually sets to a wildcard address "just to test from the
laptop", and then leaves that way. This panel shows command lines, session
summaries and pending approvals; it is an instrument on a desk, never a
service.

Environment:
- HARNESS_MC_PORT             local port (default: 8787)
- HARNESS_MC_DB               event database
                              (default: $HARNESS_STATE_DIR/mission-control/events.db)
- HARNESS_MC_HMAC_KEY         signing key file (mode 600, created on demand)
- HARNESS_MC_PROGRESS_DIRS    colon-separated roots holding `*.jsonl` progress
                              journals (default: $HARNESS_STATE_DIR/progress)
- HARNESS_MC_EXECUTOR_AUDIT   audit journal of the execution engine, read-only
                              (default: $HARNESS_STATE_DIR/executor/audit.jsonl)
- HARNESS_MC_HALT_FLAG        flag file whose presence means "engine paused"
                              (default: $HARNESS_STATE_DIR/executor/halt)
- HARNESS_MC_ROLES            colon-separated role names to show, in order
                              (default: builder:researcher)
- HARNESS_MC_ROSTER           colon-separated `<role>=<pane>` pins, bypassing
                              discovery (default: empty, discover from panes)
- HARNESS_MC_READ_TOKEN       read-token file used when a request is proxied
- HARNESS_MC_WINDOW_DAYS      alert window of the overview, days (default: 7)
- HARNESS_MC_INGEST_INTERVAL  auto-ingest throttle, seconds (default: 120)
- HARNESS_OPERATOR_SCRIPT_DIRS  where operator scripts legitimately live; the
                              panel refuses to read a script from anywhere else
- HARNESS_LLM_CLI_NAMES       agent CLI binaries; a pane running one of these
                              is an agent, anything else is a plain shell
- HARNESS_STATE_DIR           state directory (read through `_hook`)
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
import _hook   # noqa: E402  -- same state dir as every gate

# The panel binds HERE and nowhere else. See the module docstring.
HOST = "127.0.0.1"
PORT_DEFAULT = 8787
SCHEMA = os.path.join(HERE, "schema.sql")
STATIC = os.path.join(HERE, "static")

# Event kinds the panel colours and counts. An unknown kind is stored and shown
# as-is rather than rejected: a journal written by a newer agent must not be
# silently dropped by an older panel.
KNOWN_TYPES = ("deliverable", "dispatch", "mutation", "decision", "blocker",
               "pivot", "health", "halt", "circuit-break")

# Results an execution engine can journal, mapped to the event kinds above.
# Yours may use other words; override the mapping in your own ingest wrapper
# rather than renaming your engine's vocabulary to match this table.
EXECUTOR_RESULT_TYPES = {
    "escalated": "decision",
    "approved": "decision",
    "ran": "mutation",
    "rolled-back": "health",
    "blocked": "blocker",
    "halted": "halt",
    "quarantined": "blocker",
    "verify-pass": "health",
    "verify-fail": "blocker",
}

# Engine results that mean "this script no longer needs a human": seeing one of
# these clears any earlier escalation for the same script from the queue.
EXECUTOR_SETTLED = frozenset((
    "ran", "rolled-back", "blocked", "halted", "archived", "cancelled",
))
EXECUTOR_ESCALATED = frozenset(("escalated", "escalated-frozen",
                                "escalated-key-gated"))


def state_dir() -> str:
    return os.environ.get("HARNESS_STATE_DIR") or _hook.STATE_DIR


def panel_dir() -> str:
    return os.path.join(state_dir(), "mission-control")


def _path(var: str, *default_parts: str) -> str:
    raw = (os.environ.get(var) or "").strip()
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(*default_parts)


def _int(var: str, default: int, lo: int = 0, hi: int = 10 ** 9) -> int:
    raw = (os.environ.get(var) or "").strip()
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        return default


def _list(var: str, default: str) -> list:
    raw = (os.environ.get(var) or "").strip() or default
    return [item.strip() for item in raw.split(os.pathsep) if item.strip()]


def port() -> int:
    return _int("HARNESS_MC_PORT", PORT_DEFAULT, 1, 65535)


def db_path() -> str:
    return _path("HARNESS_MC_DB", panel_dir(), "events.db")


def key_file() -> str:
    return _path("HARNESS_MC_HMAC_KEY", panel_dir(), "hmac.key")


def read_token_file() -> str:
    return _path("HARNESS_MC_READ_TOKEN", panel_dir(), "read-token")


def ingest_marker() -> str:
    return os.path.join(panel_dir(), "last-auto-ingest")


def progress_dirs() -> list:
    return [os.path.expanduser(p) for p in
            _list("HARNESS_MC_PROGRESS_DIRS", os.path.join(state_dir(), "progress"))]


def executor_audit() -> str:
    return _path("HARNESS_MC_EXECUTOR_AUDIT", state_dir(), "executor", "audit.jsonl")


def halt_flag() -> str:
    return _path("HARNESS_MC_HALT_FLAG", state_dir(), "executor", "halt")


def roles() -> list:
    """Role names, in display order. Roles, never people: the panel shows what
    a pane is FOR, and two panes doing the same job carry the same name."""
    return _list("HARNESS_MC_ROLES", "builder" + os.pathsep + "researcher")


def roster_pins() -> dict:
    """{role: pane} pins that bypass discovery. Empty by default, because a
    pinned address goes stale the first time someone reorders their windows."""
    pins = {}
    for item in _list("HARNESS_MC_ROSTER", ""):
        role, sep, pane = item.partition("=")
        if sep and role.strip() and pane.strip():
            pins[role.strip()] = pane.strip()
    return pins


def agent_cli_names() -> list:
    """Binaries that mean "this pane is running an agent". Shared with the
    gates, so the panel and the guards agree on what an agent process is."""
    return [n.lower() for n in _list("HARNESS_LLM_CLI_NAMES", "claude")]


def operator_script_dirs() -> list:
    """Directories where operator scripts legitimately live. The panel reads a
    script's header to explain what it would do, and it refuses to read one
    from anywhere else: the path arrives inside a journal line the panel cannot
    verify, so an unconfined read is a file-disclosure primitive."""
    return [os.path.realpath(os.path.expanduser(p)) for p in
            _list("HARNESS_OPERATOR_SCRIPT_DIRS", os.path.join("~", "operator-scripts"))]


def window_days() -> int:
    return _int("HARNESS_MC_WINDOW_DAYS", 7, 1, 3650)


def ingest_interval() -> int:
    return _int("HARNESS_MC_INGEST_INTERVAL", 120, 0, 86400)


def schema_sql() -> str:
    """The published DDL. The database is derived and never shipped; this file
    is what travels, so anyone can rebuild the panel from their own journals."""
    with open(SCHEMA, encoding="utf-8") as fh:
        return fh.read()
