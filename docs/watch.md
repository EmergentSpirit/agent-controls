# watch -- the read-only observation panel

Every other module in this harness acts: a gate blocks, the sentinel reports,
the governor routes. `watch` is the one that does not. It is a pane of glass
over two things that already exist on disk -- the gate-stats journal and the
agent transcripts -- and it turns them into something a human can actually
look at: what the guardrails did, and what the sessions did.

The value is not the graphs. It is that **a harness whose journal nobody reads
is a harness that cannot be corrected.** A gate that fires 400 times a week on
legitimate work, a gate that has been silently dead for a month, a session that
retried a blocked gesture three different ways: all of that is already in the
journal, in JSONL, unreadable at that volume. The panel is the reading device.

## What it shows

**Dashboard** -- for a window of 7, 30 or 90 days: sessions observed, gate
events journalled, blocks (`block` + `deny`), and how many distinct sessions
took a block. Two bar charts, sessions per day and blocks per day, and one
table of hook x result. That last table is the one that pays: it is where a
gate that bites too often, or one that never bites at all, becomes visible in
a glance instead of a grep.

**Sessions** -- one line per indexed session: role, title, start, message
count, tool count, blocks taken, and the severity of its post-hoc analysis if
one was ever run.

**Trajectory** -- one session opened: the blocks it took as badges, then the
message thread. Each message is a collapsed row (sequence, type, tool,
timestamp); opening one re-reads THAT line from the source `.jsonl` at its
indexed byte offset and shows the raw JSON.

That last detail is a design rule, not an optimization: **message bodies are
never copied into the database.** The index stores where a line lives, not
what it says. A panel that duplicates transcript content into a second file
has just created a second place your secrets can leak from, and a second file
you have to remember to shred.

## Why READ-ONLY

The panel never writes a transcript, never edits a settings file, never arms
or retires a gate, never blocks anything, never re-runs a command it saw. Its
only writes are to its own derived database. Nothing it displays can act.

That is what makes it safe to leave running while you work. An observation
tool that can also fix things is an agent, and an agent watching the agents is
a second thing to supervise. The panel is deliberately inert: worst case, it
shows you something wrong, and you go read the source `.jsonl` yourself -- the
path is right there in the session metadata.

It binds `127.0.0.1` and nothing else. There is no host variable, on purpose:
a variable is a thing someone eventually sets to a wildcard address "just to
test from the laptop" and then leaves that way. Transcripts carry command
outputs; command outputs carry everything. This is an instrument on your desk,
never a service.

### The bind is NOT the only defense

That has to be said out loud, because it is the assumption that gets local
tools breached. A loopback bind stops a stranger on the network. It stops
neither of the two attacks that actually reach an unauthenticated local panel,
and both of them arrive through the operator's own browser:

- **DNS rebinding.** A page on a domain the attacker owns is loaded; the domain
  then re-resolves to `127.0.0.1`. The browser connects to the panel over
  loopback -- the bind is perfectly satisfied -- and the request carries
  `Host: evil.attacker.example`. The Host header is the only place the attack
  is visible at all.
- **CSRF.** Any page open in that same browser can fire
  `POST http://127.0.0.1:8815/api/analyze/<id>`. There is no credential to
  steal here, and none to check: without a check, the panel simply obeys.

So the panel enforces two header rules, and answers **403** before reading a
single row:

| Rule | Applies to | Accepted |
|---|---|---|
| `Host` names loopback | every request | `127.0.0.1`, `localhost`, `::1`, `[::1]`, each with or without a port |
| `Origin` is absent or loopback | every non-GET request | no `Origin` at all (curl, a systemd unit, a timer), or an `http`/`https` loopback origin |

`Origin: null` -- a sandboxed frame, a `file://` page -- is refused, not read
as absent. The port inside the `Host` header is deliberately NOT checked: it
says which port the client dialed, never who dialed it, and an SSH tunnel
(`ssh -L 9000:127.0.0.1:8815`) legitimately changes it. The NAME is the lever a
rebinding attacker controls, so the name is what is checked.

Transcript content is treated as HOSTILE all the way through. It is served as
JSON and never interpolated into HTML server-side; the browser side builds
every node with `createElement` + `textContent` and uses no raw-HTML
assignment API at all, which the test suite enforces by grepping the static
files. Plus strict CSP, `nosniff`, `no-referrer`.

## The database is derived, and it is never published

What ships is `watch/schema.sql` (the DDL) and `watch/indexer.py` (what fills
it). The database file itself is nobody's business but yours: it is an index
of one operator's sessions.

This is not a privacy footnote, it is the architecture. The `.jsonl` files are
the source of truth; the database is a disposable index of metadata and byte
offsets. Delete it and run `indexer.py --rebuild` and you are back where you
were. Because it is disposable, nothing in the harness ever has to treat it as
precious, and because it is not published, the schema can be honest about what
it holds.

The pass is incremental by `(mtime, size, offset)`: a file that grew resumes
at its offset, a file that shrank is re-indexed clean, a file that vanished is
purged. The parser is TOLERANT -- an unreadable line increments `n_unreadable`
and is never fatal, because the transcript format is not a contract and an
indexer that dies on one malformed line shows nothing on the day it matters.

Each CLI run ends on an integrity check: as many sessions indexed as there are
transcripts on disk, or the run says `MISMATCH` and exits 1. An indexer that
quietly drops files is worse than one that crashes, because the panel keeps
looking complete.

## The deliberate blind spot

`HARNESS_WATCH_EXCLUDE` is a colon-separated list of patterns whose
TRANSCRIPTS are never indexed. The gate journals of an excluded role are still
indexed.

That asymmetry is the whole idea, and it comes from a real decision. One of
the roles running on this harness holds a private dialogue; its sessions are
nobody's material, not even the operator's own dashboard. But its gates still
have to be provably alive -- a dead gate on a private pane is exactly as
dangerous as a dead gate anywhere else. So: **its hygiene is public, its
conversation is not.**

The general principle is worth stating plainly, because it is the opposite of
what an observability tool usually assumes:

> An observation panel that can see everything is a surveillance tool. The
> question is never "what could we index", it is "what do we need to see in
> order to correct the system". Those two sets are not the same, and the
> difference between them is where trust lives.

The blind spot is a decision someone takes, never one they inherit: the
default exclusion list is EMPTY. And it is retroactive -- adding a pattern
purges what was indexed before the pattern existed, sessions, messages and
analyses alike. An exclusion that only applies going forward is not an
exclusion, it is a promise.

```sh
# By role name, by directory glob, or both.
export HARNESS_WATCH_EXCLUDE="private-role:*/personal-journal/*"
```

## The analyst proposes, and never acts

The panel has one button that costs something: **Analyze**. It hands a
FINISHED session to an LLM judge, which returns a severity, a summary,
findings, and optionally a gate proposal. The verdict is stored in a row and
displayed. That is the entire effect.

Nothing downstream reads it. No gate is armed, no file is edited, no ticket is
opened, no rule is added. If a proposal is any good, a human carries it to the
governor, where it faces two adversarial judges and a human word before
anything is ever armed. A judge that could arm what it proposes would be a
judge with an incentive.

What the judge receives is a deterministic SKELETON, not the raw session:

- tool CALLS with their arguments, and the agent's own text, both truncated;
- gate events, marked when they blocked;
- tool RESULTS are EXCLUDED, unless flagged as errors, and even then truncated
  short. Command outputs are where secrets live, and the grid does not need
  them to spot a bypass;
- `mask_secrets` (the same helper the gate journal uses) is applied to
  everything that remains;
- over 80k characters, the MIDDLE is dropped with the cut counted: the opening
  carries the mandate and the closing carries the claims, which is where the
  grid reads best.

The judge itself is ISOLATED, the same way the shield reviewer and the
governor's first judge are:

- it runs the agent CLI headless in an EMPTY temporary directory, so it
  inherits no project instruction file and sees none of the files around what
  it grades. An instrument that reads the answer sheet measures nothing. The
  neutral cwd has a second effect here: the judge's own session is not written
  inside an indexed root, so the instrument stays out of its own measurement;
- its environment is purged of every `ANTHROPIC_*` variable, so an analysis
  can never silently fall back onto a metered API key;
- it is HANDLESS: `Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent,Task`
  are all disallowed. The worst case of a prompt injection inside a judged
  session is a lying verdict, never an action. The skeleton is framed as an
  OBJECT OF ANALYSIS and the prompt says outright that no instruction inside
  it can change the grid.

Severity is a closed list: `info`, `notice`, `serious`, `critical`. A verdict
that cannot be parsed, or that carries a severity outside the list, is NOT
stored -- the analysis is reported as failed instead. A panel that invents a
severity is worse than a panel that admits it did not get an answer.

### A dead analysis is visible

The judge runs in a thread, outside the request. The transient state of that
thread lives in the server and is EXPOSED by the API: `analysis_status` is
`running`, then `error`, then absent once a verdict lands, and `analysis_error`
carries the short reason.

This is a fix that was paid for. Without it, a judge thread that dies -- and
the way it died was the PATH problem below -- leaves the client polling a
frozen button forever, with no error anywhere the human is looking. The thread
catches `BaseException` on purpose, because an unknown session raises
`SystemExit`, and `except Exception` would let exactly that case die in
silence. **A failed analysis has to be as visible as a successful one.**

## Wiring it

Run it by hand, or leave it running. Two commands, no daemon required: the
server runs an incremental index pass on every dashboard request (measured at
0.0 s when nothing changed), so the view is always fresh.

```sh
python3 watch/indexer.py            # incremental pass
python3 watch/indexer.py --rebuild  # throw it away and re-index
python3 watch/server.py             # http://127.0.0.1:8815
```

`server.py` alone is enough on a brand-new machine: it opens the derived
database through the indexer, which applies `schema.sql` first, so the very
first request renders an EMPTY dashboard and the incremental pass fills it. It
used to answer `500 no such table: files` on every route until someone thought
to run the indexer by hand -- and nothing on screen said so. An empty panel is
correct; a permanently broken one is not.

### EXAMPLE -- systemd user unit

```ini
# EXAMPLE -- ~/.config/systemd/user/harness-watch.service
# Adjust the paths. This file is not shipped armed.
[Unit]
Description=Harness watch, local read-only observation panel

[Service]
Type=simple
# READ THIS BEFORE REMOVING THE LINE BELOW.
# A systemd unit starts with a MINIMAL PATH (/usr/local/bin:/usr/bin:/bin).
# The agent CLI is usually installed user-local, in ~/.local/bin, which is NOT
# on that PATH. The panel then works perfectly except for one thing: every
# analysis dies instantly with FileNotFoundError, in a background thread, with
# nothing on screen. This trap bit three times before it was written down.
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HARNESS_STATE_DIR=%h/.harness
Environment=HARNESS_WATCH_TRANSCRIPTS=builder=%h/.claude/projects/builder
Environment=HARNESS_WATCH_EXCLUDE=private-role
ExecStart=/usr/bin/python3 %h/harness/watch/server.py
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now harness-watch.service
```

The analyst also repairs its own `PATH` when it spawns the judge (it prepends
the user-local bin directory), so the panel survives a unit that forgot the
line. Both layers exist on purpose: the unit line is the fix, the code line is
the seatbelt, and the test suite asserts the seatbelt. A recurring trap
deserves two answers, and neither of them is "remember to set PATH".

Everything the panel needs must be in the unit anyway -- `HARNESS_STATE_DIR`,
`HARNESS_GATE_STATS`, the transcript roots. A unit inherits nothing from your
shell.

### EXAMPLE -- keeping the index warm on a timer

Not required (the server indexes on demand), but useful if you want the
integrity check to run daily and journal its line whether or not you opened
the panel:

```cron
# EXAMPLE -- same PATH caveat as systemd: cron gets a minimal environment.
15 6 * * * HARNESS_STATE_DIR=$HOME/.harness /usr/bin/python3 \
  $HOME/harness/watch/indexer.py
```

## Files

| Path | Role |
|---|---|
| `watch/config.py` | sources, exclusions, paths -- all resolved from the environment |
| `watch/schema.sql` | the published DDL of the derived database |
| `watch/indexer.py` | incremental index: transcripts + journals -> SQLite |
| `watch/server.py` | the local read-only HTTP panel (stdlib only) |
| `watch/analyst.py` | the post-hoc judge: proposes, never acts |
| `watch/static/` | the UI: zero build, zero dependency, `textContent` only |

## API

| Route | Returns |
|---|---|
| `GET /api/summary?days=N` | dashboard tiles, per-day series, hook x result |
| `GET /api/sessions` | the 200 most recent sessions, with `analysis_status` |
| `GET /api/session/<id>?page=N` | metadata, one page of messages, gate events, analysis |
| `GET /api/content/<id>/<seq>` | ONE message, re-read from the source file |
| `POST /api/analyze/<id>` | schedules the post-hoc judge; returns `started` or `already-running` |

`POST /api/analyze` is the only non-GET route in the panel, and it mutates
nothing outside it: it schedules a read-only judge whose entire output is one
row in the `analyses` table. Every route above, that one included, answers
`403` to a request whose `Host` does not name loopback, and `POST` answers
`403` to a foreign `Origin`. See "The bind is NOT the only defense" above.

## Environment

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_WATCH_DB` | derived database path | `$HARNESS_STATE_DIR/watch/watch.db` |
| `HARNESS_WATCH_TRANSCRIPTS` | colon-separated `[<role>=]<directory>` roots | subdirectories of `~/.claude/projects` |
| `HARNESS_WATCH_JOURNALS` | colon-separated `[<scope>=]<file>` journals | the `_hook` gate-stats journal |
| `HARNESS_WATCH_EXCLUDE` | patterns whose TRANSCRIPTS are never indexed | empty |
| `HARNESS_WATCH_PORT` | local port of the panel | `8815` |
| `HARNESS_WATCH_MODEL` | model alias for the post-hoc judge | `sonnet` |
| `HARNESS_WATCH_TIMEOUT` | hard timeout of one judge call, seconds | `600` |
| `HARNESS_WATCH_FAKE_VERDICT` | TEST ONLY: fixed verdict replacing the judge | unset |
| `HARNESS_LLM_CLI_NAMES` | agent CLI binaries; the first one is the judge | `claude` |
| `HARNESS_STATE_DIR` | state directory (read through `_hook`) | `~/.harness` |
| `HARNESS_GATE_STATS` | default journal (read through `_hook`) | `$HARNESS_STATE_DIR/gate-stats.jsonl` |

The judge timeout is long on purpose: this is post-hoc work with no latency
budget, and a large session genuinely takes minutes. `HARNESS_WATCH_FAKE_VERDICT`
exists so the suite can prove the storage, the routing and the failure paths
without a single network call; setting it in production replaces the judge with
a constant.
