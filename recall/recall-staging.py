#!/usr/bin/env python3
"""recall-staging.py -- catch an artifact at birth, as a DRAFT.

PostToolUse hook on Write. When a file that looks like a reactivable artifact
appears (a service unit, a timer, a deliverable document), it appends a draft
entry to the staging file. Never to the catalog: the catalog is what gets
served back into a prompt, and an unnamed, undeduplicated, unverified line has
no business being served.

WHY it must be automatic: a catalog maintained by hand is maintained until the
week someone is busy, and then it silently becomes a liar. The capture happens
at the moment the artifact exists, which is the only moment its path, its type
and its date are known for free.

WHY it must be a DRAFT: automatic capture with no curation produces duplicates
and near-misses. The curation pass (`curate.py`) promotes, deduplicates and
marks supersessions. Until then the entry carries `origin: auto`, and that tag
travels everywhere: an automatic entry is RECALLED, it never triggers an action
on its own.

Anti-noise: skipped when the path is already covered by the catalog or the
staging (search before add), when it lives in a transient directory, or when
the Write failed and the file is not there.

Invariants: always exit 0 (a bookkeeping hook never blocks a tool), one journal
line per execution.

Environment:
- HARNESS_RECALL_CATALOG                  curated catalog (see recall.py)
- HARNESS_RECALL_STAGING                  draft file
                                          (default: $HARNESS_STATE_DIR/recall/STAGING.md)
- HARNESS_AGENT                           role name, also settable with --agent
- HARNESS_RECALL_STAGING_GATE_DISABLE=1   session kill-switch

Exit codes:
  0  always
"""
import datetime
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
try:
    from _hook import STATE_DIR, gate_stat, read_stdin_json
except Exception:
    sys.exit(0)   # no helper, no hook: fail-open before anything else

HOOK = "recall-staging"
DISABLE_ENV = "HARNESS_RECALL_STAGING_GATE_DISABLE"

CATALOG = os.path.expanduser(
    os.environ.get("HARNESS_RECALL_CATALOG")
    or os.path.join(HERE, "CATALOG.example.md"))
STAGING = os.path.expanduser(
    os.environ.get("HARNESS_RECALL_STAGING")
    or os.path.join(STATE_DIR, "recall", "STAGING.md"))

# An artifact is born: service units anywhere, and documents only inside a
# deliverables directory. Deliberately narrow -- a capture rule that fires on
# everything produces a staging nobody reads.
ARTIFACT_RE = re.compile(
    r"(\.(service|timer|target)$)|(/(reports|docs)/[^/]+\.md$)")
# Transient, or already covered by another system (memory is not a catalog:
# memory holds facts and decisions, the catalog holds artifacts).
SKIP_RE = re.compile(
    r"/(tmp|scratch|scratchpad|session-state|memory|node_modules|__pycache__|"
    r"\.git)/|\.bak|/STAGING[^/]*\.md$|/CATALOG[^/]*\.md$")

STAGING_HEADER = """# recall -- STAGING (automatic drafts, promoted by curate)

DRAFT entries written by the PostToolUse hook when an artifact is born. NEVER
read by the match (only the catalog is served). The curation pass promotes,
deduplicates, marks supersessions, then empties this file. `origin: auto`
everywhere here: an automatic entry never triggers an action on its own.

---
"""


def agent_name(argv):
    """Role name: --agent <name>, else HARNESS_AGENT, else `agent`."""
    if "--agent" in argv:
        try:
            name = argv[argv.index("--agent") + 1].strip()
            if name:
                return name
        except IndexError:
            pass
    return (os.environ.get("HARNESS_AGENT") or "agent").strip() or "agent"


def known_paths():
    """Every `path:` already present in the catalog or the staging."""
    paths = set()
    for f in (CATALOG, STAGING):
        try:
            with open(f, encoding="utf-8") as fh:
                for ln in fh:
                    m = re.match(r"^path:\s*(\S+)", ln)
                    if m:
                        paths.add(m.group(1).rstrip("/"))
        except OSError:
            pass
    return paths


def covered(path, paths):
    """Already covered when the exact path, or any of its parents, is known."""
    p = path.rstrip("/")
    while p and p != "/":
        if p in paths:
            return True
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return False


def draft_entry(path):
    """The draft line block for a freshly born artifact."""
    ext = os.path.splitext(path)[1]
    kind = "service" if ext in (".service", ".timer", ".target") else "doc"
    base = re.sub(r"[^a-z0-9-]", "-",
                  os.path.splitext(os.path.basename(path))[0].lower())
    return ("\n## draft-%s\n"
            "aliases: %s\n"
            "path: %s\n"
            "type: %s\n"
            "status: active\n"
            "resume: (AUTO DRAFT -- to be described and normalized by curate)\n"
            "project: (to be determined)\n"
            "updated: %s\n"
            "origin: auto\n"
            % (base, base.replace("-", ", "), path, kind,
               datetime.date.today().isoformat()))


def main():
    if os.environ.get(DISABLE_ENV) == "1":
        gate_stat(HOOK, "skip-disabled")
        return 0
    agent = agent_name(sys.argv)
    data = read_stdin_json()
    if not data:
        gate_stat(HOOK, "fail-open", agent=agent)
        return 0
    if data.get("tool_name") != "Write":
        gate_stat(HOOK, "skip-tool", agent=agent)
        return 0
    path = (data.get("tool_input") or {}).get("file_path") or ""
    if not path or not ARTIFACT_RE.search(path) or SKIP_RE.search(path):
        gate_stat(HOOK, "skip-no-match", agent=agent)
        return 0
    if not os.path.exists(path):
        gate_stat(HOOK, "skip-gone", agent=agent)
        return 0                     # the Write failed: nothing to catalog
    if covered(path, known_paths()):
        gate_stat(HOOK, "skip-covered", agent=agent, path=path)
        return 0                     # search before add: already known, silence

    try:
        os.makedirs(os.path.dirname(STAGING), exist_ok=True)
        is_new = not os.path.exists(STAGING)
        with open(STAGING, "a", encoding="utf-8") as f:
            if is_new:
                f.write(STAGING_HEADER)
            f.write(draft_entry(path))
    except OSError as e:
        gate_stat(HOOK, "fail-open", agent=agent, reason=type(e).__name__)
        return 0
    gate_stat(HOOK, "observe", agent=agent, event="draft-staged", path=path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # fail-open: a bookkeeping hook never blocks a tool
