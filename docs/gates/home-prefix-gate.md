# home-prefix-gate

PreToolUse gate on `Bash`. Exit 2 = block, fail-open everywhere, every
execution logs one line to the gate-stats journal (`pass`, `block`,
`fail-open`, `kill-switch`).

## What it blocks

Any Bash command whose head is a `HOME=` assignment, in all its prefix forms:

- `HOME=/x cmd`
- `env HOME=/x cmd`, `env -i HOME=/x cmd`
- `export HOME=/x`
- `FOO=1 HOME=/x cmd` (assignment chain)
- any later segment after `&&`, `|`, `;`, `(`…: `echo ok && HOME=/x cmd`

It does NOT block `HOME=` appearing as data: `grep HOME= file`,
`sed 's|HOME=/a|HOME=/b|' f` (quotes are respected via `shlex`, not a naive
regex split), or near-miss variable names like `HOMEBREW_PREFIX=`.

Why this is worth a hard block: inside the agent's Bash tool, reassigning
`HOME` breaks the tool's own output capture (which relies on artifacts under
`$HOME`, including the shell snapshot re-sourced on every command). The
command runs, but its output comes back EMPTY with exit 0. The construct
never works in this tool, so blocking it costs zero legitimate usage.

## Founding incident

During a production debugging session, an agent measured a supposedly broken
gate with `env HOME=<other-home> python3 …`. The command returned no output
and exit 0 — silently and reproducibly — so it looked exactly like a real
measurement. The agent concluded twice in a row that a healthy gate "was not
blocking" and was about to patch code that worked perfectly (later verified
4/4 with the proper method). The dangerous part is not the failure, it is the
false result that wears the clothes of a measurement.

## Legitimate exception path

- To test something under a different `HOME`: write a script that runs the
  trial and writes its verdict to a file on disk, launch it, then read the
  file. The result no longer crosses the broken capture path.
- To read a process's environment: `tr '\0' '\n' < /proc/<pid>/environ`.
- Session kill-switch (deliberate, logged as `kill-switch`):
  `HARNESS_HOME_PREFIX_GATE_DISABLE=1`.
