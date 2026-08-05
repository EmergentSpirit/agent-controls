# shield -- behavioral rules that actually hold

A behavioral rule written in a system prompt is obeyed when the model feels
like obeying it. That is not a control, and the failure mode is specific: the
model agrees with the rule and violates it in the same breath. The shield is
the answer to that, and it is built on one observation.

**Position beats repetition.** The same sentence, moved from a standing
instruction to the top of the turn that is about to break it, changes the
outcome. So the shield does not write the rule harder. It delivers it later,
and it checks afterwards.

## Three layers, three moments

| Layer | Moment | Mechanism | Ships here |
|---|---|---|---|
| 1 | BEFORE the answer | the rule is injected when the prompt shows the risk | `shield-inject.py` |
| 2 | DURING the answer | a standing format constraint removes the slot where drift lives | your conventions line + `reviewer-rubric.example.md` |
| 3 | AFTER the answer, before display | a judge reads the outgoing message and can refuse it | `shield-reviewer.py` |

Layer 2 is the only one that is not a hook in this module: it is the standing
line every turn carries (system prompt, or a one-line `UserPromptSubmit`
reminder of your own). Its content is the rubric file, which is also what layer
3 enforces. The two are deliberately the same text: prevention and enforcement
must never drift apart, or you get a rule that is taught and not checked.

## Why three and not one

Each layer fails in a way the next one catches.

- **Layer 2 alone is a promise.** It is in context for the whole session and it
  competes with everything else in the window. Measured failure mode: a note
  stating exactly the right rule, loaded, agreed to, violated six times in one
  session.
- **Layer 1 alone is blind to what it cannot see coming.** A trigger is a
  regex on the prompt. Half the violations arrive on prompts that look
  perfectly innocent, and no pattern will ever catch those.
- **Layer 3 alone is expensive and late.** Judging every single turn with an
  LLM costs a call per turn, and it has no rule to judge against unless
  something told it what matters right now.

Wired together, layer 1 pays for layer 3. The injection ARMS the review: a
marker written at prompt time is what allows the reviewer to run at all. A
trigger armed is a review armed, so the expensive layer only fires on the turns
a cheap regex already flagged as risky. Everything else costs one journal line.

## Layer 1 -- `shield-inject.py`

`UserPromptSubmit` hook. It matches the prompt against the active entries of
the trigger registry, prints the matching rules into the turn's context
(prefixed `[shield]`, nothing else on stdout), and writes the marker under
`$HARNESS_STATE_DIR/shield/<agent>-trigger.json`.

The registry is data, not code: a YAML file of `pattern` / `rule` / `memory` /
`active` entries, editable without touching a hook. `active: false` parks an
entry instead of deleting it, so the reason it was written stays readable.
`shield/trigger-registry.example.yaml` ships as a format example -- every rule
in it is invented, and a trigger that does not come from a real incident is
noise.

**This layer never exits non-zero.** On `UserPromptSubmit`, exit 2 ERASES the
operator's prompt. Broken registry, broken regex, unreadable stdin: the prompt
goes through, and the journal says what happened.

## Layer 3 -- `shield-reviewer.py`

`Stop` hook. When armed, it takes the last assistant message of the transcript
-- the text about to be displayed -- and asks a headless agent CLI whether it
violates the rubric. A violation is exit 2: the answer is refused BEFORE
display, and the operator only ever sees the rewrite.

The rubric handed to the judge is the armed rules (from the registry, by slug)
followed by the standing invariants (the rubric file). Both are prepended
before the output, so the constant part of the prompt sits where a prompt cache
can reach it.

### The judge is isolated from what it judges

This is the part that is easy to get wrong, and getting it wrong produces a
measurement that looks perfect and means nothing.

- **Empty working directory.** The judge runs with `cwd` set to a fresh
  temporary directory. An agent CLI inherits the context of the directory it
  starts in: the project instruction file, the files sitting around it. Point
  it at the work it is grading and it reads the answer sheet. Measured once on
  an identical prompt: 59k tokens of context from the work directory versus 18k
  from an empty one, and the run from the work directory opened by ANNOUNCING
  that it recognized what it was being fed. That mistake cost about 38 USD of
  quota to learn.
- **Purged environment.** Every `ANTHROPIC_*` variable is stripped from the
  child process. A review must never silently fall back onto a metered API key;
  it runs on the interactive plan or it does not run.
- **Nothing but the rubric and the output.** The judge does not receive the
  prompt, the project, or who wrote the answer. It has no way to be helpful to
  the author, which is exactly the point.

### Zero cost at rest

Unarmed, the reviewer does no I/O beyond reading a missing file and writing one
journal line, and exits. No CLI is launched, no transcript is parsed. The
marker also has to match the CURRENT session and be fresher than
`HARNESS_SHIELD_FRESHNESS` (default 45 minutes), and it is consumed on use:
one arming, one review. A marker left behind by an abandoned session cannot
resurrect and judge a stranger.

"Zero cost" means zero LLM call, not zero journal line. Every execution logs,
armed or not. A gate that stays silent when it does nothing is
indistinguishable from a gate that has been unwired, and that is the failure
this whole harness refuses.

### Fail-open, everywhere

Missing CLI, timeout, non-zero exit, unparsable verdict, unreadable transcript,
corrupt marker, broken helper import: every one of those exits 0 and journals
`fail-open`. The only path to exit 2 is a judge that ran, answered, and said
`violation: true`.

The trade is deliberate. A reviewer that blocks when it breaks would freeze a
session on its own bug -- the worst outcome available, and one this harness has
already lived through with a different hook. A reviewer that fails open misses
a violation, which is what happened before the shield existed anyway.

The loop guard belongs to the same reasoning: a turn carrying
`stop_hook_active` is already a retry driven by a Stop hook, and blocking a
retry in a loop freezes the pane. Those turns pass and are journaled.

## Files

| Path | Role |
|---|---|
| `shield/shield-inject.py` | layer 1, `UserPromptSubmit` |
| `shield/shield-reviewer.py` | layer 3, `Stop` |
| `shield/_registry.py` | shared registry loader (PyYAML optional) |
| `shield/trigger-registry.example.yaml` | EXAMPLE trigger registry |
| `shield/reviewer-rubric.example.md` | EXAMPLE standing invariants |
| `tests/test_shield.py` | 17 cases, zero network |

`_registry.py` uses PyYAML when it is installed and falls back to a built-in
parser for the single-line scalar subset otherwise: the harness carries no
third-party dependency, and a missing library must not disarm a layer.

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_SHIELD_REGISTRY` | Trigger registry path | shipped example |
| `HARNESS_SHIELD_RUBRIC` | Standing invariants file | shipped example |
| `HARNESS_SHIELD_FRESHNESS` | Marker lifetime, seconds | `2700` |
| `HARNESS_SHIELD_TIMEOUT` | Hard timeout of the judge call, seconds | `12` |
| `HARNESS_SHIELD_MODEL` | Model alias for the judge | `haiku` |
| `HARNESS_LLM_CLI_NAMES` | Agent CLI binaries; the first is the judge | `claude` |
| `HARNESS_SHIELD_INJECT_GATE_DISABLE` | Session kill-switch, layer 1 | unset |
| `HARNESS_SHIELD_REVIEWER_GATE_DISABLE` | Session kill-switch, layer 3 | unset |
| `HARNESS_SHIELD_FAKE_VERDICT` | TEST ONLY: replaces the judge with a fixed verdict | unset |

A non-positive or non-numeric value for a numeric variable is ignored and the
default applies, so a typo cannot silently disarm a timeout. A kill-switch is
journaled as `skip-disabled`: routing around a layer stays visible.

`HARNESS_SHIELD_FAKE_VERDICT` exists so the suite can run with no network and
no quota. Set in production, it replaces the judge with a constant -- which is
either a permanently blind reviewer or a permanently blocked pane.

## Wiring

Both example launcher settings carry the two layers:

```json
"UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command",
  "command": "python3 $HARNESS_HOME/shield/shield-inject.py --agent builder"}]}],
"Stop": [{"matcher": "", "hooks": [{"type": "command",
  "command": "python3 $HARNESS_HOME/shield/shield-reviewer.py --agent builder"}]}]
```

The role name (`--agent`, or `HARNESS_AGENT`) names the marker file, so two
panes sharing a state directory never arm each other's reviewer.

Two tests in `tests/test_shield.py` read those settings files and fail if
either layer is missing from them. Unwiring a layer is then a red test rather
than a silence, because a gate that is no longer wired is a dead gate that lies
about being alive.

## Journal vocabulary

| Result | Meaning |
|---|---|
| `pass` | layer 1: no trigger matched. Layer 3: judged, no violation |
| `warn` | layer 1: a trigger matched, rule injected, reviewer armed |
| `block` | layer 3: violation, answer refused (exit 2) |
| `fail-open` | unreadable stdin, judge unavailable, unparsable verdict |
| `skip-disabled` | kill-switch set for the session |
| `skip-no-prompt` | layer 1: empty prompt |
| `skip-not-armed` | layer 3: no marker, or it belongs to another session |
| `skip-stale-marker` | layer 3: marker older than the freshness window |
| `skip-loop-guard` | layer 3: the turn is already a Stop-hook retry |
| `skip-no-text` | layer 3: nothing readable to judge |
