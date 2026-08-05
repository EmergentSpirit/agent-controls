# destructive-dry-run-gate

PreToolUse gate on `Write|Edit|MultiEdit`. Exit 2 = block, fail-open
everywhere, every execution logs one line to the gate-stats journal (`pass`,
`block`, `skip-disabled`, `skip-out-of-scope`, `skip-not-shell`,
`skip-out-of-perimeter`, `fail-open`).

## What it blocks

A `.sh` file being written into one of the operator's own script directories
(`HARNESS_OPERATOR_SCRIPT_DIRS`, colon-separated, default
`~/operator-scripts`) that carries a command destroying a disk beyond
recovery, WITHOUT both halves of the safety net.

Commands counted as destroying a disk: `wipefs` with `-a`/`-f`, `mkfs.*` /
`mkfs -t`, `cryptsetup luksFormat`, `sfdisk` (writing), `sgdisk` (writing),
`shred`, `dd … of=/dev/…`, `parted … mklabel`, `blkdiscard` — with or without
`sudo`, anywhere in a pipeline.

The safety net it demands is two-sided, and both halves must be present:

- a dry run **on by default** — a `DRYRUN=1` / `DRY_RUN=true` assignment at
  the head of a line;
- an **explicit gesture to destroy** — the script accepts `--go`,
  `--for-real`, `--execute` or `--confirm`.

Zero false positives by construction. It does NOT look at:

- anything outside the operator script directories — a script the agent runs
  itself is a script the agent reviewed; this gate is about the ones handed
  over and launched blind;
- non-shell files, or a payload writing no content;
- whole-line comments — documenting `sudo wipefs -a /dev/sdb` in prose is not
  running it;
- read-only forms — `wipefs /dev/sdb` (the listing), `sfdisk --dump`,
  `sgdisk -p`;
- any script where no disk-destroying command appears at all.

A script that has both halves of the net passes and is journaled as `pass`
with `guarded: true`: the presence of a real wipe under a real net stays
visible in the journal.

## Founding incident

An agent handed the human operator a `format-rescue-key.sh` that wiped a USB
stick. It had no dry-run mode, and not one of its guards had ever been
exercised — they were written, never run.

The operator was the one who caught it, on instinct, before launching:
*"re-read your script, it does a lot of acting in there, I would not want you
to erase something important."*

The forced re-read found two real defects. A name collision — the script
matched on `rescue`, and an encrypted volume on the same machine was
named `reserve`,
close enough that a partial match could have closed the wrong device. And a
window: `/dev/sda` was resolved once, checked, then used, with enough time in
between for a USB re-enumeration to move that name onto a different disk.

The lesson is not "review destructive scripts harder". It is that a human
catching this by hand is not a control, it is luck. A dry run by default
makes the wrong-target mistake structurally impossible and costs nothing on
the runs where everything was fine.

## Legitimate exception path

**Normal route — write the net.** It is four lines, and the gate names both
halves when either is missing:

```sh
DRYRUN=1
for a in "$@"; do case "$a" in --go) DRYRUN=0 ;; esac; done
...all the checks...
[ "$DRYRUN" -eq 1 ] && { echo 'NOTHING TOUCHED, rerun with --go'; exit 0; }
```

Run it bare first, read what it says it would do, then rerun with `--go`.

**Out of perimeter.** Scripts the agent runs itself, one-off commands, and
anything outside `HARNESS_OPERATOR_SCRIPT_DIRS` are not in scope. Point the
variable at the directories a human actually launches by hand, e.g.
`HARNESS_OPERATOR_SCRIPT_DIRS=~/bin/ops:~/handoff-scripts`.

**Session kill-switch.** `HARNESS_DESTRUCTIVE_DRY_RUN_GATE_DISABLE=1` lets a
hit through and journals it as `skip-disabled`, so routing around the gate
is visible after the fact rather than silent.
