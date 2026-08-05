# Naming and conventions (frozen at batch 1)

Names, environment variables and status vocabulary are frozen here. Changes
after batch 1 require a justification in the batch handoff.

## Modules

| Concept | Directory |
|---|---|
| Cross-cutting hook helper + gates | `hooks/` |
| Behavioral rule enforcement (3 layers) | `shield/` |
| Statutory memory format + its gate | `memory/` |
| "Already built" artifact catalog | `recall/` |
| Daily self-discovering health checks | `sentinel/` |
| Gate governance (adversarial judges) | `governor/` |
| Read-only observation panel | `watch/` |
| Agent launcher templates | `launchers/` |

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_STATE_DIR` | State directory | `~/.harness` |
| `HARNESS_GATE_STATS` | Gate-stats journal path | `$HARNESS_STATE_DIR/gate-stats.jsonl` |
| `HARNESS_AGENT` | Role name for this pane | `agent` |
| `HARNESS_WRITE_SCOPE` | Colon-separated write perimeter | session cwd |
| `HARNESS_PROTECTED_SETTINGS` | Colon-separated protected config paths | `~/.claude/settings.json` |
| `HARNESS_MAX_RESPONSE_WORDS` | Response length ceiling | `350` |

Test suites override `HARNESS_GATE_STATS` to a tempdir. Never override it in
production: the journal is what proves a gate is alive.

## Statutory memory vocabulary

Frontmatter carries `status:` from a closed list, and `superseded_by:` when
the status is `superseded`:

| Status | Index marker |
|---|---|
| `active` | (none) |
| `discarded` | ⛔ |
| `stale` | ⚠ |
| `superseded` | 🔁 |
| `dormant` | 🌙 |

The body opens with `**VERDICT — <status>.** <one sentence>`. When the verdict
contradicts the rest of the body, the verdict wins. The index line carries the
marker at the head of its summary: the index is read first, it must never lie.

## Roles

Published code takes a role via `--agent <name>` or `HARNESS_AGENT`. The
example settings ship two: `builder` (write-heavy, full mutation gate battery)
and `researcher` (read-heavy, scope-write + response-length gates).

## Gate conventions

- Journal results: `pass`, `block`, `deny`, `warn`, `skip-*`, `observe`,
  `fail-open`.
- Exit codes: `0` = allow, `2` = block (stderr is shown to the agent).
- Every gate is fail-open and logs every execution, whatever the outcome.
