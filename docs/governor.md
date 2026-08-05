# governor -- scalable oversight of the gates

A harness that works has a rich man's problem: every incident produces a new
gate, and the sum of them quickly exceeds what one human can audit. The
bottleneck is not the machine, it is attention. A system that decides at machine
speed cannot be supervised by a human who reads everything, because "reads
everything" degrades into "reads nothing" within a few weeks, and a governance
nobody reads is theatre with a log file.

The governor is the answer to that, and it is a bandwidth argument before it is
a safety argument: **everything that reaches the human is short, lived, and has
already survived two adversarial judges; everything else is silence.**

Silence has to be earned, though. The whole module is built so that no silence
is ever produced by an absence.

## The cycle

```
proposal (five bounded fields, one lived incident)
   -> propose.py: TWO adversarial judges, INDEPENDENT (neither sees the other)
        judge 1: the local agent CLI, headless, empty cwd, no metered API
        judge 2: a configured HTTP endpoint, a DIFFERENT model family
   -> one `rejected`        : archive/ + ledger line. Silence toward the human.
   -> a judge did not speak : pending-judge/ + ledger `judge-unavailable`.
                              NEVER a default yes.
   -> two `viable`          : a pitch of 5 LINES, surfaced to the human.
        The human answers in one word: GO trial / no.
   -> GO : the gate runs in OBSERVATION for 7 days. It journals `observe`
           and BLOCKS NOTHING.
   -> trial.py compiles what it WOULD have blocked -> a review of real catches
        The human answers: arm / discard.
```

The point of the shape: the human is asked exactly twice, each time about
something concrete, and each time in one word. Everything expensive (reading a
proposal in full, arguing about false positives, judging a trigger's
determinism) happens between the machines.

## Two judges, and why they must be different families

Both judges receive the RAW proposal with a mandate to demolish it. Refusal is
the default verdict; `viable` has to be earned. Two properties are
load-bearing, and both are structural rather than promised:

- **Different families.** A model grading its own kind is a complacent judge:
  it shares the blind spots it is supposed to find. Two families have two
  different blind spots, and the union of what they miss is smaller than either
  one alone. This is why judge 2 is a configurable adapter and not a second
  call to the same CLI. It is on the operator to point it at a genuinely
  different family; the code cannot verify that, and it says so out loud.
- **Independence.** Neither judge ever sees the other's verdict. A judge shown
  a previous opinion anchors on it: you pay twice, you get one opinion, and you
  call it a consensus. The two calls are separate by construction, and the code
  carries a note telling the next reader not to "optimize" them into one.

The proposal is presented as an OBJECT OF ANALYSIS, never as an instruction. A
proposal is text of unknown origin flowing into a model that has been asked for
a verdict; treating it as data is the difference between a judge and a puppet.

## An absent judge is a status, never a yes

This is the mechanism the rest of the module hangs on.

A judge that does not answer -- endpoint not configured, key absent, CLI
missing, timeout, rate limit, unreadable verdict -- returns `None`, and the
proposal is routed to `pending-judge/` with the explicit status
`judge-unavailable`. It is not built, not queued, not counted as approved. The
pitch says which judge was missing and why.

An unreadable verdict is treated the same way as an absent one: a judge whose
answer cannot be parsed did not judge, and inventing an opinion on its behalf
is exactly the failure this whole module exists to prevent.

The rule is symmetric. Judge 1 missing holds the proposal just as judge 2
missing does; neither is the optional one. The test suite proves both
directions, because a rule that only holds in the direction someone remembered
to test is a rule that will fail in the other one.

## The trial: nothing is armed on an argument

A new gate approved by the human does NOT start armed. It starts in
OBSERVATION: it detects exactly as it will once armed, journals
`result: "observe"`, and exits 0. It blocks nothing and refuses nothing. Cost:
one journal line per hit.

```python
if os.path.exists(trial_file):          # inside the gate, during the trial
    gate_stat(STEM, "observe", why=reason)
    sys.exit(0)                         # we note, we do not block
gate_stat(STEM, "block", why=reason)
sys.exit(2)
```

At the end of the window, `trial.py` compiles what the gate WOULD have blocked
and files a review: the count, up to five real samples, and one question --
arm it or throw it away. The human judges lived catches, never an idea. A gate
that caught nothing in a week, or caught the wrong things, is discarded at zero
cost: it never refused a single legitimate action.

This is also what makes the whole pipeline reversible. Every step before arming
is a file, and files are deleted without consequence.

## The audit: by exception, not by inventory

`audit.py` runs weekly. It reads the last 30 days of the gate-stats journal --
the same journal every gate writes to on every execution -- and reports ONLY
the lines where a decision is actually pending:

- **SILENT**: a wired script whose stem produced no journal event in the
  window. Dead weight, or a broken gate; both deserve a word. A gate that is
  wired and mute is worse than no gate, because it is believed.
- **NOISY**: at or above the threshold of `block`/`deny` events in the window,
  with the three most recent samples pasted in, so the false-positive review
  happens on evidence rather than on a feeling.

A gate doing its job is never mentioned. When nothing is pending, the decision
page is DELETED rather than left behind saying yesterday's news: the silence
has to be true. Every run appends one aliveness line to `audit-log.jsonl`,
because an audit timer that quietly stopped firing is itself a dead gate.

No LLM is involved here. The judges exist for NEW proposals; the existing
battery is judged on its own numbers, and those are counted, not interpreted.

The liveness check deserves a note, because the obvious implementation is
wrong. Guessing a gate's journal stem from its file name, or from a regex over
its code, wrongly accused four healthy gates in two runs: naming conventions
vary from one hook to the next. The heuristic that holds is the textual
presence, in the script's source, of a stem actually OBSERVED in the journal.
Its residual error is to miss a truly mute gate, never to accuse a live one --
which is the right way round, since an audit that cries wolf gets ignored, and
an ignored audit protects nothing.

## What the governor never does

- **It never arms anything.** It writes files and ledger lines. Turning a
  proposal into a live gate is a separate human-approved gesture, and it is the
  one step of the pipeline that a poisoned text cannot walk through.
- **It never edits a hook, a settings file, or a gate's code.**
- **It never deletes what it cannot read.** A corrupt trial file is reported
  and kept.
- **It never hides a decision.** Rejections are silent toward the human but
  written to `archive/` with the judges' reasons, and every routing decision
  appends a ledger line. Silence toward the operator is not absence of record.

The human veto is permanent and it is the only thing that arms a gate. The
governor's job is to make that veto cheap to exercise: five lines to read, one
word to answer, twice per gate in its entire lifetime.

## Configuring judge 2

Judge 2 is a generic HTTP adapter over a chat-completions shaped endpoint,
driven entirely by the environment. No provider is named anywhere in the code,
and no key is ever stored in it: `HARNESS_JUDGE2_API_KEY_ENV` holds the NAME of
the variable that holds the key, so the secret lives in your vault or your
launcher, and the harness only knows where to look.

```sh
export HARNESS_JUDGE2_URL="<your chat-completions endpoint>"
export HARNESS_JUDGE2_MODEL="<a model from a DIFFERENT family than judge 1>"
export HARNESS_JUDGE2_API_KEY_ENV="MY_VAULT_VAR"   # the NAME, not the key
```

A self-hosted or local endpoint works the same way; leave
`HARNESS_JUDGE2_API_KEY_ENV` unset and no `Authorization` header is sent. If
none of this is configured, the governor still runs, and every proposal lands
in `pending-judge/`. That is the intended behaviour of an unconfigured
governor: it holds, it does not wave things through.

The adapter recognizes response SHAPES, not vendors, so a new endpoint that
speaks one of the usual shapes needs no code change. It uses `urllib` from the
standard library: the harness installs nothing in order to govern itself.

## Files

| Path | Role |
|---|---|
| `governor/judges.py` | the two adversarial judges (local CLI + HTTP adapter) |
| `governor/propose.py` | the conveyor: parse, judge, route, journal |
| `governor/audit.py` | weekly audit of the live gates, by exception |
| `governor/trial.py` | open an observation trial, close the due ones |
| `governor/proposal.example.md` | the proposal format, entirely invented |

State, under `$HARNESS_STATE_DIR/governor/`:

| Path | Contents |
|---|---|
| `to-build/` | approved, technical class: the build queue |
| `awaiting-operator/` | life class pitches, and closed trial reviews |
| `pending-judge/` | a judge did not speak; nothing was concluded |
| `archive/` | killed by a judge, with the reasons, auditable |
| `verdicts/` | raw verdicts of both judges, per proposal |
| `trials/` | open observation windows (`<stem>.json`) |
| `audit-decisions.md` | pending decisions; DELETED when there are none |
| `audit-log.jsonl` | one aliveness line per audit run |
| `ledger.jsonl` | one line per governance decision |

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_JUDGE2_URL` | judge 2 endpoint; unset = judge 2 unavailable | unset |
| `HARNESS_JUDGE2_MODEL` | model id sent to that endpoint | unset |
| `HARNESS_JUDGE2_API_KEY_ENV` | NAME of the variable holding the API key | unset |
| `HARNESS_JUDGE1_MODEL` | model alias for the local CLI judge | CLI default |
| `HARNESS_LLM_CLI_NAMES` | agent CLI binaries; the first is judge 1 | `claude` |
| `HARNESS_GOVERNOR_TIMEOUT` | hard timeout of a judge call, seconds | `300` |
| `HARNESS_GOVERNOR_SETTINGS` | settings files whose hooks are audited | `~/.claude/settings.json` |
| `HARNESS_GOVERNOR_WINDOW_DAYS` | audit window, days | `30` |
| `HARNESS_GOVERNOR_NOISY_MIN` | block/deny count that triggers a review | `10` |
| `HARNESS_GOVERNOR_LEDGER` | ledger path override | `<governor>/ledger.jsonl` |
| `HARNESS_GOVERNOR_FAKE_JUDGE1` | TEST ONLY: fixed verdict replacing judge 1 | unset |
| `HARNESS_GOVERNOR_FAKE_JUDGE2` | TEST ONLY: fixed verdict replacing judge 2 | unset |

The two `FAKE` variables exist so the test suite can prove the routing --
including the unavailable-judge path -- without a single network call or a
single key. Setting either one in production replaces a judge with a constant,
which is the one thing this module is built to prevent.
