#!/usr/bin/env python3
"""server.py -- the fleet panel. Local, READ-ONLY by default, stdlib only.

    python3 server.py            http://127.0.0.1:8787

Five views, and they answer five questions a person actually asks:

    overview    is anything on fire, and is the log still intact
    agents      which roles are alive right now, and are they working
    schedule    what is going to run on this machine without me
    logs        what happened, filtered, with a per-line integrity verdict
    approvals   what is waiting for my decision

READ-ONLY IS THE DEFAULT, AND IT IS THE POINT. Out of the box this process
writes exactly two things: rows into its own derived event database, and the
ingest throttle marker. It cannot approve, halt, deploy or edit anything.
Everything that mutates the world outside the panel lives in an OPTIONAL
sibling module (see OPTIONAL_MODULES). Absent, the route answers 503 and the
button never appears. Present, it carries its own token gate. An operator who
installs none of them has a panel that is physically unable to act.

SECURITY, NON-NEGOTIABLE:
- bind 127.0.0.1 ONLY (config.HOST). No host variable exists, and build_server
  asserts the value. The panel shows command lines and pending operations.
- THE BIND IS NOT THE ONLY DEFENSE. Two attacks walk past a loopback socket:
  DNS REBINDING (a name the attacker owns resolves to 127.0.0.1 after the page
  loads, so the browser connects over loopback and the request carries
  `Host: attacker.example`) and CSRF (any page in that browser can fire a POST
  at the panel; there is no credential to steal because there is none). So the
  Host header must NAME loopback on every request, and a mutating request must
  carry no Origin or a loopback one. Anything else is 403 before a row is read.
- A PROXIED REQUEST MUST CARRY THE READ TOKEN. Put a tunnel or a reverse proxy
  in front of this and the peer address is STILL 127.0.0.1 -- so the peer
  address alone would trust the whole network behind the proxy. Any request
  carrying a proxy marker header therefore needs the read token, whose file is
  mode 600 and local. No token file, no proxy, no prompt.
- JOURNAL CONTENT IS UNTRUSTED: served as JSON, never interpolated into HTML
  server-side, and the client only ever uses textContent.
- strict CSP, nosniff, no-referrer, and no third-party script anywhere.

Environment: see config.py.
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import approvals                         # noqa: E402
import config as C                       # noqa: E402
import fleet                             # noqa: E402
import ingest                            # noqa: E402
import store as store_mod                # noqa: E402

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}

SUBPROCESS_TIMEOUT = 5                   # a slow scheduler is an empty table


# --- optional modules: every way this panel could ever act -------------------
# Each entry is a sibling module that is NOT part of the read-only core. The
# panel imports it if it is there and degrades to 503 if it is not, so the
# capability is a file an operator chooses to install rather than a flag.
#
#   halt      status() -> dict
#             request(role, token) -> dict
#             commit(body) -> dict     the whole request body, untouched
#             Pauses the execution engine, two-step, with its own token file.
#
#   approve   record(script, sha256, approved_by) -> dict
#             Records a human approval the execution engine will honour. Its
#             own token, and it re-checks the engine's own refusals server-side
#             rather than trusting the panel's view of them.
#
#   presence  status() -> dict
#             begin(action, body) -> dict
#             complete(action, body) -> dict
#             Hardware presence proof for the operations the engine refuses to
#             release on a click alone.
#
#   deposit   status() -> dict
#             store(slot, value, token) -> dict
#             Hands a secret to a slot without it passing through a chat log.
OPTIONAL_MODULES = ("halt", "approve", "presence", "deposit")
_LOADED = {}


def optional(name):
    """Import a sibling optional module once, or return None forever.

    Loaded BY PATH, from this directory only. `__import__(name)` would happily
    resolve a `halt` module sitting anywhere else on sys.path, which is a wide
    door for a function whose whole job is to hand it mutating requests. The
    import is attempted a single time: a module that fails to import is a
    missing capability, not a retry loop on every request."""
    if name in _LOADED:
        return _LOADED[name]
    module = None
    if name in OPTIONAL_MODULES:
        path = os.path.join(HERE, name + ".py")
        if os.path.isfile(path):
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "mission_control_" + name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as exc:      # broad on purpose: a broken optional
                module = None             # module must never break the panel
                sys.stderr.write("mission-control: optional module %r unavailable"
                                 ": %s: %s\n" % (name, type(exc).__name__, exc))
    _LOADED[name] = module
    return module


UNAVAILABLE_STATUS = 503


def unavailable(name):
    return {"ok": False, "error": "%s module not installed" % name,
            "module": name}


# --- read handlers: pure functions, no transport ----------------------------

def db():
    return store_mod.open_store()


def _window_start():
    """ISO timestamp of the alert window's start. The overview counts inside a
    WINDOW on purpose: an all-time counter keeps one bad week on screen
    forever, and a number that never moves stops being read. The full history
    stays one click away in the logs view."""
    import datetime
    start = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=C.window_days()))
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def auto_ingest():
    """Refresh the store on a view, at most once per throttle interval.

    Without this, live journals only reached the store when someone pressed the
    refresh button, so the counters were quietly late and the panel lied by
    lag. Fail-soft: a broken ingest must never take down the display of the
    rows that are already there."""
    interval = C.ingest_interval()
    if interval <= 0:
        return
    marker = C.ingest_marker()
    try:
        if os.path.isfile(marker) and time.time() - os.path.getmtime(marker) < interval:
            return
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as fh:
            fh.write("")
        ingest.run()
    except Exception:
        pass


def halt_state():
    """What the panel knows about the execution engine being paused. The
    optional module answers when installed; otherwise the flag file is read
    directly, because "is the engine paused" is a READ and must work on a
    panel that installed no ability to act."""
    module = optional("halt")
    if module is not None:
        try:
            return module.status()
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
    return {"paused": os.path.exists(C.halt_flag()),
            "flag": C.halt_flag(), "module": False}


def api_overview(_query=None):
    auto_ingest()
    conn = db()
    try:
        by_type = conn.counts_by_type(since_ts=_window_start())
        return {
            "total_events": conn.count(),
            "window_days": C.window_days(),
            "fleet": fleet.summary(),
            "agents": conn.agents(),
            "by_type": by_type,
            "alerts": {"blockers": by_type.get("blocker", 0),
                       "health": by_type.get("health", 0),
                       "circuit_break": by_type.get("circuit-break", 0),
                       "halts": by_type.get("halt", 0)},
            "integrity": conn.verify_all(),
            "halt": halt_state(),
            "recent": conn.query(limit=20),
        }
    finally:
        conn.close()


def api_agents(_query=None):
    """The live roster, each role carrying its last known event. Two sources on
    purpose: the multiplexer says who is alive, the log says what they last
    did. Neither can answer both."""
    live = fleet.live_fleet()
    conn = db()
    try:
        for entry in live:
            rows = conn.query(agent=entry["role"], limit=1)
            entry["last_event"] = rows[0] if rows else None
    except Exception:
        for entry in live:
            entry.setdefault("last_event", None)
    finally:
        conn.close()
    return {"fleet": live, "summary": fleet.summary(live)}


def _crontab():
    entries = []
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                              timeout=SUBPROCESS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return entries
    if proc.returncode != 0:
        return entries
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 5)
        if len(parts) >= 6:
            entries.append({"schedule": " ".join(parts[:5]), "command": parts[5]})
    return entries


def _timers():
    timers = []
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "list-timers", "--all", "--output=json"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return timers
    if proc.returncode != 0:
        return timers

    def stamp(value):
        # list-timers reports epoch MICROseconds; a missing or zero field means
        # "never", not 1970.
        try:
            import datetime
            micros = int(value)
            if micros <= 0:
                return ""
            return datetime.datetime.fromtimestamp(
                micros / 1e6).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    try:
        parsed = json.loads(proc.stdout or "[]")
    except ValueError:
        return timers
    for item in parsed if isinstance(parsed, list) else []:
        timers.append({"unit": item.get("unit"),
                       "activates": item.get("activates"),
                       "next": stamp(item.get("next")),
                       "last": stamp(item.get("last"))})
    return timers


def api_schedule(_query=None):
    """Everything scheduled to run on this machine without a human present.
    Both sources are read fail-soft and independently: no cron daemon and no
    user manager is a perfectly normal machine, and it gets two empty tables
    rather than an error page."""
    crons, timers = _crontab(), _timers()
    return {"crons": crons, "count": len(crons),
            "timers": timers, "timers_count": len(timers)}


def _int_or(value, default, low=1, high=1000):
    """Bounded int with a sane fallback. A non-numeric `?limit=` used to raise
    straight through the handler and drop the connection; an unbounded one
    (`?limit=999999999`) used to read the whole table into memory. Malformed
    falls back, oversized is capped."""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def api_logs(query):
    conn = db()
    try:
        rows = conn.query(agent=(query.get("agent") or [None])[0],
                          project=(query.get("project") or [None])[0],
                          type=(query.get("type") or [None])[0],
                          search=(query.get("search") or [None])[0],
                          since_ts=(query.get("since_ts") or [None])[0],
                          limit=_int_or((query.get("limit") or [None])[0], 200))
        for row in rows:
            # Per-line verdict, right next to the line. An integrity check
            # reported only as a global "OK" tells you something is wrong but
            # never which row, which is the one thing you need.
            row["sig_valid"] = conn.verify_sig(row)
        return {"events": rows, "count": len(rows)}
    finally:
        conn.close()


def api_approvals(_query=None):
    data = approvals.pending()
    data["can_approve"] = optional("approve") is not None
    data["presence_available"] = optional("presence") is not None
    return data


# --- request guard: Host, Origin, and the proxy case ------------------------
LOOPBACK_HOSTNAMES = frozenset(("127.0.0.1", "localhost", "::1", "[::1]",
                                "::ffff:127.0.0.1"))

# Headers a reverse proxy or tunnel adds to every request it forwards. Their
# presence is the ONLY reliable sign that the peer address is the proxy rather
# than the operator: a proxy connects to the backend from 127.0.0.1, so the
# socket looks local for the entire network behind it.
PROXY_MARKERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip",
                 "forwarded")


def host_problem(header):
    """None when the Host header names loopback, otherwise the reason to 403.

    The PORT is deliberately not checked: it says which port the client dialed,
    never who dialed it, and a tunnel (`ssh -L 9000:127.0.0.1:8787`)
    legitimately changes it. A rebinding attacker controls the NAME."""
    if header is None:
        return "no Host header"
    host = header.strip()
    if host.startswith("["):                       # [::1] or [::1]:8787
        name = host.partition("]")[0] + "]"
    elif host.count(":") == 1:                     # 127.0.0.1:8787
        name = host.rsplit(":", 1)[0]
    else:                                          # 127.0.0.1, localhost, ::1
        name = host
    if name.lower() in LOOPBACK_HOSTNAMES:
        return None
    return "Host header %r does not name loopback" % host[:60]


def origin_problem(header):
    """None when a mutating request may proceed.

    An absent Origin is fine: curl, a unit and a timer send none. A PRESENT one
    means a browser sent it, and then it has to be us. `Origin: null` (a
    sandboxed frame, a `file://` page) is REFUSED rather than read as absent --
    that is exactly the shape a hostile local page has."""
    if header is None:
        return None
    origin = header.strip()
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme in ("http", "https") and \
            (parsed.hostname or "").lower() in LOOPBACK_HOSTNAMES:
        return None
    return "Origin %r is not a loopback origin" % origin[:60]


def is_proxied(headers):
    return any(headers.get(marker) for marker in PROXY_MARKERS)


def read_token():
    """The expected read token, or "" when no token file exists. No file means
    no proxy is expected, and the panel does not invent a password nobody set."""
    try:
        with open(C.read_token_file(), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def token_problem(headers, query, cookies):
    """None when the request may proceed on the token rule.

    Direct loopback needs nothing. A PROXIED request needs the token, and when
    no token file exists a proxied request is refused outright: someone put a
    proxy in front of an unauthenticated panel, and guessing that they meant to
    is how the transcripts end up on the internet."""
    if not is_proxied(headers):
        return None
    expected = read_token()
    if not expected:
        return "request is proxied and no read token is configured"
    presented = (headers.get("x-panel-token")
                 or (query.get("token") or [""])[0]
                 or cookies.get("panel_token") or "")
    if presented and hmac.compare_digest(str(presented), expected):
        return None
    return "proxied request without a valid read token"


# --- HTTP -------------------------------------------------------------------

ROUTES_GET = {
    "overview": api_overview,
    "agents": api_agents,
    "schedule": api_schedule,
    "logs": api_logs,
    "approvals": api_approvals,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "harness-mission-control/1.0"

    def _guard(self, query, cookies):
        """True when the request may proceed. When it may not, the 403 has
        already been written and the caller must return at once."""
        problem = host_problem(self.headers.get("Host"))
        if problem is None:
            problem = token_problem(self._headers(), query, cookies)
        if problem is None and self.command != "GET":
            problem = origin_problem(self.headers.get("Origin"))
        if problem is None and self.command != "GET":
            # Fetch Metadata, set by the browser and not forgeable by page
            # script. `none` is a typed address bar, `same-origin` is our own
            # page; anything else is another site driving this one.
            site = str(self.headers.get("Sec-Fetch-Site") or "").lower()
            if site and site not in ("same-origin", "none"):
                problem = "cross-site request refused (Sec-Fetch-Site: %s)" % site[:20]
        if problem is None:
            return True
        self._json({"error": "forbidden: %s" % problem}, 403)
        return False

    def _headers(self):
        return {str(k).lower(): v for k, v in self.headers.items()}

    def _cookies(self):
        from http.cookies import SimpleCookie
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return {}
        try:
            jar = SimpleCookie()
            jar.load(raw)
            return {k: m.value for k, m in jar.items()}
        except Exception:
            return {}

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "object-src 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # A living local tool: without this the browser keeps a stale app.js in
        # its heuristic cache and UI changes "disappear".
        self.send_header("Cache-Control", "no-cache")
        body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def log_message(self, fmt, *args):   # sober journal on stdout
        sys.stdout.write("mission-control %s %s\n" % (self.command, self.path))

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not self._guard(query, self._cookies()):
                return
            if url.path == "/":
                return self._static("index.html")
            if len(parts) == 2 and parts[0] == "static":
                return self._static(parts[1])
            if len(parts) == 2 and parts[0] == "api":
                view = ROUTES_GET.get(parts[1])
                if view is not None:
                    return self._json(view(query))
                if parts[1] == "halt":
                    return self._json(halt_state())
                if parts[1] in ("presence", "deposit"):
                    module = optional(parts[1])
                    if module is None:
                        return self._json(unavailable(parts[1]),
                                          UNAVAILABLE_STATUS)
                    return self._json(module.status())
            self._json({"error": "unknown route"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not self._guard(urllib.parse.parse_qs(url.query), self._cookies()):
                return
            body = self._body()
            # The ONE mutating route of the read-only core, and it mutates
            # nothing outside the panel: it re-reads journals into the panel's
            # own derived database.
            if parts == ["api", "ingest"]:
                return self._json({"ok": True, "ingest": ingest.run()})
            if parts == ["api", "approve"]:
                return self._dispatch_optional("approve", body)
            if parts == ["api", "halt"]:
                return self._dispatch_optional("halt", body)
            if len(parts) == 3 and parts[:2] == ["api", "presence"]:
                return self._dispatch_optional("presence", body, parts[2])
            if parts == ["api", "deposit"]:
                return self._dispatch_optional("deposit", body)
            self._json({"error": "unknown route"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def _dispatch_optional(self, name, body, action=None):
        """Hand a mutating request to its optional module, or answer 503.

        The panel does NOT check the module's token, re-implement its rules, or
        second-guess its refusals: the module owns its own gate, and a caller
        that duplicates a security check is a caller that will eventually
        disagree with it."""
        module = optional(name)
        if module is None:
            return self._json(unavailable(name), UNAVAILABLE_STATUS)
        try:
            if name == "approve":
                result = module.record(body.get("script"), body.get("sha256"),
                                       body.get("token"))
            elif name == "halt":
                stage = str(body.get("stage") or "request").lower()
                result = (module.request(body.get("role"), body.get("token"))
                          if stage == "request" else module.commit(body))
            elif name == "presence":
                result = (module.begin(action, body) if action == "begin"
                          else module.complete(action, body))
            else:
                result = module.store(body.get("slot"), body.get("value"),
                                      body.get("token"))
        except Exception as exc:
            sys.stderr.write("mission-control %s: %s: %s\n"
                             % (name, type(exc).__name__, exc))
            return self._json({"ok": False, "error": "internal error in the "
                               "%s module" % name}, 500)
        code = 200 if (isinstance(result, dict) and result.get("ok")) else 400
        self._json(result if isinstance(result, dict) else {"ok": False}, code)

    def _static(self, name):
        # `name` comes from a split on '/', so it carries no separator; we
        # re-check anyway, because a static server that trusts its input is how
        # a local panel becomes a file-read primitive.
        path = os.path.normpath(os.path.join(C.STATIC, os.path.basename(name)))
        if not path.startswith(C.STATIC) or not os.path.isfile(path):
            return self._json({"error": "unknown file"}, 404)
        with open(path, "rb") as fh:
            self._send(200, fh.read(),
                       MIME.get(os.path.splitext(path)[1],
                                "application/octet-stream"))


def build_server(port=None):
    """The bind is asserted, not configured: a panel that can be moved off
    loopback by a typo is a panel that will be, once."""
    assert C.HOST == "127.0.0.1", "the mission-control panel serves localhost ONLY"
    return ThreadingHTTPServer((C.HOST, C.port() if port is None else port), Handler)


def main():
    srv = build_server()
    installed = [name for name in OPTIONAL_MODULES if optional(name) is not None]
    # flush=True: under a unit, stdout is a pipe and Python buffers it, so the
    # banner that says WHICH mutating modules are live would not appear in the
    # journal until the process exits. That line is the whole point of reading
    # the log at startup.
    print("mission-control: http://%s:%d  (Ctrl-C to stop)"
          % srv.server_address[:2], flush=True)
    print("mission-control: read-only core; optional modules installed: %s"
          % (", ".join(installed) or "none"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
