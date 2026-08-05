# recall -- "it already exists, do not rebuild it"

An agent rebuilds what already exists. Not out of carelessness: it rebuilds
because nothing in its context says the thing exists. The service is installed,
the pipeline ran for four months, the dataset was collected in the spring --
and none of that is in the window. So the work happens a second time, and the
second copy immediately starts diverging from the first.

Adding "check whether it already exists" to a system prompt does not fix this.
The agent has nothing to check against, and a catalog nobody reads changes
nothing. What works is delivering the answer in the same turn as the risk: the
operator types a prompt, the prompt names something that already exists, and
the fact arrives at the top of that turn.

That is the whole module. A small CURATED catalog of reactivable artifacts,
matched against the operator's own words, verified against the real filesystem
before anything is claimed.

## What belongs in the catalog

A REACTIVABLE ARTIFACT: a thing that exists and has a "how to turn it back on".
A service, a virtual environment, a site, a skill, a dataset, a document, a
pipeline.

A fact, a decision or a lesson does NOT belong here -- that is what memory is
for. The two are linked with the `memory:` field and never duplicated, because
two copies of one truth is one copy that will go stale unnoticed.

## Entry format

```
## <name>
aliases: the natural words the operator would actually type, comma-separated
path: /absolute/path
type: service|venv|site|skill|dataset|doc|pipeline
status: active|revivable|sunset|dead
resume: one line
reactivate: the command or procedure that brings it back
check: (optional) health command proving REAL life (rc 0 = alive)
project: parent project
memory: <agent>:<note-slug> (soft pointer into memory)
updated: YYYY-MM-DD
origin: human|auto|external (absent = human; auto/external never acts alone)
supersedes: name of the entry this one replaces (optional)
superseded_by: name of the entry that replaces this one (mark, NEVER delete)
sheet: (optional) working sheet whose content is injected on match
importance: optional integer (reserved, unused)
```

`aliases` carries the load. The match is on name and aliases, at word
boundaries, and nothing else: the catalog IS the relevance filter. Matching a
prompt against a raw filesystem index instead would surface a hundred plausible
paths and teach the agent to ignore the block.

`recall/CATALOG.example.md` ships as a format example. Every entry in it is
invented and none of the paths exists -- run the engine against it and it will
correctly report them as missing, which is the behaviour to expect from an
honest catalog. Copy it, empty it, point `HARNESS_RECALL_CATALOG` at your copy.

## The cycle: automatic draft, curation, catalog

A catalog maintained by hand is maintained until the week someone is busy, and
then it quietly becomes a liar. A catalog written entirely by machine fills up
with duplicates and near-misses. So the capture is automatic and the promotion
is not.

1. **Draft (automatic).** `recall-staging.py` runs on `PostToolUse` after a
   Write. When the written file looks like an artifact being born -- a service
   unit, a timer, a deliverable document -- it appends a `## draft-<name>`
   entry to the staging file with `origin: auto`. Never to the catalog. It
   skips transient trees, and it skips any path already covered by the catalog
   or the staging (search before add).
2. **Curation (periodic).** `curate.py` hands the catalog and the staging to a
   headless agent CLI with no tools, and gets back a JSON object. The model
   proposes; the script validates. The new catalog must reparse with the
   engine's own parser, and every existing entry must still be there. Then the
   staging is emptied and the actions are logged.
3. **Catalog (served).** Only the curated catalog is ever injected into a
   prompt. A draft is not served, because an unnamed, undeduplicated,
   unverified line is exactly the kind of entry that lies.

**Supersession marks, it never deletes.** When an artifact replaces another,
the old entry gets `superseded_by` and the new one gets `supersedes`. Both stay.
A deleted entry is an artifact somebody will rebuild from scratch in six weeks,
which is the exact failure this module exists to stop. The old entry stays
matchable -- someone asking about it is usually asking about its history -- but
it is floored in the ranking, never above something alive.

`origin` is the guardrail of the automatic half. An entry that a machine wrote
is RECALLED, and that is all: its `check:` is not executed, and the tag travels
into the injected block so the agent knows what it is reading.

## Why the live existence check matters

**An entry that lies is worse than no entry.** No entry costs a rebuild. A
lying entry costs the rebuild anyway, plus the time spent chasing a path that
is gone, plus the trust the agent had in the whole block. After one lie, the
right move is to ignore the module entirely.

So nothing is served on the strength of the catalog alone. At match time, for
every entry about to be injected:

- the `path:` is checked against the real filesystem. Gone: the block says so,
  in the block, next to the entry.
- the `check:` is run when the entry has one. `rc 0 = alive`. It answers a
  question presence cannot: a unit file on disk proves a file, not a service.
  "Exists" and "runs" are different claims, and only the second one means the
  thing is actually still there.
- the `updated:` date is compared to today. Past the staleness window, the
  entry is tagged as unreviewed rather than silently trusted.

Freshness has three states, not two: fresh, stale, and UNVERIFIABLE. The date
of "today" is INJECTED by the caller and no clock is read inside the engine, so
a missing date shows up as "staleness not verified" instead of quietly reading
as "nothing is stale".

A file index (`plocate`, refreshed by `recall-refresh.sh`) makes the existence
check cheap on a large home directory. It is an ACCELERATOR, never a
dependency: with no index installed, existence falls back to the disk and
answers the same thing. `tests/test_recall.py` proves that parity, with an
index and without one, because CI installs pytest and nothing else.

## The executable check, and why it is caged

`check:` is the most useful field and the most dangerous one. It is executed,
periodically, and it lives in a file a model rewrites at every curation pass.
The prompt tells the model not to touch that field. A prompt instruction is not
a constraint. Two independent barriers are:

1. **At execution.** No shell at all (`shell=False`), no shell metacharacter
   accepted, an ALLOWLIST of executables, and per-binary rules on top:
   `systemctl` is restricted to read-only verbs, `curl` may not write to disk
   or upload. `systemctl --user stop x` and `curl -o /var/tmp/x` are refused,
   not just `rm` and `bash`. A binary given as a path must resolve to the same
   file as the one on `PATH`, so a planted look-alike does not pass. Widening
   the allowlist is a code edit on purpose: it gets reviewed, and it cannot
   happen by editing a data file.
2. **At curation.** The `check:` lines are restored from the previous version
   of the catalog, always. A line added on a new entry is stripped, a modified
   line is reverted, a deleted line is put back. Every divergence is journaled
   loudly: hiding an attempt would be worse than the bug.

Either barrier alone would do. Both exist because the day someone restores
`shell=True` in the engine, nothing the model wrote will have reached the file
anyway.

A refusal is a distinct, visible outcome -- not silence, and not a failure. Five
different causes used to collapse into one null result, and a refused check
appeared nowhere at all.

## Fail-open, everywhere

Neither hook can break a turn.

- `recall-inject.py` runs on `UserPromptSubmit`, where exit 2 would ERASE the
  operator's prompt. It ALWAYS exits 0. The engine runs as a subprocess under a
  hard timeout, so an engine that hangs cannot hold up a prompt; a broken
  catalog, an unreadable stdin, a missing engine, a timeout -- each of those is
  one journal line and a prompt that goes through untouched.
- `recall-staging.py` is bookkeeping on `PostToolUse`. An unwritable staging
  file, a bad payload, an exception: exit 0, journal, move on. Losing a draft
  costs a curation pass. Blocking a tool costs the session.
- `curate.py` refuses rather than half-writes. Tidier unavailable, invalid
  output, an entry lost, a catalog that no longer parses: nothing is written
  and the previous catalog is intact.

Silence on a match is also correct, and it is not a failure: no entry matched
means nothing is known about this, and printing anything at all would be noise.
Silence in the JOURNAL is a different matter -- every execution writes its line,
whatever the outcome, because a gate that says nothing when it does nothing is
indistinguishable from a gate that has been unwired.

## Files

| Path | Role |
|---|---|
| `recall/recall.py` | engine: match, live verification, health pass, passive surface |
| `recall/curate.py` | curation: promote drafts, supersede WITHOUT deleting |
| `recall/recall-inject.py` | `UserPromptSubmit` hook: the block at prompt time |
| `recall/recall-staging.py` | `PostToolUse` hook: catch an artifact at birth |
| `recall/recall-refresh.sh` | rebuild the file index, then run the health pass |
| `recall/CATALOG.example.md` | EXAMPLE catalog (every entry invented) |
| `recall/STAGING.example.md` | EXAMPLE staging structure (empty by design) |
| `tests/test_recall.py` | 46 cases, zero network, zero LLM call |

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_RECALL_CATALOG` | Curated catalog | shipped example |
| `HARNESS_RECALL_STAGING` | Automatic draft file | `$HARNESS_STATE_DIR/recall/STAGING.md` |
| `HARNESS_RECALL_INDEX_DB` | File-index database | `$HARNESS_STATE_DIR/recall/fs-index.db` |
| `HARNESS_RECALL_INDEX_BIN` | Index query binary (optional) | `plocate` |
| `HARNESS_RECALL_SCOPE` | Tree indexed by the refresh script | `$HOME` |
| `HARNESS_RECALL_REPORT` | Freshness report | `$HARNESS_STATE_DIR/recall/freshness-report.md` |
| `HARNESS_RECALL_CURATE_LOG` | Curation log | `$HARNESS_STATE_DIR/recall/curate-log.md` |
| `HARNESS_RECALL_TODAY` | Today as `YYYY-MM-DD`, injected by the caller | unset = unverifiable |
| `HARNESS_RECALL_STALE_DAYS` | Staleness window, days | `45` |
| `HARNESS_RECALL_MAX_HITS` | Ceiling on injected entries | `4` |
| `HARNESS_RECALL_SHEET_MAX` | Ceiling on an injected working sheet, characters | `1800` |
| `HARNESS_RECALL_BOOT_MAX` | Ceiling on the passive surface, characters | `4800` |
| `HARNESS_RECALL_CURATE_TIMEOUT` | Hard timeout of the curation call, seconds | `300` |
| `HARNESS_LLM_CLI_NAMES` | Agent CLI binaries; the first is the tidier | `claude` |
| `HARNESS_RECALL_INJECT_GATE_DISABLE` | Session kill-switch, injection hook | unset |
| `HARNESS_RECALL_STAGING_GATE_DISABLE` | Session kill-switch, staging hook | unset |

A non-numeric or non-positive value for a numeric variable is ignored and the
default applies, so a typo cannot silently unbound a ceiling. A kill-switch is
journaled as `skip-disabled`: routing around the module stays visible.

## Wiring

```json
"UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command",
  "command": "python3 $HARNESS_HOME/recall/recall-inject.py --agent builder"}]}],
"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command",
  "command": "python3 $HARNESS_HOME/recall/recall-staging.py --agent builder"}]}]
```

The index refresh and the health pass belong on a timer:

```
recall/recall-refresh.sh
```

## Journal vocabulary

| Result | Meaning |
|---|---|
| `pass` | injection hook: nothing known about this prompt |
| `warn` | injection hook: a match was injected ("already built") |
| `observe` | staging hook: a draft was recorded |
| `fail-open` | unreadable stdin, engine unavailable or too slow, unwritable staging |
| `skip-disabled` | kill-switch set for the session |
| `skip-short-prompt` | injection hook: prompt too short to mean anything |
| `skip-no-engine` | injection hook: the engine is not next to the hook |
| `skip-tool` | staging hook: the tool was not a Write |
| `skip-no-match` | staging hook: the path is not an artifact being born |
| `skip-gone` | staging hook: the Write failed, there is nothing to catalog |
| `skip-covered` | staging hook: the path is already catalogued (search before add) |
