#!/usr/bin/env python3
"""fleet.py -- who is running RIGHT NOW, read from the terminal multiplexer.

    python3 fleet.py             the roster and its summary, as JSON

The event log answers "what happened". This answers the other half: is the
researcher pane alive at this second, and is it thinking or waiting for me. The
two are deliberately separate sources -- a log line proves an agent was alive
when it wrote, never that it still is.

SOURCE, in order:
  1. HARNESS_MC_ROSTER pins, `<role>=<pane>`, when someone wants a fixed map.
  2. Otherwise DISCOVERY: `tmux list-panes -a`, matching a role name against
     the pane title or its session name. Discovery is the default because a
     pinned pane address goes stale the first time windows are reordered, and
     a panel showing a stale address looks broken in a way nobody debugs.

READ-ONLY AND FAIL-SOFT. It runs one listing command and parses text. No
multiplexer, no server, a timeout, a parse failure: the fleet is empty and the
panel says so. It never raises into the HTTP surface.

ROLES, NOT NAMES. A pane is identified by the job it does (`builder`,
`researcher`, whatever HARNESS_MC_ROLES says). Naming panes after people is how
an observation panel quietly becomes a way to watch people.

Environment: see config.py (HARNESS_MC_ROLES, HARNESS_MC_ROSTER,
HARNESS_LLM_CLI_NAMES).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C                       # noqa: E402

# A pane title carrying a spinner glyph means the agent is mid-turn; a static
# title means it is waiting for a human. A heuristic on purpose: it costs one
# listing command instead of instrumenting the agent.
# Codepoints of the braille animation block plus the sparkles an agent CLI
# paints while it works. Written as numbers so this file stays pure ASCII.
SPINNER_GLYPHS = frozenset(
    [chr(cp) for cp in range(0x2800, 0x2900)] +
    [chr(cp) for cp in (0x2728, 0x2731, 0x2733, 0x2734, 0x273B)])

LIST_TIMEOUT = 5                          # seconds; a hung multiplexer is idle

PANE_FORMAT = ("#{session_name}:#{window_index}.#{pane_index}\t#{pane_title}\t"
               "#{pane_current_command}\t#{pane_active}\t#{pane_pid}\t"
               "#{pane_current_path}")


def list_panes() -> dict:
    """{pane_address: {title, command, active, pid, cwd}} for every pane.
    No multiplexer, no server, a timeout: {} and the panel shows an empty
    fleet, which is the truth."""
    try:
        proc = subprocess.run(["tmux", "list-panes", "-a", "-F", PANE_FORMAT],
                              capture_output=True, text=True, timeout=LIST_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    panes = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        address, title, command, active, pid = parts[:5]
        panes[address] = {"title": title, "command": command,
                          "active": active == "1", "pid": pid,
                          "cwd": parts[5] if len(parts) > 5 else ""}
    return panes


def discover(panes: dict, roles=None) -> dict:
    """{role: pane_address} matched by name against the pane title or its
    session name. One pane per role and one role per pane: the first match
    wins and the pane is then taken, so two roles never claim the same pane and
    a duplicate title cannot make one agent look like two."""
    found = {}
    taken = set()
    for role in (roles if roles is not None else C.roles()):
        needle = role.lower()
        for address, pane in panes.items():
            if address in taken:
                continue
            haystack = ((pane.get("title") or "") + " " +
                        address.split(":", 1)[0]).lower()
            if needle in haystack:
                found[role] = address
                taken.add(address)
                break
    return found


def classify(pane, cli_names=None) -> str:
    """working | idle | shell | dead.

    `dead` is a pane the roster expects and the multiplexer does not have.
    `shell` is a pane that exists but is running something other than an agent
    CLI -- a utility window, not a seat that stopped working. Telling those two
    apart is the difference between "your researcher died" and "that is just
    your terminal"."""
    if pane is None:
        return "dead"
    command = (pane.get("command") or "").lower()
    if command not in (cli_names if cli_names is not None else C.agent_cli_names()):
        return "shell"
    title = pane.get("title") or ""
    return "working" if any(ch in SPINNER_GLYPHS for ch in title) else "idle"


def live_fleet() -> list:
    """One entry per role, in the configured order: role, pane, cwd, alive,
    command, activity, state, resolved.

    `resolved` says HOW the pane was found -- `pin`, `discovered`, or `none`.
    A panel that hides which of its answers came from a guess is a panel you
    cannot debug when it is wrong."""
    panes = list_panes()
    pins = C.roster_pins()
    discovered = discover(panes, [r for r in C.roles() if r not in pins])

    out = []
    seen = set()
    for role in list(C.roles()) + [r for r in pins if r not in C.roles()]:
        if role in seen:
            continue
        seen.add(role)
        address = pins.get(role) or discovered.get(role)
        resolved = "pin" if role in pins else ("discovered" if address else "none")
        pane = panes.get(address) if address else None
        if pane is None and resolved == "pin":
            resolved = "none"            # pinned at an address that is gone
        out.append({
            "role": role,
            "pane": address or "",
            "cwd": (pane or {}).get("cwd", ""),
            "alive": pane is not None,
            "command": (pane or {}).get("command", ""),
            "activity": (pane or {}).get("title", ""),
            "state": classify(pane),
            "resolved": resolved,
        })
    return out


def summary(fleet=None) -> dict:
    """Compact counts for the overview tiles."""
    fleet = live_fleet() if fleet is None else fleet
    return {"total": len(fleet),
            "alive": sum(1 for f in fleet if f["alive"]),
            "working": sum(1 for f in fleet if f["state"] == "working"),
            "idle": sum(1 for f in fleet if f["state"] == "idle"),
            "shell": sum(1 for f in fleet if f["state"] == "shell"),
            "dead": sum(1 for f in fleet if not f["alive"])}


def main() -> int:
    fleet = live_fleet()
    print(json.dumps({"fleet": fleet, "summary": summary(fleet)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
