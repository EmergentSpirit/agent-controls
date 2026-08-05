# scope-write-gate

PreToolUse gate on `Write|Edit|MultiEdit|NotebookEdit`. Exit 2 = block,
fail-open everywhere, every execution logs one line to the gate-stats journal
(`pass`, `block`, `skip-stamp`, `skip-disabled`, `skip-out-of-scope`,
`skip-no-path`, `fail-open`). Its companion CLI `hooks/scope-stamp.py` posts
the sanctioned bypass stamp and journals it as `observe`.

## What it blocks

Any write, edit or notebook edit whose target lands OUTSIDE the perimeter this
agent owns. The perimeter is `HARNESS_WRITE_SCOPE` (colon-separated); each
entry is a directory (everything under it is writable) or an exact file.
Default: the session cwd plus the system temp dir -- scratch files are not the
drift this gate is about. EXAMPLE for a research role:

```sh
HARNESS_WRITE_SCOPE=~/work/research:~/.harness/memory:/tmp
```

So, with that perimeter: a dated brief under `~/work/research/reports/`
passes, and dropping a hook file into the shared config directory, editing
another role's repository, or rewriting `~/.claude/settings.json` blocks.

Matching is deliberate, with no way around it by spelling:

- relative `file_path` resolves against the session cwd, symlinks are
  followed, and `<perimeter>/../elsewhere/x.md` normalizes out of the
  perimeter and blocks;
- comparison is on whole path segments, so `~/work/research2` is NOT inside
  `~/work/research`;
- a FILE entry grants that file only, never its siblings.

Out of scope, and logged as such: any other tool (`skip-out-of-scope`) and a
payload carrying no path at all (`skip-no-path`).

## Founding incident

A research-role agent drifted twice in the same week into work that belonged
to the builder role: it dropped a hook file on disk, edited the shared
settings file, and hand-fabricated a test transcript -- while its mandate
stopped at the written brief it was supposed to hand over.

Nothing crashed, and that is the whole problem. The drift is SILENT: the work
looks done, it lands in a directory nobody owns, and the role that owns the
gesture never reviews it. Worse, the artifacts were plausible enough to be
trusted later.

A memory note saying "stay in your lane" already existed. It lost, in the same
session it was loaded. That is the argument for a gate rather than one more
reminder: effectiveness must not depend on the model's good will, so a write
outside the perimeter is exit 2, not a WARN one can talk past.

## Legitimate exception path

- **Normal route -- deliver, then hand over.** Write the dated deliverable
  INSIDE your perimeter and pass the gesture to the role that owns it. That
  is the behavior the gate exists to produce, not a workaround.
- **The perimeter is simply wrong.** Widen it for good:
  `HARNESS_WRITE_SCOPE=<dir>:<dir>:<file>`. A perimeter that fights the actual
  mandate every day teaches everyone to route around gates.
- **Sanctioned one-shot, after an explicit human GO.** Post a stamp that
  opens ONE prefix for 30 minutes; the reason is mandatory and journaled:

  ```sh
  python3 hooks/scope-stamp.py /allowed/path/prefix --reason "human GO: ..."
  ```

  Every pass-through is then journaled as `skip-stamp` with the target, the
  prefix and the reason, so a forged GO is visible after the fact. The bypass
  is the only fail-CLOSED part of this gate: a stamp that is missing, expired,
  corrupt, prefix-less, relative, or that does not cover the target changes
  nothing -- the block stands. Stamp path:
  `HARNESS_SCOPE_WRITE_STAMP` (default `$HARNESS_STATE_DIR/scope-write.stamp`).
  The trade-off is deliberate: the hard criterion becomes "good will plus a
  dated trace plus a short window" rather than "good will" alone.
- **Session kill-switch.** `HARNESS_SCOPE_WRITE_GATE_DISABLE=1` lets a hit
  through and journals it as `skip-disabled`, so routing around the gate is
  visible after the fact rather than silent.
