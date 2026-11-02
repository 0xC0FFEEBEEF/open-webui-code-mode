#!/usr/bin/env python3
"""code-preview.py -- a read-only static preview server for Code chat folders.

Each Code chat owns a project folder under PREVIEW_ROOT (default
/home/coder/projects/chats) named <chat_id>. This server serves those folders
behind a signed URL so Open WebUI can embed a live preview panel next to the
chat.

Hard rules (see BRIEF.md):

* Python 3.12 standard library only.
* It only ever reads files. It never writes, deletes or executes anything.
* When the chat has started its own server (a process listening on a port whose working directory is inside the
  chat's folder), requests are passed to that server instead of served from disk, so the panel shows the app the
  agent built and checked, backend included (2026-10-09, user's choice). Only a chat's own processes qualify, so no
  other service on this machine is reachable this way, and the preview's security headers replace the server's.
* With PREVIEW_SECRET unset it refuses to start (exit code 2).

URLs served (see BRIEF.md):

    GET /p/<chat_id>/<sig>/<path>    a file inside the chat's folder
    GET /p/<chat_id>/<sig>/          the folder's index.html, else a file listing
    GET /p/<chat_id>/<sig>/__version JSON version manifest for the panel
"""

import hashlib
import hmac
import html
import http.client
import time
import json
import mimetypes
import os
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, quote

# chat_id must match this; sig is the first 32 hex chars of HMAC-SHA256.
CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# /p/<chat_id>/<sig>/<rest-with-leading-slash-or-empty>
ROUTE_RE = re.compile(r"^/p/(?P<chat_id>[^/]+)/(?P<sig>[^/]*)(?P<rest>/.*)?$")
PROXY_TIMEOUT = 60
STREAM_IDLE_S = 3600        # an event stream with nothing sent for this long is closed (the page reconnects)
PORT_CACHE_S = 2.0
# The chat server's own headers that the preview replaces (security, caching) or that only make sense on one hop.
DROP_RESPONSE_HEADERS = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length", "content-type",
                         "content-security-policy", "content-security-policy-report-only", "x-frame-options",
                         "referrer-policy", "cache-control", "x-content-type-options", "server", "date",
                         "proxy-authenticate", "trailer"}

DEFAULT_FRAME_ANCESTORS = "http://cryoai:8080 http://cryoai:8080"
DEFAULT_ROOT = "/home/coder/projects/chats"
DEFAULT_PORT = 4097
MAX_WALK_FILES = 2000

# Why the frame-ancestors/CSP is shaped this way: the previewed pages are written
# by a model and run in the user's own browser, which can reach the home LAN.
# connect-src 'self' and form-action 'self' stop those pages from calling LAN
# devices; frame-ancestors restricts which Open WebUI panels may embed them.
FRAME_ANCESTORS = os.environ.get("PREVIEW_FRAME_ANCESTORS", DEFAULT_FRAME_ANCESTORS)


def _secret():
    return os.environ["PREVIEW_SECRET"]


def root_dir():
    return os.environ.get("PREVIEW_ROOT", DEFAULT_ROOT)


def mask_sig(raw_path):
    """Replace the HMAC sig in a raw request path with *** for the log.

    A signed request looks like ``/p/<chat_id>/<sig>/<path>``; the chat_id and
    sig are the second and third path segments. Anything that is not a signed
    preview request is returned unchanged.
    """
    p = raw_path.split("?", 1)[0]
    parts = p.split("/")
    if len(parts) >= 4 and parts[1] == "p" and parts[3]:
        parts[3] = "***"
        return "/".join(parts)
    return p


def expected_sig(chat_id):
    key = _secret().encode("utf-8")
    msg = chat_id.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def valid_sig(chat_id, sig):
    try:
        want = expected_sig(chat_id)
    except KeyError:
        return False
    return hmac.compare_digest(want, sig)


def chat_dir_for(chat_id):
    return os.path.realpath(os.path.join(root_dir(), chat_id))


def compute_version(chat_dir):
    """Return (version_hash, file_count, entry).

    version changes whenever any non-dot file is added, removed or modified.
    Walks at most MAX_WALK_FILES files.
    """
    h = hashlib.sha256()
    base = os.path.realpath(chat_dir)
    count = 0
    entry = None
    if os.path.isfile(os.path.join(chat_dir, "index.html")):
        entry = "index.html"
    for dirpath, dirnames, filenames in os.walk(chat_dir):
        # Never descend into dot directories (.git, .venv, ...).
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            # Same rule as serving: a symlink may not count something outside the folder or a dot entry.
            relreal = os.path.relpath(os.path.realpath(full), base)
            if relreal.startswith("..") or any(part.startswith(".") for part in relreal.split(os.sep)):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, chat_dir)
            h.update(rel.encode("utf-8", "surrogateescape"))
            h.update(b"\x00")
            h.update(str(st.st_size).encode("ascii"))
            h.update(b"\x00")
            h.update(str(st.st_mtime_ns).encode("ascii"))
            h.update(b"\x00")
            count += 1
            if count >= MAX_WALK_FILES:
                break
        if count >= MAX_WALK_FILES:
            break
    return h.hexdigest(), count, entry


def _listening_ports():
    """{socket inode: port} of every listening TCP socket on this machine (IPv4 and IPv6)."""
    out = {}
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name) as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "0A":          # 0A = LISTEN
                out[parts[9]] = int(parts[1].rsplit(":", 1)[1], 16)
    return out


_port_cache = {}


def app_port(chat_dir):
    """The port of a server the chat itself started: a listening socket held by a process whose working directory
    is inside the chat's folder (the folder itself preferred, then the lowest port). Only this user's processes
    are visible here, and only a chat's own qualify. None when the chat runs no server."""
    base = os.path.realpath(chat_dir)
    hit = _port_cache.get(base)
    if hit and time.monotonic() - hit[0] < PORT_CACHE_S:
        return hit[1]
    ports, found = _listening_ports(), []
    for pid in os.listdir("/proc") if ports else []:
        if not pid.isdigit():
            continue
        try:
            cwd = os.readlink("/proc/%s/cwd" % pid)
            fds = os.listdir("/proc/%s/fd" % pid)
        except OSError:
            continue
        if cwd != base and not cwd.startswith(base + os.sep):
            continue
        for fd in fds:
            try:
                link = os.readlink("/proc/%s/fd/%s" % (pid, fd))
            except OSError:
                continue
            if link.startswith("socket:[") and link[8:-1] in ports:
                found.append((cwd != base, ports[link[8:-1]]))
    port = min(found)[1] if found else None
    _port_cache[base] = (time.monotonic(), port)
    return port


def human_size(n):
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return ("%d%s" % (value, unit)) if unit == "B" else ("%.1f%s" % (value, unit))
        value /= 1024.0
    return "%dB" % n


def listing_html(entries, inner, prefix):
    """entries: list of (name, is_dir, size, mtime); newest first.

    prefix is the URL up to and including this directory (e.g.
    "/p/<id>/<sig>/") so the entry links are absolute and work when the page is
    embedded in the preview panel.
    """
    rows = []
    for name, is_dir, size, mtime in entries:
        href = prefix + quote(inner, safe="/") + "/" + quote(name, safe="/")
        if is_dir:
            href += "/"
        label = html.escape(name) + ("/" if is_dir else "")
        mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(
            '<tr><td><a href="%s">%s</a></td><td>%d</td><td>%s</td></tr>'
            % (href, label, size, mtime_str)
        )
    body = "\n".join(rows)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Project files</title>\n"
        "<style>\n"
        ":root { color-scheme: light dark; }\n"
        "body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;"
        " margin: 2rem; line-height: 1.4; }\n"
        "h1 { font-size: 1.4rem; margin: 0 0 1rem; }\n"
        "table { border-collapse: collapse; width: 100%; }\n"
        "th, td { text-align: left; padding: 0.3rem 0.75rem;"
        " border-bottom: 1px solid #ccc; }\n"
        "th { font-weight: 600; }\n"
        "a { text-decoration: none; color: inherit; }\n"
        "a:hover { text-decoration: underline; }\n"
        "td:nth-child(2), td:nth-child(3) { white-space: nowrap; }\n"
        "@media (prefers-color-scheme: dark) {\n"
        "  body { color: #e6e6e6; background: #161616; }\n"
        "  th, td { border-color: #444; }\n"
        "  td:nth-child(2n) { background: #1f1f1f; }\n"
        "}\n"
        "</style>\n</head>\n<body>\n"
        "<h1>Project files</h1>\n"
        "<table>\n<tr><th>Name</th><th>Size</th><th>Modified</th></tr>\n"
        + body
        + "\n</table>\n</body>\n</html>\n"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "code-preview/1.0"

    # Silence the default timestamped logging; we emit our own single line.
    def log_message(self, fmt, *args):
        return

    def _send(self, status, body=b"", content_type=None, extra_headers=None,
              sanitized=None, proxied=False):
        if sanitized is None:
            sanitized = mask_sig(self.path)
        if status >= 400 and not body and not proxied:  # a real page: Firefox shows its own error screen for an empty one
            body, content_type = self._error_page(status), "text/html; charset=utf-8"
        self._head(status, content_type, extra_headers, length=len(body))
        if body and self.command != "HEAD":
            self.wfile.write(body)
        sys.stderr.write("%s %s %d%s\n" % (self.command, sanitized, status, " (app)" if proxied else ""))
        sys.stderr.flush()

    def _head(self, status, content_type, extra_headers, length=None):
        """Status line and headers. The security headers are the preview's own on every answer, the app's included."""
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        csp = ("default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: https:; "
               "connect-src 'self'; form-action 'self'; frame-ancestors " + FRAME_ANCESTORS)
        self.send_header("Content-Security-Policy", csp)
        if content_type:
            self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        if extra_headers:
            for key, val in extra_headers.items():
                self.send_header(key, val)
        self.end_headers()

    def _error_page(self, status):
        m = ROUTE_RE.match(self.path.split("?", 1)[0])
        home = ""
        if status == 404 and m and CHAT_ID_RE.match(m.group("chat_id")) and m.group("sig"):
            home = '<p><a href="/p/%s/%s/">Back to the project</a></p>' % (m.group("chat_id"), quote(m.group("sig")))
        text = {403: "This preview link is not valid.", 404: "That page is not in this project.",
                405: "Only GET and HEAD are allowed here (this project runs no server)."}.get(status, "Error %d." % status)
        return ("<!doctype html><meta charset=utf-8><title>%d</title><style>body{font:15px system-ui,sans-serif;"
                "margin:40px;color:#e5e5e5;background:#171717}a{color:#4f8cff}@media (prefers-color-scheme: light)"
                "{body{color:#222;background:#fff}}</style><p>%s</p>%s" % (status, text, home)).encode("utf-8")

    def _route(self):
        path = self.path.split("?", 1)[0]
        m = ROUTE_RE.match(path)
        if not m:
            self._send(404, sanitized=path)
            return
        chat_id = m.group("chat_id")
        sig = m.group("sig")
        rest = m.group("rest") or ""
        # Base URL for building absolute links in file listings.
        self.listing_prefix = "/p/%s/%s/" % (chat_id, sig)

        # 1. chat id format -> 404
        if not CHAT_ID_RE.match(chat_id):
            self._send(404)
            return
        # 2. signature -> 403 (verified before touching the filesystem)
        if not valid_sig(chat_id, sig):
            self._send(403)
            return
        chat_dir = chat_dir_for(chat_id)
        if not os.path.isdir(chat_dir):
            self._send(404)
            return

        # URL-decode the remaining path, then constrain it.
        inner = unquote(rest).strip("/")
        # 3. any dot component (.git, .env, ..) -> 404
        for comp in inner.split("/"):
            if comp.startswith("."):
                self._send(404)
                return
        target = os.path.realpath(os.path.join(chat_dir, inner))
        base = os.path.realpath(chat_dir)
        # 4. must stay inside the chat's folder (blocks .. and stray symlinks)
        if target != base and not target.startswith(base + os.sep):
            self._send(404)
            return
        # 5. ...and a symlink inside the folder must not lead to a dot entry (.env, .git) either
        rel = os.path.relpath(target, base)
        if rel != "." and any(part.startswith(".") for part in rel.split(os.sep)):
            self._send(404)
            return

        if inner == "__version":
            self._version(chat_dir)
            return
        port = app_port(chat_dir)
        if port and self._proxy(port, rest or "/", self.path.partition("?")[2]):
            return
        if self.command not in ("GET", "HEAD"):
            self._send(405)
            return
        if os.path.isdir(target):
            index = os.path.join(target, "index.html")
            if os.path.isfile(index):
                self._serve_file(index)
            else:
                self._listing(target, inner, self.listing_prefix)
        else:
            self._serve_file(target)

    def _proxy(self, port, rest, query):
        """Pass the request to the chat's own server on 127.0.0.1:port. False if it doesn't answer (then the
        files are served from disk)."""
        target = rest + ("?" + query if query else "")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "connection", "referer", "origin", "accept-encoding", "content-length",
                                        "keep-alive", "upgrade", "te", "proxy-authorization")}
        headers["Host"] = "localhost:%d" % port
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=PROXY_TIMEOUT)
        try:
            conn.request(self.command, target, body=body, headers=headers)
            sock = conn.sock      # getresponse() drops conn's own reference when the answer ends at close
            resp = conn.getresponse()
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            # An event stream (EventSource) never ends, and an answer of unknown length may take as long as it
            # likes: both are passed on as they arrive. Waiting for the end, as below, left a page's live updates
            # dead in the Preview (2026-10-13: a rooms app that pushes messages over api/sse/...).
            stream = self.command != "HEAD" and (ctype == "text/event-stream" or (
                resp.status == 200 and resp.getheader("Content-Length") is None))
            data = b"" if stream else resp.read()
        except (OSError, http.client.HTTPException):
            conn.close()
            return False
        extra = {}
        for k, v in resp.getheaders():
            if k.lower() in DROP_RESPONSE_HEADERS:
                continue
            if k.lower() == "location":                      # keep redirects inside this chat's preview
                m = re.match(r"^(?:https?://(?:localhost|127\.0\.0\.1)(?::\d+)?)?/(.*)$", v)
                if m:
                    v = self.listing_prefix + m.group(1)
            extra[k] = v
        try:
            if stream:
                self._stream(sock, resp, extra)
            else:
                self._send(resp.status, body=data, content_type=resp.getheader("Content-Type"), extra_headers=extra,
                           proxied=True)
        finally:
            conn.close()
        return True

    def _stream(self, sock, resp, extra):
        """Pass the app's answer on piece by piece until the app ends it or the browser leaves. Never raises: the
        headers are out by then, so nothing else may be written to this connection."""
        self.close_connection = True
        try:
            self._head(resp.status, resp.getheader("Content-Type"), dict(extra, **{"X-Accel-Buffering": "no"}))
            sys.stderr.write("%s %s %d (app, stream)\n" % (self.command, mask_sig(self.path), resp.status))
            sys.stderr.flush()
            sock.settimeout(STREAM_IDLE_S)
            while True:
                piece = resp.read1(65536)
                if not piece:
                    break
                self.wfile.write(piece)
                self.wfile.flush()
        except (OSError, http.client.HTTPException, ValueError):
            pass
        finally:
            resp.close()

    def _version(self, chat_dir):
        version, files, entry = compute_version(chat_dir)
        # app: the port of the chat's own server the preview passes requests to right now (None = files from disk).
        # The Code pipe uses it to tell the app's address apart from other localhost addresses in a reply.
        payload = json.dumps(
            {"version": version, "files": files, "entry": entry, "app": app_port(chat_dir)}
        ).encode("utf-8")
        self._send(
            200,
            body=payload,
            content_type="application/json",
            extra_headers={"Access-Control-Allow-Origin": "*"},
        )

    def _serve_file(self, full):
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send(404)
            return
        content_type, _ = mimetypes.guess_type(full)
        if content_type is None:
            try:
                data.decode("utf-8")
                content_type = "text/plain; charset=utf-8"
            except UnicodeDecodeError:
                content_type = "application/octet-stream"
        self._send(200, body=data, content_type=content_type)

    def _listing(self, directory, inner, prefix):
        try:
            names = os.listdir(directory)
        except OSError:
            self._send(404)
            return
        entries = []
        for name in names:
            if name.startswith("."):
                continue
            full = os.path.join(directory, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            entries.append((name, os.path.isdir(full), st.st_size, st.st_mtime))
        entries.sort(key=lambda e: e[3], reverse=True)  # newest first
        body = listing_html(entries, inner, prefix).encode("utf-8")
        self._send(200, body=body, content_type="text/html; charset=utf-8")

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def _method_not_allowed(self):
        self._send(405)

    # Only GET and HEAD are allowed; everything else is 405. We bind a handful
    # of common verbs explicitly so they show up in dir(), and catch any other
    # method the handler dispatches (e.g. TRACE) through __getattr__ below.
    # Only the chat's own server may answer these (an app's API); for files on disk they are 405.
    def do_POST(self):
        self._route()

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_OPTIONS = do_POST

    def __getattr__(self, name):
        # BaseHTTPRequestHandler dispatches to do_<METHOD>; for any method we
        # did not bind explicitly, answer 405 instead of the default 501.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)


def main():
    if not os.environ.get("PREVIEW_SECRET"):
        sys.stderr.write("code-preview: PREVIEW_SECRET is not set; refusing to start\n")
        sys.exit(2)
    port = int(os.environ.get("PREVIEW_PORT", DEFAULT_PORT))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    # Startup banner goes to stdout so stderr stays a clean one-line-per-request stream.
    sys.stdout.write(
        "code-preview: serving %s on 0.0.0.0:%d\n" % (root_dir(), port)
    )
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
