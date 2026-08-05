# harness

**Discipline for coding agents, armed rather than promised.**

Every team that works with a coding agent writes the same document. Always
verify before claiming. Never delete without a backup. Do not rewrite the
config on your own initiative. It goes in the system prompt, and for a while
it works.

Then the context gets long, the task gets interesting, and the rule quietly
stops applying. Nothing errors. Nothing looks wrong. The agent reports
success, and the success is not real.

A rule in a prompt is a request. This repository turns the rules that matter
into mechanisms: deterministic hooks that run outside the model, on files, in
plain Python, with no way for a persuasive turn of phrase to talk them out of
it.

```
$ Bash(rm -rf /srv/app/releases)

BLOCKED (destructive-command-gate) - recursive delete outside /tmp (rm -r).
   If this is intended, the human decides BY SAYING SO: add to the command
   [DESTRUCTIVE-AUTHORIZED reason=<why>]
   Session kill switch: HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE=1
```

The agent reads that, and either takes the reversible path or states a reason
that lands in a journal someone can count later. Every block message in this
repository is pasted from the gate that produces it, never paraphrased.

## What is in here

Thirteen gates, extracted from a system that has been running daily and
carrying real work. Each one exists because something went wrong once:

- a shell command prefixed with `HOME=` returned a **silently false result**
  that got reported as fact
- a regex with unbounded quantifiers **OOM-killed two panes** on two separate
  days
- renaming a live hook instead of copying it **froze a production session**:
  config is read at boot, and a vanished hook blocks every prompt after
- an LLM judge that could see the answer it was grading returned a **flawless
  verdict** on work that was wrong, and cost real money to produce
- an index line said a name had been relaunched; the real decision, buried in
  a note body, was that the name was taken. **The index won.** Work restarted
  down a dead path

That last one is why memory here is statutory: a verdict at the top, a closed
status vocabulary, and an index that carries the state of what it points at.
An index is read first. It must never lie.

Beyond the gates: a three-layer behavioral shield that injects a rule **at the
moment of risk** instead of hoping a permanent prompt is remembered; a catalog
that answers "we already built this" before an agent rebuilds it; a daily
sentinel; a governor that puts new rules in front of two adversarial judges;
and a read-only observation panel.

## The load-bearing idea

**A gate that stays silent cannot be told apart from a gate that is dead.**

So every hook writes one line to a journal on every execution, whatever the
outcome, including the boring ones. A gate that blocks nothing today still
proves it ran. The sentinel cross-checks that journal daily and flags anything
wired but silent, because a dead gate does not scream: it goes quiet, and
quiet reads exactly like a good day.

Everything follows from that:

- **fail-open** everywhere. A crashing gate lets the work through and says so.
  A harness that can halt the work becomes the thing you disable under
  pressure, and a disabled harness protects nothing.
- **a kill-switch is allowed, and never silent.** Route around a gate and the
  journal records `skip-disabled`. Bypasses are legitimate; invisible bypasses
  are not.
- **nothing arms itself.** Cloning this repository changes nothing. Gates run
  because a settings file names them, and that file is yours.
- **stdlib only.** No install step, no dependency to audit, no supply chain.
  Where an optional accelerator exists, there is a fallback and a test proving
  the two agree.

## Start here

```bash
git clone <this-repo> ~/harness && cd ~/harness
python3 -m pytest tests/ -q         # 275 tests, no network, no install
```

Then [docs/quickstart.md](docs/quickstart.md): fifteen minutes from clone to a
gate blocking something on purpose, with the real output at every step.

| If you want to | Read |
|---|---|
| Understand how it holds together | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Know what each gate blocks and why | [docs/gates/](docs/gates/) |
| Enforce behavior, not just commands | [docs/shield.md](docs/shield.md) |
| Stop rebuilding what exists | [docs/recall.md](docs/recall.md) |
| Prove the harness is alive | [docs/sentinel.md](docs/sentinel.md) |
| Govern which rules earn their place | [docs/governor.md](docs/governor.md) |
| Keep memory that does not lie | [docs/statutory-memory.md](docs/statutory-memory.md) |
| Observe without interfering | [docs/watch.md](docs/watch.md) |
| Wire it into your own launcher | [docs/launchers.md](docs/launchers.md) |
| Add a gate | [CONTRIBUTING.md](CONTRIBUTING.md) |

Take a piece or take all of it. A single gate in your own hooks directory is a
legitimate way to use this.

## One rule if you add your own

**A gate is born from an incident, never from a good idea.**

If you cannot tell the story of the day it hurt, the rule is noise, and noise
costs false positives until someone switches the whole thing off. Every gate
here carries its founding incident in its documentation, anonymized but real.

That is also why the governor exists. A human cannot audit a system that makes
decisions at machine speed, so a proposed rule faces two adversarial judges
from different model families, then earns a seven-day trial where it journals
and blocks nothing, and only then gets armed. Most proposals die at the judges.
That is the mechanism working.

## License

Apache-2.0. See [LICENSE](LICENSE).
