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
| `HARNESS_HOOK_DIRS` | Colon-separated directories holding live hooks | gate dir + `~/.claude/hooks` |
| `HARNESS_SETTINGS_STAMP` | Path of the "operator said go" stamp file | `$HARNESS_STATE_DIR/settings-go.stamp` |
| `HARNESS_OPERATOR_SCRIPT_DIRS` | Colon-separated dirs of scripts a human runs by hand | `~/operator-scripts` |
| `HARNESS_LLM_CLI_NAMES` | Colon-separated agent CLI binaries to watch | `claude` |
| `HARNESS_DESTRUCTIVE_COMMAND_FAMILIES` | Armed destructive families, comma-separated | `1,2,6` |
| `HARNESS_DESTRUCTIVE_COMMAND_SECRET_FILES` | Colon-separated secret-bearing filename markers | `.secrets:.age:authorized_keys:known_hosts` |
| `HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS` | Extra deny regexes, one per line | empty |
| `HARNESS_SCOPE_WRITE_STAMP` | Path of the scope-bypass stamp file | `$HARNESS_STATE_DIR/scope-write.stamp` |
| `HARNESS_INTERLOCK_SCRATCH_DIRS` | Colon-separated scratch dirs exempt from interlock | system temp + `/tmp` + `/var/tmp` |
| `HARNESS_SHIELD_REGISTRY` | Shield trigger registry path | `shield/trigger-registry.example.yaml` |
| `HARNESS_SHIELD_RUBRIC` | Shield standing-invariants file | `shield/reviewer-rubric.example.md` |
| `HARNESS_SHIELD_FRESHNESS` | Lifetime of the layer-1 marker, seconds | `2700` |
| `HARNESS_SHIELD_TIMEOUT` | Hard timeout of the shield judge call, seconds | `12` |
| `HARNESS_SHIELD_MODEL` | Model alias for the shield judge | `haiku` |
| `HARNESS_SHIELD_FAKE_VERDICT` | TEST ONLY: fixed verdict replacing the judge | unset |
| `HARNESS_<GATE_NAME>_GATE_DISABLE` | Per-gate session kill-switch (`=1`) | unset |

A kill-switch is deliberate and never silent: the gate lets the hit through
and journals it as `skip-disabled`, so routing around a gate stays visible.

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
