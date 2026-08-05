#!/usr/bin/env python3
"""Constrained execution of a command line nobody in this repository wrote.

Two organs run commands that come from a CONFIG FILE, unattended, on a timer:

  - `sentinel/sentinel.py`  the optional probe file, one command per line;
  - `recall/recall.py`      the `check:` field of a catalog entry -- and that
                            catalog is rewritten by a MODEL at every curation
                            pass, which makes it the wider surface of the two.

Same danger, same rule, so ONE implementation, here. It used to be two copies
of the same forty lines, and they diverged exactly the way copies do: the curl
blocklist was replaced by an allowlist on the sentinel side while the recall
side kept the vulnerable code verbatim, holes included. A defect fixed in one
of two copies is a defect that comes back.

Defense in depth. The caller owns step 1 (it names its own outcome for a
refusal), this module owns steps 2 and 3:

  1. no shell metacharacter, and no shell at all (`shell=False`).
     SHELL_META_RE is exported so the two callers cannot drift on the class of
     characters they refuse. Allowlisting the first word of a string then
     handed to `bash -c` protects nothing: `test -d /tmp; echo PWNED > /x`
     passes a check on `test` and runs the half after the semicolon;
  2. the ALLOWLIST is a BINARY, not a word. `argv[0]` must be in it, and when
     it is written as a path it must resolve to the SAME file as
     `which <basename>`, so a planted `/tmp/systemctl` does not pass;
  3. PER-BINARY rules, allowlists as well: each binary declares the options it
     may receive, and everything else is refused.

Widening any list here is a code edit on purpose: it gets reviewed, and it
cannot happen by editing a data file a model can rewrite.

stdlib only, no state, and no I/O beyond `which` and `realpath`: every
function is pure enough to call from a test with no fixture at all.
"""
import os
import re
import shutil

# A probe line, or a `check:` line, is an ARGV. Not a shell line.
SHELL_META_RE = re.compile(r"[;&|<>`$(){}\[\]!*?~\\]")

# The per-binary rules are ALLOWLISTS, never blocklists.
#
# They used to be a blocklist, and the cost was measured on curl 8.5.0: every
# line below walked past a list that named -O, -T, -d, -F and --config, and
# every one of them was reported OK instead of refused.
#
#   -o/tmp/loot.txt                 the value glued to the flag, so the token
#                                   never equals "-o"
#   -fsSo /tmp/loot.txt             the same letter buried in a short cluster
#   --data-ascii @/etc/hostname     the bytes of a local file POSTed to the
#                                   remote host: exfiltration, no disk write
#   --json @/etc/hostname           the same thing, another spelling
#   -D, --dump-header, --stderr,    a dozen more options that write a file,
#   --trace-ascii, -c, --etag-save  none of which the blocklist author listed
#   -w '%output{/tmp/loot}'         curl >= 8.3 writes from the FORMAT STRING
#   file:///etc/hostname            the URL scheme was never checked either
#
# A blocklist over a CLI with hundreds of options is a promise nobody can
# keep: the next curl release adds one, and the gate goes on claiming a
# guarantee it no longer provides. What follows names what a HEALTH CHECK
# needs -- is it up, how fast, what status -- and refuses everything else,
# including the forms a naive parser never sees: a value glued to its flag, a
# value joined with "=", and a value-taking letter hidden in a short cluster.
#
# Widening any of these lists is a code edit on purpose: it gets reviewed.

SYSTEMCTL_RO_VERBS = {"is-active", "is-enabled", "is-failed", "status", "show",
                      "list-units", "list-timers"}
# Value-less options only, exact tokens. An option taking a SEPARATE value can
# hide the verb: `systemctl --signal status kill x` reads `status` as the first
# non-option token and runs `kill`.
SYSTEMCTL_ALLOWED_FLAGS = {"--user", "--system", "--quiet", "-q", "--no-pager",
                           "--all", "--full", "--plain"}
# `test` has no write and no upload primitive: this narrowing keeps a health
# line predictable, it does not close a hole of its own.
TEST_ALLOWED_OPS = {"-e", "-f", "-d", "-r", "-w", "-x", "-s", "-h", "-L",
                    "-p", "-S", "-b", "-c"}
# curl, in three parts: value-less flags, the same flags as combinable short
# letters, and the options that take exactly one SEPARATE value.
CURL_ALLOWED_FLAGS = {"-s", "--silent", "-S", "--show-error", "-f", "--fail",
                      "-I", "--head"}
CURL_ALLOWED_CLUSTER = "sSfI"
CURL_ALLOWED_VALUE_OPTS = {"-o", "--output", "-w", "--write-out",
                           "-m", "--max-time", "--connect-timeout"}
CURL_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
CURL_SECONDS_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _systemctl_refusal(args):
    """systemctl: a read-only verb, and no option that could hide another."""
    verb = None
    for a in args:
        if a.startswith("-"):
            if a not in SYSTEMCTL_ALLOWED_FLAGS:
                return "systemctl: option outside the allowlist: %s" % a[:60]
            continue
        if verb is None:
            verb = a
    if verb not in SYSTEMCTL_RO_VERBS:
        return "systemctl: verb is not read-only: %s" % ([verb] if verb else "?")
    return None


def _test_refusal(args):
    """test: one allowlisted unary operator, one absolute path, nothing else."""
    if len(args) != 2:
        return ("test: exactly one operator and one absolute path are allowed: %s"
                % (" ".join(args)[:60] or "(nothing)"))
    operator, operand = args
    if operator not in TEST_ALLOWED_OPS:
        return "test: operator outside the allowlist: %s" % operator[:60]
    if not os.path.isabs(operand):
        return "test: the path must be absolute: %s" % operand[:60]
    return None


def _curl_value_refusal(option, value):
    """The VALUE of an allowlisted curl option. -o and -w are allowlisted and
    still write to disk when their value is left unchecked."""
    if option in ("-o", "--output"):
        if value != "/dev/null":
            return "curl: -o pointing somewhere other than /dev/null"
        return None
    if option in ("-w", "--write-out"):
        if value.startswith("@"):
            return "curl: -w reading its format from a file: %s" % value[:60]
        if "%output{" in value.lower():
            return "curl: -w writing to a file through its format string"
        return None
    if not CURL_SECONDS_RE.match(value) or float(value) <= 0:
        return ("curl: %s takes a positive number of seconds: %s"
                % (option, value[:60]))
    return None


def _curl_refusal(args):
    """curl: an ALLOWLIST of options, plus exactly one http(s) URL."""
    urls, i = [], 0
    while i < len(args):
        a = args[i]
        if a in CURL_ALLOWED_FLAGS:
            i += 1
            continue
        if a in CURL_ALLOWED_VALUE_OPTS:
            if i + 1 >= len(args):
                return "curl: %s with no value" % a
            problem = _curl_value_refusal(a, args[i + 1])
            if problem:
                return problem
            i += 2
            continue
        if a.startswith("--"):
            head = a.split("=", 1)[0]
            if "=" in a and (head in CURL_ALLOWED_VALUE_OPTS
                             or head in CURL_ALLOWED_FLAGS):
                return ("curl: the value of %s must be a separate argument: %s"
                        % (head, a[:60]))
            return "curl: option outside the allowlist: %s" % a[:60]
        if a.startswith("-") and len(a) > 1:
            for letter in a[1:]:
                if letter in CURL_ALLOWED_CLUSTER:
                    continue
                if "-" + letter in CURL_ALLOWED_VALUE_OPTS:
                    return ("curl: -%s takes a value and may be neither glued "
                            "nor combined: %s" % (letter, a[:60]))
                detail = a[:60] if len(a) == 2 else "-%s in %s" % (letter, a[:60])
                return "curl: option outside the allowlist: %s" % detail
            i += 1
            continue
        urls.append(a)
        i += 1
    if len(urls) != 1:
        return "curl: exactly one URL is allowed, %d found" % len(urls)
    if not CURL_URL_RE.match(urls[0]):
        return "curl: URL scheme is not http or https: %s" % urls[0][:60]
    return None


PER_BINARY_RULES = {"systemctl": _systemctl_refusal, "test": _test_refusal,
                    "curl": _curl_refusal}


def binary_refusal(argv, allow):
    """None when this argv may run, otherwise the reason it may not.

    `allow` is the caller's set of basenames: the sentinel reads it from
    HARNESS_SENTINEL_PROBE_ALLOW, recall keeps its own constant. The rules
    below are the same for both, because the danger is.

    A binary in `allow` with no entry in PER_BINARY_RULES runs with its
    arguments UNCHECKED: adding one is a deliberate, reviewable act, and it is
    worth writing the rule that goes with it."""
    base = os.path.basename(argv[0])
    if base not in allow:
        return "'%s' is not in the allowlist %s" % (base, sorted(allow))
    canonical = shutil.which(base)
    if canonical is None:
        return "binary not on PATH: %s" % base
    if "/" in argv[0]:
        try:
            if os.path.realpath(argv[0]) != os.path.realpath(canonical):
                return "non-canonical path: %s" % argv[0]
        except OSError:
            return "unresolvable path: %s" % argv[0]
    rule = PER_BINARY_RULES.get(base)
    return rule(argv[1:]) if rule else None
