# Contributing

Read [ARCHITECTURE.md](ARCHITECTURE.md) first: the four invariants there are not
style preferences, and a patch that breaks one will be sent back even if it
works.

---

## The rule of gold

**A module without its suite does not ship.** Not "add tests later", not "the
tests are in a branch". The suite travels in the same commit as the code.

This is a product argument before it is a hygiene argument. Everything in this
repository claims to enforce something. A claim about enforcement is worth
exactly the evidence attached to it, and the evidence is a green CI run over
tests that fail when the enforcement is removed. **The green suite in CI IS the
product.** Remove it and what is left is a directory of scripts asserting they
are careful, which is the thing this harness was built to stop believing.

CI is [.github/workflows/ci.yml](.github/workflows/ci.yml), two jobs, and it is
short on purpose:

| Job | What it runs |
|---|---|
| `pytest` | `python -m pytest tests/ -v` on ubuntu-22.04 and ubuntu-24.04, Python 3.10 and 3.12 |
| `shellcheck` | `shellcheck` over every `*.sh` in the tree |

The matrix is the interesting part: four combinations, one install step
(`pip install pytest`), no service container, no key, no network. If your change
needs anything more than that to be tested, the change is wrong, not the CI.

Current state of the suites, for reference:

```
264 passed, 148 subtests passed in 6.84s
```

---

## The most important rule: a gate is born from an INCIDENT

**Not from a good idea. Not from a best practice. Not from something that could
go wrong.** From a thing that went wrong, that you can date, that cost
something.

Here is why this is the hard rule rather than a preference.

A gate is not free. It costs a fraction of a second per tool call, a line of
configuration, a page of documentation and a suite to maintain. Those are the
cheap costs. The expensive one is **false positives**, and a false positive is
not a small annoyance: it is the moment an agent, or a human, learns that the
guardrails get in the way of real work. From that moment on, the reflex is to
route around gates, and the routing is indiscriminate. One noisy gate does not
cost you one gate. It costs you the credibility of every other gate beside it,
including the one that would have caught the expensive mistake next month.

A rule derived from a real incident has a known cost/benefit: something already
went wrong, so the benefit is at least one repetition avoided, and the shape of
the incident tells you exactly where to draw the line so that ordinary work is
untouched. A rule derived from an idea has an unknown benefit and an unbounded
false-positive rate, because nobody has ever seen it fire.

There is a second reason, and it is about the message. Every block in this
repository explains why it is blocking. A gate born from an incident can say
"this exact shape lost 144 extractions in one production run", and that sentence
gets obeyed. A gate born from an idea can only say "this is not best practice",
and that sentence gets argued with.

So the test is simple and it is not negotiable:

> **If you cannot tell the story of the time this hurt, the gate has no place
> here.**

Not a hypothetical story. What happened, when, what it cost, and what the wrong
result looked like from the inside, which is usually the valuable half: most of
the incidents in this repository were expensive precisely because the failure
looked like a success. Write that story into
`docs/gates/<name>.md` under **Founding incident**, and write it in enough
detail that a stranger can judge whether your line is drawn in the right place.

Look at the twelve pages in [docs/gates/](docs/gates/) before writing yours.
Every one of them names a real day. Some of them name the amount of money the
lesson cost.

If you have a good idea and no incident: it is not rejected, it is not ready.
Run it in OBSERVATION first (see below). A week of `observe` lines is exactly
the incident report you were missing, or exactly the proof the idea was noise.

---

## Adding a gate

### 1. The skeleton

One file, `hooks/<name>-gate.py`, standard library only. This is the full
shape, and every existing gate follows it:

```python
#!/usr/bin/env python3
"""PreToolUse gate on <Tool>: one sentence saying what it refuses.

WHY (production post-mortem): the incident, in three to ten lines. What was
measured, what the wrong result looked like, what it cost. This docstring is
read by whoever debugs the gate at 6 in the morning.

WORKAROUND to hand the agent: the shape that works.

Disarm (one session): HARNESS_<NAME>_GATE_DISABLE=1
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import gate_stat
except Exception:
    sys.exit(0)          # fail-open: a broken helper must never block the work

HOOK = "<name>"                                   # NO "-gate" suffix
DISABLE_ENV = "HARNESS_<NAME>_GATE_DISABLE"
WATCHED_TOOL = "<Tool>"


def main() -> int:
    if os.environ.get(DISABLE_ENV) == "1":
        gate_stat(HOOK, "skip-disabled")
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        gate_stat(HOOK, "fail-open")
        return 0          # unreadable input: never block blindly
    if not isinstance(payload, dict):
        gate_stat(HOOK, "fail-open")
        return 0
    if payload.get("tool_name", "") != WATCHED_TOOL:
        gate_stat(HOOK, "skip-out-of-scope")
        return 0

    hit = detect(payload)
    if not hit:
        gate_stat(HOOK, "pass")
        return 0

    gate_stat(HOOK, "block", why=str(hit)[:70])
    sys.stderr.write(
        "BLOCKED (<name> gate): what was refused.\n"
        "WHY it is refused, in one or two sentences a model will act on.\n"
        "Do this instead: <the working alternative>.\n"
        "Session kill-switch: %s=1\n" % DISABLE_ENV
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)       # fail-open: a broken gate never blocks the work
```

Non-negotiable points in that skeleton:

- **exit 0 or exit 2, nothing else.** Any other non-zero code is an accident.
- **the outer `try` is not optional.** Fail-open on the detection is what makes
  a gate safe to wire. If your gate has a fail-CLOSED path (a bypass stamp, a
  state file that would otherwise be pickable), say so in its page and prove it
  in the suite, the way `interlock-gate` and `scope-write-gate` do.
- **the stderr message ends with an alternative**, not with a refusal.
- on `UserPromptSubmit`, **never return 2 at all**: exit 2 there erases the
  operator's prompt.

### 2. Journal conventions, frozen

`gate_stat(HOOK, result, **fields)` on **every** path, including the ones that
do nothing. The result vocabulary is closed:

| Result | When |
|---|---|
| `pass` | the gate ran, found nothing, allowed |
| `block` | refused, exit 2 |
| `deny` | refused by a hard-deny layer (counted with `block` in the audits) |
| `warn` | something was injected or flagged without refusing |
| `skip-<reason>` | out of scope, exempt, unarmed, or deliberately let through |
| `observe` | detected exactly as if armed, but blocking nothing (trial mode) |
| `fail-open` | the gate itself failed and stepped aside |

`skip-*` is an open family with a closed shape: the suffix names the reason in
kebab-case (`skip-disabled`, `skip-out-of-scope`, `skip-not-bash`,
`skip-no-path`, `skip-stamp`, `skip-authorized`, `skip-loop-guard`). Invent the
suffix you need, list it at the top of your gate page, and never reuse one for
two different reasons.

Two naming rules that are easy to get wrong:

- **`HOOK` carries no `-gate` suffix.** The file is `home-prefix-gate.py`, the
  journal stem is `home-prefix`. The sentinel normalizes the file stem
  (drop `.py`, underscores to dashes, drop a trailing `-gate` or `-hook`) and
  matches it against the journal. A gate journaling under a name unrelated to
  its filename produces a false WARN forever.
- **the kill-switch is `HARNESS_<NAME>_GATE_DISABLE`**, with `<NAME>` the file
  stem upper-cased and dashes turned into underscores:
  `hooks/scope-write-gate.py` -> `HARNESS_SCOPE_WRITE_GATE_DISABLE`. It is
  checked FIRST, and it journals `skip-disabled` before returning 0. A kill
  switch that returns silently is a hole; one that journals is a deliberate,
  countable decision.

Two shipped gates deliberately have NO kill-switch (`interlock-gate`,
`hook-retire-gate`). Read their pages before copying that choice: in both cases
the founding incident was a guardrail that shipped switched off. Standing those
down means removing the line from the settings file, which is a visible,
reviewable gesture.

### 3. The page: `docs/gates/<name>.md`, three sections

Same three headings as the other twelve, in this order:

```markdown
# <name>-gate

PreToolUse gate on `<Tool>`. Exit 2 = block, fail-open everywhere, every
execution logs one line to the gate-stats journal (`pass`, `block`,
`fail-open`, `skip-disabled`, ...).

## What it blocks

The exact shapes, with examples that fire. Then, and this half matters as much:
what it does NOT block, and why the false-positive rate is what it is.

## Founding incident

The story. What happened, when, what it cost. If you cannot write this
section, stop here.

## Legitimate exception path

The normal route first (usually: do the thing correctly, it is one line).
Then the sanctioned bypass, if there is one. Then the session kill-switch,
last, because it is the least good answer.
```

The "what it does NOT block" half is not padding. It is the claim you are making
about false positives, and it is what the reviewer checks your tests against.

### 4. The suite: `tests/test_<name>_gate.py`

Drive the hook as a SUBPROCESS with JSON on stdin, exactly the way the real
harness does. Never import the gate and call `main()`: that tests a function,
not a hook.

```python
HERE = Path(__file__).resolve().parent
HOOK = HERE.parent / "hooks" / "<name>-gate.py"


def run_hook(stdin_text: str, env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("HARNESS_<NAME>_GATE_DISABLE", None)   # isolate from the caller
    env.update(env_extra)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=30)


class TestMyGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stats = Path(self.tmp.name) / "stats.jsonl"
        self.env = {"HARNESS_GATE_STATS": str(self.stats)}   # NEVER the real one
```

The minimum the suite must cover:

1. the nominal case passes, exit 0, **empty stderr**;
2. the founding-incident payload blocks, exit 2, and the stderr carries the
   alternative (assert on the substring, so a rewrite that drops the way out
   fails the suite);
3. each documented false positive from the "what it does NOT block" list;
4. the kill-switch: exit 0 and `result == "skip-disabled"`;
5. fail-open: garbage on stdin, exit 0, `result == "fail-open"`;
6. the journal line itself: `result` and the fields you promised on the page.

Assert on the journal, not only on the exit code. The journal is the aliveness
signal the sentinel reads; a gate that blocks correctly and journals nothing is
a gate that will be reported dead.

### 5. Make sure the test BITES

A suite that passes proves nothing until you have seen it fail. Before you open
the pull request, unplug the gate by hand and check the suite goes red.

The cheapest mutation is turning the refusal into an allow:

```sh
# in hooks/<name>-gate.py, change the block path:
#     return 2      ->      return 0
python3 -m pytest tests/test_<name>_gate.py -q
```

Real run of that mutation on a shipped gate, so you know what "red" looks like:

```
>       self.assertEqual(r.returncode, 2, r.stderr)
E       AssertionError: 0 != 2 : BLOCKED (HOME= prefix gate): reassigning HOME as a command prefix BREAKS the Bash tool's output capture [...the rest of the gate's stderr, cut here for width...]

tests/test_home_prefix_gate.py:55: AssertionError
=========================== short test summary info ============================
SUBFAILED(cmd='env HOME=/tmp/x python3 -V') tests/test_home_prefix_gate.py::TestHomePrefixGate::test_env_and_export_variants_block
SUBFAILED(cmd='env -i HOME=/tmp/x python3 -V') tests/test_home_prefix_gate.py::TestHomePrefixGate::test_env_and_export_variants_block
SUBFAILED(cmd='export HOME=/tmp/x') tests/test_home_prefix_gate.py::TestHomePrefixGate::test_env_and_export_variants_block
SUBFAILED(cmd='FOO=1 HOME=/tmp/x ./run.sh') tests/test_home_prefix_gate.py::TestHomePrefixGate::test_env_and_export_variants_block
SUBFAILED(cmd='echo ok && HOME=/tmp/x python3 -V') tests/test_home_prefix_gate.py::TestHomePrefixGate::test_env_and_export_variants_block
FAILED tests/test_home_prefix_gate.py::TestHomePrefixGate::test_home_prefix_blocks_with_message
6 failed, 7 passed, 3 subtests passed in 0.18s
```

Notice what went red: every block assertion AND every subtest variant, each one
naming the exact command it expected to be refused. Then restore the file and
re-run:

```
8 passed, 8 subtests passed in 0.17s
```

If the mutation leaves the suite green, your tests assert on the wrong thing,
and the gate is untested no matter how many cases it has. Worth mutating too:
delete the `gate_stat(...)` on the block path (a suite that asserts on the
journal goes red), and make `detect()` always return nothing (every block case
goes red).

**Do not commit the mutation.** `git status` before you push.

### 6. Wire it, or do not

Adding a gate to `launchers/settings.example.json` is a separate decision from
adding the gate. The examples are not an inventory of everything that exists;
they are two opinionated starting points (`builder`, write-heavy;
`researcher`, read-heavy). A new gate can perfectly well ship unwired, with its
page explaining who should wire it.

If you do wire it into an example, the sentinel's `coverage` check will start
asking about it daily, which is the correct pressure.

### 7. Prefer a trial to an argument

If you are not sure the line is drawn in the right place, do not argue about it.
Ship the gate in OBSERVATION: detect exactly as you would once armed, journal
`observe`, exit 0.

```python
if os.path.exists(trial_file):          # inside the gate, during the trial
    gate_stat(HOOK, "observe", why=reason)
    sys.exit(0)                         # we note, we do not block
gate_stat(HOOK, "block", why=reason)
sys.exit(2)
```

A week of `observe` lines tells you the false-positive rate, gives you real
samples for the page, and costs nothing if the answer is "discard": the gate
never refused a legitimate action. See [docs/governor.md](docs/governor.md) for
the full cycle.

---

## Hard constraints on any patch

**Zero third-party dependency.** Standard library only, in every module. If you
need an accelerator (a faster index, a real YAML parser), it must be OPTIONAL,
with a stdlib fallback AND a parity test proving the two agree. The two live
examples are `plocate` in `recall` (`tests/test_recall.py` T20-T22) and PyYAML
in `shield` (`tests/test_shield.py` T15). A fallback without a parity test is a
code path nobody runs, which is a code path that is already broken.

**Zero network call in the tests.** No API key, no endpoint, no fixture
downloaded at test time. Every LLM judge in the harness has a fake-verdict
variable for exactly this: `HARNESS_SHIELD_FAKE_VERDICT`,
`HARNESS_GOVERNOR_FAKE_JUDGE1`, `HARNESS_GOVERNOR_FAKE_JUDGE2`,
`HARNESS_WATCH_FAKE_VERDICT`. Set one and the call is short-circuited with a
constant, so the suite proves the routing, the storage and every failure path
(including "the judge did not answer") without spending a cent. If you add a
judge, add its fake variable in the same commit, and document it in
[docs/naming-table.md](docs/naming-table.md) as TEST ONLY.

**Never touch the real journal from a test.** Override `HARNESS_GATE_STATS` to a
tempdir in `setUp`. A suite that appends to the operator's journal corrupts the
sentinel's coverage window and the governor's noise counts.

**English only, and no accented characters.** Anywhere: code, comments, tests,
documentation, commit messages, block messages. This check must return nothing:

```sh
grep -rlP "[\x{00C0}-\x{024F}]" --include='*' . | grep -v '^./.git/'
```

That range covers every accented Latin letter, and it currently matches nothing
in the tree. The only non-ASCII characters in the repository today are
typographic (em dash, ellipsis, arrow, the box-drawing rules some docstrings
use, guillemets in the secret-masking placeholder) and the four
statutory-memory status markers, which are part of a documented format. Do not
add new ones, and never add a localized string: a block message is read by a
model, and an operator, and a grep.

**No identity, no secret, no private path.** No personal name, no hostname, no
IP, no `/home/<someone>`, no API key, not even in a comment or a test fixture.
Example files are named `*.example.*` and everything in them is invented:
`recall/CATALOG.example.md`, `shield/trigger-registry.example.yaml`,
`governor/proposal.example.md`. Run the engine against them and they correctly
report their own paths as missing, which is the behaviour to expect from an
honest example.

**Nothing writes into the repository at runtime.** State goes under
`$HARNESS_STATE_DIR`. A module that drops a file next to its own source has
broken invariant 2.3, and it will show up as a `git status` diff on somebody
else's machine.

**Nothing arms itself.** No patch may edit a settings file, enable a unit,
install a timer or turn a gate on. Ship the example, document the gesture, let
a human make it.

---

## Changing something that already exists

**A frozen name stays frozen.** [docs/naming-table.md](docs/naming-table.md)
freezes the environment variables, the journal vocabulary and the module names.
Changing one is not forbidden, it is expensive: it breaks every deployed
settings file and every operator's muscle memory. Bring a reason, and update the
table in the same commit.

**Widening a gate's perimeter follows the journal, never the imagination.**
`destructive-command-gate` ships with three of its six families armed for
exactly this reason: the perimeter widens on what actually fires, never on what
feels dangerous in the abstract. An early draft of that gate would have
protected the whole state directory, which is written on every single call, and
would have blocked the harness against itself within minutes.

**A gate that is wrong for real work is a bug in the gate.** If a shipped gate
fights your legitimate mandate every day, the answer is not the kill-switch: it
is an issue with the journal lines pasted in. Routing around a gate silently is
what the journal exists to expose, including when the person doing it is right.

**Retiring a gate is a two-step gesture.** Remove its line from every settings
file, restart every pane, and only then delete the file. A pane reads the
settings of its OWN boot: a `UserPromptSubmit` hook whose file has stopped
existing exits non-zero, and a non-zero hook on that event is a block. That is
how a production pane once locked itself out of every prompt it tried to send.
`hook-retire-gate` exists to catch exactly that gesture; do not route around it
with the settings-GO stamp unless you have actually restarted the panes.

---

## Pull request checklist

- [ ] the founding incident is written down, dated, and real
- [ ] `docs/gates/<name>.md` has the three sections, including what it does NOT block
- [ ] `tests/test_<name>_gate.py` covers pass / block / documented false positives / kill-switch / fail-open / the journal line
- [ ] the suite BITES: `return 2` -> `return 0` turns it red, and you saw it
- [ ] the mutation is reverted (`git status` is clean of it)
- [ ] `python -m pytest tests/ -q` is green locally
- [ ] `shellcheck` is clean on any `*.sh` you touched
- [ ] stdlib only; any accelerator has a fallback and a parity test
- [ ] no network, no key, no real journal path in the tests
- [ ] ASCII, English, no identity, no private path, no secret
- [ ] `docs/naming-table.md` updated if you added a variable or a journal word
- [ ] nothing in the diff arms anything
