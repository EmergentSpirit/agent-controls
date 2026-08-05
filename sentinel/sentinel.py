#!/usr/bin/env python3
"""sentinel.py -- daily self-discovering health check for the harness.

WHY: a hook stayed dead for a whole working day and nobody saw it. The file
was on disk, the settings file still named it, the pane looked normal. What
was missing was the only thing that proves a gate runs: its trace in the
journal. A dead gate does not scream, it goes quiet, and quiet reads exactly
like "nothing bad happened".

Two laws, and everything below follows from them.

1. POSITIVE proof, never the absence of an error. "Alive" means WIRED AND
   LOGGING, never "present on disk". The load-bearing check of this whole
   module is the coverage diff: a gate wired in a settings file with zero
   trace in the gate-stats journal over N days is reported, every day, until
   someone explains it.
2. The sentinel does not share the failure of what it watches. It runs from
   its own timer, OUTSIDE the agent's hooks, it READS and REPORTS, and it
   mutates nothing: no push, no repair, no rewrite. Its only write is its own
   report file.

Self-discovering by construction: the list of what is checked is DERIVED at
each run from the settings files it is pointed at, never from a list baked
into this file. A hook wired tomorrow shows up in tomorrow's report with no
edit here.

Check families, in the order they run:

  settings   the settings files parse, and how many hooks each one wires
  script     every wired script exists, and its syntax compiles
  orphan     a hook script on disk that no settings file wires (dead weight,
             or a hook someone believes is armed and is not)
  journal    the gate-stats journal itself: present, parsable tail, fresh
  coverage   a WIRED gate with NO journal trace over the window (the check
             that catches the dead gate that lies)
  probe      OPTIONAL site-specific probes, off unless a probe file is given

One verdict per run: FAIL if any check failed, else WARN if any warned, else
OK. SKIP never moves the verdict -- an undecidable check must not read as a
pass. Exit code is 0 by default (a monitor that kills its own timer is
noise); --strict returns 1 on a FAIL verdict for a caller that wants it.

Environment:
- HARNESS_STATE_DIR              state directory (default: ~/.harness)
- HARNESS_GATE_STATS             gate-stats journal path
- HARNESS_HOOK_DIRS              colon-separated live hook directories
- HARNESS_SENTINEL_SETTINGS      colon-separated settings files to audit
- HARNESS_SENTINEL_REPORT_DIR    where the daily report is written
- HARNESS_SENTINEL_COVERAGE_DAYS coverage window in days (default: 7)
- HARNESS_SENTINEL_FRESHNESS_HOURS journal freshness window (default: 24)
- HARNESS_SENTINEL_EXEMPT        colon-separated script basenames exempt from
                                 the orphan and coverage families
- HARNESS_SENTINEL_ACTIVITY_PATHS colon-separated paths whose mtime proves an
                                 agent session ran (turns a silent journal
                                 from WARN into FAIL)
- HARNESS_SENTINEL_PROBES        file of site-specific probe commands
- HARNESS_SENTINEL_PROBE_ALLOW   colon-separated allowed probe commands
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import py_compile
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
try:
    from _hook import GATE_STATS, STATE_DIR, gate_stat
except Exception:  # MANDATORY fallback: mirrors the helper's own defaults.
    _HOME = os.path.expanduser("~")           # A partial install must still
    STATE_DIR = (os.environ.get("HARNESS_STATE_DIR")    # produce a report, not
                 or os.path.join(_HOME, ".harness"))    # a traceback.
    GATE_STATS = (os.environ.get("HARNESS_GATE_STATS")
                  or os.path.join(STATE_DIR, "gate-stats.jsonl"))

    def gate_stat(*a, **k):
        pass

# The probe family EXECUTES lines from a config file: its guard is not
# optional. Fail-open protects the operator's WORK from a broken gate; there
# is no work to protect here, and the only thing that would keep running is
# unchecked execution. So a missing guard closes the probe family (every line
# becomes a SKIP carrying that reason, never a silence) and the four other
# families still produce their report.
EXEC_GUARD_ERROR = ""
try:
    from _exec_guard import SHELL_META_RE, binary_refusal
except Exception as exc:
    EXEC_GUARD_ERROR = "%s: %s" % (type(exc).__name__, exc)
    SHELL_META_RE = None
    binary_refusal = None

DEFAULT_SETTINGS_GLOB = os.path.join(
    os.path.expanduser("~"), ".claude", "*settings*.json")
DEFAULT_COVERAGE_DAYS = 7
DEFAULT_FRESHNESS_HOURS = 24
DEFAULT_PROBE_ALLOW = ("test", "curl", "systemctl")
PROBE_TIMEOUT_S = 10
SYNTAX_TIMEOUT_S = 15

# A stamp tool is run BY A HUMAN, by design: it lives next to the hooks and is
# wired nowhere. Reporting it as an orphan every single day would train the
# reader to ignore the orphan family, which is how a real orphan gets missed.
DEFAULT_EXEMPT_SUFFIXES = ("-stamp.py",)
# Interpreters and command prefixes: none of them is ever "the script".
INTERPRETERS = {"bash", "sh", "dash", "zsh", "ksh", "node", "deno", "ruby",
                "perl", "env", "sudo", "nice", "timeout", "command", "exec"}
PYTHON_RE = re.compile(r"^python[\d.]*$")
UNEXPANDED_RE = re.compile(r"\$\w|\$\{")
STATUSES = ("OK", "WARN", "FAIL", "SKIP")


# --- report -----------------------------------------------------------------

class Report:
    """The run's lines, its counts, and the single verdict they produce."""

    def __init__(self):
        self.lines = []

    def note(self, status, family, name, detail=""):
        self.lines.append((status, family, str(name), detail))

    def counts(self):
        n = dict.fromkeys(STATUSES, 0)
        for status, _f, _n, _d in self.lines:
            n[status] = n.get(status, 0) + 1
        return n

    def verdict(self):
        """SKIP never moves the verdict: an undecidable check is not a pass."""
        n = self.counts()
        if n["FAIL"]:
            return "FAIL"
        return "WARN" if n["WARN"] else "OK"

    def text(self, duration=0.0):
        n = self.counts()
        out = ["# sentinel -- %s -- %.1f s"
               % (datetime.now().strftime("%Y-%m-%d %H:%M"), duration)]
        for status, family, name, detail in self.lines:
            row = "%-4s %-9s %s" % (status, family, name)
            out.append("%s - %s" % (row, detail) if detail else row)
        out.append("VERDICT %s - %d OK / %d WARN / %d FAIL / %d SKIP"
                   % (self.verdict(), n["OK"], n["WARN"], n["FAIL"], n["SKIP"]))
        return "\n".join(out) + "\n"


# --- small helpers ----------------------------------------------------------

def env_list(name):
    """Colon-separated environment list, ~ expanded, empty entries dropped."""
    raw = os.environ.get(name) or ""
    return [os.path.expanduser(p) for p in raw.split(":") if p.strip()]


def env_int(name, default):
    """A typo must never silently disarm a window: bad value, default wins."""
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def resolve(token):
    """~ and $VAR expanded. An unset variable stays literal, on purpose: the
    caller then reports 'unresolved' instead of silently skipping the line."""
    return os.path.expandvars(os.path.expanduser(token))


def labels_for(paths):
    """Unique short label per settings file. Two files can both be called
    settings.json; the parent directory disambiguates them."""
    out, seen = {}, {}
    for p in paths:
        seen.setdefault(os.path.basename(p), []).append(p)
    for p in paths:
        base = os.path.basename(p)
        out[p] = base if len(seen[base]) == 1 else os.path.join(
            os.path.basename(os.path.dirname(p)), base)
    return out


def journal_key(name):
    """Normalized name used to match a script against a journaled hook name.

    Convention: a gate journals a name derived from its own file stem
    (home-prefix-gate.py journals `home-prefix`). Normalizing both sides makes
    that match without hard-coding a single naming style."""
    n = os.path.basename(str(name or "")).strip().lower()
    for suffix in (".py", ".sh"):
        if n.endswith(suffix):
            n = n[:-len(suffix)]
    n = n.replace("_", "-")
    for suffix in ("-gate", "-hook"):
        if n.endswith(suffix):
            n = n[:-len(suffix)]
    return n


def keys_match(a, b):
    """Substring match both ways, on names long enough to mean something."""
    if len(a) < 3 or len(b) < 3:
        return a == b and bool(a)
    return a == b or a in b or b in a


def newest_mtime(paths, cap=20000):
    """Most recent mtime under `paths`. Walk capped: a health check must not
    turn into a filesystem crawl on a large tree."""
    newest, seen = 0.0, 0
    for p in paths:
        if not os.path.exists(p):
            continue
        if os.path.isfile(p):
            try:
                newest = max(newest, os.path.getmtime(p))
            except OSError:
                pass
            continue
        for root, _dirs, files in os.walk(p):
            for f in files:
                seen += 1
                if seen > cap:
                    return newest
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
                except OSError:
                    pass
    return newest


# --- family: settings (discovery) -------------------------------------------

def settings_files(cli_paths):
    """Settings files to audit: CLI, then env, then the default glob. Globs
    are expanded; an explicit path that matches nothing is KEPT so the run
    reports it missing instead of auditing an empty set in silence."""
    raw = list(cli_paths or []) or env_list("HARNESS_SENTINEL_SETTINGS")
    if not raw:
        return sorted(glob.glob(DEFAULT_SETTINGS_GLOB))
    out = []
    for entry in raw:
        entry = resolve(entry)
        magic = any(ch in entry for ch in "*?[")
        hits = sorted(glob.glob(entry)) if magic else []
        out.extend(hits or [entry])
    return out


def enumerate_hooks(paths, report):
    """Every hook wired by those settings files. This is the whole point of
    the module: the inventory is DERIVED here, never declared in this file."""
    hooks = []
    labels = labels_for(paths)
    for path in paths:
        label = labels[path]
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            report.note("FAIL", "settings", label, "file not found: %s" % path)
            continue
        except Exception as exc:
            report.note("FAIL", "settings", label, "unreadable JSON: %s" % exc)
            continue
        events = data.get("hooks") if isinstance(data, dict) else None
        if events is None:
            report.note("SKIP", "settings", label, "no hooks key: nothing wired here")
            continue
        if not isinstance(events, dict):
            report.note("FAIL", "settings", label, "the hooks key is not an object")
            continue
        found = 0
        for event, groups in events.items():
            for group in groups if isinstance(groups, list) else []:
                entries = group.get("hooks") if isinstance(group, dict) else None
                for entry in entries if isinstance(entries, list) else []:
                    command = (entry.get("command") or "") if isinstance(entry, dict) else ""
                    script, kind = script_of(command)
                    hooks.append({"source": path, "label": label, "event": event,
                                  "command": command, "script": script, "kind": kind})
                    found += 1
        if found:
            report.note("OK", "settings", label, "%d hooks wired" % found)
        else:
            report.note("SKIP", "settings", label, "0 hooks wired, nothing to check")
    return hooks


def script_of(command):
    """(path, kind) for a hook command. kind is one of:

    script      an absolute path to a real script was found
    inline      the command carries no script (e.g. a plain shell one-liner)
    unresolved  a script path was there but an environment variable in it is
                unset, so nothing can be verified -- reported, never dropped

    The first absolute token is NOT the answer: an interpreter (a venv python,
    bash) or a data file passed as an argument would win. The script is the
    first absolute token that is not an interpreter and either ends in .py/.sh
    or carries the exec bit."""
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        return (None, "inline")
    unresolved = False
    for token in tokens:
        candidate = resolve(token)
        if candidate.endswith((".py", ".sh")) and UNEXPANDED_RE.search(candidate):
            unresolved = True
            continue
        if not os.path.isabs(candidate):
            continue
        base = os.path.basename(candidate)
        if base in INTERPRETERS or PYTHON_RE.match(base):
            continue
        if candidate.endswith((".py", ".sh")) or os.access(candidate, os.X_OK):
            return (os.path.normpath(candidate), "script")
    return (None, "unresolved" if unresolved else "inline")


# --- family: script ---------------------------------------------------------

def python_syntax_error(path):
    """Compile-check a Python file. The .pyc goes to a temporary directory:
    the sentinel writes NOTHING next to what it inspects."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            py_compile.compile(path, cfile=os.path.join(tmp, "check.pyc"),
                               doraise=True)
    except py_compile.PyCompileError as exc:
        return str(exc).strip().splitlines()[-1][:160]
    except Exception as exc:
        return "not compilable: %s" % str(exc)[:120]
    return None


def shell_syntax_error(path_or_command, inline=False):
    """bash -n: parse without executing. No bash on the box, no check -- said
    out loud rather than counted as a pass."""
    args = ["bash", "-n", "-c", path_or_command] if inline else ["bash", "-n", path_or_command]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=SYNTAX_TIMEOUT_S)
    except FileNotFoundError:
        return "SKIP"
    except Exception as exc:
        return "not checkable: %s" % str(exc)[:120]
    if proc.returncode == 0:
        return None
    return (proc.stderr or b"").decode("utf-8", "replace").strip()[:160] or "syntax error"


def check_scripts(hooks, report):
    """Presence and syntax of everything the settings files point at."""
    seen = set()
    for hook in hooks:
        if hook["kind"] == "unresolved":
            report.note("SKIP", "script", hook["command"][:60],
                        "unresolved variable in the command (%s): set it before "
                        "running the sentinel" % hook["label"])
            continue
        if hook["kind"] == "inline":
            problem = shell_syntax_error(hook["command"], inline=True)
            if problem == "SKIP":
                report.note("SKIP", "script", hook["command"][:60],
                            "inline command, bash unavailable for the check")
            elif problem:
                report.note("FAIL", "script", hook["command"][:60],
                            "inline command, invalid shell syntax: %s" % problem)
            continue
        path = hook["script"]
        if path in seen:
            continue
        seen.add(path)
        name = os.path.basename(path)
        if not os.path.exists(path):
            report.note("FAIL", "script", name,
                        "MISSING on disk but wired in %s (%s): every prompt "
                        "hitting this event fails" % (hook["label"], hook["event"]))
            continue
        problem = None
        if path.endswith(".py"):
            problem = python_syntax_error(path)
        elif path.endswith(".sh"):
            problem = shell_syntax_error(path)
            if problem == "SKIP":
                report.note("SKIP", "script", name, "bash unavailable for the check")
                continue
        if problem:
            report.note("FAIL", "script", name, "syntax: %s" % problem)
        else:
            report.note("OK", "script", name, "present, syntax OK (%s)" % hook["label"])
    return seen


# --- family: orphan ---------------------------------------------------------

def hook_dirs(cli_dirs):
    """Directories whose top-level files are meant to be live hooks. Same
    convention as the hook-retire gate: HARNESS_HOOK_DIRS, else the harness
    hooks directory plus the agent's own ~/.claude/hooks."""
    raw = list(cli_dirs or []) or env_list("HARNESS_HOOK_DIRS")
    if not raw:
        raw = [os.path.join(ROOT, "hooks"), os.path.expanduser("~/.claude/hooks")]
    return sorted({os.path.normpath(os.path.expanduser(p)) for p in raw if p})


def is_exempt(name, exempt):
    return name in exempt or name.endswith(DEFAULT_EXEMPT_SUFFIXES)


def check_orphans(hooks, dirs, exempt, report):
    """A hook script sitting in a hooks directory and wired in NO settings
    file. Either dead weight, or -- the expensive case -- a gate somebody
    believes is armed. Both deserve a line."""
    wired = {os.path.realpath(h["script"]) for h in hooks if h["script"]}
    for directory in dirs:
        if not os.path.isdir(directory):
            report.note("SKIP", "orphan", directory, "not a directory")
            continue
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path) or not name.endswith((".py", ".sh")):
                continue
            if name.startswith("_") or name.startswith("test") or ".example." in name:
                continue
            if is_exempt(name, exempt):
                report.note("SKIP", "orphan", name, "exempt, run by hand by design")
                continue
            if os.path.realpath(path) not in wired:
                report.note("WARN", "orphan", name,
                            "on disk in %s but wired in NO settings file"
                            % directory)


# --- family: journal --------------------------------------------------------

def journal_hits(days):
    """{journaled hook name: count} over the window, and the number of lines
    read. Returns (None, 0) when the journal is missing or empty: the caller
    must then REFUSE to conclude instead of reading silence as coverage."""
    if not os.path.exists(GATE_STATS) or os.path.getsize(GATE_STATS) == 0:
        return (None, 0)
    floor = time.time() - days * 86400
    hits, read = {}, 0
    try:
        with open(GATE_STATS, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                read += 1
                try:
                    rec = json.loads(line)
                    stamp = datetime.fromisoformat(rec.get("ts")).timestamp()
                except Exception:
                    continue
                if stamp < floor:
                    continue
                name = rec.get("hook")
                if name:
                    hits[name] = hits.get(name, 0) + 1
    except Exception:
        return (None, 0)
    return (hits, read)


def check_journal(report, freshness_h, activity_paths):
    """The journal is the aliveness signal of the whole harness. If IT is
    dead, no coverage claim below it means anything."""
    name = os.path.basename(GATE_STATS)
    if not os.path.exists(GATE_STATS):
        report.note("FAIL", "journal", name,
                    "absent (%s): nothing proves any gate is alive" % GATE_STATS)
        return
    if os.path.getsize(GATE_STATS) == 0:
        report.note("FAIL", "journal", name, "empty: no gate has ever logged")
        return
    try:
        with open(GATE_STATS, encoding="utf-8", errors="replace") as f:
            tail = [line for line in f.read().splitlines() if line.strip()][-1]
        json.loads(tail)
    except Exception as exc:
        report.note("FAIL", "journal", name, "last line is not JSON: %s" % exc)
        return
    age_h = (time.time() - os.path.getmtime(GATE_STATS)) / 3600.0
    if age_h < freshness_h:
        report.note("OK", "journal", name,
                    "fresh (%.1f h), last line valid JSON" % age_h)
        return
    activity = newest_mtime(activity_paths) if activity_paths else 0.0
    if activity and (time.time() - activity) / 3600.0 < freshness_h:
        report.note("FAIL", "journal", name,
                    "a session ran %.0f h ago but the journal has been silent "
                    "for %.0f h: the wired gates are mute"
                    % ((time.time() - activity) / 3600.0, age_h))
    else:
        report.note("WARN", "journal", name,
                    "silent for %.0f h: either no session ran, or every gate "
                    "is mute (undecidable without HARNESS_SENTINEL_ACTIVITY_PATHS)"
                    % age_h)


# --- family: coverage -------------------------------------------------------

def check_coverage(hooks, days, exempt, report):
    """THE check. A gate can be present, syntactically perfect, named in the
    settings file, and still never run: a matcher that matches nothing, an
    event that never fires, a pane booted before the wiring. Only a trace in
    the journal proves otherwise."""
    hits, _read = journal_hits(days)
    if hits is None:
        report.note("SKIP", "coverage", "all",
                    "journal absent or empty: coverage is UNDECIDABLE, no gate "
                    "is declared alive on this run")
        return
    seen = {journal_key(k): v for k, v in hits.items()}
    done = set()
    for hook in hooks:
        path = hook["script"]
        if not path or not os.path.exists(path):
            continue
        name = os.path.basename(path)
        if name in done or is_exempt(name, exempt):
            continue
        done.add(name)
        key = journal_key(name)
        total = sum(count for other, count in seen.items() if keys_match(key, other))
        if total:
            report.note("OK", "coverage", name,
                        "%d trace(s) in the journal over %d days" % (total, days))
        else:
            report.note("WARN", "coverage", name,
                        "wired (%s, %s) but NO trace in the journal over %d "
                        "days: not instrumented, never triggered, or DEAD"
                        % (hook["event"], hook["label"], days))


# --- family: probe (optional, site-specific) --------------------------------

# The rule itself lives in hooks/_exec_guard.py, and it is the SAME OBJECT
# recall imports for its `check:` field -- not a copy of it, not "the same by
# construction". It was two copies once: the curl blocklist became an
# allowlist here while the recall copy kept every hole, for the length of one
# commit. What is shared is the allowlist of options per binary and the
# binary-resolution rule; the outcome vocabulary stays local to each caller
# (SKIP here, refused there).
def probe_refusal(argv, allow):
    """None when this argv may run, otherwise the reason it may not."""
    return binary_refusal(argv, allow)


def probe_argv(command, allow):
    """(argv, refusal) for ONE probe line. argv is None whenever refusal is not.

    The parsing lives HERE, per line, inside its own try. It used to sit bare
    in the loop, so a single unclosed quote anywhere in the probe file raised
    all the way to the module's fail-open handler: no dated report written at
    all, and not one other family run. A malformed line must cost that line."""
    if EXEC_GUARD_ERROR:
        return (None, "exec guard unavailable, nothing runs: %s" % EXEC_GUARD_ERROR)
    if SHELL_META_RE.search(command):
        return (None, "shell metacharacter: a probe line is an argv, not a shell line")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return (None, "unparsable line (%s)" % exc)
    if not argv:
        return (None, "nothing left after parsing")
    return (argv, probe_refusal(argv, allow))


def run_probes(path, allow, report):
    """OPTIONAL. One command per line, # comments ignored. Only an allowlisted
    binary, with no shell and no metacharacter, actually runs: the sentinel is
    a timer job, and a config file must never become an arbitrary execution
    surface. Everything else becomes a SKIP line, never a silence.

    This is the same constraint as recall's `check:` field, and since
    2026-08-05 it is literally the same code: both import
    hooks/_exec_guard.py. Saying "the same by construction" while keeping two
    copies is how the two drifted -- the copies were fixed one at a time, and
    for the length of one commit the recall side still accepted `-o/tmp/loot`.

    The first hole was measured earlier and in the other direction: the first
    word was matched against the allowlist, then the WHOLE LINE was handed to
    `bash -c`, so `test -d /tmp; echo PWNED > /somewhere` passed the check on
    `test` and ran the half after the semicolon. Allowlisting the head of a
    string you then give to a shell protects exactly nothing.

    Defense in depth, in order:
      1. no shell metacharacter allowed (and no shell at all: shell=False);
      2. the ALLOWLIST applies to argv[0], and when it is given as a path it
         must resolve to the SAME binary as `which <basename>` (a planted
         /tmp/test does not pass);
      3. PER-BINARY rules, ALLOWLISTS as well: each binary declares the options
         it may receive and everything else is refused, including a value
         glued to its flag, joined with "=", or hidden in a short cluster.
         curl is held to read-only options plus one http(s) URL, systemctl to
         read-only verbs and value-less flags, test to one operator and one
         absolute path. This used to be a blocklist of forbidden options, and
         `-o/tmp/loot`, `-fsSo /tmp/loot` and `--data-ascii @/etc/hostname`
         (the content of a local file POSTed to the remote host) all walked
         past it and were reported OK. See _exec_guard.py for the lists.

    This is where site-specific checks live -- a service unit, an HTTP probe,
    a mount point. None of that ships in the harness, because none of it is
    true anywhere but on the machine that wrote it."""
    if not path:
        return
    if not os.path.exists(path):
        report.note("WARN", "probe", os.path.basename(path),
                    "probe file configured but missing: %s" % path)
        return
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except Exception as exc:
        report.note("WARN", "probe", os.path.basename(path), "unreadable: %s" % exc)
        return
    for line in lines:
        command = line.strip()
        if not command or command.startswith("#"):
            continue
        argv, refusal = probe_argv(command, allow)
        if refusal:
            report.note("SKIP", "probe", command[:50], refusal)
            continue
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True,
                                  timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            report.note("FAIL", "probe", command[:50],
                        "timeout after %d s" % PROBE_TIMEOUT_S)
            continue
        except Exception as exc:
            report.note("WARN", "probe", command[:50], "not runnable: %s" % exc)
            continue
        report.note("OK" if proc.returncode == 0 else "FAIL", "probe",
                    command[:50], "rc=%d" % proc.returncode)


# --- run --------------------------------------------------------------------

def write_report(report_dir, text):
    """The only write of the whole module. The freshness of THIS file is what
    a dead man's switch watches: a sentinel that stops running is itself a
    silent failure."""
    try:
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, datetime.now().strftime("%Y-%m-%d") + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception:
        return None


def build_parser():
    p = argparse.ArgumentParser(
        description="Daily self-discovering health check for the harness.")
    p.add_argument("--settings", nargs="*", default=None,
                   help="settings files to audit (globs allowed). Default: "
                        "HARNESS_SENTINEL_SETTINGS, else ~/.claude/*settings*.json")
    p.add_argument("--hook-dirs", nargs="*", default=None,
                   help="directories holding live hooks, for the orphan family")
    p.add_argument("--report-dir", default=None, help="where the daily report is written")
    p.add_argument("--coverage-days", type=int, default=None,
                   help="coverage window, in days")
    p.add_argument("--freshness-hours", type=int, default=None,
                   help="journal freshness window, in hours")
    p.add_argument("--probes", default=None, help="optional probe file")
    p.add_argument("--enumerate", action="store_true",
                   help="print the derived inventory and run no check")
    p.add_argument("--strict", action="store_true",
                   help="exit 1 when the verdict is FAIL (default: always 0)")
    p.add_argument("--quiet", action="store_true", help="write the report, print nothing")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    started = time.time()
    report = Report()

    paths = settings_files(args.settings)
    hooks = enumerate_hooks(paths, report)

    if args.enumerate:
        for hook in hooks:
            print("hook  %-18s %-10s %s" % (hook["event"], hook["kind"],
                                            hook["script"] or hook["command"][:60]))
        print("total: %d hooks wired across %d settings file(s)"
              % (len(hooks), len(paths)))
        return 0

    days = args.coverage_days or env_int("HARNESS_SENTINEL_COVERAGE_DAYS",
                                         DEFAULT_COVERAGE_DAYS)
    fresh_h = args.freshness_hours or env_int("HARNESS_SENTINEL_FRESHNESS_HOURS",
                                              DEFAULT_FRESHNESS_HOURS)
    exempt = set(os.path.basename(p) for p in env_list("HARNESS_SENTINEL_EXEMPT"))
    allow = set(env_list("HARNESS_SENTINEL_PROBE_ALLOW")) or set(DEFAULT_PROBE_ALLOW)
    probes = args.probes or os.environ.get("HARNESS_SENTINEL_PROBES")
    report_dir = (args.report_dir or os.environ.get("HARNESS_SENTINEL_REPORT_DIR")
                  or os.path.join(STATE_DIR, "sentinel"))

    check_scripts(hooks, report)
    check_orphans(hooks, hook_dirs(args.hook_dirs), exempt, report)
    check_journal(report, fresh_h, env_list("HARNESS_SENTINEL_ACTIVITY_PATHS"))
    check_coverage(hooks, days, exempt, report)
    run_probes(probes, allow, report)

    verdict = report.verdict()
    text = report.text(time.time() - started)
    write_report(os.path.expanduser(report_dir), text)
    if not args.quiet:
        sys.stdout.write(text)
    counts = report.counts()
    # The sentinel journals like everything else: `observe` because it blocks
    # nothing, with the verdict carried as a field. A sentinel that stops
    # logging must be as visible as the dead gates it hunts.
    gate_stat("sentinel", "observe", verdict=verdict, ok=counts["OK"],
              warn=counts["WARN"], fail=counts["FAIL"], skip=counts["SKIP"],
              hooks=len(hooks), settings=len(paths))
    return 1 if (args.strict and verdict == "FAIL") else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # fail-open: a broken sentinel is not an outage
        gate_stat("sentinel", "fail-open", error=str(exc)[:200])
        sys.stderr.write("sentinel: unexpected error: %s\n" % exc)
        sys.exit(0)
