# mission-control -- the fleet panel

`watch` reads the past: journals and transcripts, indexed after the fact.
`mission-control` reads the PRESENT, and it exists because those are different
questions. A log line proves an agent was alive when it wrote the line. It
never proves the agent is alive now, that its pane did not die an hour ago,
that a timer is about to fire, or that something has been sitting there since
lunch waiting for a human to say yes.

Run more than one agent and the failure mode stops being "an agent did
something wrong". It becomes **nobody noticed**: a role that quietly stopped, a
scheduled unit nobody remembers arming, an operation blocked on an approval
that never arrived. The panel is the one screen where those show up as a
number instead of as a surprise.

## What it shows

**Overview** -- roles alive over roles expected, how many are mid-turn, the
size of the signed log, and the counts that matter inside a window (blockers,
health alerts, circuit breaks). Plus two verdicts: whether the log's HMAC still
checks out, and whether the execution engine is paused.

The counts are WINDOWED, seven days by default. An all-time counter keeps one
bad afternoon on screen forever, and a number that never moves stops being
read. The full history is one click away in Logs.

**Agents** -- one line per role: alive or not, working or waiting, what its
pane title says it is doing right now, its directory, and its last recorded
event. Two sources, deliberately: the multiplexer answers "alive", the log
answers "what happened". Neither can answer both.

**Schedule** -- everything on this machine that will run without a human
present: user timers and the crontab. Both are read fail-soft and
independently, because a machine with no cron daemon and no user manager is
perfectly normal and should get two empty tables, not an error.

**Logs** -- the signed event log, filtered by role, project, kind or a search
over the summary, with a signature verdict ON EVERY LINE. An integrity check
reported only as a global "OK" tells you something is wrong and never which
row, which is the one thing you need.

**Approvals** -- operations an execution engine refused to run alone. Each one
shows what it does in plain language, whether it is reversible, and why the
engine stopped. This view is empty and says so when you run no engine.

## Read-only by default, and physically so

Out of the box this process writes exactly two things: rows into its own
derived event database, and a throttle marker. It cannot approve, halt, deploy
or edit anything.

Everything that mutates the world outside the panel lives in an **optional
sibling module**. Missing, the route answers `503` and the button never
appears. Present, the module carries its own token gate, and the panel does not
re-implement it -- a caller that duplicates a security check is a caller that
will eventually disagree with it.

| Module | File | Contract |
|---|---|---|
| Pause the engine | `mission-control/halt.py` | `status()`, `request(role, token)`, `commit(body)` |
| Record an approval | `mission-control/approve.py` | `record(script, sha256, token)` |
| Hardware presence proof | `mission-control/presence.py` | `status()`, `begin(action, body)`, `complete(action, body)` |
| Hand over a secret | `mission-control/deposit.py` | `status()`, `store(slot, value, token)` |

An operator who installs none of them has a panel that is physically unable to
act. That is the intended default, and it is why the panel is safe to leave
open on a second screen all day.

`status()` for the halt module is the one exception, and it is a READ: with no
module installed the panel reads the flag file directly, because "is the engine
paused" has to work on a panel that installed no ability to act.

## The signed log

Events land in one SQLite table that is append-only twice over.

**The engine refuses.** `BEFORE UPDATE` and `BEFORE DELETE` triggers raise
`ABORT` (see `schema.sql`). Nothing going through this connection rewrites or
erases a row. That covers accidents and well-behaved code.

**The signature detects the rest.** Every row carries an HMAC-SHA256 over its
canonical fields. Someone who edits the file with another tool defeats the
triggers and not the signature: `verify_all` names the row ids that no longer
match, and the Logs view marks them line by line. A tampered log looks
tampered instead of looking clean.

The key is a local mode-600 file, created on first use. It is not shared with
anyone. It exists so a log cannot be edited by whoever reaches the disk but not
the running process. This is integrity and non-repudiation, **not**
confidentiality: rows are not encrypted.

**The database is never published.** What travels is `schema.sql` plus the
ingester that fills it, so anyone can rebuild the same panel from their own
journals. Delete the file and re-run `ingest.py`: nothing is lost, because the
`.jsonl` journals are the source of truth.

## Ingestion pulls, never pushes

Nothing instruments the agents. They write their own `.jsonl` lines wherever
they already write them, and `ingest.py` reads those files. The panel has no
runtime coupling with anything it observes, so an agent that crashes mid-line
costs one skipped line rather than a broken panel.

Every line maps to a provenance triple `(source, session, sequence)` held
UNIQUE by the store, so running the ingester on a timer, on the button and by
hand in the same second inserts each line exactly once.

Engine audit lines have no sequence number of their own, so the triple uses
whichever natural identity the line carries (`action_id`, `snapshot_id`, `ts`)
and pins the sequence to `0` rather than leaving it NULL. SQLite treats every
NULL as distinct inside a UNIQUE constraint: leaving it NULL would silently
disable the dedup and duplicate the whole audit on every pass.

## Roles, not people

A pane is identified by the job it does -- `builder`, `researcher`, whatever
`HARNESS_MC_ROLES` says. Two panes doing the same job carry the same name.

This is not cosmetic. Naming panes after people is how an observation panel
quietly becomes a way to watch people, and the moment the panel is about
individuals rather than about work, nobody wants it running.

Roles are found by DISCOVERY: a pane whose title or session name contains the
role name. First match wins and the pane is then taken, so two roles never
claim the same pane. `HARNESS_MC_ROSTER` can pin `<role>=<pane>` explicitly,
but discovery is the default, because a pinned pane address goes stale the
first time somebody reorders their windows, and a panel showing a stale address
looks broken in a way nobody debugs.

The panel says HOW each answer was reached -- `pin`, `discovered`, `none`. A
panel that hides which of its answers came from a guess is a panel you cannot
debug when it is wrong.

## Security

### The bind

`127.0.0.1`, and `build_server` asserts it. There is no host variable, on
purpose: a variable is a thing someone eventually sets to a wildcard address
"just to test from the laptop" and then leaves that way. This panel shows
command lines, pane titles and pending operations.

### The bind is not the only defense

A loopback bind stops a stranger on the network. It does not stop the two
attacks that come through the operator's own browser.

**DNS rebinding.** A name the attacker owns resolves to `127.0.0.1` after the
page has loaded. The browser then connects to the panel over loopback -- so the
bind is satisfied -- and the request carries `Host: attacker.example`. The Host
header is the only place that attack is visible at all, so every request must
carry a Host header NAMING loopback.

The port is deliberately not checked: it says which port the client dialed,
never who dialed it, and a tunnel (`ssh -L 9000:127.0.0.1:8787`) legitimately
changes it. A rebinding attacker controls the name.

**CSRF.** Any page open in that same browser can fire a POST at the panel, and
there is no credential to steal because there is none. So a mutating request
must carry no `Origin` (curl, a unit, a timer send none) or a loopback one.
`Origin: null` -- a sandboxed frame, a `file://` page -- is REFUSED rather than
read as absent, because that is exactly the shape a hostile local page has.
`Sec-Fetch-Site` is checked too: the browser sets it and page script cannot
forge it.

### The proxy case

Put a tunnel or a reverse proxy in front of this and the peer address is STILL
`127.0.0.1`, because the proxy connects to the backend from loopback. Trusting
the peer address would therefore trust the entire network behind the proxy.

So a request carrying a proxy marker header (`X-Forwarded-For`,
`X-Forwarded-Host`, `X-Real-IP`, `Forwarded`) must present the read token, in
`X-Panel-Token`, `?token=` or the `panel_token` cookie. **With no token file
configured, a proxied request is refused outright**: somebody put a proxy in
front of an unauthenticated panel, and guessing that they meant to is how logs
end up on the internet.

### Reading a script is confined

The Approvals view explains what a pending operation would do, which means
reading the script's header. That path arrives inside a journal line the panel
cannot verify -- it does not hold the key that signed it.

Unconfined, that is a file-disclosure primitive: anything able to append one
line to the journal picks a file and the panel renders its contents. So every
path is resolved with `realpath` (symlinks followed, `..` collapsed) and must
land inside `HARNESS_OPERATOR_SCRIPT_DIRS`. Anything else is skipped: not read,
not hashed, not shown.

The panel also shows the script's current sha256, and an approval carries it
back, so approving means approving the bytes that were on screen and not
whatever the file became in between.

### The page

No third-party script, no CDN, no build step. Strict CSP, `nosniff`,
`no-referrer`. Journal content, pane titles and script descriptions are
UNTRUSTED: everything the API returns reaches the DOM through `createElement`
and `textContent`, never through a raw-HTML assignment. A summary line written
by an agent must not be able to become script execution.

## Running it

```sh
python3 mission-control/ingest.py          # one pass, prints what it inserted
python3 mission-control/server.py          # http://127.0.0.1:8787
```

The server runs an ingest pass itself when a view is opened, throttled to once
per `HARNESS_MC_INGEST_INTERVAL` seconds, so the counters are never quietly
late. It is fail-soft: a broken ingest never takes down the display of the rows
already there.

As a user unit:

```ini
[Unit]
Description=mission-control fleet panel (loopback only)

[Service]
ExecStart=/usr/bin/python3 %h/agent-controls/mission-control/server.py
Restart=on-failure
Environment=HARNESS_STATE_DIR=%h/.harness

[Install]
WantedBy=default.target
```

Each module also runs standalone and prints JSON, which is the fastest way to
find out why a view is empty:

```sh
python3 mission-control/fleet.py           # the roster and why each pane resolved
python3 mission-control/approvals.py       # the queue, after path confinement
python3 mission-control/store.py           # self-check: dedup, triggers, signatures
```

## Environment

See the `mission-control` block of [naming-table.md](naming-table.md). The
panel reuses `HARNESS_STATE_DIR`, `HARNESS_OPERATOR_SCRIPT_DIRS` and
`HARNESS_LLM_CLI_NAMES` rather than inventing its own copies: the panel and the
gates have to agree on where state lives, where operator scripts live, and what
an agent process looks like.
