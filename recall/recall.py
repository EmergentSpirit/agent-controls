#!/usr/bin/env python3
"""recall.py -- the "already built" engine: match, verify live, report.

THE PROBLEM. An agent rebuilds what already exists, because nothing in its
context says the thing exists. The artifact is on disk, the service is
installed, the dataset was collected six weeks ago -- and none of that is in
the window. So the work happens twice, and the second copy diverges from the
first.

THE ANSWER, in one sentence: a small CURATED catalog of reactivable artifacts,
matched against the operator's own words, verified against the real filesystem
before anything is claimed.

Two entry points, one engine: the prompt hook (primary, `recall-inject.py`)
and a manual command (fallback). The catalog is the RELEVANCE FILTER: the text
is matched against name + aliases of a small curated set, never against a raw
filesystem index. The index (layer 1) verifies the EXISTENCE of what already
matched; it never generates candidates.

Output = one bounded "already known" block, or nothing. Silence is a correct
answer: `match` and `boot-surface` print NOTHING when there is nothing.

Usage:
  recall.py match "<text>"       -> injection block (empty when nothing matches)
  recall.py show <name>          -> one entry (curated catalog only)
  recall.py list                 -> every entry (curated catalog only)
  recall.py check-all [--report] -> health pass; --report writes the freshness
                                    report read by `boot-surface`
  recall.py boot-surface         -> bounded passive surface for session start

Environment:
- HARNESS_STATE_DIR          state directory (default ~/.harness), imported
                             from the hook helper, never recomputed here
- HARNESS_RECALL_CATALOG     curated catalog path (default: the shipped
                             CATALOG.example.md next to this file -- copy it,
                             then point the variable at your own)
- HARNESS_RECALL_INDEX_DB    file-index database (default:
                             $HARNESS_STATE_DIR/recall/fs-index.db)
- HARNESS_RECALL_INDEX_BIN   index query binary (default: plocate). OPTIONAL:
                             with no index at all, existence falls back to the
                             disk and the answers are identical.
- HARNESS_RECALL_REPORT      freshness report path
- HARNESS_RECALL_CURATE_LOG  curation log path
- HARNESS_RECALL_STALE_DAYS  age after which a status is "not reviewed" (45)
- HARNESS_RECALL_MAX_HITS    hard ceiling on injected entries (4)
- HARNESS_RECALL_SHEET_MAX   max characters of an injected working sheet (1800)
- HARNESS_RECALL_BOOT_MAX    max characters of the passive surface (4800)
- HARNESS_RECALL_TODAY       today as YYYY-MM-DD, INJECTED by the caller. No
                             clock is read here: an unverifiable date must stay
                             visibly unverifiable instead of silently passing.
"""
import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "hooks"))
from _hook import STATE_DIR  # noqa: E402

HOME = os.path.expanduser("~")
RECALL_STATE = os.path.join(STATE_DIR, "recall")

CATALOG = os.path.expanduser(
    os.environ.get("HARNESS_RECALL_CATALOG")
    or os.path.join(HERE, "CATALOG.example.md"))
INDEX_DB = os.path.expanduser(
    os.environ.get("HARNESS_RECALL_INDEX_DB")
    or os.path.join(RECALL_STATE, "fs-index.db"))
INDEX_BIN = os.environ.get("HARNESS_RECALL_INDEX_BIN") or "plocate"
REPORT = os.path.expanduser(
    os.environ.get("HARNESS_RECALL_REPORT")
    or os.path.join(RECALL_STATE, "freshness-report.md"))
CURATE_LOG = os.path.expanduser(
    os.environ.get("HARNESS_RECALL_CURATE_LOG")
    or os.path.join(RECALL_STATE, "curate-log.md"))

# Injected by the caller, never read from the clock here. Empty = the freshness
# of every status is UNVERIFIABLE, and that is reported as such.
TODAY = os.environ.get("HARNESS_RECALL_TODAY", "")


def int_env(name, default):
    """Positive integer from the environment; anything else is ignored, so a
    typo cannot silently unbound a ceiling."""
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


STALE_DAYS = int_env("HARNESS_RECALL_STALE_DAYS", 45)
MAX_HITS = int_env("HARNESS_RECALL_MAX_HITS", 4)
SHEET_MAX = int_env("HARNESS_RECALL_SHEET_MAX", 1800)
BOOT_MAX_CHARS = int_env("HARNESS_RECALL_BOOT_MAX", 4800)

FIELDS = ("aliases", "path", "type", "status", "resume", "reactivate", "check",
          "project", "memory", "updated", "origin", "supersedes",
          "superseded_by", "importance", "sheet")

# `sheet`: path of a working sheet whose CONTENT is injected on match, loaded
# just in time and bounded so a match cannot re-inflate the window. It is read
# ONLY under these segments: before that restriction it could point at any
# readable file on the machine and push it into the context.
SHEET_ALLOWED_SEGMENTS = ("/memory/projects/", "/memory/clients/")

# `origin`: human|auto|external -- ABSENT means human (back-compat: the old
# entries were curated by hand). This is the guardrail of the automatic half:
# an auto/external entry is RECALLED but never triggers an action on its own.
# Since it became a real branch (see _origin_human) rather than a label,
# run_check is not even called for those entries.
# `superseded_by` / `supersedes`: explicit supersession -- we MARK, we never
# delete (version control keeps the history, the catalog keeps the pointer).
# RESERVED (parsed, never read by the code): supersedes, type, project,
# importance.
DEPRIORITIZED_STATUS = {"superseded", "sunset", "dead"}

# Literals shared between the report writer and the boot surface that reparses
# it. ONE definition, so editing one side cannot silently break the other.
REPORT_NONE = "- (none)"
SECT_MISSING = "MISSING from the real filesystem"
SECT_CHECK_FAIL = "check: FAILS"
SECT_CHECK_REFUSED = "check: REFUSED"
SECT_STALE = "NOT REVIEWED"
SECT_UNVERIFIABLE = "UNVERIFIABLE dates"


def _norm(v):
    """Normalize a field value before comparing it (status, origin): the
    catalog is rewritten by a model at curation time, its casing is not a
    contract."""
    return (v or "").strip().lower()


# --- catalog parsing --------------------------------------------------------

def parse_catalog(path=None):
    """Parse a catalog file: one entry per `## <name>` section, `key: value`
    lines below it. A duplicate name INSIDE one file is reported on stderr
    (both stay listed; the match keeps one, see parse_all_catalogs)."""
    path = path or CATALOG
    try:
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, ValueError) as e:
        print("recall: unreadable catalog %s: %s" % (path, type(e).__name__),
              file=sys.stderr)
        return []
    entries, cur, in_fence, seen = [], None, False, set()
    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if cur:
                entries.append(cur)
            name = m.group(1).strip()
            if name.lower() in seen:
                print("recall: duplicate name \"%s\" in %s"
                      % (name, os.path.basename(path)), file=sys.stderr)
            seen.add(name.lower())
            cur = {"name": name}
            continue
        if cur is None:
            continue
        km = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if km and km.group(1) in FIELDS:
            cur[km.group(1)] = km.group(2).strip()
    if cur:
        entries.append(cur)
    return entries


def catalog_paths():
    """The curated catalog (source of truth, read first) plus every GENERATED
    catalog `*.gen.md` sitting next to it. The curated one wins on a duplicate
    name (see parse_all_catalogs)."""
    paths = [CATALOG]
    d = os.path.dirname(CATALOG)
    try:
        for f in sorted(os.listdir(d)):
            if f.endswith(".gen.md"):
                paths.append(os.path.join(d, f))
    except OSError:
        pass
    return paths


def parse_all_catalogs():
    """Curated entries plus generated ones, deduplicated by name. First seen
    wins (curated first), so a hand-written entry OVERRIDES its automatic
    namesake. Used ONLY by the contextual match: the passive surface,
    check-all, show and list stay on the curated catalog alone (zero noise at
    session start). It is deliberate that `match` can surface a generated entry
    that `show <name>` reports as unknown."""
    seen, out = set(), []
    for p in catalog_paths():
        for e in parse_catalog(p):
            key = e["name"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
    return out


# --- matching ---------------------------------------------------------------

def terms(entry):
    """Name plus aliases of an entry, lowercased, deduplicated."""
    out = [entry["name"].lower()]
    for a in entry.get("aliases", "").split(","):
        a = a.strip().lower()
        if a and a not in out:
            out.append(a)
    return out


def _word_present(term, text):
    """Word-boundary match. No wide fuzzy matching, so no noise: a false
    "already built" costs more than a miss."""
    return re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])",
                     text) is not None


def deprioritized(entry):
    """Replaced or extinguished entry: still matchable (the history is what
    someone is asking about), but floored in the ranking -- never above a
    living artifact."""
    return (_norm(entry.get("status")) in DEPRIORITIZED_STATUS
            or bool(entry.get("superseded_by")))


def match_entries(text):
    """At most MAX_HITS entries whose name or one alias appears in the text."""
    text = text.lower()
    hits = []
    for e in parse_all_catalogs():
        matched = [t for t in terms(e) if _word_present(t, text)]
        if matched:
            # score = length of the longest matched term (precision, not
            # frequency: "mail" is weaker evidence than "invoice-pipeline")
            score = 0 if deprioritized(e) else max(len(t) for t in matched)
            hits.append((score, e))
    hits.sort(key=lambda x: -x[0])
    return [e for _, e in hits[:MAX_HITS]]


# --- live existence ---------------------------------------------------------
# An entry that LIES is worse than no entry: it sends the agent to a path that
# is gone, and it costs more than rebuilding would have. So nothing is claimed
# without being verified against the real filesystem at the moment of the match.

def _on_disk(p):
    try:
        return os.path.exists(p)
    except Exception:
        return False


def index_lookup(p):
    """True/False from the file index, or None when the index cannot answer.

    The index is an ACCELERATOR, never a dependency: the harness ships with no
    third-party requirement. No binary, no database, an error, an empty result:
    all of it returns None and the caller falls back to the disk.
    """
    if not os.path.isfile(INDEX_DB):
        return None
    if shutil.which(INDEX_BIN) is None:
        return None
    try:
        r = subprocess.run([INDEX_BIN, "-d", INDEX_DB, "-e", p],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if r.returncode == 0:
        return p in r.stdout.splitlines()
    # rc 1 = absent from the index, which may only mean the index lags behind;
    # rc >= 2 = index error. Neither is an answer.
    return None


def path_exists(p):
    """Does this path exist right now? None when the entry carries no path.

    The index only ever covers HOME; outside that perimeter the disk is asked
    directly (before that, a path like /etc/hostname came back "MISSING")."""
    if not p:
        return None
    if not p.startswith(HOME):
        return _on_disk(p)
    hit = index_lookup(p)
    return _on_disk(p) if hit is None else hit


# --- check: -- CONSTRAINED execution ----------------------------------------
# This field comes from files a MODEL rewrites (the curation pass, and any
# *.gen.md derived without human curation), and it is executed on a timer. A
# prompt instruction is not a constraint. Here the constraint is code.
#
# Defense in depth, in order:
#   1. no shell metacharacter allowed (and no shell at all: shell=False);
#   2. an ALLOWLIST of executables -- argv[0] must be in it, and when given as
#      a path it must resolve to the SAME binary as `which <basename>` (a
#      planted /tmp/systemctl does not pass);
#   3. PER-BINARY rules: systemctl restricted to read-only verbs, curl with no
#      disk write and no upload. `systemctl --user stop x` and `curl -o /tmp/x`
#      must be refused, not only `rm` and `bash`.
#
# Widening CHECK_ALLOWED_BINS is a code edit on purpose: it is reviewed, it
# does not happen by editing a data file a model can rewrite.
_SHELL_META_RE = re.compile(r"[;&|<>`$(){}\[\]!*?~\\]")

CHECK_ALLOWED_BINS = {"systemctl", "test", "curl", "pgrep", "plocate"}
SYSTEMCTL_RO_VERBS = {"is-active", "is-enabled", "is-failed", "status", "show",
                      "list-units", "list-timers"}
CURL_FORBIDDEN_OPTS = {"-O", "--remote-name", "-T", "--upload-file",
                       "-d", "--data", "--data-raw", "--data-binary",
                       "--data-urlencode", "-F", "--form", "-K", "--config"}

# run_check: distinguishable statuses. Before, five different causes collapsed
# into a single None and a refusal appeared nowhere.
CHECK_OK = "ok"
CHECK_FAIL = "fail"
CHECK_REFUSED = "refused"
CHECK_UNAVAILABLE = "unavailable"   # binary missing, timeout, exception
CHECK_ABSENT = None                 # the entry carries no check:


def _refuse_binary(argv):
    """Apply the allowlist and the per-binary rules. None when allowed,
    otherwise a short reason for the report."""
    base = os.path.basename(argv[0])
    if base not in CHECK_ALLOWED_BINS:
        return "binary outside the allowlist: %s" % base
    canonical = shutil.which(base)
    if canonical is None:
        return "binary not on PATH: %s" % base
    if "/" in argv[0]:
        try:
            if os.path.realpath(argv[0]) != os.path.realpath(canonical):
                return "non-canonical path: %s" % argv[0]
        except OSError:
            return "unresolvable path: %s" % argv[0]
    if base == "systemctl":
        verbs = [a for a in argv[1:] if not a.startswith("-")]
        if not verbs or verbs[0] not in SYSTEMCTL_RO_VERBS:
            return "systemctl: verb is not read-only: %s" % (verbs[:1] or "?")
    if base == "curl":
        args = argv[1:]
        for i, a in enumerate(args):
            if a in CURL_FORBIDDEN_OPTS:
                return "curl: forbidden option: %s" % a
            if a in ("-o", "--output") and args[i + 1:i + 2] != ["/dev/null"]:
                return "curl: -o pointing somewhere other than /dev/null"
    return None


def run_check(cmd):
    """`check:` = a health command that tests REAL life, not mere presence
    (rc 0 = alive). Returns (status, reason)."""
    if not cmd:
        return (CHECK_ABSENT, None)
    if _SHELL_META_RE.search(cmd):
        return (CHECK_REFUSED, "shell metacharacter: %s" % cmd[:60])
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return (CHECK_REFUSED, "not splittable: %s" % cmd[:60])
    if not argv:
        return (CHECK_ABSENT, None)
    refusal = _refuse_binary(argv)
    if refusal:
        return (CHECK_REFUSED, "%s -- %s" % (refusal, cmd[:60]))
    try:
        r = subprocess.run(argv, shell=False, capture_output=True,
                           text=True, timeout=5)
        return (CHECK_OK, None) if r.returncode == 0 else (CHECK_FAIL, None)
    except FileNotFoundError:
        return (CHECK_UNAVAILABLE, "binary not found: %s" % argv[0])
    except Exception as e:
        return (CHECK_UNAVAILABLE, type(e).__name__)


def _origin_human(entry):
    """THE origin guardrail: only a human-curated entry (or one with no origin
    at all, for back-compat) may have its check: executed. auto/external is
    recalled, it never triggers anything."""
    return _norm(entry.get("origin")) in ("", "human")


def check_entry(entry):
    """run_check GATED by origin and status. The only call path."""
    if not _origin_human(entry):
        return (CHECK_REFUSED, "non-human origin (%s): check skipped"
                % _norm(entry.get("origin")))
    if deprioritized(entry):
        return (CHECK_ABSENT, None)   # the replaced or extinguished runs nothing
    return run_check(entry.get("check"))


# --- dates ------------------------------------------------------------------

def _days_since(date_str):
    """Days between date_str (YYYY-MM-DD) and TODAY, computed with datetime.
    None = unverifiable (TODAY not injected, or the date is unreadable)."""
    if not TODAY or not date_str:
        return None
    try:
        return (datetime.date.fromisoformat(TODAY)
                - datetime.date.fromisoformat(date_str)).days
    except ValueError:
        return None


def stale_flag(updated):
    """True = stale, False = fresh, None = UNVERIFIABLE. Three distinct states
    on purpose: "no date injected" used to display as "nothing is stale"."""
    d = _days_since(updated)
    if d is None:
        return None
    return d > STALE_DAYS


# --- rendering --------------------------------------------------------------

def _neutralize(txt):
    """The injected content comes from files a model rewrites. Role and system
    markers are neutralized so nothing in there can disguise itself as an
    instruction to the agent reading the block."""
    return re.sub(r"</?\s*(system-reminder|system|assistant|human)\s*>",
                  "(tag neutralized)", txt, flags=re.I)


def _sheet_allowed(sheet):
    try:
        real = os.path.realpath(sheet)
    except OSError:
        return False
    return any(seg in real for seg in SHEET_ALLOWED_SEGMENTS)


def render(entry, verify=True):
    """One entry as injected text, with its live verification tags."""
    p = entry.get("path", "")
    tags = []
    if verify:
        exists = path_exists(p)
        if exists is False:
            tags.append("! PATH MISSING on the real filesystem (status to review)")
        check_status, reason = check_entry(entry)
        if check_status == CHECK_FAIL:
            tags.append("! check: FAILS (on disk but dead or disabled)")
        elif check_status == CHECK_OK:
            tags.append("ok check alive")
        elif check_status == CHECK_REFUSED and reason \
                and reason.startswith("non-human origin"):
            pass   # the [origin: ...] tag below already says it
        elif check_status == CHECK_REFUSED:
            tags.append("! check: REFUSED (%s)" % reason)
    flag = stale_flag(entry.get("updated", ""))
    if flag is True:
        tags.append("! status not reviewed for more than %d days" % STALE_DAYS)
    if entry.get("superseded_by"):
        tags.append("! SUPERSEDED by %s" % entry["superseded_by"])
    origin = _norm(entry.get("origin"))
    if origin and origin != "human":
        tags.append("[origin: %s -- never acts on its own]" % origin)
    line = "* %s [%s] -- %s" % (entry["name"], entry.get("status", "?"),
                               entry.get("resume", ""))
    parts = [line, "    path: %s" % p]
    if entry.get("reactivate"):
        parts.append("    reactivate: %s" % entry["reactivate"])
    if entry.get("memory"):
        parts.append("    memory: [[%s]]" % entry["memory"])
    sheet = entry.get("sheet", "")
    if sheet and os.path.exists(sheet):
        if not _sheet_allowed(sheet):
            print("recall: sheet outside the allowed perimeter, ignored: %s"
                  % sheet, file=sys.stderr)
        else:
            try:
                with open(sheet, encoding="utf-8") as f:
                    txt = f.read().strip()
            except (OSError, ValueError):
                txt = ""
            if txt.startswith("---"):   # skip the YAML frontmatter (noise)
                end = txt.find("\n---", 3)
                if end != -1:
                    txt = txt[end + 4:].lstrip()
            if txt:
                if len(txt) > SHEET_MAX:
                    txt = txt[:SHEET_MAX] + \
                        "\n... (truncated -- read %s for the rest)" % sheet
                parts.append("    -- working sheet: %s --" % sheet)
                parts += ["    " + ln for ln in _neutralize(txt).splitlines()]
    if tags:
        parts.append("    " + " | ".join(tags))
    return _neutralize("\n".join(parts))


# --- commands ---------------------------------------------------------------

MATCH_HEADER = "[recall -- already built, do not rebuild]"
MATCH_FOOTER = "[end recall]"
BOOT_HEADER = "[recall -- passive surface]"


def cmd_match(text):
    hits = match_entries(text)
    if not hits:
        return ""   # silence is correct, not a gap
    body = "\n".join(render(e) for e in hits)
    return MATCH_HEADER + "\n" + body + "\n" + MATCH_FOOTER


def cmd_list():
    return "\n".join("- %s (%s): %s" % (e["name"], e.get("status", "?"),
                                        e.get("resume", ""))
                     for e in parse_catalog())


def cmd_show(name):
    for e in parse_catalog():
        if e["name"].lower() == name.lower():
            return render(e)
    return "(no entry named \"%s\" in the catalog)" % name


def cmd_check_all(write_report=False):
    """DETERMINISTIC health pass (zero LLM): existence, check:, staleness.
    With --report, writes the freshness report the passive surface reads at
    session start. SEMANTIC staleness stays a curation job, not this one."""
    lines, missing, failing, refused, stale, unknown = [], [], [], [], [], []
    for e in parse_catalog():
        exists = path_exists(e.get("path", ""))
        mark = "OK " if exists else "MISSING"
        lines.append("[%s] %s -> %s" % (mark, e["name"], e.get("path", "")))
        if deprioritized(e):
            continue   # the replaced or extinguished neither runs nor pollutes
        check_status, reason = check_entry(e)
        flag = stale_flag(e.get("updated", ""))
        if exists is False:
            missing.append(e["name"])
        if check_status == CHECK_FAIL:
            failing.append("%s (%s)" % (e["name"], e.get("check", "")))
        elif check_status == CHECK_REFUSED \
                and not (reason or "").startswith("non-human origin"):
            refused.append("%s (%s)" % (e["name"], reason))
        if flag is True:
            stale.append("%s (updated %s)" % (e["name"], e.get("updated", "?")))
        elif flag is None and e.get("updated"):
            unknown.append("%s (updated %s unreadable)"
                           % (e["name"], e.get("updated", "?")))
        elif flag is None and not TODAY:
            unknown.append(e["name"])
    if not TODAY:
        unknown = ["(HARNESS_RECALL_TODAY not injected: staleness NOT verified "
                   "on the whole catalog)"]
    if write_report:
        rep = ["# freshness report -- %s" % (TODAY or "unknown date"),
               "(regenerated by check-all on the refresh timer; disposable)", ""]
        rep.append("## %s (%d)" % (SECT_MISSING, len(missing)))
        rep += ["- %s" % n for n in missing] or [REPORT_NONE]
        rep.append("\n## %s (%d)" % (SECT_CHECK_FAIL, len(failing)))
        rep += ["- %s" % n for n in failing] or [REPORT_NONE]
        rep.append("\n## %s (%d)" % (SECT_CHECK_REFUSED, len(refused)))
        rep += ["- %s" % n for n in refused] or [REPORT_NONE]
        rep.append("\n## %s > %dd (%d)" % (SECT_STALE, STALE_DAYS, len(stale)))
        rep += ["- %s" % n for n in stale] or [REPORT_NONE]
        rep.append("\n## %s (%d)" % (SECT_UNVERIFIABLE, len(unknown)))
        rep += ["- %s" % n for n in unknown] or [REPORT_NONE]
        try:
            os.makedirs(os.path.dirname(REPORT), exist_ok=True)
            with open(REPORT, "w", encoding="utf-8") as f:
                f.write("\n".join(rep) + "\n")
            lines.append("(report written: %s)" % REPORT)
        except OSError as e:
            print("recall: report not written: %s" % type(e).__name__,
                  file=sys.stderr)
    return "\n".join(lines)


def cmd_boot_surface():
    """Passive surface at session start: automatic, never blind. HARD bounded
    to BOOT_MAX_CHARS characters -- context rot is the failure mode this whole
    module is fighting. Sections: recent additions (7 days), health (from the
    freshness report), last curation actions."""
    out = []
    recent = [e for e in parse_catalog()
              if (lambda d: d is not None and 0 <= d <= 7)(
                  _days_since(e.get("updated", "")))]
    if recent:
        out.append("recent (7d): " + " . ".join(
            "%s[%s]" % (e["name"], _norm(e.get("origin")) or "human")
            for e in recent[:8]))
    try:
        with open(REPORT, encoding="utf-8") as f:
            rep = f.read()
        section, keep = None, []
        for ln in rep.splitlines():
            if ln.startswith("## "):
                section = ln[3:]
                continue
            if ln.startswith("- ") and ln != REPORT_NONE and section:
                keep.append("%s: %s" % (section.split(" (")[0], ln[2:]))
        if keep:
            out.append("health: " + " . ".join(keep[:6]))
    except OSError:
        pass
    try:
        with open(CURATE_LOG, encoding="utf-8") as f:
            tail = f.read().strip().splitlines()
        if tail:
            out.append("curation: " + " . ".join(tail[-4:]))
    except OSError:
        pass
    if not out:
        return ""
    return (BOOT_HEADER + "\n" + "\n".join(out))[:BOOT_MAX_CHARS]


def _print_if_any(s):
    """`match` and `boot-surface` promise SILENCE: a bare print() always adds a
    newline, so stdout was never actually empty."""
    if s:
        print(s)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    c = argv[1]
    if c == "match":
        _print_if_any(cmd_match(argv[2] if len(argv) > 2 else ""))
    elif c == "list":
        print(cmd_list())
    elif c == "show" and len(argv) > 2:
        print(cmd_show(argv[2]))
    elif c == "check-all":
        print(cmd_check_all(write_report="--report" in argv))
    elif c == "boot-surface":
        _print_if_any(cmd_boot_surface())
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
