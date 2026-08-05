#!/usr/bin/env python3
"""destructive-command-gate.py - PreToolUse (Bash matcher). BLOCKS (exit 2)
destructive / privilege-escalation commands until an explicit authorization tag
is written INTO the command.

WHY: the rule "production mutation / payable / destructive -> the human decides"
lived in the prompt and in discipline only. Nothing enforced it mechanically. A
drifting agent could run `rm -rf`, `sudo`, or overwrite the secret vault with no
wall in front of it. The design criterion here: effectiveness must not depend on
the model's goodwill.

The idea comes from a public Apache-2.0 sysadmin-agent benchmark which MEASURES
how often an agent reaches for `su`/`sudo` in its bash calls. This gate does not
measure: it blocks.

TIGHT START (deliberate): only families 1 (escalation), 2 (mass deletion) and 6
(secret/config purge) are armed by default. Families 3/4/5 (raw disk, DBMS,
services) are written but DISARMED: the perimeter widens on what actually fires
in the journal, not on what we imagine. Arm them all with
HARNESS_DESTRUCTIVE_COMMAND_FAMILIES="1,2,3,4,5,6".

Escape hatch for the human: put the tag in the command itself

    [DESTRUCTIVE-AUTHORIZED reason=<text with no closing bracket>]

or disarm for one session: HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE=1

FAIL-OPEN on an unreadable payload: this is an anti-drift gate, not a network
security wall. The shield's hard-deny layer stays fail-CLOSED on unreadable
mutating input; this gate sits BESIDE it, it does not replace it.

Environment:
- HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE   "1" disarms for the session
- HARNESS_DESTRUCTIVE_COMMAND_FAMILIES       armed families, e.g. "1,2,3,4,5,6"
                                             (default: 1,2,6)
- HARNESS_DESTRUCTIVE_COMMAND_SECRET_FILES   colon-separated secret-bearing file
                                             markers (default:
                                             .secrets:.age:authorized_keys:known_hosts)
- HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS newline-separated extra regexes
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import gate_stat
except Exception:
    def gate_stat(*a, **k):
        pass

GATE = "destructive-command"

# Authorization tag, searched on the RAW command (before stripping), because a
# human may legitimately write it inside quotes. `reason=` is mandatory:
# authorizing without saying why is not a decision, it is a reflex.
TAG_RE = re.compile(r"\[DESTRUCTIVE-AUTHORIZED\s+reason=[^\]]{3,}\]", re.IGNORECASE)

# Command position: start of line, or right after a separator. This is what
# tells `sudo rm ...` (the gesture) apart from `grep -r sudo .` (the mention).
# Without it, any audit that NAMES a destructive verb would be blocked: the
# false positive that kills trust in a gate.
CMDPOS = r"(?:^|[;&|(\n]|&&|\|\|)\s*(?:sudo\s+)?"

# Files whose deletion/overwrite is a secret or trust-anchor loss. EXAMPLE list,
# widen it for your own vault names through HARNESS_DESTRUCTIVE_COMMAND_SECRET_FILES.
DEFAULT_SECRET_FILES = (".secrets", ".age", "authorized_keys", "known_hosts")


def _secret_markers():
    raw = os.environ.get("HARNESS_DESTRUCTIVE_COMMAND_SECRET_FILES", "").strip()
    if not raw:
        return list(DEFAULT_SECRET_FILES)
    out = [p.strip() for p in raw.split(":") if p.strip()]
    return out or list(DEFAULT_SECRET_FILES)


def _secret_alternation():
    """Regex alternation over the secret-bearing markers, word-bounded so that
    `.age` matches `vault.age` and `vault.age.bak` but not `.agent`."""
    return "|".join(re.escape(m) + r"\b" for m in _secret_markers())


def _strip_literals(cmd):
    """Drop heredocs and quoted strings BEFORE analysis.

    `echo "never run rm -rf"` must not fire: the pattern is TEXT, not a gesture.
    Replaced by a space (not by nothing) so two neighbouring tokens are never
    glued together.
    """
    # heredoc: <<EOF ... EOF  /  <<-'EOF' ... EOF
    cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?[\s\S]*?^[ \t]*\1[ \t]*$", " ", cmd, flags=re.M)
    # unterminated heredoc (truncated payload): cut everything from the <<
    cmd = re.sub(r"<<-?\s*['\"]?\w+['\"]?[\s\S]*$", " ", cmd)
    cmd = re.sub(r"'[^']*'", " ", cmd)
    cmd = re.sub(r'"[^"]*"', " ", cmd)
    return cmd


def _rm_targets(cmd):
    """Targets of a recursive `rm`. Returns [] when no recursive rm is present."""
    out = []
    for m in re.finditer(CMDPOS + r"rm\s+((?:-\S+\s+)*)([^;&|\n]*)", cmd):
        flags, rest = m.group(1) or "", m.group(2) or ""
        if not re.search(r"-\S*r", flags, re.IGNORECASE):
            continue  # not recursive -> out of scope for this rule
        for tok in rest.split():
            if tok.startswith("-"):
                continue
            out.append(tok)
    return out


def _rm_recursive_outside_tmp(cmd):
    """`rm -r` with AT LEAST one target outside /tmp. Housekeeping in /tmp stays free."""
    targets = _rm_targets(cmd)
    if not targets:
        return False
    safe = ("/tmp/", "/tmp", "/var/tmp/")
    for t in targets:
        if not (t.startswith(safe) or t.startswith("$TMPDIR")):
            return True
    # `rm -rf` with no named target (glob eaten by the strip) -> suspicious
    return False


_SECRETS_ALT = _secret_alternation()

# (family, regex|callable, label). callable(stripped_cmd) -> bool
RULES = [
    # ---- family 1: privilege escalation ------------------------------------------
    (1, re.compile(CMDPOS + r"sudo\b"), "privilege escalation: sudo"),
    (1, re.compile(CMDPOS + r"su\b(?!\w)"), "privilege escalation: su"),
    (1, re.compile(r"\b(?:pkexec|doas)\b"), "privilege escalation: pkexec/doas"),
    # ---- family 2: mass deletion ---------------------------------------------------
    (2, _rm_recursive_outside_tmp, "recursive delete outside /tmp (rm -r)"),
    (2, re.compile(r"\bfind\b[^\n;&|]*\s-delete\b"), "find ... -delete"),
    (2, re.compile(CMDPOS + r"shred\b"), "shred (irreversible destruction)"),
    # ---- family 6: secret / config purge -------------------------------------------
    # TIGHTENED vs the founding brief: the brief named a whole dotted directory,
    # which would have covered the LIVE STATE directory (gate stats, reports),
    # written on every single call. Gating that would have made the harness
    # unusable. We aim at the secret bearers themselves.
    (6, re.compile(r"(?:^|[;&|(\n]|&&|\|\|)\s*(?:sudo\s+)?(?:rm|shred|mv|cp|truncate|tee|install)\b"
                   r"[^\n;&|]*(?:" + _SECRETS_ALT + r")",
                   re.IGNORECASE),
     "delete/overwrite of a secret-bearing file"),
    (6, re.compile(r">\s*\|?\s*\S*(?:" + _SECRETS_ALT + r")", re.IGNORECASE),
     "redirection overwriting a secret-bearing file"),
    # ---- families 3/4/5: written, DISARMED by default (see module docstring) --------
    (3, re.compile(r"\bdd\s+if=|\bmkfs\b|\bwipefs\b|>\s*/dev/(?:sd|nvme|vd)[a-z]", re.IGNORECASE),
     "raw disk overwrite (dd/mkfs/wipefs/>/dev/sdX)"),
    (4, re.compile(r"\bDROP\s+(?:TABLE|DATABASE)\b|\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
     "destructive DBMS statement (DROP/TRUNCATE)"),
    (4, re.compile(r"\bDELETE\s+FROM\b(?![\s\S]*\bWHERE\b)", re.IGNORECASE),
     "DELETE FROM with no WHERE"),
    (5, re.compile(r"\bsystemctl\b[^\n;&|]*\b(?:stop|disable|mask)\b", re.IGNORECASE),
     "service stop/disable"),
    (5, re.compile(r"\bkill\s+-9\b|\bpkill\b|\bcrontab\s+-r\b", re.IGNORECASE),
     "kill -9 / pkill / crontab -r"),
]

DEFAULT_FAMILIES = {1, 2, 6}


def _active_families():
    raw = os.environ.get("HARNESS_DESTRUCTIVE_COMMAND_FAMILIES", "").strip()
    if not raw:
        return DEFAULT_FAMILIES
    out = set()
    for part in re.split(r"[,\s]+", raw):
        if part.isdigit():
            out.add(int(part))
    return out or DEFAULT_FAMILIES


def _extra_patterns():
    raw = os.environ.get("HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS", "").strip()
    if not raw:
        return []
    out = []
    for pat in [p for p in raw.split("\n") if p.strip()]:
        try:
            out.append(re.compile(pat, re.IGNORECASE))
        except re.error:
            continue
    return out


def block(why, pattern):
    gate_stat(GATE, "block", pattern=pattern[:60])
    print(
        f"BLOCKED (destructive-command-gate) - {why}.\n"
        f"   If this is intended, the human decides BY SAYING SO: add to the command\n"
        f"   [DESTRUCTIVE-AUTHORIZED reason=<why>]\n"
        f"   Session kill switch: HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE=1",
        file=sys.stderr,
    )
    sys.exit(2)


def main():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        gate_stat(GATE, "skip-empty")
        sys.exit(0)
    if os.environ.get("HARNESS_DESTRUCTIVE_COMMAND_GATE_DISABLE", "0") == "1":
        gate_stat(GATE, "skip-disabled")
        sys.exit(0)
    try:
        data = json.loads(raw)
    except Exception:
        # Fail-OPEN by design: anti-drift gate, not a network wall. The shield's
        # hard-deny layer is fail-CLOSED on unreadable mutating input; the two
        # layers complete each other.
        gate_stat(GATE, "fail-open", pattern="unreadable payload")
        print("WARN (destructive-command-gate) unreadable payload - fail-open",
              file=sys.stderr)
        sys.exit(0)

    if data.get("tool_name", "") != "Bash":
        gate_stat(GATE, "skip-not-bash")
        sys.exit(0)
    cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""
    if not cmd.strip():
        gate_stat(GATE, "skip-empty")
        sys.exit(0)

    # Tag searched on the RAW command (it may legitimately live inside quotes).
    if TAG_RE.search(cmd):
        gate_stat(GATE, "skip-authorized")
        sys.exit(0)

    scan = _strip_literals(cmd)
    fams = _active_families()

    for fam, rule, why in RULES:
        if fam not in fams:
            continue
        hit = rule(scan) if callable(rule) else bool(rule.search(scan))
        if hit:
            block(why, why)

    for rx in _extra_patterns():
        if rx.search(scan):
            block("custom pattern from HARNESS_DESTRUCTIVE_COMMAND_EXTRA_PATTERNS "
                  f"({rx.pattern[:40]})", rx.pattern)

    gate_stat(GATE, "pass")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        gate_stat(GATE, "fail-open", pattern=("crash: " + str(e))[:60])
        print(f"WARN (destructive-command-gate) internal error ({e}) - fail-open",
              file=sys.stderr)
        sys.exit(0)
