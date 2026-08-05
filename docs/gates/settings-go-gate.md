# settings-go-gate

PreToolUse gate on `Write|Edit|MultiEdit`. Exit 2 = block, fail-open
everywhere, every execution logs one line to the gate-stats journal (`pass`,
`block`, `skip-stamp`, `skip-disabled`, `skip-out-of-scope`, `fail-open`).

## What it blocks

Any write or edit landing on a protected settings file, through any of the
three edit tools:

- `<perimeter>/settings.json`
- `<perimeter>/settings.local.json`
- `<perimeter>/<name>-settings.json`

The perimeter is `HARNESS_PROTECTED_SETTINGS` (colon-separated, default
`~/.claude/settings.json`). Each entry is either a FILE -- protected as
itself, and its directory joins the settings-family scan above -- or a
DIRECTORY, when the entry ends with `/` or names an existing directory, whose
settings family is protected. Example:
`HARNESS_PROTECTED_SETTINGS=~/.claude/settings.json:~/.config/agent/`.

Zero false positives outside that perimeter, by construction:

- `~/projects/some-app/settings.json` -- same name, ordinary file, untouched
- `<perimeter>/hooks/guard.py`, `<perimeter>/agents/reviewer.md` -- only the
  settings family is standing configuration
- `Bash`, `Read` and every other tool -- out of scope, journaled and allowed

A relative `file_path` is resolved against the session cwd before matching:
the same file reached by a shorter name is the same file.

## Founding incident

An agent decided a hook was worth wiring and edited the shared settings file
to add it. The intent was good and the hook was sound -- that is not the
point.

The vendor's built-in auto-mode classifier did stop it that day, which is
exactly what made the episode worth a gate. That classifier is not ours: it
does not bite in accept-edits mode, and it can be recalibrated at any time
without notice. Leaning on it means the protection exists until the day it
silently does not.

And the blast radius is not local. A settings file is read AT BOOT by every
pane on the machine: adding one line changes the STANDING behavior of every
future session, including the ones the agent that wrote it will never see.
An edit whose consequences outlive the session that made it is a call for
the human operator, not for the model.

## Legitimate exception path

- **Normal route -- ask first.** Say what you want to wire and why, in the
  session. The block message spells out the same three steps.
- **Sanctioned edit.** After an explicit human GO, refresh the stamp -- the
  same lock the hook-retire gate uses:

  ```sh
  touch "${HARNESS_SETTINGS_STAMP:-$HOME/.harness/settings-go.stamp}"
  ```

  then redo the edit. The window is 30 minutes and the pass-through is
  journaled as `skip-stamp` with the path, so a forged GO is visible after
  the fact. Writing the reason into the stamp file instead of an empty
  `touch` keeps that trace readable:
  `echo "go: wire the end-of-mission hook" > "$HARNESS_SETTINGS_STAMP"`.
- **Session kill-switch.** `HARNESS_SETTINGS_GO_GATE_DISABLE=1` lets a hit
  through and journals it as `skip-disabled`, so routing around the gate is
  visible after the fact rather than silent.
