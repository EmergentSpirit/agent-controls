#!/usr/bin/env python3
"""PreToolUse gate on Bash: blocks shadowed `grep` calls that blow up ugrep.

WHY (measured in production, full re-verification over four trials):
the agent CLI ships TWO multicall grep engines inside its own binary: ripgrep
14.1.1 (healthy) and ugrep 7.5.0 (a DFA engine). On every command the Bash
tool spawns, the shell re-sources a snapshot that replaces `grep` with a shell
FUNCTION routing to the embedded ugrep (`exec -a ugrep <agent-binary>`). It is
hardcoded in the product, there is no kill-switch for it, and the dedicated
Grep tool does not even exist in some main sessions: EVERY grep the model
writes goes through that function.

The ugrep DFA engine explodes at PATTERN COMPILATION time when two bounded
quantifiers overlap, roughly 2^min(N,M) states, independently of the corpus.
Measured (exact shadow function extracted with `declare -f`, 2 GB memcg, RSS):

    control 'foo'                 →   6.7 MB, rc=0 (path + instrument healthy)
    -E '.{0,20}X.{0,20}'          →   2 GB, killed (memcg OOM)
    BRE '.\\{0,20\\}X.\\{0,20\\}'  →   2 GB, killed (the escaped BRE kills too)
    GNU grep -E, same pattern     →   3.5 MB (witness)
    -P (PCRE2), same pattern      →   7.5 MB (complete workaround)

Two production panes were OOM-killed by exactly this (15 GB and 27.8 GB RSS):
both were shadowed `grep` calls from the Bash tool. In the OOM dumps the child
carries the comm of the agent binary, captured live by a /proc sampler as
`comm=<agent-version> cmd=ugrep -G --ignore-files …`.

PROVEN INNOCENT (trial 1, must NOT be blocked):
  - child scripts (`bash foo.sh`, `bash -c`): shell functions are not exported
    (no BASH_FUNC entry) → GNU grep. Writing a dangerous pattern INSIDE a .sh
    is fine.
  - execvp wrappers from PATH: command/env/sudo/xargs/timeout/nice/find -exec
    → /usr/bin/grep, the GNU one. The shadow function only fires in command
    position of the CURRENT shell (plus eval, plus keywords such as `time`).
  - the dedicated Grep tool (sub-agents) = ripgrep, with no ugrep fallback in
    the binary, 11 MB on the killer pattern → the Grep matcher is NOT gated
    (an earlier version did: a door that does not exist in a main session, and
    false positives everywhere else).

Rule: inside a shell segment whose command is a bare grep (or an eval carrying
one), 2 or more bounded quantifiers {m,n} or \\{m,n\\} of magnitude
>= MAGNITUDE_THRESHOLD, without -P nor -F → BLOCK exit 2, with the measured
safe rewrites. When the line is unparsable (shlex), a coarse whole-line rule
applies, deliberately conservative: a false positive costs one rewrite, a
false negative costs a pane.

Disarm (one session): HARNESS_GREP_QUANTIFIER_GATE_DISABLE=1
"""
import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from _hook import gate_stat
except Exception:
    def gate_stat(*a, **k):
        pass

# {x,10}x2 measured at 9 MB, but each +1 DOUBLES it: block from 2 occurrences.
MAGNITUDE_THRESHOLD = 10

# {3} · {3,} · {3,10} · {,10} and their BRE forms \{...\} — at least one digit
QUANT_RE = re.compile(r"\\?\{(\d+)?,?(\d+)?\\?\}")
# grep in command position: the shadow function only applies to these names
GREP_CMDS = {"grep", "ugrep", "ug"}
# transparent bash keywords: `time grep …` stays in the current shell → function
KEYWORDS = {"time", "!", "if", "then", "elif", "else", "while", "until",
            "do", "done", "fi", "{", "}"}
SEPARATORS = {"|", "||", "&&", ";", ";;", "&", "(", ")", "\n"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# -P/--perl-regexp (PCRE2, measured 7.5 MB) and -F/--fixed-strings (no regex)
SAFE_FLAG_RE = re.compile(r"^-[A-Za-z]*[PF]|^--perl-regexp|^--fixed-strings")


def big_quants(text: str):
    out = []
    for m in QUANT_RE.finditer(text):
        nums = [int(g) for g in m.groups() if g is not None]
        if nums and max(nums) >= MAGNITUDE_THRESHOLD:
            out.append(max(nums))
    return out


def segments(cmd: str):
    """Shell segments, quotes respected. None when unparsable → fallback."""
    try:
        lex = shlex.shlex(cmd, posix=False, punctuation_chars=True)
        toks = list(lex)
    except ValueError:
        return None
    segs, cur = [], []
    for t in toks:
        if t in SEPARATORS:
            if cur:
                segs.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def dangerous_segment(seg) -> bool:
    """True when this segment invokes the shadowed grep function with a killer
    pattern."""
    # command position: skip VAR= assignments and transparent keywords
    i = 0
    while i < len(seg) and (seg[i] in KEYWORDS or ASSIGN_RE.match(seg[i])
                            or seg[i].strip("\\`") == ""):
        i += 1  # non-posix shlex isolates `\` as its own token (\grep → \, grep)
    if i >= len(seg):
        return False
    # bash resolves functions AFTER quote removal: 'grep' quoted is shadowed too
    head = seg[i].strip("'\"").lstrip("\\`")
    rest = seg[i + 1:]
    if head == "eval":
        # eval goes back through the current shell: a bare grep in there is the
        # function
        if not any(t.strip("'\"").lstrip("\\") in GREP_CMDS
                   or re.search(r"(^|[\s|;&(`'\"])u?grep\s", t) for t in rest):
            return False
    elif head not in GREP_CMDS:
        return False  # execvp wrappers (sudo/xargs/…) and paths → GNU from PATH
    if any(SAFE_FLAG_RE.match(t) for t in rest):
        return False
    return len(big_quants(" ".join(rest))) >= 2


def dangerous_command(cmd: str) -> bool:
    segs = segments(cmd)
    if segs is None:
        # unparsable: coarse whole-line rule, deliberately conservative
        return (re.search(r"(^|[\s|;&(`])\\?(u?grep|ug)\s", cmd) is not None
                and len(big_quants(cmd)) >= 2
                and not re.search(r"(^|\s)-[A-Za-z]*[PF]", cmd))
    if any(dangerous_segment(s) for s in segs):
        return True
    # safety net: `backtick substitution` — non-posix shlex does not isolate it
    # as a segment
    for m in re.finditer(r"`([^`]+)`", cmd):
        sub = segments(m.group(1))
        if sub and any(dangerous_segment(s) for s in sub):
            return True
    return False


def main() -> int:
    if os.environ.get("HARNESS_GREP_QUANTIFIER_GATE_DISABLE") == "1":
        gate_stat("grep-quantifier", "skip-disabled")
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        gate_stat("grep-quantifier", "fail-open")
        return 0  # unreadable input = never block blindly
    if data.get("tool_name") != "Bash":
        gate_stat("grep-quantifier", "skip-not-bash")
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not dangerous_command(cmd):
        gate_stat("grep-quantifier", "pass")
        return 0
    # an uninstrumented gate is a stillborn invisible gate (post-mortem, v1)
    gate_stat("grep-quantifier", "block", cmd=cmd[:120])
    sys.stderr.write(
        "BLOCKED (ugrep quantifier gate, measured): this command contains a "
        "`grep` with 2 or more bounded quantifiers of magnitude >= 10. In the "
        "Bash tool, `grep` is shadowed to the embedded ugrep (DFA): this "
        "pattern explodes at compilation time (~2^min(N,M) states; {0,20} "
        "twice = 2 GB, OOM, dead pane). MEASURED safe rewrites: (1) add -P "
        "(PCRE2, 7.5 MB, same match); (2) `command grep` = the system GNU one "
        "(3.5 MB); (3) put the command in a .sh and run `bash foo.sh` (child "
        "scripts get the healthy GNU grep); (4) use ONE bounded quantifier "
        "only, or non-overlapping character classes. "
        "Session kill-switch: HARNESS_GREP_QUANTIFIER_GATE_DISABLE=1\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
