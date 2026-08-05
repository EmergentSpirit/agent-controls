# grep-quantifier-gate

PreToolUse gate on `Bash`. Exit 2 = block, fail-open everywhere, every
execution logs one line to the gate-stats journal (`pass`, `block`,
`fail-open`, `kill-switch`).

## What it blocks

A Bash command whose shell segment invokes a BARE `grep` (or `ugrep`, `ug`,
or an `eval` carrying one) with **2 or more bounded quantifiers** (`{m,n}` or
the escaped BRE form `\{m,n\}`) of magnitude **>= 10**, and without `-P` or
`-F`:

- `grep -E '.{0,20}X.{0,20}' file`
- `grep '.\{0,20\}X.\{0,20\}' file` (the escaped BRE kills too)
- the same thing in any pipeline segment, after `&&`, inside `` `backticks` ``,
  behind a transparent keyword (`time grep …`), or with a quoted head
  (`'grep' …` — bash resolves functions after quote removal)

Inside the agent's Bash tool, `grep` is not the system binary: the shell
snapshot re-sourced on every command replaces it with a shell FUNCTION routing
to an embedded ugrep (a DFA engine). That engine explodes at PATTERN
COMPILATION time when two bounded quantifiers overlap, roughly `2^min(N,M)`
states, independently of the corpus size. Measured under a 2 GB memcg:

| Command | RSS | Result |
|---|---|---|
| control `'foo'` | 6.7 MB | ok (path and instrument healthy) |
| `-E '.{0,20}X.{0,20}'` | 2 GB | OOM-killed |
| BRE `'.\{0,20\}X.\{0,20\}'` | 2 GB | OOM-killed |
| system GNU `grep -E`, same pattern | 3.5 MB | ok |
| `-P` (PCRE2), same pattern | 7.5 MB | ok |

What it does NOT block, because those paths were measured to reach the healthy
system GNU grep:

- execvp wrappers and absolute paths: `command grep`, `env`, `sudo`, `xargs`,
  `timeout`, `nice`, `find -exec`, `/usr/bin/grep`
- child scripts: `bash foo.sh`, `bash -c …` (shell functions are not exported,
  no `BASH_FUNC` entry). Writing a dangerous pattern INTO a `.sh` is fine, and
  the `Write` tool is out of scope entirely.
- the dedicated `Grep` tool used by sub-agents: that one is ripgrep with no
  ugrep fallback, measured at 11 MB on the killer pattern
- a single bounded quantifier, or quantifiers below the magnitude threshold

When the command line is unparsable (`shlex` raises on an unclosed quote), the
gate falls back to a coarse whole-line rule and still blocks. That is the
opposite trade-off from most gates, on purpose: here a false positive costs one
rewrite, a false negative costs a whole session.

## Founding incident

Two production panes were OOM-killed on two separate days, at 15 GB and
27.8 GB RSS. Both looked like ordinary searches. A `/proc` sampler catching the
processes in flight showed the culprit: a child carrying the comm of the agent
binary itself, running `ugrep -G --ignore-files …` — the shadowed `grep`, not
the system one. Re-verification under a memory cgroup reproduced the blowup in
isolation and showed it is corpus-independent: the memory is spent compiling
the pattern, before a single byte of input is read. An earlier version of this
gate also gated the dedicated `Grep` matcher; measurement showed that matcher
is ripgrep and stays at 11 MB, so gating it only produced false positives and
it was removed.

## Legitimate exception path

You almost never need the kill-switch: the pattern has three measured rewrites
that keep the same match.

- **Add `-P`** (PCRE2 backend, 7.5 MB): `grep -P '.{0,20}X.{0,20}' file`.
- **Force the system binary**: `command grep -E '…' file` (3.5 MB).
- **Move it into a script**: put the command in a `.sh` and run
  `bash foo.sh` — child processes get the healthy GNU grep.
- **Rewrite the pattern**: one bounded quantifier only, or non-overlapping
  character classes instead of two overlapping `.{m,n}`.
- Session kill-switch (deliberate, logged as `kill-switch`):
  `HARNESS_GREP_QUANTIFIER_GATE_DISABLE=1`.
