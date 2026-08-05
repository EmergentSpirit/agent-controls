#!/usr/bin/env python3
"""server.py -- the observation panel. Local, READ-ONLY, stdlib only.

    python3 server.py            http://127.0.0.1:8815

READ-ONLY is the contract, and it is what makes this thing safe to leave
running: the panel never writes a transcript, never edits a settings file,
never arms or retires a gate, never blocks anything. Its only writes are to
its own derived database (the incremental index, and the analyst's verdict
rows). Nothing it displays can act.

Security, non-negotiable:
- bind 127.0.0.1 ONLY (config.HOST). Never a wildcard bind, never a public
  host. Transcripts carry command outputs; this is an instrument on your desk,
  and a test greps this file for any wildcard address literal.
- transcript content is UNTRUSTED: served as JSON, never interpolated into
  HTML server-side, and the client only ever uses textContent (a test greps
  the static files for the raw-HTML assignment APIs).
- strict CSP, nosniff, no-referrer.

An incremental indexer pass runs on each /api/summary: measured at 0.0 s when
nothing changed, so the view is always fresh without a daemon.

Environment: see config.py (HARNESS_WATCH_DB, HARNESS_WATCH_PORT,
HARNESS_WATCH_TRANSCRIPTS, HARNESS_WATCH_JOURNALS, HARNESS_WATCH_EXCLUDE).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C                      # noqa: E402
import indexer                          # noqa: E402

STATIC = os.path.join(HERE, "static")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}


def db():
    conn = sqlite3.connect(C.db_path())
    conn.row_factory = sqlite3.Row
    return conn


# --- analysis state ---------------------------------------------------------
# Transient state belongs to the living server; the database only keeps
# verdicts. Without this, a judge thread that dies (a CLI not found on the
# minimal PATH of a systemd unit, for one real example) is INVISIBLE to the
# client, which then polls forever on a frozen button. An analysis that failed
# must be as visible as one that succeeded.
RUNNING = set()
ERRORS = {}


def analysis_status(session_id):
    """running | error | None -- the transient state the UI must be able to
    render. Exposed by the API on purpose: a dead thread is never invisible."""
    if session_id in RUNNING:
        return "running"
    if session_id in ERRORS:
        return "error"
    return None


def start_analysis(session_id):
    """Run the judge OUTSIDE the request. Its output acts on NOTHING: it is
    stored and displayed."""

    def task():
        try:
            import analyst
            analyst.analyze(session_id)
        # BaseException on purpose: skeleton() raises SystemExit on an unknown
        # session, and `except Exception` would let that die in silence -> the
        # client polls forever.
        except BaseException as exc:
            ERRORS[session_id] = ("%s: %s" % (type(exc).__name__, exc))[:200]
            sys.stderr.write("watch analysis %s: %s: %s\n"
                             % (session_id[:8], type(exc).__name__, exc))
        finally:
            RUNNING.discard(session_id)

    if session_id in RUNNING:
        return "already-running"
    ERRORS.pop(session_id, None)               # a relaunch starts clean
    RUNNING.add(session_id)
    threading.Thread(target=task, daemon=True).start()
    return "started"


# --- API --------------------------------------------------------------------

def api_summary(query):
    days = int((query.get("days") or ["30"])[0])
    conn = db()
    indexer.scan(conn)                  # incremental: the view is always fresh
    conn.commit()
    window = "-%d days" % days

    tiles = {
        "sessions": conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE first_ts >= datetime('now', ?)",
            (window,)).fetchone()[0],
        "events": conn.execute(
            "SELECT COUNT(*) FROM gate_events WHERE ts >= datetime('now', ?)",
            (window,)).fetchone()[0],
        "blocks": conn.execute(
            "SELECT COUNT(*) FROM gate_events WHERE result IN ('block','deny') "
            "AND ts >= datetime('now', ?)", (window,)).fetchone()[0],
        "blocked_sessions": conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM gate_events "
            "WHERE result IN ('block','deny') AND session_id IS NOT NULL "
            "AND ts >= datetime('now', ?)", (window,)).fetchone()[0],
    }
    sessions_by_day = conn.execute(
        "SELECT date(first_ts) d, COUNT(*) n FROM sessions "
        "WHERE first_ts >= datetime('now', ?) GROUP BY d ORDER BY d",
        (window,)).fetchall()
    blocks_by_day = conn.execute(
        "SELECT date(ts) d, COUNT(*) n FROM gate_events WHERE result IN ('block','deny') "
        "AND ts >= datetime('now', ?) GROUP BY d ORDER BY d", (window,)).fetchall()
    hooks = conn.execute(
        "SELECT hook, result, COUNT(*) n FROM gate_events WHERE ts >= datetime('now', ?) "
        "GROUP BY hook, result ORDER BY n DESC", (window,)).fetchall()
    conn.close()
    return {"tiles": tiles,
            "sessions_by_day": [dict(r) for r in sessions_by_day],
            "blocks_by_day": [dict(r) for r in blocks_by_day],
            "hooks": [dict(r) for r in hooks]}


def api_sessions(_query=None):
    conn = db()
    rows = conn.execute(
        "SELECT s.id, s.agent, s.title, s.first_ts, s.last_ts, s.n_user, "
        "s.n_assistant, s.n_tool, s.models, a.severity, "
        "(SELECT COUNT(*) FROM gate_events g WHERE g.session_id = s.id "
        " AND g.result IN ('block','deny')) blocks "
        "FROM sessions s LEFT JOIN analyses a ON a.session_id = s.id "
        "ORDER BY s.last_ts DESC LIMIT 200").fetchall()
    conn.close()
    sessions = []
    for row in rows:
        item = dict(row)
        item["analysis_status"] = analysis_status(item["id"])
        sessions.append(item)
    return {"sessions": sessions}


def api_session(session_id, query):
    page = int((query.get("page") or ["0"])[0])
    conn = db()
    meta = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not meta:
        conn.close()
        return None
    total = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?",
                         (session_id,)).fetchone()[0]
    messages = conn.execute(
        "SELECT seq, ts, type, tool, byte_size FROM messages WHERE session_id=? "
        "ORDER BY seq LIMIT ? OFFSET ?",
        (session_id, C.PAGE_SIZE, page * C.PAGE_SIZE)).fetchall()
    gates = conn.execute(
        "SELECT ts, hook, result, tool, extra FROM gate_events WHERE session_id=? "
        "ORDER BY ts", (session_id,)).fetchall()
    analysis = conn.execute("SELECT * FROM analyses WHERE session_id=?", (session_id,)).fetchone()
    conn.close()
    return {"meta": dict(meta), "total": total, "page": page,
            "page_size": C.PAGE_SIZE,
            "messages": [dict(r) for r in messages],
            "gates": [dict(r) for r in gates],
            "analysis": dict(analysis) if analysis else None,
            "analysis_status": analysis_status(session_id),
            "analysis_error": ERRORS.get(session_id)}


def api_content(session_id, seq):
    """Re-read ONE line of the source transcript at its indexed offset. Raw
    content, as JSON. Nothing is copied into the database."""
    conn = db()
    row = conn.execute(
        "SELECT m.byte_offset, m.byte_size, s.path FROM messages m "
        "JOIN sessions s ON s.id = m.session_id "
        "WHERE m.session_id=? AND m.seq=?", (session_id, int(seq))).fetchone()
    conn.close()
    if not row:
        return None
    try:
        with open(row["path"], "rb") as fh:
            fh.seek(row["byte_offset"])
            raw = fh.read(row["byte_size"])
        return {"content": json.loads(raw)}
    except Exception as exc:
        return {"error": "cannot re-read: %s" % type(exc).__name__}


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "harness-watch/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "object-src 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # A living local tool: without this header the browser keeps a stale
        # app.js in its heuristic cache and UI changes "disappear".
        self.send_header("Cache-Control", "no-cache")
        body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def log_message(self, fmt, *args):   # sober journal on stdout
        sys.stdout.write("watch %s %s\n" % (self.command, self.path))

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        try:
            if url.path == "/":
                return self._serve_static("index.html")
            if parts and parts[0] == "static" and len(parts) == 2:
                return self._serve_static(parts[1])
            if parts and parts[0] == "api" and len(parts) >= 2:
                if parts[1] == "summary":
                    return self._json(api_summary(query))
                if parts[1] == "sessions":
                    return self._json(api_sessions(query))
                if parts[1] == "session" and len(parts) == 3:
                    data = api_session(parts[2], query)
                    return self._json(data) if data else self._json(
                        {"error": "unknown session"}, 404)
                if parts[1] == "content" and len(parts) == 4:
                    data = api_content(parts[2], parts[3])
                    return self._json(data) if data else self._json(
                        {"error": "unknown message"}, 404)
            self._json({"error": "unknown route"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def do_POST(self):
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        try:
            # The ONLY mutating route, and it mutates nothing outside the
            # panel: it schedules a read-only judge whose output is a row.
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "analyze":
                return self._json({"status": start_analysis(parts[2])})
            self._json({"error": "unknown route"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def _serve_static(self, name):
        # `name` comes from a split on '/', so it carries no separator; we
        # re-check anyway, because a static server that trusts its input is
        # how a local panel becomes a file-read primitive.
        path = os.path.normpath(os.path.join(STATIC, os.path.basename(name)))
        if not path.startswith(STATIC) or not os.path.isfile(path):
            return self._json({"error": "unknown file"}, 404)
        with open(path, "rb") as fh:
            self._send(200, fh.read(),
                       MIME.get(os.path.splitext(path)[1], "application/octet-stream"))


def build_server(port=None):
    """The bind is asserted, not configured: a panel that can be moved off
    loopback by a typo is a panel that will be, once."""
    assert C.HOST == "127.0.0.1", "the watch panel serves localhost ONLY"
    return ThreadingHTTPServer((C.HOST, C.port() if port is None else port), Handler)


def main():
    srv = build_server()
    print("watch: http://%s:%d  (Ctrl-C to stop)" % srv.server_address[:2])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
