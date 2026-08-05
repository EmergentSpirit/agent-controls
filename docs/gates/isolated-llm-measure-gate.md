# isolated-llm-measure-gate

PreToolUse gate on `Write` / `Edit` / `MultiEdit`. Exit 2 = block, fail-open
everywhere, every execution logs one line to the gate-stats journal (`pass`,
`block`, `fail-open`, `skip-disabled`, `skip-not-write`, `skip-nothing-written`,
`skip-not-python`).

## What it blocks

Writing, into a `.py` file, a Python `subprocess` call that launches the agent
CLI in headless mode with no explicit `cwd=`:

```python
subprocess.run(["claude", "-p", prompt], capture_output=True, text=True)
```

Three markers must be present in the SAME call before the gate fires: a
`subprocess.run` / `Popen` / `check_output` / `call` / `check_call`, a quoted
CLI binary name, and a quoted `-p` or `--print`. The absence of `cwd=` is what
makes it a block.

Zero false positives by construction. Out of scope, and logged as such:

- anything that is not a `.py` file (a doc quoting the trap stays writable),
- a `claude -p` typed by hand in a terminal or run through the Bash tool,
- a comment, a docstring, a commit message, a shell string handed to
  `os.system`,
- a call to the same binary without the headless flag (`claude --version`),
- a call that already carries `cwd=` (that is the shape we want).

`HARNESS_LLM_CLI_NAMES` (colon-separated, default `claude`) retargets the gate
at another agent CLI.

## Founding incident

An anti-sycophancy benchmark launched the agent CLI in headless mode from its
OWN directory. The CLI inherits the context of its working directory: it loaded
the project instruction file and it could see the benchmark files sitting next
to it. Same prompt, same model, same day, two runs:

| Launched from | Context loaded | Behaviour |
|---|---|---|
| the benchmark directory | 59k tokens | announced it recognized an item of the corpus being fed to it |
| an empty directory | 18k tokens | no recognition |

The judge scored a perfect result because it could SEE the expected answer in
its own context. The instrument had been contaminated by the thing it was
measuring. About 38 USD of quota equivalent bought a 100/100 that meant
nothing, and the contamination was only visible because someone compared the
token counts of the two runs.

The generalization is the reason this is a hard block rather than a warning: a
measurement that is wrong in the FLATTERING direction produces no error, no
crash and no anomaly. It looks exactly like success. Nothing downstream will
ever catch it.

## Legitimate exception path

- **Normal fix, one line.** Hand the call an empty temporary directory:

  ```python
  subprocess.run(cmd, capture_output=True, text=True, cwd=tempfile.mkdtemp())
  ```

  A neutral directory is also cheaper: less context loaded per call.

- **You genuinely want the project context** (you are testing the agent IN a
  repository, not measuring a model in the abstract): pass it explicitly,
  `cwd=repo_path`. The gate asks for an explicit directory, not for an empty
  one. Stating which directory you measure from is the whole point.

- **Writing test fixtures or documentation that must SHOW the trap.** In a
  `.py` file, build the offending snippet from a template rather than spelling
  it out, so the file itself is not a Python file containing an unisolated
  call:

  ```python
  FIXTURE = 'subprocess.run([%s, %s, prompt])' % ('"claude"', '"-p"')
  ```

  This gate's own test suite uses that dodge; see
  `tests/test_isolated_llm_measure_gate.py`. In a Markdown file no dodge is
  needed: non-Python paths are out of scope.

- **Session kill-switch** (deliberate, logged as `skip-disabled`):
  `HARNESS_ISOLATED_LLM_MEASURE_GATE_DISABLE=1`.
