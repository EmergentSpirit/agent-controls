# interlock-gate

PreToolUse gate on `Write|Edit|MultiEdit`. Exit 2 = block, fail-open on hook
errors, every execution logs one line to the gate-stats journal (`pass`,
`block`, `skip-recent-step`, `skip-doc`, `skip-scratch-path`,
`skip-replace-all`, `skip-out-of-scope`, `skip-no-path`, `fail-open`). Its
companion CLI `hooks/interlock-stamp.py` posts the proof of the preparation
step and journals it as `observe`.

This is a lock with two doors, plus the stamper that opens them. There is
deliberately NO activation variable and NO kill-switch: see the founding
incident.

## What it blocks

A SUBSTANTIAL edit made with no preparation step proved on disk inside the
last 60 minutes. Four thresholds, inherited and measured, any one of them is
enough:

- `Write` of a NEW file over 30 lines;
- Python AST diff adding 2 or more structural nodes
  (`If`, `While`, `For`, `FunctionDef`, `ClassDef`, `AsyncFunctionDef`) --
  a three-line edit that drops in two functions is a build, not a typo fix;
- non-Python file gaining more than 30 lines;
- 3 or more distinct CODE files touched inside 10 minutes.

Below every threshold it is a `pass`, and the gate is calibrated so that
normal work never meets it. What it never blocks, each exemption journaled and
never mute:

- `.md` and `.txt` (`skip-doc`) -- a handoff or a memory note is not a build to
  decompose; without this, every end-of-session write would be a guaranteed
  block;
- anything under a scratch directory (`skip-scratch-path`) -- ephemeral work
  and deliverables, and also where the proof artifacts live, so there is no
  chicken and egg. Default: the system temp dir, `/tmp`, `/var/tmp`; override
  with `HARNESS_INTERLOCK_SCRATCH_DIRS` (colon-separated). EXAMPLE, adding a
  deliverable directory:

  ```sh
  HARNESS_INTERLOCK_SCRATCH_DIRS=/tmp:/var/tmp:~/deliverables
  ```

- an `Edit` with `replace_all` (`skip-replace-all`) -- a mechanical rename;
- any other tool (`skip-out-of-scope`) and a payload with no path
  (`skip-no-path`).

A MISSING or CORRUPT state file is NOT a fail-open: above the threshold with
no proof is a block. Otherwise corrupting one JSON file would pick the lock.
The fail-open covers hook errors only -- unreadable stdin, or a crash in the
gate itself.

## Founding incident

Two incidents, one gate.

The first is the predicate. An agent that starts writing a large module
straight from the prompt discovers the design problem halfway through the
file, and the fix arrives as a patch on top of a shape that was already wrong.
The cheap preparation step -- write the steps down, or inventory the files and
the risks -- catches that in the minute before the first line, not in the hour
after. The step existed as written guidance for months. It was skipped exactly
when the work was big enough to need it, which is the moment guidance stops
being read.

The second incident is why this gate has no switch. Its own ancestor shipped
with a WARN mode plus two activation environment variables. Neither variable
was ever exported. For months an inventory listed a guardrail that had never
blocked a single gesture, and nobody noticed, because a WARN nobody reads and
a BLOCK that never fires look identical from the outside. It is the classic
dead-flag failure: a feature that ships switched off while the documentation
keeps describing it as active.

So: WARN mode removed, activation variables removed, no kill-switch added
back. The gate is armed the moment it is wired. Standing it down means
removing its line from the settings file -- a visible, reviewable gesture,
not an environment variable one can forget to set.

## Legitimate exception path

The message on the block is an andon cord, not a wall. Put the artifact in a
scratch directory, never in the tree you are about to edit, then open either
door.

- **Door 1 -- decompose the work.** A JSON list of steps, one object per step
  carrying `step`, `action`, `pass_fail_criteria`, `evidence_expected`:

  ```sh
  python3 hooks/interlock-stamp.py --session <session-id> --decomp steps.json
  ```

- **Door 2 -- ack the mission.** The inventory of the files you will touch
  plus the risks, at least 5 non-empty lines:

  ```sh
  python3 hooks/interlock-stamp.py --session <session-id> --ack ack.md
  ```

Both doors open the same lock for 60 minutes, then the work re-anchors. The
stamper VALIDATES the artifact: an empty steps list, a step missing a key, a
three-line ack or a bare `touch` are refused with exit 1 and journaled as
`stamp-refused`. The state file
`$HARNESS_STATE_DIR/interlock-<session>.json` keeps the timestamp AND the path
of the artifact, so what was actually decomposed or acked stays auditable
after the fact.

- **The threshold is wrong for your work.** Say so to the operator and get the
  constant changed, or widen `HARNESS_INTERLOCK_SCRATCH_DIRS` if the target is
  genuinely scratch. A gate that fights the real mandate every day teaches
  everyone to route around gates.
- **Standing it down.** Remove the gate's line from the settings file. There
  is no environment variable for it, on purpose.
