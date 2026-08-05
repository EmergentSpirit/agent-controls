# workflow-gate

PreToolUse gate on the `Workflow` tool (scripts that orchestrate sub-agents).
Exit 2 = block, fail-open everywhere, every execution logs one line to the
gate-stats journal (`pass`, `block`, `fail-open`, `skip-disabled`,
`skip-out-of-scope`, `skip-no-script`).

The script is read from `tool_input.script`, or from the file at
`tool_input.scriptPath` when the workflow arrives as a path.

## What it blocks

Three checks, in order. The first one that fires blocks, and the journal line
carries which one (`check: "A" | "C" | "B"`).

**A. A deterministic task handed to an LLM agent.** An `agent(...)` call whose
label or prompt names work that code does better:

```js
const l = await agent(pr, {label:'list', schema:S})          // blocked
await agent('With the Bash tool, list all the files under /x', {label:'x'})
```

Labels caught: `list`, `ls`, `glob`, `grep`, `count`, `parse`, `format`,
`dedup`, `wc`, `write`, `copy`, `rename`, `sort`. Prompts caught: listing
files, `ls -<flag>`, `wc -l`, globbing for files, driving the shell tool to do
any of it. Listing, counting, parsing and writing files are CODE: cheap,
instant, deterministic. An agent doing them is expensive, slow,
non-deterministic and a single point of failure.

**C. An agent persisting DATA through the Write tool.** Caught: `with/using the
Write tool`, `write ... to/into a file`, a write followed by a `.json` /
`.txt` / `.csv` / `.md` target. Workflow agents return data through their
SCHEMA; the harness journals it; the main loop harvests the journal and writes
the files itself.

**B. A fan-out with no validation attestation.** Any `parallel(` or `pipeline(`
whose script carries neither `@small-run` nor `@sample-tested` in a comment.
The markers are the point, not the ceremony:

- `@small-run` -- this run IS the validation, on a small sample,
- `@sample-tested` -- big run, the mechanism was already validated on a sample.

Out of scope, and logged as such: any other tool (`skip-out-of-scope`), and a
`Workflow` call with no script to judge (`skip-no-script`).

## Founding incident

One production session, two mistakes, both already written down in a memory
note, both repeated anyway.

First, a listing of 416 files was delegated to a sub-agent. A deterministic
`ls` became an LLM call: slower, billable, and able to return a different
answer each time.

Second, a fan-out of 416 agents was launched without the mechanism ever having
been tried on 3 to 5 items first. It ran, and 144 extractions were lost,
because the design asked each agent to persist its own result with the Write
tool. An agent that skips or hallucinates that call produces no error and no
crash: the data is simply not there, and nothing downstream notices.

The operator's verdict was the reason this file exists: a memory depends on the
model's good will, so carve a hook. The gate is deterministic; the memory was
not.

## Legitimate exception path

- **You need a listing, a count, a parse.** Do it in the main loop in Bash or
  Python and pass the result into the workflow via `args`. It is faster and it
  cannot hallucinate.
- **You need the agents' output on disk.** Return it through the schema, let
  the harness journal it, then harvest the journal and write the files from the
  main loop. One writer, one place to check.
- **You really are running a big fan-out.** Validate on 3 to 5 items first with
  `// @small-run`, then declare `// @sample-tested` on the full run. The
  attestation is a one-line comment; the reflex is what it buys.
- **A label that only LOOKS mechanical** (`sort` in the sense of triaging
  arguments, `format` in the sense of rewriting prose): rename the label to say
  what the agent actually judges. The name of a delegated task should not read
  like a shell command.
- **Session kill-switch** (deliberate, logged as `skip-disabled`):
  `HARNESS_WORKFLOW_GATE_DISABLE=1`.
