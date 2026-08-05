# response-length-gate

Stop hook. Exit 2 = block, fail-open everywhere, every execution logs one line
to the gate-stats journal (`pass`, `block`, `fail-open`, `skip-disabled`,
`skip-loop-guard`, `skip-no-text`).

## What it blocks

An assistant turn carrying more than `HARNESS_MAX_RESPONSE_WORDS` words of
PROSE (default `350`) in its last message.

Prose is what is left after four subtractions, because evidence is not chatter:

| Not counted | Why |
|---|---|
| fenced code blocks ` ``` ` | that is the output, the proof |
| inline code `` `...` `` | identifiers, paths, flags |
| markdown table rows (`\| ... \|`) | measured data |
| URLs | references |

So a 400-line command dump plus a three-sentence verdict passes, while 400
words of narration around a one-line answer does not. The gate pushes toward
the shape the operator can actually read in a terminal pane: the verdict first,
then only what changes a decision.

Two paths in, one rule: the text is taken from `assistant_text` when the
payload carries it (wrappers, tests), otherwise from the last assistant message
of the JSONL transcript at `transcript_path` (real runtime).

Out of scope, and logged as such:

- a turn with `stop_hook_active` set: it is already a retry driven by a Stop
  hook, and blocking in a loop would freeze the pane (`skip-loop-guard`),
- nothing measurable: empty text, missing or unreadable transcript
  (`skip-no-text`).

## Founding incident

In a single production session, an agent shipped six answers between 600 and
900 words. The operator eventually cut in: he could not digest pages of prose
in a terminal pane, and had said so before.

Two things had already failed by then. First, a memory note stating exactly
that rule existed and was loaded. Second, a soft validator running alongside
fired four separate "too verbose" classifications DURING that same session: it
saw the fault in real time and warned, and the model kept going anyway.

That is the argument for a hard gate rather than one more reminder. A warning
is obeyed only if the model feels like obeying it, so it does not count as a
control. The ceiling is arithmetic, the block is exit 2, and the failure mode
it fixes is the one that soft rules never fix: the model agreeing with a rule
and violating it in the same breath.

## Legitimate exception path

- **You have more to say.** That is expected, and it is not a reason to raise
  the ceiling. Write the long form to a dated file and hand back its path in
  one line. The answer stays readable, the detail stays available, and it
  survives the session.
- **The volume IS the deliverable** (a long report requested explicitly, a
  generated document): raise the ceiling for that run with
  `HARNESS_MAX_RESPONSE_WORDS=<n>`. A value that is not a positive integer is
  ignored and the default applies, so a typo cannot silently disarm the gate.
- **Evidence-heavy answers need no exception.** Put the dump in a fenced code
  block or a table; it costs zero words.
- **Session kill-switch** (deliberate, logged as `skip-disabled`):
  `HARNESS_RESPONSE_LENGTH_GATE_DISABLE=1`.
