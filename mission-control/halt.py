#!/usr/bin/env python3
"""halt.py -- OPTIONAL module: pause the execution engine, in two steps.

    python3 halt.py              the current halt state, as JSON
    python3 halt.py --token      the path of this module's token file

Install this file and the panel gains exactly one way to act: it can PAUSE.
Delete it and the route answers 503 and the button disappears. Nothing else
changes, which is the point of the optional-module split (see server.py).

THE GESTURE IS IN TWO STEPS, AND THE STEPS ARE NOT DECORATION. A halt command
fired by accident -- a stray click, a page reloaded onto a POST, a script that
retried -- is exactly the failure mode this module exists to prevent, and it is
a failure mode that costs a whole autonomous run. So:

    request(role, token)  ->  verifies the token, WRITES NOTHING, and hands
                              back a short-lived confirm token
    commit(body)          ->  verifies the token AGAIN, verifies the confirm
                              token, verifies it has not expired, and only then
                              touches the disk

One step alone halts nothing. Neither does a token alone.

THE TOKEN IS THIS MODULE'S OWN, and it is deliberately not the panel's read
token: reading the fleet and stopping it are two different authorisations, and
a machine allowed to look must not thereby be allowed to stop. The file is
created mode 600 on first use, and `status()` never touches it -- reading the
state has to work for someone who holds no token at all.

The confirm token is derived, not stored: HMAC(secret, "<role>|<issued_at>"),
truncated. No nonce table to keep, nothing to clean up, and a panel restarted
between the two steps does not lose a pending confirmation. It can be replayed
inside its TTL by whoever already holds the secret, which changes nothing: the
result of halting twice is that it is halted.

THE HALT IS A FLAG ON DISK, NEVER A VARIABLE IN THIS PROCESS. Everything that
runs on this machine can read a file; nothing outside this process can read our
memory. A panel that restarts must not quietly release a halt someone set an
hour ago, and an engine that never talks to the panel must still be able to see
it. `HARNESS_MC_HALT_FLAG` names that file, and it names the file the engine
ALREADY reads: this module feeds an existing switch rather than inventing a
second one that would then have to be kept in sync.

Halting a single ROLE writes a separate flag and does NOT touch the engine
flag, so stopping one worker can never stop the fleet by side effect. Be
honest about what that flag does: it is ADVISORY. It stops nothing on its own
unless that role checks its own flag at a phase boundary. The panel shows it,
`status()` marks it `advisory`, and the engine flag stays the one with teeth.

EVERY OUTCOME IS JOURNALLED into the panel's signed, append-only event log --
requests, refusals, commits and releases alike. A refused token attempt is
precisely the line you want to find later, and `schema.sql` already reserves
the case: an event with no journal behind it leaves the provenance triple NULL.

Environment:
- HARNESS_MC_HALT_FLAG   flag file whose presence means "engine paused"
                         (default: $HARNESS_STATE_DIR/executor/halt)
- HARNESS_MC_HALT_TOKEN  this module's token file, mode 600, created on demand
                         (default: $HARNESS_STATE_DIR/mission-control/halt-token)
- HARNESS_STATE_DIR      state directory, read through config.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C                       # noqa: E402
import store as store_mod                # noqa: E402

# The engine is a role like any other in the contract, and the panel may call
# it several things. One canonical name, so a request for "all" and a commit
# for "engine" are the same target rather than a silent mismatch.
ENGINE = "executor"
ENGINE_ALIASES = frozenset(("", "engine", "executor", "runner", "all", "*",
                            "global", "panic"))

# Per-role flags live in their own directory, never next to the engine flag:
# the engine flag can be pointed anywhere by env, and writing siblings into
# somebody else's directory is not ours to do.
ROLE_FLAG_DIR = "halt-roles"

# Life of a confirm token. Long enough to read the sentence on screen, short
# enough that a tab left open overnight confirms nothing.
CONFIRM_TTL_S = 120

# Tolerance for a confirm token issued slightly in the future (clock skew
# between the two steps). Without it, `now - issued_at` going negative would
# make an absurdly future-dated stamp look permanently fresh.
FUTURE_SKEW_S = 5

JOURNAL_TYPE = "halt"
JOURNAL_AGENT = "mission-control"
JOURNAL_PROJECT = "mission-control"

MAX_REASON = 500


# --- naming and paths -------------------------------------------------------

def normalize_role(role) -> str:
    """Canonical target name. Anything the panel calls the engine, plus an
    empty or missing value, resolves to the engine: the button with no role
    picked is the engine button, and the confirmation sentence says so out loud
    before anyone commits."""
    name = str(role or "").strip()
    return ENGINE if name.lower() in ENGINE_ALIASES else name


def is_engine(role) -> bool:
    return normalize_role(role) == ENGINE


def role_flag(role) -> str:
    """Flag file of a single role. The name is filtered down to alphanumerics,
    `-` and `_`: it arrives in a request body, and a target name is not allowed
    to choose a path."""
    safe = "".join(ch for ch in normalize_role(role)
                   if ch.isalnum() or ch in "-_") or "unknown"
    return os.path.join(C.panel_dir(), ROLE_FLAG_DIR, safe)


def flag_for(role) -> str:
    return C.halt_flag() if is_engine(role) else role_flag(role)


def token_file() -> str:
    """This module's token file. Resolved on every call, like everything in
    config.py: a long-lived panel must not have frozen its paths at import."""
    raw = (os.environ.get("HARNESS_MC_HALT_TOKEN") or "").strip()
    if raw:
        return os.path.expanduser(raw)
    return os.path.join(C.panel_dir(), "halt-token")


# --- the token --------------------------------------------------------------

def load_token() -> str:
    """The halt secret, created mode 600 on first use.

    Opened with O_CREAT and an explicit 0600, so it is never briefly
    world-readable between creation and a later chmod."""
    path = token_file()
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fresh = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, fresh.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return fresh


def _token_ok(presented, secret) -> bool:
    """`compare_digest`, and an empty value is a refusal rather than a match
    against an empty expectation. Fail toward refusing."""
    if not presented or not secret:
        return False
    return hmac.compare_digest(str(presented), str(secret))


def _confirm_token(role, issued_at, secret) -> str:
    """The second-step proof. Derived from the target and the moment, under the
    halt secret, so it cannot be produced by someone who only watched the first
    response go by, and it is bound to the role that was actually asked for."""
    payload = ("%s|%d" % (normalize_role(role), int(issued_at))).encode("utf-8")
    return hmac.new(str(secret).encode("utf-8"), payload,
                    hashlib.sha256).hexdigest()[:32]


# --- the flag on disk -------------------------------------------------------

def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_flag(path, payload) -> None:
    """Write the flag atomically. A reader that catches a half-written file
    would read an empty reason and, worse, a truncated file that still EXISTS:
    the state would flicker instead of flipping.

    Mode 0644 explicitly, independent of the umask of whatever unit runs the
    panel. The content is not secret -- a target, a reason and a timestamp --
    and the engine reading it may well run as another user; a halt nobody can
    read is a halt nobody can explain."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    try:
        os.chmod(tmp, 0o644)
    except OSError:
        pass
    os.replace(tmp, path)


def _read_flag(path) -> dict:
    """What a flag file says. A flag written by hand (`touch`) or by an older
    version is still a halt: presence is the state, the content is only the
    explanation, and an unreadable one must never be mistaken for absence."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.loads(fh.read() or "{}")
        return data if isinstance(data, dict) else {"raw": True}
    except (OSError, ValueError):
        return {"raw": True}


# --- the journal ------------------------------------------------------------

def _journal(event, summary, refs=None) -> bool:
    """One signed row per outcome, and the caller is told whether it landed.

    Fail-soft on the way out, never silent: a broken database must not turn a
    halt into an error, but a halt whose trace was lost says `journaled: false`
    rather than claiming a clean record it does not have."""
    try:
        store = store_mod.open_store()
    except Exception:
        return False
    try:
        record = dict(refs or {})
        record["event"] = event
        store.append(ts=_stamp(), agent=JOURNAL_AGENT, project=JOURNAL_PROJECT,
                     type=JOURNAL_TYPE, summary=summary, refs=record)
        return True
    except Exception:
        return False
    finally:
        store.close()


# --- the contract the panel calls -------------------------------------------

def status() -> dict:
    """Is the engine paused, and what else is flagged. A PURE READ.

    It creates no token, no directory and no file, because it is the one part
    of this module a panel with no authority at all still has to answer: the
    core falls back to reading the flag itself when this module is absent, and
    the two answers have to mean the same thing."""
    flag = C.halt_flag()
    paused = os.path.exists(flag)
    out = {"paused": paused, "flag": flag, "module": True, "two_step": True,
           "confirm_ttl_s": CONFIRM_TTL_S, "reason": "", "since": "",
           "roles": {}}
    if paused:
        detail = _read_flag(flag)
        out["reason"] = detail.get("reason") or ""
        out["since"] = detail.get("ts") or ""
    roles_dir = os.path.join(C.panel_dir(), ROLE_FLAG_DIR)
    try:
        names = sorted(os.listdir(roles_dir))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(roles_dir, name)
        if not os.path.isfile(path):
            continue
        detail = _read_flag(path)
        # Say it here rather than only in the docs: this flag stops nothing by
        # itself. A panel that displays an advisory flag as an enforced one is
        # a panel that will be trusted at exactly the wrong moment.
        out["roles"][name] = {"reason": detail.get("reason") or "",
                              "since": detail.get("ts") or "",
                              "advisory": True}
    return out


def request(role, token) -> dict:
    """STEP ONE. Check the token, write NOTHING, hand back a confirm token.

    No flag is touched here, and that is the entire value of the step: the
    request is the part that can be fired by accident, so it is the part that
    is made harmless."""
    target = normalize_role(role)
    secret = load_token()
    if not _token_ok(token, secret):
        _journal("halt-refused",
                 "halt request refused for %r: invalid token" % target,
                 {"role": target, "stage": "request"})
        return {"ok": False, "stage": "request", "role": target,
                "error": "invalid token"}
    issued_at = int(time.time())
    engine = is_engine(target)
    message = ("Confirm pausing the execution engine. It stops picking up work "
               "until the halt is released."
               if engine else
               "Confirm the halt flag for role %r. ADVISORY: it stops that "
               "role only if the role checks its own flag." % target)
    _journal("halt-requested",
             "halt requested for %r, awaiting confirmation" % target,
             {"role": target, "engine": engine})
    return {"ok": True, "stage": "request", "role": target, "engine": engine,
            "advisory": not engine, "confirm_token": _confirm_token(
                target, issued_at, secret),
            "issued_at": issued_at, "expires_in_s": CONFIRM_TTL_S,
            "message": message}


def commit(body) -> dict:
    """STEP TWO, and the only step that touches the disk.

    Takes the whole request body because the panel hands it over untouched: it
    does not re-implement this module's gate, so it has no reason to know which
    fields the gate reads."""
    body = body if isinstance(body, dict) else {}
    target = normalize_role(body.get("role"))
    reason = str(body.get("reason") or "").strip()[:MAX_REASON]
    secret = load_token()

    def refuse(error):
        _journal("halt-refused",
                 "halt commit refused for %r: %s" % (target, error),
                 {"role": target, "stage": "commit"})
        return {"ok": False, "stage": "commit", "role": target, "error": error}

    # The token is re-checked at the second step. The first check proves
    # nothing about this request: they are two HTTP calls, and only one of them
    # writes.
    if not _token_ok(body.get("token"), secret):
        return refuse("invalid token")
    try:
        issued_at = float(body.get("issued_at"))
    except (TypeError, ValueError):
        return refuse("invalid issued_at")
    age = time.time() - issued_at
    if age > CONFIRM_TTL_S or age < -FUTURE_SKEW_S:
        return refuse("confirm token expired")
    if not _token_ok(body.get("confirm_token"),
                     _confirm_token(target, issued_at, secret)):
        return refuse("invalid confirm token")

    path = flag_for(target)
    try:
        _write_flag(path, {"role": target, "reason": reason, "ts": _stamp(),
                           "source": "mission-control"})
    except OSError as exc:
        # Fail LOUD. Everywhere else in this project a failure degrades
        # quietly; here, a caller who is told "paused" and is not is a caller
        # who will walk away from a running fleet.
        _journal("halt-failed",
                 "halt commit failed for %r: %s" % (target, exc),
                 {"role": target, "flag": path})
        return {"ok": False, "stage": "commit", "role": target,
                "error": "could not write the halt flag: %s" % exc,
                "paused": os.path.exists(C.halt_flag())}
    engine = is_engine(target)
    journaled = _journal(
        "halt-committed",
        "halt committed for %r%s" % (target, ": " + reason if reason else ""),
        {"role": target, "engine": engine, "flag": path})
    return {"ok": True, "stage": "commit", "role": target, "engine": engine,
            "advisory": not engine, "flag": path,
            "paused": os.path.exists(C.halt_flag()), "journaled": journaled}


def release(role=ENGINE, reason="") -> dict:
    """Remove a halt flag. DELIBERATELY NOT REACHABLE FROM THE PANEL.

    server.py routes `status`, `request` and `commit`, and nothing else, so
    there is no button that resumes an engine. Stopping is the safe direction
    and it gets a token and two steps; STARTING AGAIN is the dangerous one, and
    the project's answer to it is a proof of hardware presence (`presence.py`,
    another optional module) or a human at a shell removing the file.

    This function exists so that path is journalled like everything else rather
    than being an untraceable `rm`."""
    target = normalize_role(role)
    path = flag_for(target)
    try:
        removed = False
        if os.path.exists(path):
            os.remove(path)
            removed = True
    except OSError as exc:
        _journal("halt-release-failed",
                 "halt release failed for %r: %s" % (target, exc),
                 {"role": target, "flag": path})
        return {"ok": False, "role": target, "removed": False,
                "paused": os.path.exists(C.halt_flag()),
                "error": "could not remove the halt flag: %s" % exc}
    _journal("halt-released",
             "halt released for %r%s" % (target, ": " + reason if reason else ""),
             {"role": target, "flag": path, "removed": removed})
    return {"ok": True, "role": target, "removed": removed,
            "paused": os.path.exists(C.halt_flag())}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--token" in argv:
        # The PATH, never the secret. It is a mode-600 file on the operator's
        # own disk; whoever needs the value can read it, and a value printed on
        # a terminal ends up in a scrollback, a screenshot or a shell history.
        print(json.dumps({"token_file": token_file(),
                          "exists": os.path.exists(token_file())},
                         ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
