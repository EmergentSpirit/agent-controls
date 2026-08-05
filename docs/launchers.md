# launchers -- vault, environment, CLI, and nothing in between

An agent pane is not a program you configure once. It is a process you START,
with a role, a perimeter, a set of credentials and a wiring, and everything the
harness enforces afterwards depends on what was true at the moment of that
`exec`. A gate reads an environment variable that was never exported. A pane
boots with the settings of another role. A key ends up in a shell history
because it was passed on a command line. None of these are exotic failures:
they are what happens when the boot sequence is improvised in a shell alias.

So the boot sequence is a file, it is a TEMPLATE, and it has three links:

```
VAULT                  ENVIRONMENT                CLI
decrypted ONCE   -->   values live in memory  --> exec, never fork
at boot                only, never on disk        (the CLI inherits the env)
```

Each link exists to make the next one honest.

## One role per pane, and no wire between panes

The harness runs several agents. They never talk to each other.

There is no bus, no shared queue, no manager process, no cross-pane state.
One pane holds one role, the operator drives each pane by hand, and a result
travels from one to the other only when a human carries it -- usually as a
file, which is a thing you can read before you act on it.

This is not asceticism, it is the mechanism:

- **A perimeter you can state in one sentence.** A role that cannot receive
  instructions from another agent has exactly one instruction source. When a
  pane does something surprising, the list of things that could have told it to
  is one item long.
- **A blast radius that stops at the pane.** A poisoned instruction, a bad
  inference, a runaway loop stays inside the process it started in. Nothing
  propagates, because nothing is connected.
- **Isolation you did not have to configure.** An unbuilt wire cannot be
  misconfigured, cannot silently reconnect after an update, and does not need a
  permission model. The cheapest security property is the one that comes from
  an absent feature.

The write perimeter follows the same logic: each role starts in its own
workspace, and `HARNESS_WRITE_SCOPE` defaults to that workspace. Two agents
that never share a working tree cannot overwrite each other's work.

## The launcher, step by step

`launchers/launch-agent.example.sh` takes the role as an ARGUMENT, so one
template serves every pane:

```
./launch-agent.example.sh builder
./launch-agent.example.sh researcher
./launch-agent.example.sh builder --resume     # extra arguments reach the CLI
```

What it does, in order:

1. **Validates the role.** It becomes part of a filename, so it is checked
   against `^[a-z][a-z0-9-]*$` before anything else.
2. **Prepends the per-user bin directory to `PATH`.** The same omission under a
   systemd unit is what silently killed three scheduled jobs (see below).
3. **Exports `HARNESS_HOME`, `HARNESS_AGENT`, `HARNESS_STATE_DIR`.** The gates
   and the settings examples all read these; they must exist BEFORE the CLI
   starts, not after.
4. **Enters the workspace** and derives `HARNESS_WRITE_SCOPE` from it.
5. **Opens the vault, once.** Absent vault, missing `age`, cancelled
   decryption: the session continues with no keys and SAYS SO on stderr. A
   launcher that dies on an absent optional file is a launcher nobody can
   debug at six in the morning.
6. **Renders the settings file for the role.** `builder` loads
   `settings.example.json`, any other role `r` loads
   `settings.<r>.example.json`. The shipped examples reference
   `$HARNESS_HOME` inside their hook commands, so the launcher writes a
   resolved copy to `$HARNESS_STATE_DIR/settings.<role>.json` and points the
   CLI at that. The repository file stays the single source of truth; nobody
   edits JSON by hand. Set `HARNESS_SETTINGS` to bypass all of this and use
   your own file verbatim.
7. **Refuses to start with no wiring.** An unreadable settings file is a hard
   exit, not a warning: an agent running with the gates silently absent is
   worse than an agent that did not start.
8. **Builds the CLI arguments as an array**, so an option you did not set adds
   nothing at all instead of an empty string.
9. **`exec`s the CLI.** The launcher is REPLACED by the agent process. No
   parent shell survives holding the decrypted values, and closing the pane
   leaves nothing behind.

### What is deliberately NOT in the template

- **The role's system prompt.** Its job, its voice, its standing constraints:
  that file is yours and lives outside this repository. Point
  `HARNESS_ROLE_PROMPT` at it and it is appended at boot.
- **Connectors.** `HARNESS_MCP_CONFIG` is unset by default, which means no
  external connector at all. An agent that reaches nothing by default is an
  agent whose perimeter you can actually describe.
- **The boot prompt.** `HARNESS_BOOT_PROMPT` is the first thing the agent
  reads. A useful one points at the continuity file and then stops, for
  example: *"Read the handoff file at ~/state/handoff.md, summarize it in three
  lines, then wait."*

## The vault

`launchers/vault.example.sh` builds and inspects it; the launcher only reads
it.

The model: **one age-encrypted env file, decrypted once at boot, values
exported into the process environment, never written back to disk in clear and
never printed.**

```
./vault.example.sh template ~/secrets.env   # an EXAMPLE plaintext file
./vault.example.sh encrypt  ~/secrets.env   # encrypt, then destroy the plaintext
./vault.example.sh names                    # the NAMES it carries, no values
./vault.example.sh check                    # does it still open at all
```

Why not a plaintext file with mode 600? Because mode 600 protects against
another user, not against a backup, a sync client, a snapshot, a crash dump or
a grep across the home directory. Plaintext at rest quietly multiplies, and the
copies are what leak. The vault is opened ONCE per pane, at a moment a human is
present; every later read is a read of process memory, which dies with the
pane.

The identity can be a file, or it can be **backed by hardware** -- a PIV token,
for instance. In that case `HARNESS_VAULT_IDENTITY_CMD` names a command that
PRINTS the identity, and the launcher consumes it through a file descriptor, so
the identity is never written down either. The encrypted file alone is then
worth nothing without the physical key. That is a deployment choice: nothing in
this repository requires it.

Three rules the helper enforces by construction:

- a value is **never printed** to a terminal, a log or a journal entry -- the
  launcher reports a COUNT, and `names` reports names only;
- a value is **never passed on a command line**, where it would appear in the
  process list;
- there is **no decrypted copy on disk**, not even briefly, not even in a
  temporary directory.

`HARNESS_VAULT_KEYS` narrows what a role receives: a colon-separated allowlist
of names. Empty means everything the vault carries. A researcher pane has no
business inheriting a deployment credential.

## Timers: the units, and the trap in every one of them

`launchers/systemd/` ships three example user units and their timers:

| Unit | Cadence | What it does |
|---|---|---|
| `harness-sentinel` | daily, 06:30 | proves the wired gates are alive |
| `harness-recall-refresh` | boot + every 20 min | rebuilds the file-existence index |
| `harness-governor-audit` | weekly, Monday | audits the live battery, closes due trials |

Copy them to `~/.config/systemd/user/`, adjust the paths (`%h` already expands
to your home directory), then:

```
systemctl --user daemon-reload
systemctl --user enable --now harness-sentinel.timer
systemctl --user start harness-sentinel.service    # first run, right now
```

`Persistent=true` is on every timer on purpose: a machine asleep at the
scheduled hour runs the job at the next boot instead of skipping the period in
silence.

### Every unit carries `Environment=PATH=...`, and here is why

**A systemd unit does not inherit your login `PATH`.** It starts with a minimal
one that does NOT contain the per-user bin directory where an agent CLI, a
language runtime or a helper binary normally lives.

The failure that follows is silent, which is what makes it expensive. The job
shells out to a binary BY NAME, the call fails inside a subprocess with
"command not found", the unit itself still exits 0, and the timer keeps firing
on schedule -- doing nothing. Three scheduled jobs died exactly that way, and
each was found by accident days later. The `systemctl --user list-timers`
output looked perfect the whole time.

So the rule is absolute here: **every unit declares its `PATH` explicitly, even
when its current `ExecStart` looks like it does not need one.** The line costs
nothing. Its absence costs a week of believing a dead job is alive, which is
the exact failure mode the sentinel exists to catch -- and the sentinel is one
of the three jobs.

The same trap applies to cron, with less warning: cron gives you an even
smaller environment, and any variable the job needs must be set in the crontab
line itself.

Secrets in a unit file deserve one line of their own: **never**. Unit files are
world-readable by default. The governor example passes a judge key BY NAME
(`HARNESS_JUDGE2_API_KEY_ENV`), which is read from the environment at call
time; the value stays in the vault.

## Adapting the template

Copy `launch-agent.example.sh` next to your own configuration, or use it as-is
and drive it entirely by environment. Everything below has a default, and every
default is the narrow choice:

| Variable | Meaning | Default |
|---|---|---|
| `HARNESS_HOME` | agent-controls checkout | parent directory of the launcher |
| `HARNESS_AGENT` | Role name for this pane | the role argument |
| `HARNESS_STATE_DIR` | State directory | `~/.harness` |
| `HARNESS_WORKSPACE` | Directory the agent starts in | `$PWD` |
| `HARNESS_WRITE_SCOPE` | Write perimeter | the workspace |
| `HARNESS_SETTINGS` | Settings file used verbatim | unset (rendered from the example) |
| `HARNESS_CLI` | CLI binary to exec | first name of `HARNESS_LLM_CLI_NAMES`, else `claude` |
| `HARNESS_ROLE_PROMPT` | File holding the role's system prompt | unset |
| `HARNESS_BOOT_PROMPT` | First prompt handed to the agent | unset |
| `HARNESS_MCP_CONFIG` | Connector config | unset = no external connector |
| `HARNESS_VAULT` | Encrypted env file | `~/.harness-secrets.env.age` |
| `HARNESS_VAULT_IDENTITY` | age identity file | `~/.config/age/identity.txt` |
| `HARNESS_VAULT_IDENTITY_CMD` | Command printing an identity (hardware-backed key) | unset |
| `HARNESS_VAULT_KEYS` | Colon-separated allowlist of names to export | empty = all |

Adding a role is three steps and no code:

1. write `launchers/settings.<role>.example.json` (start from the closest
   shipped example);
2. put the role's system prompt wherever you keep it, and point
   `HARNESS_ROLE_PROMPT` at it;
3. `./launch-agent.example.sh <role>`.

The launcher never learns about roles. It resolves a filename, and a role that
has no settings file is a hard error naming the file it looked for.

## Files

| Path | Role |
|---|---|
| `launchers/launch-agent.example.sh` | the parameterized launcher template |
| `launchers/vault.example.sh` | build, encrypt, inspect and verify the vault |
| `launchers/settings.example.json` | EXAMPLE wiring, `builder` role |
| `launchers/settings.researcher.example.json` | EXAMPLE wiring, `researcher` role |
| `launchers/systemd/*.service`, `*.timer` | EXAMPLE user units, PATH declared |
