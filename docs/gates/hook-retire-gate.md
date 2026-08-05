# hook-retire-gate

PreToolUse gate on `Bash`. Exit 2 = block, fail-open everywhere, every
execution logs one line to the gate-stats journal (`pass`, `block`,
`skip-stamp`, `fail-open`).

## What it blocks

Any command that RETIRES a file sitting at the top level of a live hooks
directory — `mv`, `git mv`, `rm`, `unlink` — including after a shell
separator (`&&`, `||`, `;`, `|`) or behind a command prefix
(`sudo`, `env`, `command`, `nice`, `timeout`, inline `VAR=value`):

- `mv <hooks>/guard.py /tmp/x.py`
- `git mv <hooks>/scan.py old-scan.py`
- `rm <hooks>/pretool-guard.py`, `cd /tmp && rm <hooks>/notify.sh`

It does NOT block anything that is not a retirement of a live hook:

- `cp <hooks>/guard.py /tmp/guard.bak` — this IS the safe path
- `rm <hooks>/end-of-mission.py.bak-20260731` — cleaning up the copies
- `mv <hooks>/tests/test_x.py /tmp/`, `rm <hooks>/test_helper.py` — test files
- `mv /tmp/new-hook.py <hooks>/new-hook.py` — dropping a hook IN; only the
  sources of an `mv` count, never its destination
- `rm <hooks>/data/cache.json` — only the TOP level holds wired hooks

Which directories count as live is set by `HARNESS_HOOK_DIRS`
(colon-separated); the default is the directory this gate itself lives in
plus `~/.claude/hooks`.

## Founding incident

An agent shipped a reworked end-of-mission hook: it rewired the settings
file, then `git mv`-ed the two superseded hooks to `.bak`. The reasoning
looked airtight — nothing points at those files any more.

Except a pane reads the settings of its OWN BOOT. Every already-open session
was still calling the old paths. A `UserPromptSubmit` hook whose file has
stopped existing exits non-zero, and a non-zero PreToolUse/UserPromptSubmit
hook is a BLOCK: the operator's production pane locked itself out of every
single prompt it tried to send. It took a second agent to diagnose and
restore.

The lesson is not "be careful with `mv`". It is that rewiring the
configuration does not free the file — only restarting every pane does.
Until then, the file must still be there.

## Legitimate exception path

- **Normal route — copy, don't move.** `cp` the hook to a dated `.bak`, edit
  or replace the original in place, and delete the old file only after every
  pane has been restarted. The gate never touches `cp`, and never touches
  `*.bak*`, so the whole safe cycle stays open.
- **Sanctioned orphan cleanup.** When a human explicitly greenlights
  retiring a file now, refresh the settings-GO stamp — the same lock the
  protected-settings gate uses:

  ```sh
  touch "${HARNESS_SETTINGS_STAMP:-$HOME/.harness/settings-go.stamp}"
  ```

  then redo the gesture. The window is 30 minutes and the pass-through is
  journaled as `skip-stamp`, so a forged GO is visible after the fact.

There is deliberately no activation or kill-switch environment variable: the
gate is on as soon as it is wired, and the only way through is the stamp,
which leaves a trace.
