# Architecture

How this thing holds together, and why each load-bearing choice is the one it
is. If you want to run it, start with [docs/quickstart.md](docs/quickstart.md).
If you want to add to it, read [CONTRIBUTING.md](CONTRIBUTING.md) after this.

The premise in one line: **discipline that depends on a model's goodwill is not
a control.** Every mechanism below exists because a written rule was loaded,
agreed to, and violated in the same session. So the rules are enforced by
processes with exit codes, and the enforcement leaves a trace that something
else reads.

---

## 1. The hook contract

A gate is a standalone process. It has no framework, no plugin API, no import
from the agent runtime. It speaks four things:

| Direction | Channel | Content |
|---|---|---|
| in | stdin | one JSON object: the tool call the agent is about to make |
| out | exit code | `0` = allow, `2` = block |
| out | stderr | on a block, the text the agent reads and acts on |
| out | the journal | one line per execution, whatever the outcome |

The payload carries `tool_name`, `tool_input` (the tool's own arguments), and
context fields the harness uses: `session_id`, `cwd`, `transcript_path`,
`stop_hook_active`. A gate takes what it needs and ignores the rest.

That contract is small enough to drive by hand, which is exactly how the test
suites drive it and how the quickstart makes a gate bite:

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"sudo systemctl restart nginx"}}' \
  | python3 hooks/destructive-command-gate.py ; echo "exit=$?"
```

Three consequences worth stating out loud.

**Exit 2 is the only refusal.** Any other non-zero code is an accident and is
treated as one; the gate skeleton wraps `main()` so a crash exits 0.

**stderr is a message to a reader, not a log line.** The reader is the model
that just got refused, and it will act on what it says. Every block message in
this repository has the same four parts: what was blocked, why, **the working
alternative**, and the documented way out. A refusal that only says no teaches
the agent to route around gates, and it takes exactly one such gate to poison
the credibility of all the others.

**On `UserPromptSubmit`, exit 2 erases the operator's prompt.** So the hooks
wired on that event (`shield/shield-inject.py`, `recall/recall-inject.py`)
never exit non-zero at all, whatever happens inside them. Their output is
context added to the turn, never a veto.

## 2. Four invariants

These hold across every module. Break one and the harness stops being provable.

### 2.1 Fail-open: a bug in a gate never breaks the flow

Unreadable stdin, missing helper, unparsable regex, crash: exit 0 and journal
`fail-open`. The skeleton is literally this:

```python
try:
    from _hook import gate_stat
except Exception:
    sys.exit(0)          # a broken helper must never block the work
...
if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)      # a broken gate never blocks the work
```

The trade is deliberate and it is not the safe-looking one. A fail-CLOSED gate
that breaks freezes the pane on its own bug, which is the worst outcome
available and one this harness has already lived through: a hook whose file
stopped existing turned every prompt into a block, and the operator was locked
out of an open production session until a second agent restored the file. A
fail-open gate that breaks misses a violation, which is where you were before
the gate existed.

Two deliberate exceptions, both narrow, both documented where they live:

- **`interlock-gate`**: a missing or corrupt state file is a BLOCK, not a
  fail-open. Otherwise corrupting one JSON file picks the lock. The fail-open
  there covers hook errors only.
- **`scope-write-gate`'s bypass stamp**: the stamp is evaluated fail-CLOSED. A
  stamp that is missing, expired, corrupt, relative or that does not cover the
  target changes nothing and the block stands.

The rule of thumb: fail-open on the DETECTION, fail-closed on the BYPASS. A
broken detector should let work through; a broken authorization should not
authorize.

### 2.2 Every execution journals

One JSON line per hook execution, appended to `$HARNESS_GATE_STATS`
(default `$HARNESS_STATE_DIR/gate-stats.jsonl`), whatever the result.

```json
{"ts": "2026-08-05T12:06:42", "hook": "destructive-command", "result": "block", "pattern": "privilege escalation: sudo"}
```

The frozen vocabulary: `pass`, `block`, `deny`, `warn`, `skip-*`, `observe`,
`fail-open`. `gate_stat()` never raises: a journal that could crash a gate would
defeat the first invariant.

Free fields beyond `ts` / `hook` / `result` are the caller's responsibility.
Gates keep them short and structural (`pattern`, `path`, `check`, a command
truncated to ~120 characters).

Every string value is scrubbed by `gate_stat()` itself on the way in, through
`_hook.mask_secrets()`: gates journal the command or the path that tripped
them, and a command can carry a token. Scrubbing lives in the writer rather
than in each caller, because the one caller that forgets is the one that leaks,
and the journal would quietly become the least protected copy of a secret on
the machine. `governor/audit.py` and `watch/analyst.py` scrub a second time
over anything they paste into a review or hand to a judge. Still treat the
journal as readable by whoever can read your state directory, and do not put a
payload in it.

**Why this is an invariant and not a nice-to-have.** A gate that stays silent
when it has nothing to do is indistinguishable from a gate that has been
unwired. A hook once stayed dead for a full working day in production: the file
was on disk, the settings file still named it, nothing errored, and the gate it
enforced simply stopped existing. It was found by accident. A dead gate does not
scream; it goes quiet, and quiet reads exactly like a good day.

So the journal is the ONLY positive proof a gate is alive, and it is what the
sentinel cross-checks daily and what the governor's audit counts weekly. This
is also why a kill-switch journals `skip-disabled` instead of just returning:
routing around a gate is allowed here, and it is never silent.

The invariant holds on every path, including the boring ones: a payload for a
tool a gate does not cover journals `skip-not-bash` rather than returning in
silence. Three gates used to return quietly there. That was found while writing
this page, and fixed rather than documented as a quirk: an invariant with three
exceptions is not an invariant, it is a habit.

### 2.3 State lives under `$HARNESS_STATE_DIR`, never in the repository

Journals, stamps, markers, trial files, reports, ledgers, the observation
database, rendered settings: all of it under one directory, `~/.harness` by
default, overridable in one variable.

- the checkout stays something you can `git pull` without merge noise, and
  nothing you run writes into it;
- a state directory is disposable. Delete it and you lose history, never code;
- one operator can run several roles side by side by pointing them at different
  state directories, with no shared mutable file to race on;
- the test suites override the journal path to a tempdir, which is the only
  legitimate reason to override it. In production, never: the journal is what
  proves a gate is alive.

The corollary is that **nothing in the repository is armed by cloning it.** A
checkout is inert. Arming requires a settings file, exported variables, and in
the case of timers, a unit you installed yourself.

### 2.4 Zero third-party dependency

Standard library only, everywhere: `urllib` for the HTTP judge, `sqlite3` for
the observation index, `http.server` for the panel, `ast` for the Python diff,
`shlex` for command parsing. `pytest` is a test-time dependency and nothing
else installs.

This is not minimalism as a style. A guardrail whose availability depends on a
package resolving is a guardrail that disappears on the day the environment is
degraded, which correlates with the day you need it. It also means the CI
command and the laptop command are the same command.

**When an accelerator exists, there is a fallback AND a parity test.** Two live
cases:

| Accelerator | Falls back to | Parity proven by |
|---|---|---|
| `plocate` file index (recall existence check) | asking the disk directly | `tests/test_recall.py` T20/T21/T22: with an index, with no database, with no binary -- all asserted equal to `os.path.exists` |
| `PyYAML` (shield trigger registry) | a built-in parser for the single-line scalar subset | `tests/test_shield.py` T15: the fallback parse must equal the PyYAML parse of the shipped registry |

The parity test is the part that matters. Without it the fallback is a code path
nobody runs, which is a code path that is already broken.

## 3. The module map

```
                          hooks/_hook.py
                (gate_stat, read_stdin_json, mask_secrets,
                 STATE_DIR, GATE_STATS, transcript helpers)
                                 |
   +---------+---------+---------+---------+---------+---------+
   |         |         |         |         |         |         |
hooks/   memory/   shield/   recall/  sentinel/ governor/   watch/
12 gates  1 gate   2 hooks   2 hooks   health    2 judges   indexer
2 stamps           registry   engine   checks    proposal   server
                   rubric    curation            trial      analyst
                                                 audit      static UI
```

`hooks/_hook.py` is the **only** shared socket. Every other module reaches it
the same way, by path, and tolerates its absence:

```python
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
try:
    from _hook import gate_stat, read_stdin_json
except Exception:
    sys.exit(0)
```

Who depends on whom, in full:

| Module | Depends on | Nothing depends on it |
|---|---|---|
| `hooks/_hook.py` | stdlib only | (it is the socket) |
| `hooks/*-gate.py` | `_hook` | yes, each gate is a leaf |
| `memory/memory-verdict-gate.py` | `_hook` | yes |
| `shield/` | `_hook`, `shield/_registry.py` | yes |
| `recall/` | `_hook`, `recall/recall.py` (engine, reused by `curate.py` as its own validator) | yes |
| `sentinel/` | `_hook`; READS settings files and the journal | yes |
| `governor/` | `_hook` via `governor/_state.py`; `governor/judges.py` | yes |
| `watch/` | `_hook` via `watch/config.py`; READS transcripts and journals | yes |

Two properties fall out of that shape.

**Modules communicate through files, never through imports.** The sentinel does
not import a gate; it reads the settings file and the journal. The governor does
not call a gate; it counts journal lines. `watch` reads `.jsonl` on disk. So a
module can be deleted from a deployment and the others keep working, minus what
that module was reading.

**Every gate is a leaf.** Nothing imports a gate, so a gate can be added,
retired, or rewritten with a blast radius of exactly one file plus its suite
plus its line in a settings file.

`recall/recall.py` is the one place where a module reuses another module's
internals rather than a file: `curate.py` imports the engine so that the
engine's own parser IS the validator for anything an LLM proposed. Validating
with a second parser would let a catalog pass curation and fail at read time.

## 4. Isolating an LLM judge

Four places in this harness hand work to a model for a verdict: the shield
reviewer (layer 3), the governor's judge 1, the governor's judge 2 (a different
family, over HTTP), and the `watch` post-hoc analyst.

The failure they all guard against is specific and it has a price tag. An
anti-sycophancy benchmark launched the agent CLI headless from its own
directory. The CLI inherits the context of the directory it starts in, so it
loaded the project instruction file and could see the benchmark files sitting
next to it. Same prompt, same model, same day:

| Launched from | Context loaded | Behaviour |
|---|---|---|
| the benchmark directory | 59k tokens | announced it recognized an item of the corpus it was being fed |
| an empty directory | 18k tokens | no recognition |

It scored 100/100 because it could read the answer sheet. About 38 USD of quota
bought a perfect number that meant nothing, and the contamination was only
visible because someone compared the token counts of the two runs.

That is the generalization worth carrying: **a measurement that is wrong in the
FLATTERING direction produces no error, no crash and no anomaly. It looks
exactly like success, and nothing downstream will ever catch it.** A judge that
sees what it measures returns a compliment, and a compliment passes every test
you have.

So every judge call in this repository carries three mechanisms.

**1. Neutral working directory.** The judge runs with `cwd` set to a fresh empty
temporary directory (`tempfile.TemporaryDirectory`). It inherits no project
instruction file and sees none of the files it is grading. In `watch` the
neutral cwd has a second effect: the judge's own session is not written inside
an indexed transcript root, so the instrument stays out of its own measurement.

**2. Purged environment.** Every `ANTHROPIC_*` variable is stripped from the
child process. A judgment must never silently fall back onto a metered API key:
it runs on the interactive plan or it does not run. The failure it prevents is
not a wrong verdict, it is an invisible bill.

**3. Restricted input, and no hands.** The judge receives the artifact and the
rubric, and nothing else: not the prompt, not the project, not who wrote it. It
has no way to be helpful to the author, which is the point. Where the judge
could act, the tools are removed outright: `governor/judges.py` and
`watch/analyst.py` both pass
`--disallowedTools Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent,Task`.
The artifact is framed as an OBJECT OF ANALYSIS, never as an instruction, so the
worst case of a prompt injection inside a judged session is a lying verdict,
never an action.

Two more properties, because isolation alone is not enough.

**An absent judge is a status, never a yes.** A judge that does not answer
(endpoint unconfigured, key absent, CLI missing, timeout, unparsable verdict)
returns `None`. In the governor that routes the proposal to `pending-judge/`
with the explicit status `judge-unavailable`: not built, not queued, not
approved. An unreadable verdict is treated exactly like an absent one, because a
judge whose answer cannot be parsed did not judge, and inventing an opinion on
its behalf is the failure the module exists to prevent.

**Two judges, two families, no cross-talk.** The governor asks two judges, and
neither ever sees the other's verdict. A model grading its own kind shares the
blind spots it is supposed to find; a judge shown a previous opinion anchors on
it, and you pay twice for one opinion you then call a consensus. Judge 2 is a
configurable HTTP adapter precisely so it can be a different family; the code
cannot verify that it is, and it says so out loud.

The fake-verdict variables (`HARNESS_SHIELD_FAKE_VERDICT`,
`HARNESS_GOVERNOR_FAKE_JUDGE1/2`, `HARNESS_WATCH_FAKE_VERDICT`) exist so the
suites prove the routing, the storage and every failure path with zero network
call and zero key. Set in production, each one replaces a judge with a constant,
which is either a permanently blind reviewer or a permanently blocked pane.

There is a fifth LLM call in the repository and it is deliberately NOT a judge:
`recall/curate.py` hands the catalog and the auto-captured drafts to a headless
CLI and asks for a tidy-up. It purges `ANTHROPIC_*` and runs the model handless
(`--tools ""`, `--strict-mcp-config`), but it does not use a neutral cwd,
because it is not measuring anything and there is no answer sheet to read. What
protects it is a different mechanism: **the model proposes, the script
validates.** The result must reparse with the engine's OWN parser, every
pre-existing entry must still be present, and every executable `check:` line is
restored from the previous catalog whatever the model wrote. Nothing is written
if any of that fails.

## 5. The lifecycle of a gate

Nothing here arms itself. The pipeline exists because a working harness has a
rich man's problem: every incident produces a gate, and the sum quickly exceeds
what one human can audit. "Reads everything" degrades into "reads nothing"
within weeks, and governance nobody reads is theatre with a log file.

```
an INCIDENT                     (never an idea: see CONTRIBUTING.md)
   -> a proposal, five bounded fields
   -> governor/propose.py: two adversarial judges, independent, refusal default
        one rejected      -> archive/ + ledger line. Silence toward the human.
        a judge silent    -> pending-judge/. NEVER a default yes.
        two viable        -> a pitch of FIVE LINES to the human. One word: GO / no.
   -> GO: the gate runs in OBSERVATION for 7 days. It journals `observe`
          and BLOCKS NOTHING.
   -> governor/trial.py compiles what it WOULD have blocked: real catches,
      up to five samples -> the human answers: arm / discard.
   -> ARMED: a line in a settings file, added by a human.
   -> governor/audit.py, weekly, by exception: SILENT gates and NOISY gates.
      A gate doing its job is never mentioned.
```

The human is asked exactly twice in a gate's entire lifetime, each time about
something concrete, each time in one word. Everything expensive happens between
the machines. And every step before arming is a file, so every step before
arming is reversible by deleting it.

The observation phase is the part people skip and should not. A gate approved on
an argument is a gate whose false-positive rate is unknown; a gate that spent a
week journaling `observe` has a measured one, and discarding it costs nothing
because it never refused a single legitimate action.

## 6. What this system does NOT do

Stated as clearly as the rest, because a tool that is vague about its limits
gets trusted where it should not be.

**It does not replace human judgment.** Every gate certifies a SHAPE, never a
content. `memory-verdict-gate` checks that a note carries a verdict line and
that its status matches the frontmatter; it cannot check that the verdict is
true, and a verdict whose whole sentence is the word "ok" passes.
`destructive-command-gate` sees `sudo`; it has no idea whether that restart was
a good idea. The gates buy you the reflex and the trace. The decision stays
yours, and the one gesture that turns a proposal into a live gate is a human
editing a settings file.

**It does not arm itself.** No module in this repository writes a settings file,
enables a unit, installs a timer or turns a gate on. The governor writes files
and ledger lines and stops there. `watch`'s analyst can propose a gate; nothing
downstream reads that proposal, and a human has to carry it to the governor,
where it faces two judges and a human word. A judge that could arm what it
proposes would be a judge with an incentive.

**It has no inter-agent communication.** No bus, no queue, no manager process,
no shared mutable state between panes. One pane holds one role; the operator
drives each pane by hand; a result travels between roles only when a human
carries it, usually as a file, which is a thing you can read before you act on
it. This is the mechanism, not asceticism: a role that cannot receive
instructions from another agent has exactly one instruction source, so when a
pane does something surprising the list of things that could have told it to is
one item long. A poisoned instruction stays inside the process it started in.
And an unbuilt wire cannot be misconfigured, cannot silently reconnect after an
update, and needs no permission model.

**It is not a security boundary.** The gates are anti-drift instruments aimed at
a cooperative agent making mistakes, not a sandbox aimed at an adversary. They
are fail-open by design, they parse text with regular expressions, and every one
of them has a documented way out. A determined bypass is one `bash -c` away. If
you need containment, containment is a different layer: a container, a user
account, a filesystem.

**It watches gates, not people.** `watch` indexes byte offsets and metadata,
never message bodies; `HARNESS_WATCH_EXCLUDE` removes a role's transcripts
retroactively while its gate journals stay indexed, so hygiene stays provable
without the conversation being readable. The panel binds `127.0.0.1` and there
is no host variable, on purpose: a variable is a thing someone eventually sets
to a wildcard "just to test from the laptop" and then leaves that way.
