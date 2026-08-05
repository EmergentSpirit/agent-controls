# Statutory memory

A file-based memory note is not free prose. It carries a statute: a YAML
frontmatter with a status taken from a closed list, a VERDICT block as the
FIRST line of the body, and an index line that shows that status at the head
of its summary.

`memory/memory-verdict-gate.py` enforces the statute. PreToolUse gate on
`Write|Edit`, exit 2 = block, fail-open, every decision journaled
(`pass`, `block`, `skip-out-of-scope`, `skip-index`, `skip-unresolvable`,
`skip-no-index`). The vocabulary is frozen in
[naming-table.md](naming-table.md); this page is the format it belongs to.

## The problem

A memory made of files grows. It grows by accretion, one note per fact, and
nothing in a directory of markdown forces two notes to agree. Six months in,
the store holds notes that contradict each other, which is normal: decisions
get reversed, that is what decisions do.

What is not normal is WHICH of the two contradictory statements gets read.
A reader, human or agent, does not read the store. It reads the index, picks
two or three notes, and acts. The index is a summary written when the note was
young. If the note has since been reversed and the index still carries the
young summary, the system does not fail loudly: it answers confidently, with
the wrong answer, and the reader has no signal that anything is stale. A
memory that lies with confidence is worse than no memory, because no memory at
least makes you go look.

## Founding incident

An index line said a domain name had been RELAUNCHED. The real decision, taken
later, was the opposite: the name was already registered by someone else, and
the project attached to it had been dropped. That decision existed, correctly
written down, buried in the middle of the body of a long note, under the
history that led to it.

The index was read first. The index won. Work restarted on a dead track and
ran until something external contradicted it.

Two lessons, and they are the whole format:

1. **The verdict goes to the top.** A decision written at the bottom of a body
   is a decision nobody will read at the moment it matters. The current state
   is the first line, and the history lives underneath it.
2. **The index carries the state marker.** The surface that gets read first is
   the surface that must never lie. The marker sits at the head of the
   summary, before the prose, where it cannot be skimmed past.

## The format

### 1. Frontmatter

A YAML block opening at the very first character of the file and closed by a
`---` line.

| Field | Required | Content |
|---|---|---|
| `name` | always | slug of the note, stable, usually the file name without `.md` |
| `description` | always | one line, what the note is about |
| `status` | always | one of `active` `discarded` `stale` `superseded` `dormant` |
| `superseded_by` | when `status: superseded` | slug of the note that replaces this one |

The gate reads the frontmatter line by line rather than parsing YAML: a
top-level `key: value` whose key matches `[A-Za-z_]+`, with surrounding double
quotes stripped from the value. `status` is compared case-insensitively, so
`Active` and `active` are the same state. Any other field you want (tags,
dates, source) is free and untouched.

### 2. The VERDICT block

The body is what follows the closing `---`, leading blank lines ignored. Its
FIRST line is the verdict:

```markdown
**VERDICT — superseded.** One sentence: what was decided, and what holds now.
```

Rules the gate applies to that line:

- it must be the first line of the body, not the second, not after an
  introduction;
- the separator is a literal em dash. A hyphen (`VERDICT - active`) does not
  match and is blocked;
- the state named in the verdict must be the state in the frontmatter. A note
  that says `stale` in its verdict and `active` in its frontmatter is blocked
  with both values quoted back at you.

Only the head of the line is checked. The sentence after it is yours, and it
is the part that carries the value.

### 3. The index line

`MEMORY.md` sits in the same directory as the notes. One line per note, and
the state marker comes first in the summary:

```markdown
- [Nightly index rebuild](nightly-index-rebuild.md) — 🔁 SUPERSEDED by index-rebuild-on-write (the on-write rebuild replaced the fixed slot)
```

| Status | Marker | Index line |
|---|---|---|
| `active` | (none) | plain summary, no marker |
| `stale` | ⚠ | marker at the head of the summary |
| `superseded` | 🔁 | marker at the head, plus what replaced it |
| `discarded` | ⛔ | marker at the head, plus why it was dropped |
| `dormant` | 🌙 | marker at the head, plus what would wake it |

An `active` note is never required to appear in the index: the gate asks
nothing of it. Every other status is required to be present in `MEMORY.md`
AND to carry its marker on that line. This is the check that the founding
incident bought.

Practical consequence on ordering: `MEMORY.md` is out of the gate's scope, so
when you flip a note to a non-active status, **write the index line first,
then rewrite the note**. Doing it the other way round earns you a block that
tells you exactly which line to add.

## Precedence: the verdict wins

When the verdict contradicts the rest of the body, the verdict wins. No
exception, no reading of the dates, no adjudication.

The body is HISTORY: what was tried, what was measured, what failed and why.
The verdict is STATE: what holds right now. History is not edited when a
decision is reversed, because the reasons that produced the wrong turn are the
most valuable thing in the note. Only the verdict is rewritten, and the old
verdict is demoted into the body where it becomes another line of history.

This rule is what makes a long note safe to read fast: the first line is
binding, everything below it is context.

## Lifecycle

A note is born `active` and moves, once, to one of four terminal-ish states.
Nothing is ever deleted.

| From active to | Meaning | What the note becomes |
|---|---|---|
| `stale` | still true as history, no longer safe to act on: the world moved | a record of a state that used to hold |
| `superseded` | another note replaces it; `superseded_by` names the successor | the "why we changed" behind the successor |
| `discarded` | the path is dead: reversed decision, rejected option, wrong premise | the reason nobody should try it again |
| `dormant` | not wrong, just parked: right idea, wrong season | a note waiting for a trigger |

Each transition is three gestures, in this order:

1. add or refresh the line in `MEMORY.md` with the marker at the head of the
   summary;
2. flip `status:` in the frontmatter (and add `superseded_by:` if the new
   status is `superseded`);
3. rewrite the first line of the body as the new verdict, and push the old
   verdict down into the body as history.

**Deletion is not a transition.** A discarded note that explains WHY a path is
dead is worth strictly more than a hole where the note used to be, because the
hole is indistinguishable from "never considered", and "never considered" is
an invitation to try it again. The marked note answers the question the hole
cannot: we went there, here is what we hit.

## What the gate checks, and what stays a human call

Checked mechanically, all issues accumulated into ONE error block (revealing
them one at a time costs a round trip each):

1. the frontmatter exists. This one is a hard stop on its own: the other
   checks are unverifiable without it, and the minimal example printed with
   the block already shows the whole expected shape;
2. `name`, `description`, `status` are present and non-empty;
3. `status` is inside the closed list;
4. the body starts with the VERDICT block;
5. the verdict state equals the frontmatter status;
6. `status: superseded` comes with a `superseded_by` field;
7. a non-active note appears in `MEMORY.md` and its line carries the right
   marker.

Not checked, and deliberately so, because these are judgments:

- **whether the verdict is true.** A perfectly conformant note can be wrong.
  The gate certifies the shape, never the content;
- **whether the verdict sentence says anything.** `**VERDICT — active.** ok`
  passes. The value of a note lives in that sentence, and no regex can put it
  there;
- **whether `superseded_by` points at a note that exists.** The field's
  presence is checked, its target is not;
- **whether the index summary describes the note honestly.** Only the marker
  is verified. The prose beside it stays your responsibility;
- **the index scan takes the FIRST `MEMORY.md` line containing the note's file
  name.** A note whose name is a substring of another entry can be satisfied
  by the wrong line. This is a floor, not a certification.

**The gate never writes a verdict for you.** Nothing is auto-fixed, on
purpose: choosing between `stale` and `discarded`, or writing the one sentence
that says what holds now, is exactly the judgment the format exists to
capture. A gate that guessed it would produce conformant notes that mean
nothing, which is the failure mode the whole page is written against.

### Scope and grace period

- In scope: any `.md` file whose path contains a `/memory/` component.
- Out of scope: `MEMORY.md` and any `index-*.md` (`skip-index`) since they are
  indexes, not notes; anything else (`skip-out-of-scope`).
- An old note written before the format is NOT frozen. An `Edit` is judged on
  the text the file will carry AFTER the edit: bring the note into conformance
  in the same gesture and it passes. The gate demands the format, it does not
  forbid touching the note.
- If the `Edit`'s `old_string` is not in the file, the gate steps aside
  (`skip-unresolvable`): that edit is going to fail on its own.
- No `MEMORY.md` in the directory and nothing else wrong: `skip-no-index`.
  Missing index does not erase the issues already found (the frontmatter and
  verdict checks still block).
- Fail-open: an unreadable payload or a crash inside the gate lets the write
  through, and journals `fail-open` on the way out. A block is the only thing
  that stops a write. The journal line matters: a gate that dies silently is
  invisible to the sentinel's aliveness check, which is exactly how a dead
  gate goes on lying about being armed.
- Standing it down: `HARNESS_MEMORY_VERDICT_GATE_DISABLE=1` for the session,
  journaled as `skip-disabled` so routing around the gate stays visible, or
  remove its line from the settings file for good.

Wiring, in the `PreToolUse` matcher for `Write|Edit`:

```json
{ "type": "command", "command": "python3 $HARNESS_HOME/memory/memory-verdict-gate.py" }
```

## Nested fields written by sync tools

Some sync tools (basic-memory among them) rewrite the frontmatter they manage
and MOVE custom fields under an indented `metadata:` block:

```yaml
---
name: nightly-index-rebuild
description: Why the rebuild moved off the boot path.
metadata:
  type: note
  status: superseded
  superseded_by: index-rebuild-on-write
---
```

The gate reads `status` and `superseded_by` in both places. Top-level keeps
priority when a key exists at both levels; among indented occurrences, the
first one wins. A note whose status only exists nested passes, and a
`superseded` note whose `superseded_by` only exists nested does not raise a
false "superseded without superseded_by".

The fallback invents nothing: a note with no `status` at either level is still
blocked. Reading two places is a tolerance, not a default value.

Why we do not simply require the fields back at top level: **you do not win a
write war against an automated sync.** The daemon rewrites the file on its own
schedule; a rule demanding the opposite shape would fire on notes that were
conformant when written, would be un-fixable by the agent that gets blocked,
and would teach everyone that this gate is noise. A gate that fights a daemon
loses twice: it fails to hold the line, and it costs the credibility of every
other gate beside it.

## A conforming note, end to end

```markdown
---
name: nightly-index-rebuild
description: Why the search index rebuild left the boot path, and what replaced it.
status: superseded
superseded_by: index-rebuild-on-write
---
**VERDICT — superseded.** The scheduled full rebuild is replaced by an
incremental on-write rebuild; keep this note for the disk-budget constraint,
which still holds.

## Why it left the boot path

First shape: rebuild at boot. On a machine that reboots inside a maintenance
window, a dozen services woke at the same second, the rebuild lost its share
of the disk budget, and the index came up half written. Search returned
nothing for twenty minutes and reported no error, because a half-built index
is a valid index with no rows.

## Why the fixed nightly slot held for a while

Second shape: one full rebuild per night, outside the backup window. Correct
as long as the store fit in the slot. The reason is the part worth keeping:
the binding constraint was never the clock, it was contention on the disk
budget.

## What replaced it

Incremental rebuild on write, sized so no single write can exceed the budget.
See `index-rebuild-on-write`.
```

And its line in `MEMORY.md`:

```markdown
- [Nightly index rebuild](nightly-index-rebuild.md) — 🔁 SUPERSEDED by index-rebuild-on-write (incremental on-write rebuild; the disk-budget constraint still holds)
```

Read that index line and you learn, before opening anything, that the note is
no longer the state of the world and where the state moved. That is the whole
point: the index is read first, so the index must never lie.
