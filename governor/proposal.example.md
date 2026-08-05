# proposal: example-commit-after-piped-tests
touches: technical
what: Block a git commit chained in the SAME shell command as a test runner whose output is piped: the pipe swallows the exit code, so a red suite reads as green.
incident: EXAMPLE, invented. 2026-01-14: a commit landed on a red suite (runner piped into grep, then `&& git commit`), noticed two commits later during a bisect.
blocks: `test-runner | grep -c PASS && git commit -m ok` -- the commit runs even when the suite is red, because the shell reads grep's exit code, not the runner's.
error_cost: A legitimate one-liner that pipes test output for readability is refused; the author splits it in two commands, which costs one extra round-trip.
trial: 7 days, observation only (the gate journals `observe` and blocks nothing).
detail:
EXAMPLE FILE. Every fact above is invented; it ships to show the FORMAT, and a
proposal that does not come from a real incident is noise. Copy it, replace all
five fields with your own lived event, and run:

    python3 governor/propose.py governor/proposal.example.md

The five fields are capped at 200 characters each, refused at the source. This
free block is not capped: it is written for the judges, not for the human, and
it is where the exact trigger belongs.

Proposed trigger (deterministic, no fuzzy judgment):
- PreToolUse on Bash. Parse the command with shlex, quotes respected.
- If a segment contains `|` with a known test-runner head AND a later segment
  is a `git commit`, AND `set -o pipefail` is absent from the command: block.
- Message hands back the fix: `set -o pipefail;` in front, or two commands.

Adjacent existing gates to weigh for redundancy (the judges are asked to check
this: a harness gets heavier one gate at a time):
- the false-success gate, which already refuses a runner whose exit code is
  discarded in some shapes;
- the destructive-command gate, which does not cover this family at all.

Honest counter-argument for the judges: the trigger needs a list of test-runner
names, and a list is a maintenance burden that silently rots. A generic version
(any pipe followed by a commit) would be more deterministic but would bite much
more legitimate work.
