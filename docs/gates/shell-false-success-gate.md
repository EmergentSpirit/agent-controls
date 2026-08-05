# shell-false-success-gate

PreToolUse gate on `Write|Edit|MultiEdit`. Exit 2 = block, fail-open
everywhere, every execution logs one line to the gate-stats journal (`pass`,
`block`, `skip-not-shell`, `skip-nothing-written`).

## What it blocks

Shell being written (`.sh`, `.bash`, or any file whose shebang says shell) that
carries one of three shapes making a script report the OPPOSITE of the truth:

- `CODE=$(curl … -w '%{http_code}' … || echo 000)` — on a failed connection
  curl already prints `000` through `-w` **and** exits non-zero, so the
  `|| echo` appends a second one: the variable holds `000000`.
- `[ "$CODE" != "000" ]` as a success test (whenever `http_code` appears in the
  file) — "not the one known failure value" is not "a success value":
  `000000`, `404` and `500` all pass it.
- `VAR=$(… grep … | wc -l)` with no `|| true`, **only** when the target file
  runs `set -e` + `pipefail` — a grep that finds nothing exits 1, pipefail
  carries that through `wc`, `set -e` kills the script: it dies on the SUCCESS
  case.

Scope is deliberately narrow: the counter pattern is judged only inside a
`set -e` + `pipefail` file, and whole-line comments are stripped before
matching, so documenting the trap does not trip the gate.

## Founding incident

Two losses on the same production day. A TLS watchdog reported "CERTIFICATE
ISSUED" while the handshake was in fact broken: the concatenated `000000`
passed its inequality test. Hours later, a cutover wrapper died on its own
success case — nothing left to migrate meant the `grep | wc -l` exited 1 under
pipefail — and the operator spent a day believing the work had not been done.
The nastier half was found afterwards: the same counter shape sat in the
wrapper's FINAL VERIFICATION, where it would have failed the run *after* the
mutation had already been applied, filing a completed change under `failed/`.

## Legitimate exception path

There is no kill switch, because every hit has a one-line rewrite:

- HTTP status: drop the `|| echo` and test membership.
  ```sh
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$URL")
  case "$CODE" in 2??|3??) ok ;; *) fail ;; esac
  ```
- Counters under `set -e` + `pipefail`: `VAR=$(… | wc -l || true)`.
- Explaining the trap in prose is already allowed (whole-line comments are
  stripped before matching).

If the pattern is genuinely miscalibrated on your code, say so and change the
gate — routing around it silently is what the journal is there to expose.
