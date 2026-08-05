#!/usr/bin/env python3
"""curate.py -- the tidier of the recall catalog: promote drafts, never delete.

The hooks write DRAFTS automatically (`recall-staging.py`), one per artifact
birth. Drafts are noise until someone names them, deduplicates them and points
the superseded ones at their replacement. That someone is a headless agent CLI
with NO TOOLS, handed the catalog and the staging file, and asked to return a
JSON object. This script is the deterministic half around it.

The model proposes. This script VALIDATES before anything is written:

- the new catalog must REPARSE with the same parser the engine uses;
- EVERY existing entry must still be there. The tidier MARKS (`superseded_by`),
  it never deletes: a deleted entry is an artifact that will be rebuilt from
  scratch in six weeks, which is the exact failure this module exists to stop;
- the `check:` field is restored from the previous version, always. That field
  is EXECUTED on a timer, so it never comes from a model, and every divergence
  is journaled loudly. Hiding an attempt would be worse than the bug.

A commit before and after makes the pass reversible. Any failure anywhere:
nothing is written and the catalog stays exactly as it was.

Environment:
- HARNESS_RECALL_CATALOG         curated catalog (the file rewritten here)
- HARNESS_RECALL_STAGING         draft file written by the staging hook
- HARNESS_RECALL_CURATE_LOG      append-only log of curation actions
- HARNESS_RECALL_CURATE_TIMEOUT  hard timeout of the tidier call (default 300)
- HARNESS_LLM_CLI_NAMES          colon-separated agent CLI binaries; the first
                                 one is the tidier (default: claude)

Exit codes:
  0  a pass ran (or the staging was empty: nothing to tidy)
  1  refused -- nothing written, the catalog is intact
  2  misconfigured (pointed at the shipped example catalog)
"""
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import recall  # noqa: E402  the engine's parser IS the validator

CATALOG = recall.CATALOG
STAGING = os.path.expanduser(
    os.environ.get("HARNESS_RECALL_STAGING")
    or os.path.join(recall.RECALL_STATE, "STAGING.md"))
LOG = recall.CURATE_LOG
DIR = os.path.dirname(CATALOG)
EXAMPLE_CATALOG = os.path.join(HERE, "CATALOG.example.md")
TIMEOUT = recall.int_env("HARNESS_RECALL_CURATE_TIMEOUT", 300)

PROMPT = """You are the TIDIER of an "already built" artifact catalog. You get CATALOG.md (the truth) and STAGING.md (automatic drafts). STRICT rules:
1. Promote every `## draft-*` entry of the staging into the catalog: drop the draft- prefix, pick a unique, speaking kebab-case name.
2. DEDUPLICATE: when a draft matches an existing entry (same path, same artifact, or a parent already catalogued), do NOT create a duplicate. Refresh the existing entry's `updated` date and enrich its aliases/resume if the draft adds anything.
3. Relative dates become absolute (ISO YYYY-MM-DD).
4. SUPERSESSION: when an artifact clearly replaces another, set superseded_by on the old one and supersedes on the new one. NEVER delete an entry or a section.
5. Preserve `origin` EVERYWHERE: do not add origin to entries that have none; promoted drafts keep origin: auto.
6. Improve the resume of a draft from its name/path/type; when it cannot be determined, write "(auto: to be described)".
7. Touch nothing else: no rewording of existing entries beyond deduplication and supersession.
STRICT output: ONLY a JSON object {"catalog": "<full content of the new CATALOG.md>", "log": ["<action taken>", ...]} -- no text before or after."""


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def tidier_cli():
    raw = os.environ.get("HARNESS_LLM_CLI_NAMES") or "claude"
    names = [n.strip() for n in raw.split(":") if n.strip()]
    return names[0] if names else "claude"


def git_commit(msg):
    """Reversibility, when the catalog lives in a repo. Not a repo: both calls
    fail quietly and the pass still runs -- version control is a safety net
    here, not a requirement."""
    sh("git", "-C", DIR, "add", "-A")
    sh("git", "-C", DIR, "commit", "-qm", msg)


def extract_json(text):
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        raise ValueError("no JSON in the tidier output")
    return json.loads(text[a:b + 1])


def names(path):
    return {e["name"] for e in recall.parse_catalog(path)}


# --- the check: field is out of the model's reach ---------------------------
# It is EXECUTED by the health pass. A model that writes that field writes a
# command that will run on the machine. The original validation (reparse plus
# no entry lost) did not cover it: the prompt said "do not touch it", and a
# prompt instruction is not a constraint.
#
# We RESTORE rather than refuse: a legitimate pass must not fail because of
# this, and nothing the model writes into `check:` ever reaches execution.
_CHECK_LINE_RE = re.compile(r"^check:.*$", re.M)


def _checks_by_entry(text):
    """{entry name: the full `check:` line}. Computed on the RAW text, because
    that exact text is what gets rewritten."""
    out = {}
    for block in text.split("\n## ")[1:]:
        name = block.split("\n", 1)[0].strip()
        m = _CHECK_LINE_RE.search(block)
        if m:
            out[name] = m.group(0)
    return out


def sanitize_checks(new_text, old_text):
    """Put the original `check:` lines back, and strip any posted on a new
    entry. Returns (fixed_text, anomalies)."""
    before = _checks_by_entry(old_text)
    after = _checks_by_entry(new_text)
    anomalies = []
    blocks = new_text.split("\n## ")
    for i, block in enumerate(blocks):
        if i == 0:
            continue                       # file header, not an entry
        name = block.split("\n", 1)[0].strip()
        old, new = before.get(name), after.get(name)
        if old is None and new is None:
            continue
        if old is None:                    # check: posted on a NEW entry -> removed
            blocks[i] = _CHECK_LINE_RE.sub("", block).replace("\n\n\n", "\n\n")
            anomalies.append("check: ADDED by the tidier on \"%s\" -> REMOVED "
                             "(executed field, not model-writable)" % name)
        elif new is None:                  # check: deleted -> restored
            lines = block.split("\n")
            lines.insert(1, old)
            blocks[i] = "\n".join(lines)
            anomalies.append("check: DELETED on \"%s\" -> restored" % name)
        elif new != old:                   # check: modified -> original value
            blocks[i] = _CHECK_LINE_RE.sub(lambda _m: old, block, count=1)
            anomalies.append("check: MODIFIED on \"%s\" -> original value "
                             "restored" % name)
    return "\n## ".join(blocks), anomalies


def main():
    if os.path.realpath(CATALOG) == os.path.realpath(EXAMPLE_CATALOG):
        print("[curate] refusing to rewrite the shipped example catalog. "
              "Copy it, then point HARNESS_RECALL_CATALOG at your copy.",
              file=sys.stderr)
        return 2

    staging = ""
    if os.path.exists(STAGING):
        with open(STAGING, encoding="utf-8") as f:
            staging = f.read()
    if not re.search(r"^## draft-", staging, re.M):
        print("[curate] empty staging, nothing to tidy")
        return 0

    with open(CATALOG, encoding="utf-8") as f:
        catalog = f.read()
    old_names = names(CATALOG)
    git_commit("curate: state before the pass")

    framed = "=== CATALOG.md ===\n%s\n\n=== STAGING.md ===\n%s" % (catalog,
                                                                  staging)
    # Never the metered API: the tidier runs on the interactive plan or not at
    # all. HOME is kept, the vendor keys are not.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    try:
        r = sh(tidier_cli(), "-p", "--tools", "", "--strict-mcp-config",
               "--append-system-prompt", PROMPT,
               input=framed, timeout=TIMEOUT, env=env)
    except Exception as e:
        print("[curate] tidier unavailable (%s): nothing written"
              % type(e).__name__, file=sys.stderr)
        return 1
    if r.returncode != 0:
        print("[curate] tidier rc=%d: nothing written" % r.returncode,
              file=sys.stderr)
        return 1
    try:
        data = extract_json(r.stdout)
        new_catalog = data["catalog"]
        actions = [str(a) for a in data.get("log", [])]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        print("[curate] invalid output (%s): nothing written" % e,
              file=sys.stderr)
        return 1

    # The `check:` field is executed by the health pass: it NEVER comes from
    # the model.
    new_catalog, check_anomalies = sanitize_checks(new_catalog, catalog)
    for a in check_anomalies:
        print("[curate] ! %s" % a, file=sys.stderr)
    actions.extend(check_anomalies)

    # Hard validation: it reparses AND no entry was lost (marking != deleting).
    tmp = CATALOG + ".curate-candidate"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_catalog if new_catalog.endswith("\n")
                else new_catalog + "\n")
    new_names = names(tmp)
    lost = old_names - new_names
    if lost or len(new_names) < len(old_names):
        os.unlink(tmp)
        print("[curate] REFUSED: entries lost %s: nothing written" % sorted(lost),
              file=sys.stderr)
        return 1

    os.replace(tmp, CATALOG)
    # Empty the staging (the drafts are promoted or merged), keep its header.
    header = staging.split("---", 1)[0] + "---\n"
    with open(STAGING, "w", encoding="utf-8") as f:
        f.write(header)
    today = time.strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        for a in actions or ["pass with no detailed action"]:
            f.write("- %s: %s\n" % (today, a))
    git_commit("curate: pass %s (%d action(s))" % (today, len(actions)))
    print("[curate] ok: %d action(s), %d net entry/entries"
          % (len(actions), len(new_names) - len(old_names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
