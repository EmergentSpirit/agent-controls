# destructive-command-gate

PreToolUse gate on `Bash`. Exit 2 = block, fail-open everywhere, every execution
writes one line to the gate-stats journal (`pass`, `block`, `fail-open`,
`skip-not-bash`, `skip-empty`, `skip-authorized`, `skip-disabled`).

## What it blocks

Six families of commands, judged on the command as the shell would run it. Only
families 1, 2 and 6 are armed by default; 3, 4 and 5 are written and tested but
disarmed until `HARNESS_DESTRUCTIVE_COMMAND_FAMILIES` says otherwise.

| Family | Armed by default | Shapes |
|---|---|---|
| 1 privilege escalation | yes | `sudo`, `su`, `pkexec`, `doas` |
| 2 mass deletion | yes | `rm -r` with any target outside `/tmp`, `find … -delete`, `shred` |
| 6 secret / trust-anchor purge | yes | `rm`/`shred`/`mv`/`cp`/`truncate`/`tee`/`install` touching a secret-bearing file, and any `>` redirection overwriting one |
| 3 raw disk | no | `dd if=`, `mkfs`, `wipefs`, `> /dev/sdX` |
| 4 DBMS | no | `DROP TABLE/DATABASE`, `TRUNCATE TABLE`, `DELETE FROM` with no `WHERE` |
| 5 services | no | `systemctl … stop\|disable\|mask`, `kill -9`, `pkill`, `crontab -r` |

Two design choices keep the false-positive rate at zero on ordinary work:

- **Command position, not mention.** Escalation and deletion verbs only count at
  the start of a command or right after a separator (`;`, `&&`, `||`, `|`, `(`).
  `grep -r sudo .` is an audit, not a gesture.
- **Literals stripped first.** Heredocs and quoted strings are removed before
  matching, so `echo "never run rm -rf on this box"` and a commit message that
  explains the trap both go through.

The same stripping is a known blind spot on family 4: SQL passed as a quoted
argument (`psql -c 'DROP TABLE clients'`) is invisible to the gate. That is the
accepted price of never blocking prose; family 4 catches the unquoted and piped
forms.

Secret-bearing files are matched by marker, and the shipped list is an EXAMPLE
(`.secrets`, `.age`, `authorized_keys`, `known_hosts`). Point
`HARNESS_DESTRUCTIVE_COMMAND_SECRET_FILES` at your own vault names.

## Founding incident

No single spectacular loss founded this gate; what founded it was a measurement.
The rule "a production, payable or destructive mutation is a HUMAN decision"
existed only in the system prompt and in the operator's discipline. Nothing
enforced it. A public Apache-2.0 sysadmin-agent benchmark had already published
the uncomfortable number: coding agents reach for `su`/`sudo` on their own, and
that work only *counts* the reaches. The criterion adopted here was that a rule
whose effectiveness depends on the model's goodwill is not a rule.

The second half of the incident happened during the build, and is why the
perimeter is what it is. The initial brief asked for a whole dotted home
directory to be protected. That directory is the LIVE STATE directory: gate
stats, reports, journals, written on every single call. Arming it would have
blocked the harness against itself within minutes. It was caught before wiring
and the rule was narrowed to the secret bearers themselves. Hence the shipped
default of three families instead of six: the perimeter widens on what actually
fires in the journal, never on what feels dangerous in the abstract.

This gate is deliberately fail-OPEN on an unreadable payload. It is an
anti-drift gate, not a network wall: the shield's hard-deny layer stays
fail-CLOSED on unreadable mutating input, and the two layers are meant to sit
side by side.

## Legitimate exception path

1. **Authorize the specific command, in writing.** Put the tag inside the
   command; the reason is mandatory (3 characters minimum, no closing bracket):

   ```sh
   rm -rf /opt/stale-build [DESTRUCTIVE-AUTHORIZED reason=cleanup_after_failed_deploy]
   ```

   The tag is read on the raw command, so it survives being quoted. A bare
   `[DESTRUCTIVE-AUTHORIZED]` with no reason does not authorize anything:
   allowing without saying why is a reflex, not a decision. The journal records
   `skip-authorized`, so authorizations stay countable.

2. **Disarm for one session:** `HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE=1`
   (journaled as `skip-disabled`).

3. **Reshape the perimeter** instead of routing around it:

   ```sh
   HARNESS_DESTRUCTIVE_COMMAND_FAMILIES="1,2,3,4,5,6"     # arm everything
   HARNESS_DESTRUCTIVE_COMMAND_SECRET_FILES=".vaultkey:.age"
   HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS="terraform\s+destroy"
   ```

   `HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS` takes one regex per line; an
   unparsable one is skipped rather than crashing the gate.

4. **Housekeeping is already free.** `rm -rf` under `/tmp`, `/var/tmp` or
   `$TMPDIR` never fires, and a non-recursive `rm -f` is outside family 2
   entirely.
