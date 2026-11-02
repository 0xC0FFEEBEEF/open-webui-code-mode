#!/usr/bin/env python3
"""code-previews: the address a person can open for a Code chat's app.

    http://<the Open WebUI host>/previews/<chat id>/<path>

A Code chat's app runs on the coding machine (VM 129). The agent calls it "http://localhost:8000", which means
nothing in the user's browser; the Code pipe writes this address in its place (2026-10-13, the user asked for
"something like cryoai/previews/chatid").

It only forwards. Open WebUI sets its sign-in cookie (`token`) for this host, whatever the port, so the visitor's
browser sends it here too. That sign-in is handed to Open WebUI, where the Code pipe answers with the chat's signed
preview link if, and only if, the visitor owns the chat; the browser is then sent on to the preview server.

No project page is ever served from this address. Pages a model wrote must not run on the host the Open WebUI
sign-in belongs to: the cookie counts for every port of the host and Open WebUI accepts it on its API, so such a
page could act as the signed-in user.

Settings (environment): PREVIEWS_PORT (80), OWUI_URL (http://127.0.0.1:8080, how this service reaches Open WebUI),
OWUI_PORT (8080, for the "sign in" link the visitor gets), CODE_MODEL (the Code pipe's entry in Open WebUI).
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PREVIEWS_PORT", "80"))
OWUI_URL = os.environ.get("OWUI_URL", "http://127.0.0.1:8080").rstrip("/")
OWUI_PORT = os.environ.get("OWUI_PORT", "8080")
CODE_MODEL = os.environ.get("CODE_MODEL", "code_mode.ornith-1.5-35b-a3b")
ASK_TIMEOUT = 10
ROUTE_RE = re.compile(r"^/previews/([A-Za-z0-9_-]{8,64})(?:/(.*))?$")
PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>body{font:15px/1.5 system-ui,sans-serif;margin:48px;color:#e5e5e5;background:#171717}"
        "a{color:#4f8cff}@media (prefers-color-scheme: light){body{color:#222;background:#fff}}</style>"
        "<p><b>%s</b></p><p>%s</p>")


def where_is(chat_id, token):
    """Ask the Code pipe, as the visitor, for the chat's preview link.
    -> ("ok", url) | ("signin", None) | ("none", None) | ("down", None)"""
    # code_github rides along for a Code pipe older than 0.19.0 (after a roll back): it answers "unknown GitHub op"
    # and stops there. Without it such a pipe would take this for a chat message and start a coding turn.
    body = json.dumps({"model": CODE_MODEL, "stream": False, "messages": [{"role": "user", "content": "preview"}],
                       "code_preview": {"chat": chat_id}, "code_github": {"op": "preview"}}).encode()
    req = urllib.request.Request(OWUI_URL + "/api/chat/completions", data=body, method="POST", headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=ASK_TIMEOUT) as r:
            got = json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return ("signin" if e.code in (401, 403) else "none"), None
    except (OSError, ValueError):
        return "down", None
    url = got.get("url") if isinstance(got, dict) and got.get("ok") else None
    if isinstance(url, str) and re.match(r"^https?://[^\s/]+/\S*$", url):
        return "ok", url if url.endswith("/") else url + "/"
    return "none", None


class Handler(BaseHTTPRequestHandler):
    server_version = "code-previews"
    sys_version = ""

    def log_message(self, fmt, *args):
        return

    def _token(self):
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
        except CookieError:
            return ""
        return jar["token"].value if "token" in jar else ""

    def _out(self, status, title="", text="", location=None):
        body = (PAGE % (html.escape(title), html.escape(title), text)).encode() if title else b""
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)
        # the path names the chat, nothing more: never the sign-in, never the signed link
        sys.stderr.write("%s %s %d\n" % (self.command, self.path.split("?", 1)[0][:120], status))
        sys.stderr.flush()

    def _sign_in(self):
        host = re.sub(r":\d+$", "", self.headers.get("Host") or "")
        link = ""
        if re.fullmatch(r"[A-Za-z0-9.-]+", host):
            owui = "http://%s:%s/" % (host, OWUI_PORT)
            link = ' at <a href="%s">%s</a>' % (owui, owui)
        self._out(401, "Sign in to Open WebUI first",
                  "This address shows a Code chat's app to the person the chat belongs to. Sign in to Open WebUI%s "
                  "in this browser, then open this address again." % link)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        m = ROUTE_RE.match(path)
        if not m:
            return self._out(404, "Nothing here", "Previews are at /previews/&lt;chat id&gt;/.")
        token = self._token()
        if not token:
            return self._sign_in()
        state, url = where_is(m.group(1), token)
        if state == "ok":
            return self._out(302, location=url + (m.group(2) or "") + ("?" + query if query else ""))
        if state == "signin":
            return self._sign_in()
        if state == "down":
            return self._out(502, "Open WebUI did not answer", "Try again in a moment.")
        self._out(404, "No preview for this chat", "There is no Code chat with this id on your account.")

    do_HEAD = do_GET

    def _no(self):
        self._out(405, "Only GET works here", "This address only forwards to a preview.")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sys.stdout.write("code-previews: /previews/<chat id>/ on 0.0.0.0:%d, asking %s\n" % (PORT, OWUI_URL))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
