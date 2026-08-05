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

### recall

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_RECALL_CATALOG` | Curated catalog | shipped example |
| `HARNESS_RECALL_STAGING` | Auto-captured drafts | `$HARNESS_STATE_DIR/recall/STAGING.md` |
| `HARNESS_RECALL_INDEX_DB` | File-index database | `$HARNESS_STATE_DIR/recall/fs-index.db` |
| `HARNESS_RECALL_INDEX_BIN` | Index query binary (optional accelerator) | `plocate` |
| `HARNESS_RECALL_SCOPE` | Tree the refresh script indexes | `$HOME` |
| `HARNESS_RECALL_REPORT` | Freshness report | `$HARNESS_STATE_DIR/recall/freshness-report.md` |
| `HARNESS_RECALL_CURATE_LOG` | Curation log | `$HARNESS_STATE_DIR/recall/curate-log.md` |
| `HARNESS_RECALL_TODAY` | Today's date, injected by the caller | unset = undecidable |
| `HARNESS_RECALL_STALE_DAYS` | Staleness window, days | `45` |
| `HARNESS_RECALL_MAX_HITS` | Ceiling on injected entries | `4` |
| `HARNESS_RECALL_SHEET_MAX` | Ceiling on an injected sheet, characters | `1800` |
| `HARNESS_RECALL_BOOT_MAX` | Ceiling on the boot surface, characters | `4800` |
| `HARNESS_RECALL_CURATE_TIMEOUT` | Hard timeout of a curation pass, seconds | `300` |

### sentinel

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_SENTINEL_SETTINGS` | Settings files whose wiring is audited | `~/.claude/*settings*.json` |
| `HARNESS_SENTINEL_REPORT_DIR` | Where daily reports land | `$HARNESS_STATE_DIR/sentinel` |
| `HARNESS_SENTINEL_COVERAGE_DAYS` | Journal window for the coverage check, days | `7` |
| `HARNESS_SENTINEL_FRESHNESS_HOURS` | Journal freshness ceiling, hours | `24` |
| `HARNESS_SENTINEL_EXEMPT` | Extra colon-separated exemption globs | empty |
| `HARNESS_SENTINEL_ACTIVITY_PATHS` | Paths proving a session ran (silence becomes FAIL) | empty |
| `HARNESS_SENTINEL_PROBES` | Optional probe file; unset = probe family off | unset |
| `HARNESS_SENTINEL_PROBE_ALLOW` | Allowed probe commands (first word) | `test:curl:systemctl` |

### governor

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_JUDGE1_MODEL` | Model alias for the local CLI judge | CLI default |
| `HARNESS_JUDGE2_URL` | Second judge endpoint; unset = judge 2 unavailable | unset |
| `HARNESS_JUDGE2_MODEL` | Model id sent to that endpoint | unset |
| `HARNESS_JUDGE2_API_KEY_ENV` | NAME of the variable holding the key, never the key | unset |
| `HARNESS_GOVERNOR_TIMEOUT` | Hard timeout of one judge call, seconds | `300` |
| `HARNESS_GOVERNOR_SETTINGS` | Settings files whose hooks are audited | `~/.claude/settings.json` |
| `HARNESS_GOVERNOR_WINDOW_DAYS` | Audit window, days | `30` |
| `HARNESS_GOVERNOR_NOISY_MIN` | Blocks in the window that earn a review | `10` |
| `HARNESS_GOVERNOR_LEDGER` | Ledger path override | `$HARNESS_STATE_DIR/governor/ledger.jsonl` |

The second judge is a deployment choice, never a hard dependency: no provider
is named in the code. An absent judge produces an explicit `judge-unavailable`
status and routes to `pending-judge/`. It never produces a yes by default,
which is the whole point of having two.

Variables suffixed `_FAKE_JUDGE1`, `_FAKE_JUDGE2`, `_FAKE_VERDICT` exist for
tests only: they short-circuit an LLM call with a fixed verdict so the suites
never touch the network.

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
