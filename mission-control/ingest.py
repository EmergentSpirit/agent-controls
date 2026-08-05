#!/usr/bin/env python3
"""ingest.py -- pull the journals into the signed store. Idempotent, fail-soft.

    python3 ingest.py            one full pass, prints what it inserted

PULL, NEVER PUSH. Nothing instruments the agents: they write their own `.jsonl`
lines wherever they already write them, and this reads those files. The panel
therefore has no runtime coupling with anything it observes, and an agent that
crashes mid-line costs one skipped line rather than a broken panel.

IDEMPOTENT BY CONSTRUCTION. Every line maps to a provenance triple
(source, session, sequence) that the store holds UNIQUE. Running this on a
cron, on a button, and by hand in the same second inserts each line once.

FAIL-SOFT PER FILE AND PER LINE. A half-written JSON line, a directory that
does not exist, a file that was rotated between the listing and the read: all
of these skip and continue. The one thing this must never do is take down the
panel that displays it.

Two source families, and neither is required:

- PROGRESS journals, `*.jsonl` under HARNESS_MC_PROGRESS_DIRS. One object per
  line, with `ts / agent / session / project / type / summary / refs / seq`.
- The EXECUTOR audit, HARNESS_MC_EXECUTOR_AUDIT. One object per line from
  whatever engine runs operator scripts for you. Its result vocabulary is
  mapped to event kinds through `config.EXECUTOR_RESULT_TYPES`; a result the
  table does not know becomes a `mutation`, which is the conservative reading.

Environment: see config.py.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C                       # noqa: E402
import store as store_mod                # noqa: E402


def iter_jsonl(path):
    """Yield one object per readable line. An unreadable line is skipped, not
    fatal: journals are appended to while they are read, and the last line of a
    live file is regularly half a line."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _text(value, default=""):
    return str(value) if value is not None else default


def ingest_progress(store, roots=None) -> int:
    """Read every `*.jsonl` under each progress root. Returns rows inserted."""
    inserted = 0
    for root in (roots if roots is not None else C.progress_dirs()):
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                if not name.endswith(".jsonl"):
                    continue
                for obj in iter_jsonl(os.path.join(dirpath, name)):
                    if not isinstance(obj, dict):
                        continue
                    agent = _text(obj.get("agent"), "unknown")
                    seq = obj.get("seq")
                    seq = int(seq) if isinstance(seq, (int, float)) else None
                    row = store.append(
                        ts=_text(obj.get("ts")),
                        agent=agent,
                        project=_text(obj.get("project"), "unknown"),
                        type=_text(obj.get("type"), "deliverable"),
                        summary=_text(obj.get("summary"))[:300],
                        refs=obj.get("refs", []),
                        src_agent=agent,
                        src_session=_text(obj.get("session")),
                        src_seq=seq)
                    if row is not None:
                        inserted += 1
    return inserted


def ingest_executor(store, path=None) -> int:
    """Read the execution engine's audit journal. Returns rows inserted.

    An engine line has no sequence number of its own, so the provenance triple
    uses its NATURAL identity: whichever of `action_id / snapshot_id / ts` it
    carries. `src_seq` is pinned to 0 rather than left NULL, because SQLite
    treats every NULL as distinct inside a UNIQUE constraint -- leaving it NULL
    would silently disable the dedup and duplicate the whole audit on every
    pass."""
    path = path or C.executor_audit()
    if not os.path.isfile(path):
        return 0
    inserted = 0
    for obj in iter_jsonl(path):
        if not isinstance(obj, dict):
            continue
        # Engines that hash-chain their audit wrap the payload; flat lines are
        # read as-is. Both shapes are common, neither is worth demanding.
        entry = obj.get("entry") or obj.get("event") or obj
        if not isinstance(entry, dict):
            continue
        result = _text(entry.get("result") or entry.get("decision")).lower()
        natural = (entry.get("action_id") or entry.get("snapshot_id")
                   or entry.get("ts") or obj.get("ts") or "")
        summary = _text(entry.get("summary") or entry.get("reason") or result)
        refs = {k: entry[k] for k in ("script", "verdict", "op_class", "tier")
                if k in entry}
        row = store.append(
            ts=_text(entry.get("ts") or obj.get("ts")),
            agent="executor",
            project="executor",
            type=C.EXECUTOR_RESULT_TYPES.get(result, "mutation"),
            summary=summary[:300],
            refs=refs,
            src_agent="executor",
            src_session=_text(natural),
            src_seq=0)
        if row is not None:
            inserted += 1
    return inserted


def run(db_path=None, progress_dirs=None, executor_audit=None) -> dict:
    """One full pass. Safe to call from a request handler: it opens the store,
    reads what is there, and closes."""
    store = store_mod.open_store(db_path=db_path)
    try:
        progress = ingest_progress(store, progress_dirs)
        executor = ingest_executor(store, executor_audit)
        return {"progress_inserted": progress, "executor_inserted": executor,
                "total_rows": store.count()}
    finally:
        store.close()


def main() -> int:
    print(json.dumps(run(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
