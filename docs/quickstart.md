# Quickstart -- 15 minutes, from clone to a gate that bites

Everything below was run end to end before it was written down, in a throwaway
tree at `/tmp/agent-controls-quickstart`. Every output on this page is the real
output of the command above it. Paste the commands as they are and you should
see the same thing, modulo timestamps and paths.

**Requirements:** `python3` (3.10 or newer), `git`. Nothing else. The harness
itself has zero third-party dependency; `pytest` is needed only to run the test
suites.

**What you will have at the end:** three gates wired on a scratch project, one
deliberate block with its real message, a journal that proves each gate ran,
and a daily health check that tells you which gates are alive and which are
only present.

---

## 1. Get the code and prove it works (4 min)

Nothing in this repository is armed by cloning it. A checkout is inert.

```sh
mkdir -p /tmp/agent-controls-quickstart && cd /tmp/agent-controls-quickstart
git clone https://github.com/EmergentSpirit/agent-controls.git
python3 -V
```

```
Python 3.12.3
```

Run the suites before you trust a single line of this page. They are the
product's argument: a module without its suite does not ship here.

```sh
python3 -m venv .venv
.venv/bin/pip install pytest
cd /tmp/agent-controls-quickstart/agent-controls
../.venv/bin/python -m pytest tests/ -q
```

```
..................................... [ 11%]
........................... [ 20%]
............................................. [ 34%]
............................................................................................................... [ 70%]
.................................................. [ 86%]
...........................................             [100%]
359 passed, 179 subtests passed in 39.26s
```

The suites themselves make zero network call and use no API key: every LLM judge
in the harness has a fake-verdict variable the tests set instead. That is
deliberate, and it is why the same command works on a laptop, in CI, and behind
a firewall. `pytest` is the only thing that had to be downloaded, and it is the
only third-party package involved anywhere.

## 2. Point the harness at a state directory (1 min)

Four variables. They are the whole configuration surface for this walkthrough.

```sh
cd /tmp/agent-controls-quickstart
mkdir -p demo/.claude state

export HARNESS_HOME=/tmp/agent-controls-quickstart/agent-controls  # the checkout
export HARNESS_STATE_DIR=/tmp/agent-controls-quickstart/state      # where state lives
export HARNESS_HOOK_DIRS=$HARNESS_HOME/hooks                       # dirs holding live hooks
export HARNESS_WRITE_SCOPE=/tmp/agent-controls-quickstart/demo     # this agent's perimeter

cd /tmp/agent-controls-quickstart/demo
printf '%s\n' "$HARNESS_HOME" "$HARNESS_STATE_DIR" "$HARNESS_HOOK_DIRS" "$HARNESS_WRITE_SCOPE"
```

```
/tmp/agent-controls-quickstart/agent-controls
/tmp/agent-controls-quickstart/state
/tmp/agent-controls-quickstart/agent-controls/hooks
/tmp/agent-controls-quickstart/demo
```

`HARNESS_STATE_DIR` matters more than it looks. **State never lives in the
repository**: journals, stamps, reports and databases all land under that
directory, so the checkout stays something you can `git pull` without merge
noise, and so a state directory is something you can delete without losing
code. Default is `~/.harness` when the variable is unset.

Full list of variables: [naming-table.md](naming-table.md).

## 3. Wire three gates (3 min)

A gate is a script the agent CLI runs before a tool call. Wiring is one JSON
file. Three gates is a deliberate starting point: two on `Bash`, one on the
write tools.

```sh
cat > .claude/settings.json <<'JSON'
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command",
           "command": "python3 $HARNESS_HOME/hooks/home-prefix-gate.py"},
          {"type": "command",
           "command": "python3 $HARNESS_HOME/hooks/destructive-command-gate.py"}
        ]
      },
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          {"type": "command",
           "command": "python3 $HARNESS_HOME/hooks/scope-write-gate.py"}
        ]
      }
    ]
  }
}
JSON
```

Ask the sentinel what it sees, before running anything:

```sh
python3 $HARNESS_HOME/sentinel/sentinel.py --settings .claude/settings.json --enumerate
```

```
hook  PreToolUse         script     /tmp/agent-controls-quickstart/agent-controls/hooks/home-prefix-gate.py
hook  PreToolUse         script     /tmp/agent-controls-quickstart/agent-controls/hooks/destructive-command-gate.py
hook  PreToolUse         script     /tmp/agent-controls-quickstart/agent-controls/hooks/scope-write-gate.py
total: 3 hooks wired across 1 settings file(s)
```

That inventory is DERIVED from your settings file, never from a list baked into
the sentinel. Wire a fourth gate tomorrow and it shows up here with no edit to
any module.

To point a real agent session at this file, hand it to your CLI
(`--settings .claude/settings.json`) or use the launcher template, which renders
a resolved copy for you: see [launchers.md](launchers.md).

## 4. Make a gate bite, on purpose (3 min)

A hook speaks a small contract: **JSON on stdin, exit 0 = allow, exit 2 = block,
stderr = what the agent reads.** So you can drive any gate by hand, with exactly
the payload the agent CLI would send. That is also how the test suites do it.

### 4.1 First, a command that passes

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"echo hello && ls /tmp"}}' \
  | python3 $HARNESS_HOME/hooks/home-prefix-gate.py
echo "exit=$?"
```

```
exit=0
```

Silence and exit 0. Nothing on stderr, nothing in the agent's way. That is what
a gate does on almost every call it ever sees.

### 4.2 Now the block

`home-prefix-gate` blocks a command prefixed with a `HOME=` assignment. Inside
an agent's shell tool that construct returns EMPTY output with exit 0, so it
looks exactly like a real measurement and is not one.

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"HOME=/tmp/elsewhere python3 -c \"print(1)\""}}' \
  | python3 $HARNESS_HOME/hooks/home-prefix-gate.py
echo "exit=$?"
```

```
BLOCKED (HOME= prefix gate): reassigning HOME as a command prefix BREAKS the Bash tool's output capture — the output comes back EMPTY with exit=0, even for a plain print. The false result is silent and reproducible: it looks like a measurement and is not one. Instead: write a .sh that runs the trial and WRITES ITS VERDICT TO A FILE, launch it, then read the file. To just read a process's environment, `tr '\0' '\n' < /proc/<pid>/environ`. Session kill-switch: HARNESS_HOME_PREFIX_GATE_DISABLE=1
exit=2
```

Read that message again, because it is the template every gate follows: what
was blocked, WHY it is blocked, **the working alternative**, and the way out.
A block that only says no teaches the agent to route around gates. A block that
hands over a working alternative gets obeyed.

### 4.3 A second gate, and its exception path

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"sudo systemctl restart nginx"}}' \
  | python3 $HARNESS_HOME/hooks/destructive-command-gate.py
echo "exit=$?"
```

```
BLOCKED (destructive-command-gate) - privilege escalation: sudo.
   If this is intended, the human decides BY SAYING SO: add to the command
   [DESTRUCTIVE-AUTHORIZED reason=<why>]
   Session kill switch: HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE=1
exit=2
```

Every gate has a documented legitimate exception path. Here it is an in-band
tag, and the reason is mandatory:

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"sudo systemctl restart nginx [DESTRUCTIVE-AUTHORIZED reason=operator_asked_for_a_restart]"}}' \
  | python3 $HARNESS_HOME/hooks/destructive-command-gate.py
echo "exit=$?"
```

```
exit=0
```

It went through, and step 5 will show that it went through VISIBLY. An
authorization that leaves no trace is an authorization nobody can audit.

### 4.4 The third gate: the write perimeter

`scope-write-gate` refuses a write landing outside `HARNESS_WRITE_SCOPE`.

```sh
echo '{"tool_name":"Write","tool_input":{"file_path":"/tmp/agent-controls-quickstart/elsewhere/notes.md","content":"x"}}' \
  | python3 $HARNESS_HOME/hooks/scope-write-gate.py
echo "exit=$?"
```

```
BLOCKED (scope-write gate): writing OUTSIDE this agent's perimeter.
  target:    /tmp/agent-controls-quickstart/elsewhere/notes.md
  perimeter: /tmp/agent-controls-quickstart/demo
Each role owns a perimeter. Writing code or mutating configuration outside it is another role's gesture, and that drift is silent: it looks like work done, it lands where nobody reviews it.
Normal route: leave a dated deliverable INSIDE your perimeter and hand the gesture over to the role that owns it.
Sanctioned one-shot, only AFTER an explicit human GO:
  python3 /tmp/agent-controls-quickstart/agent-controls/hooks/scope-stamp.py <prefix> --reason '...'
(30-minute window, ONE prefix, journaled as skip-stamp.)
Widen the perimeter for good instead: HARNESS_WRITE_SCOPE (colon-separated).
Session kill-switch: HARNESS_SCOPE_WRITE_GATE_DISABLE=1
exit=2
```

## 5. Read the journal (1 min)

Every gate execution appends one JSON line to the gate-stats journal, whatever
the outcome. This file is the only positive proof that a gate ran.

```sh
cat $HARNESS_STATE_DIR/gate-stats.jsonl
```

```
{"ts": "2026-08-05T14:59:22", "hook": "home-prefix", "result": "pass"}
{"ts": "2026-08-05T14:59:22", "hook": "home-prefix", "result": "block", "cmd": "HOME=/tmp/elsewhere python3 -c \"print(1)\""}
{"ts": "2026-08-05T14:59:22", "hook": "destructive-command", "result": "block", "pattern": "privilege escalation: sudo"}
{"ts": "2026-08-05T14:59:23", "hook": "destructive-command", "result": "skip-authorized"}
{"ts": "2026-08-05T14:59:23", "hook": "scope-write", "result": "block", "path": "/tmp/agent-controls-quickstart/elsewhere/notes.md"}
```

Five lines, five executions, and the fourth one is the authorization you granted
in 4.3: `skip-authorized`, countable after the fact. Routing around a gate is
allowed here, and it is never silent.

The `result` vocabulary is frozen: `pass`, `block`, `deny`, `warn`, `skip-*`,
`observe`, `fail-open`. The extra fields are each gate's own, always short and
always truncated. Anything that could carry a credential goes through
`_hook.mask_secrets()` first; treat the journal as readable by whoever can read
your state directory.

## 6. Run the sentinel and read its verdict (2 min)

A wired gate can be dead: the file exists, the settings file names it, and it
never runs. That failure is silent and reads exactly like a good day. The
sentinel is what refuses to accept "present" as proof of "alive".

```sh
python3 $HARNESS_HOME/sentinel/sentinel.py --settings .claude/settings.json
echo "exit=$?"
```

```
# sentinel -- 2026-08-05 14:59 -- 0.0 s
OK   settings  settings.json - 3 hooks wired
OK   script    home-prefix-gate.py - present, syntax OK (settings.json)
OK   script    destructive-command-gate.py - present, syntax OK (settings.json)
OK   script    scope-write-gate.py - present, syntax OK (settings.json)
WARN orphan    destructive-dry-run-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
WARN orphan    grep-quantifier-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
WARN orphan    hook-retire-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
WARN orphan    interlock-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
SKIP orphan    interlock-stamp.py - exempt, run by hand by design
WARN orphan    isolated-llm-measure-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
WARN orphan    response-length-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
SKIP orphan    scope-stamp.py - exempt, run by hand by design
WARN orphan    settings-go-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
WARN orphan    shell-false-success-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
WARN orphan    workflow-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
OK   journal   gate-stats.jsonl - fresh (0.0 h), last line valid JSON
OK   coverage  home-prefix-gate.py - 2 trace(s) in the journal over 7 days
OK   coverage  destructive-command-gate.py - 2 trace(s) in the journal over 7 days
OK   coverage  scope-write-gate.py - 1 trace(s) in the journal over 7 days
VERDICT WARN - 8 OK / 9 WARN / 0 FAIL / 2 SKIP
exit=0
```

How to read it:

- the three `coverage` lines are the point of the whole exercise: each wired
  gate left a trace, so each one is provably ALIVE, not merely present;
- the nine `orphan` WARNs are correct and expected here. `hooks/` ships twelve
  gates and you wired three; the other nine sit on disk, wired nowhere. A gate
  somebody believes is armed and is not, is exactly the expensive case;
- the two `SKIP orphan` lines are the stamp tools, exempt by design (a human
  runs them by hand). Exemptions are printed rather than hidden, so an exemption
  cannot become a blind spot;
- **exit is 0 whatever the verdict.** A monitor that kills its own timer unit
  stops monitoring. Use `--strict` if a caller wants exit 1 on FAIL.

The same text is written to a dated report file, and the sentinel journals its
own run:

```sh
ls $HARNESS_STATE_DIR/sentinel/
tail -1 $HARNESS_STATE_DIR/gate-stats.jsonl
```

```
2026-08-05.txt
{"ts": "2026-08-05T14:59:23", "hook": "sentinel", "result": "observe", "verdict": "WARN", "ok": 8, "warn": 9, "fail": 0, "skip": 2, "hooks": 3, "settings": 1}
```

`observe` because the sentinel blocks nothing. It reads, it reports, a human
decides. A watchdog that also repairs is a watchdog you eventually stop
trusting, because you can no longer tell what it found from what it did.

## 7. Prove the sentinel is not flattering you (1 min)

Unwire a gate and look again:

```sh
python3 - <<'PY'
import json
p = ".claude/settings.json"
d = json.load(open(p))
pre = d["hooks"]["PreToolUse"][0]["hooks"]
d["hooks"]["PreToolUse"][0]["hooks"] = [h for h in pre if "destructive" not in h["command"]]
json.dump(d, open(p, "w"), indent=2)
print("destructive-command-gate unwired from settings.json")
PY

python3 $HARNESS_HOME/sentinel/sentinel.py --settings .claude/settings.json \
  | grep -E 'destructive-command|VERDICT'
```

```
destructive-command-gate unwired from settings.json
WARN orphan    destructive-command-gate.py - on disk in /tmp/agent-controls-quickstart/agent-controls/hooks but wired in NO settings file
VERDICT WARN - 6 OK / 10 WARN / 0 FAIL / 2 SKIP
```

The gate left the wiring and the report said so on the next run, without anyone
telling the sentinel that gate ever existed. That is the whole design: the only
inventory that can be trusted is the one the system itself declares.

## Clean up

```sh
rm -rf /tmp/agent-controls-quickstart
```

Nothing was installed, no unit was enabled, nothing outside that directory was
touched.

## Where to go next

| You want | Read |
|---|---|
| how the pieces hold together | [../ARCHITECTURE.md](../ARCHITECTURE.md) |
| what each shipped gate blocks, and the incident behind it | [gates/](gates/) |
| to launch a real pane with a role, a vault and a perimeter | [launchers.md](launchers.md) |
| behavioral rules that hold at the moment of the violation | [shield.md](shield.md) |
| a memory format that cannot lie in its own index | [statutory-memory.md](statutory-memory.md) |
| "we already built that, do not rebuild it" | [recall.md](recall.md) |
| daily proof that the wired gates are alive | [sentinel.md](sentinel.md) |
| how a new gate gets approved without eating your attention | [governor.md](governor.md) |
| a local read-only panel over journals and sessions | [watch.md](watch.md) |
| every variable, every journal word | [naming-table.md](naming-table.md) |
| to add a gate of your own | [../CONTRIBUTING.md](../CONTRIBUTING.md) |

One last thing before you wire the battery on a real pane. **Do not arm the
whole battery on day one.** Wire two or three, live with them for a week, read
the journal, and add the next one when an incident asks for it. A gate that
fights your actual work every day teaches everyone to route around gates, which
costs you every other gate beside it.
