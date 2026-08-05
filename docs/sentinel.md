# sentinel -- proving the harness is alive, not present

A hook stayed dead for a whole working day. The file was on disk. The settings
file still named it. Nothing errored, no pane misbehaved in a visible way, and
the gate it was supposed to enforce simply stopped existing. It was found by
accident.

That is the failure mode this module exists for, and it has a shape worth
naming: **a dead gate does not scream, it goes quiet, and quiet reads exactly
like nothing bad happened.** Every other failure in the harness announces
itself. This one is indistinguishable from a good day.

So the sentinel refuses the only evidence that was available that day.

> **Alive means WIRED AND LOGGING. Never "present".**

A file on disk proves nothing. A line in a settings file proves nothing: a
matcher that matches nothing, an event that never fires, a pane booted before
the wiring landed, all leave the configuration looking perfect. The only
positive proof that a gate ran is the trace it left in the gate-stats journal,
and that is what the sentinel goes looking for, every day, on its own timer.

## Two laws

**1. Positive proof, never the absence of an error.** The sentinel does not
ask "did anything break". It asks "what left a trace, and what did not". A
check that cannot be decided says so and stays SKIP; it never becomes a pass.
Absent noise is not zero noise.

**2. The sentinel does not share the failure of what it watches.** It runs
from its own timer, OUTSIDE the agent's hooks. If the whole hook layer is
broken, the sentinel still runs and still reports. It READS and it REPORTS: no
push, no repair, no rewrite, no restart. Its only write is its own report
file. A watchdog that also fixes things is a watchdog you eventually stop
trusting, because you can no longer tell what it found from what it did.

## Self-discovering, by construction

The list of what gets checked is **derived at every run** from the settings
files the sentinel is pointed at. There is no inventory baked into the code.

Point it at your settings, and a hook wired tomorrow appears in tomorrow's
report with no edit to this module:

```
sentinel.py --settings ~/.claude/settings.json ~/.claude/settings.local.json
sentinel.py --enumerate        # the derived inventory, no checks run
```

This matters more than it looks. A health checker with a hard-coded list is a
second configuration file that drifts away from the first one, silently, and
the drift is invisible precisely where you needed the check. The only list
that can be trusted is the one the system itself declares.

## The check families

| Family | Question it answers |
|---|---|
| `settings` | do the settings files parse, and how many hooks does each wire |
| `script` | does every wired script exist, and does its syntax compile |
| `orphan` | is there a hook script on disk that NO settings file wires |
| `journal` | is the gate-stats journal present, parsable, and fresh |
| `coverage` | is a WIRED gate leaving traces, or is it silently dead |
| `probe` | OPTIONAL site-specific checks, off unless configured |

### `script` -- presence and syntax

Every command in the settings files is parsed for the script it actually runs.
The first absolute token is not the answer: an interpreter (`bash`, a venv
`python3`) or a data file passed as an argument would win that race. The
script is the first absolute token that is not an interpreter and either ends
in `.py` / `.sh` or carries the exec bit.

Python files go through `py_compile` with the byte-code sent to a temporary
directory: the sentinel writes NOTHING next to what it inspects. Shell files
go through `bash -n`. A command carrying an unset variable (`$HARNESS_HOME`
never exported, for instance) cannot be verified, and that produces a SKIP
line naming the variable -- never a silent pass.

### `orphan` -- on disk, wired nowhere

A hook script sitting in a hooks directory that no settings file references.
Two cases, and both deserve a line: dead weight left after a rewiring, or --
the expensive one -- a gate somebody believes is armed. Hook directories come
from `HARNESS_HOOK_DIRS`, the same convention the hook-retire gate uses.

Files starting with `_` (shared helpers), files starting with `test`, files
with `.example.` in the name, and files ending in `-stamp.py` (tools a human
runs by hand, wired nowhere by design) are exempt. Add your own with
`HARNESS_SENTINEL_EXEMPT`. Exemptions are printed as SKIP lines, so an
exemption stays visible instead of turning into a blind spot.

### `coverage` -- the check the whole module exists for

For each gate the settings files wire, the sentinel looks for its trace in the
gate-stats journal over a window (7 days by default). No trace means one of
three things: the gate is not instrumented, it has genuinely never been
triggered, or it is dead. All three are worth a line, every day, until someone
explains which one it is.

This is the check that would have caught the founding incident on day one.

Matching is by normalized name: file stem, `.py` / `.sh` dropped, underscores
folded to dashes, a trailing `-gate` or `-hook` removed. So
`home-prefix-gate.py` matches a gate journaling `home-prefix`. A gate that
journals under a name unrelated to its filename produces a false WARN -- which
costs one line to explain, where a false OK would hide a dead gate.

**With no journal at all, coverage is UNDECIDABLE and says so.** It emits one
SKIP line and declares no gate alive. Reading an empty journal as "nothing to
report" is the exact reasoning error this module was built to kill.

### `probe` -- everything that is true only on your machine

Service units, HTTP endpoints, mount points, backup age: none of that ships
here, because none of it is true anywhere but on the machine that wrote it.
That layer is a config file of shell commands, off unless you point at one:

```
sentinel.py --probes ~/my-probes.txt
```

One command per line, `#` comments ignored, exit 0 is OK. Everything that is
not allowed to run becomes a SKIP line carrying its reason, never a silence.
See `sentinel/probes.example.txt` for the format.

**A probe line is an ARGV, not a shell line.** The sentinel runs unattended
from a timer, so a config file must never become an arbitrary execution
surface. Steps 2 and 3 below live in `hooks/_exec_guard.py` and are the same
OBJECT recall's `check:` field uses (`recall/recall.py`, `run_check`): one
implementation, imported twice, and `tests/test_sentinel.py` asserts the
identity rather than claiming it. It was two copies for a while, and they
diverged the way copies do -- this side was hardened first while the recall
side kept the old blocklist, holes included.

1. **no shell metacharacter, and no shell at all.** A line carrying any of
   ``; & | < > ` $ ( ) { } [ ] ! * ? ~ \`` is SKIPped; what remains is split
   with `shlex` and run with `shell=False`. Allowlisting the first word of a
   string you then hand to `bash -c` protects nothing:
   `test -d /tmp; echo PWNED > /somewhere` passes a check on `test` and then
   runs the half after the semicolon. That was a real hole, and this is what
   closed it;
2. **the allowlist is a BINARY, not a word.** `argv[0]` must be in the list
   (`test`, `curl`, `systemctl` by default, override with
   `HARNESS_SENTINEL_PROBE_ALLOW`), and when it is written as a path it must
   resolve to the same binary as `which <basename>` -- a planted `/tmp/test`
   does not pass;
3. **per-binary rules, and they are ALLOWLISTS too.** An allowlisted binary is
   still a binary that can act, and a list of *forbidden* options is a promise
   nobody can keep on a CLI with hundreds of them. So each binary declares what
   it may receive, and everything else is refused:

   | binary | what a probe line may carry |
   |---|---|
   | `curl` | `-s` `--silent`, `-S` `--show-error`, `-f` `--fail`, `-I` `--head`, `-o`/`--output` **whose value is exactly `/dev/null`**, `-w`/`--write-out`, `-m`/`--max-time <seconds>`, `--connect-timeout <seconds>`, and exactly **one** `http://` or `https://` URL |
   | `systemctl` | the read-only verbs `is-active`, `is-enabled`, `is-failed`, `status`, `show`, `list-units`, `list-timers`, plus the value-less flags `--user`, `--system`, `--quiet`, `-q`, `--no-pager`, `--all`, `--full`, `--plain` |
   | `test` | one operator among `-e -f -d -r -w -x -s -h -L -p -S -b -c`, and one **absolute** path |

   The forms matter as much as the names. A value **glued** to its flag
   (`-o/tmp/loot`), **joined** with `=` (`--output=/tmp/loot`) or **hidden in a
   short cluster** (`-fsSo /tmp/loot`) is refused: a value-taking option must
   stand alone and its value must be the next argument. Clustering is allowed
   only for the value-less letters, so `-fsS` still works. `-w` is allowlisted
   and its VALUE is checked too: `@file` (a format read from disk) and
   `%output{...}` (curl >= 8.3 writes a file from the format string) are
   refused. And because curl reads `file://` and `scp://` as happily as it
   reads HTTP, the URL scheme is checked rather than assumed.

   This replaced a blocklist, and the difference was measured on curl 8.5.0:
   `-o/tmp/loot.txt`, `-fsSo /tmp/loot.txt`, `--data-ascii @/etc/hostname`
   (which POSTs the content of a local file to the remote host and writes
   nothing at all), `--json`, `-D`, `--dump-header`, `--trace-ascii`,
   `--stderr`, `-c`, `--cookie-jar`, `--etag-save`, `--remote-name-all` and
   `--form-string` all walked past a list naming `-O`, `-T`, `-d`, `-F` and
   `--config`, and all were reported **OK**. Widening any of these lists is a
   code edit on purpose: it gets reviewed.

   `-w`, in practice, is nearly always refused earlier by rule 1: curl's
   variables are written `%{http_code}`, and braces are shell metacharacters.
   The rule is there so the guarantee does not rest on that coincidence.

   A binary added to `HARNESS_SENTINEL_PROBE_ALLOW` with no rule of its own
   runs with its arguments **unchecked**. Adding one is a deliberate act, and
   it is worth writing the rule that goes with it.

If a probe genuinely needs a shell -- a variable, a glob, a pipeline -- put it
in a script of your own and put that script's name in the allowlist. That is a
deliberate code-visible decision, not a line someone slips into a config file.

**One malformed line costs one line.** An unclosed quote is a SKIP naming that
probe; the parse happens per line, inside its own `try`. It used to happen
outside, so a single bad quote raised all the way to the fail-open handler:
no dated report was written at all, and no other family ran that day.

## The daily verdict

One run, one verdict:

- **FAIL** if any check failed
- **WARN** if none failed and at least one warned
- **OK** if neither

**SKIP never moves the verdict.** An undecidable check must not read as a
pass, and it must not read as a failure either.

```
# sentinel -- 2026-01-14 06:30 -- 0.4 s
OK   settings  settings.json - 12 hooks wired
OK   script    home-prefix-gate.py - present, syntax OK (settings.json)
FAIL script    ghost-gate.py - MISSING on disk but wired in settings.json (PreToolUse): every prompt hitting this event fails
WARN orphan    forgotten-gate.py - on disk in /srv/agent/hooks but wired in NO settings file
OK   journal   gate-stats.jsonl - fresh (0.2 h), last line valid JSON
OK   coverage  home-prefix-gate.py - 34 trace(s) in the journal over 7 days
WARN coverage  silent-gate.py - wired (PreToolUse, settings.json) but NO trace in the journal over 7 days: not instrumented, never triggered, or DEAD
VERDICT FAIL - 4 OK / 2 WARN / 1 FAIL / 0 SKIP
```

The same text is written to `$HARNESS_STATE_DIR/sentinel/YYYY-MM-DD.txt`.

The exit code is **0 by default**, whatever the verdict: a monitor that kills
its own timer unit is a monitor that stops monitoring. Use `--strict` to get
exit 1 on a FAIL verdict when a caller wants to branch on it.

### The sentinel watches itself too

Every run writes one line to the gate-stats journal, result `observe` (it
blocks nothing), carrying the verdict and the counts. And the daily report
file is dated. Both give you the same dead man's switch: **if the freshness of
the report is not watched by something, a sentinel that stopped running is one
more silent failure.** The cheapest place to check it is the agent's own boot:
one line reading the newest file in the report directory and shouting if it is
older than 36 hours.

## Wiring the timer

### EXAMPLE -- systemd user units

`~/.config/systemd/user/harness-sentinel.service`:

```ini
# EXAMPLE -- adjust the paths, this file is not shipped armed.
[Unit]
Description=Harness sentinel, daily health check

[Service]
Type=oneshot
Environment=HARNESS_STATE_DIR=%h/.harness
ExecStart=/usr/bin/python3 %h/agent-controls/sentinel/sentinel.py \
  --settings %h/.claude/settings.json
```

`~/.config/systemd/user/harness-sentinel.timer`:

```ini
# EXAMPLE
[Unit]
Description=Run the harness sentinel every morning

[Timer]
OnCalendar=*-*-* 06:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

```
systemctl --user daemon-reload
systemctl --user enable --now harness-sentinel.timer
systemctl --user start harness-sentinel.service   # first run, right now
```

`Persistent=true` matters: a machine asleep at 06:30 runs the check at the
next boot instead of skipping the day in silence.

### EXAMPLE -- cron

```cron
# EXAMPLE -- daily health check, 06:30, output goes to the report file.
30 6 * * * HARNESS_STATE_DIR=$HOME/.harness /usr/bin/python3 \
  $HOME/agent-controls/sentinel/sentinel.py --settings $HOME/.claude/settings.json --quiet
```

Cron runs with a minimal environment. Anything the sentinel needs
(`HARNESS_STATE_DIR`, `HARNESS_GATE_STATS`, any variable your settings file
interpolates) has to be set in the crontab line itself, or the run will report
`unresolved` lines it cannot verify. That is a feature: an unverifiable check
is reported, never assumed good.

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_STATE_DIR` | State directory | `~/.harness` |
| `HARNESS_GATE_STATS` | Gate-stats journal path | `$HARNESS_STATE_DIR/gate-stats.jsonl` |
| `HARNESS_HOOK_DIRS` | Colon-separated live hook directories | harness `hooks/` + `~/.claude/hooks` |
| `HARNESS_SENTINEL_SETTINGS` | Colon-separated settings files to audit | `~/.claude/*settings*.json` |
| `HARNESS_SENTINEL_REPORT_DIR` | Where the daily report is written | `$HARNESS_STATE_DIR/sentinel` |
| `HARNESS_SENTINEL_COVERAGE_DAYS` | Coverage window, in days | `7` |
| `HARNESS_SENTINEL_FRESHNESS_HOURS` | Journal freshness window, in hours | `24` |
| `HARNESS_SENTINEL_EXEMPT` | Colon-separated basenames exempt from `orphan` and `coverage` | empty |
| `HARNESS_SENTINEL_ACTIVITY_PATHS` | Colon-separated paths whose mtime proves a session ran | empty |
| `HARNESS_SENTINEL_PROBES` | Site-specific probe file | unset (family off) |
| `HARNESS_SENTINEL_PROBE_ALLOW` | Colon-separated allowed probe commands | `test:curl:systemctl` |

A non-numeric or non-positive value for a numeric variable is ignored and the
default applies: a typo must not silently widen a window.

`HARNESS_SENTINEL_ACTIVITY_PATHS` is what turns a guess into a verdict. A
silent journal alone is ambiguous -- maybe no agent ran at all -- so it is a
WARN. Point this variable at whatever your agent touches when it works
(transcript directory, session log) and the ambiguity disappears: a session
that ran while the journal stayed silent means the wired gates are mute, and
that is a FAIL.

## Files

| Path | Role |
|---|---|
| `sentinel/sentinel.py` | the whole module, stdlib only |
| `sentinel/probes.example.txt` | EXAMPLE site-specific probe file |
| `hooks/_exec_guard.py` | the probe execution guard, SHARED with recall's `check:` field |
| `tests/test_sentinel.py` | 40 cases, no outbound network, tempdir harness |

## Journal vocabulary

| Result | Meaning |
|---|---|
| `observe` | one run finished; the `verdict` field carries OK / WARN / FAIL |
| `fail-open` | the sentinel itself hit an unexpected error and exited 0 |

The sentinel never blocks anything, so it never journals `block`. It reports,
and a human decides.
