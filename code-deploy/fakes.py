"""Fakes for code-deploy: a fake manager (code-features) and a fake Open WebUI.

They enforce auth and the exact wire formats the real services do, so a
forgiving fake cannot let the last project pass its tests and then fail against
the real thing.

* FakeManager -- Bearer-enforced, real op lifecycle (202 -> running -> ok|failed
  with remote), a files endpoint, 409 when busy, records every call.
* FakeOpenWebUI -- Bearer-enforced (one admin + one user token), signin, the two
  update endpoints (400 {"detail"} when content contains RAISE_ON_LOAD), and a
  temp sqlite db with function/tool/user tables for the read-only reads.
"""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# --------------------------------------------------------------------------
# Shared
# --------------------------------------------------------------------------
def _now():
    return int(time.time())


def _sha256_bytes(b):
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def _b64url_decode(s):
    if isinstance(s, str):
        s = s.encode("ascii")
    s += b"=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def verify_jwt(token, key):
    """Return the payload dict if `token` is a HS256 JWT signed with `key` and
    not expired, else None. (Fakes only; mirrors the service's signing.)"""
    try:
        h_b64, p_b64, s_b64 = token.split(".")
    except ValueError:
        return None
    signing_input = (h_b64 + "." + p_b64).encode("ascii")
    try:
        good = hmac.new(key, signing_input, hashlib.sha256).digest()
    except TypeError:
        return None
    try:
        if not hmac.compare_digest(good, _b64url_decode(s_b64)):
            return None
    except ValueError:
        return None
    try:
        payload = json.loads(_b64url_decode(p_b64))
    except ValueError:
        return None
    if payload.get("exp", 0) < _now():
        return None
    return payload


# --------------------------------------------------------------------------
# Fake manager
# --------------------------------------------------------------------------
class FakeManager:
    def __init__(self, token):
        self.token = token
        self.lock = threading.Lock()
        self.features = {}          # n -> feature view
        self.next_n = 1
        self.ops = {}               # op id -> op.json
        self.logs = {}              # op id -> log text
        self.busy = None            # op id running
        self.files = {}             # path -> bytes
        self.calls = []             # (method, path)
        self.fail_feature = None    # feature n whose op ends failed
        self.raise_on_load_paths = set()  # served files that fail to load on OWUI
        self.reject_feature = None  # feature n whose op action answers 400
        self.server = None
        self.thread = None

    # ---- sample / build helpers -----------------------------------------
    def add_feature(self, n=None, title="", summary="", status="ready",
                    chat_id=None, touched=None, remote_touched=None,
                    components=None, can_deploy=True, can_discard=True,
                    update_available=False, blocked=None, deployed=False,
                    rolled_back=False, ready=True):
        if n is None:
            n = self.next_n
            self.next_n += 1
        chat_id = chat_id or ("chat-%d" % n)
        comps = components or [{
            "name": "desktop-cli", "label": "Desktop tool", "ahead": 2,
            "commits": [{"sha": "a" * 40, "short": "a1b2c3d", "subject": "Add the thing",
                         "time": _now()}],
            "tests": {"ok": True, "summary": "46 passed", "at": _now()},
        }]
        view = {
            "n": n, "chat_id": chat_id, "dir": "/tmp/fake/features/%d" % n,
            "branch": "feature/%d" % n,
            "title": title, "summary": summary, "status": status,
            "created": _now() - 3600, "updated": _now() - 60,
            "ready_at": _now() - 60 if ready else None,
            "deployed_at": _now() if deployed else None,
            "rolled_back_at": _now() if rolled_back else None,
            "components": comps,
            "touched": touched or [comps[0]["name"]],
            "remote_touched": remote_touched or [],
            "update_available": update_available,
            "can_deploy": can_deploy,
            "can_rollback": deployed,
            "can_discard": can_discard,
            "blocked": blocked,
            "last_op": None,
        }
        self.features[n] = view
        return view

    def seed_demo(self):
        """Six features (one in progress, one draft, one ready touching the
        page script, one deployed, one deployed with an update waiting, one
        rolled back), plus a discarded one; a few ops; one drift entry."""
        self.add_feature(1, title="Faster preview reload", status="draft",
                         summary="Adds debounced reloads to the preview server.",
                         components=None)
        self.add_feature(2, title="Desktop scroll-to", status="ready",
                         summary="Scrolls the desktop view to a given element.",
                         remote_touched=[], deployed=False)
        self.add_feature(3, title="Code-mode page script", status="ready",
                         summary="Adds the Features panel to the loader.",
                         remote_touched=["owui/code-toggle.js"],
                         components=[{
                             "name": "code-mode", "label": "Open WebUI side", "ahead": 1,
                             "commits": [{"sha": "b" * 40, "short": "b2c3d4e",
                                          "subject": "Wire the Features panel in",
                                          "time": _now()}],
                             "tests": {"ok": True, "summary": "ok", "at": _now()}}])
        self.add_feature(4, title="Bug fixer triage", status="deployed",
                         summary="Triage route for low-confidence fixes.")
        self.add_feature(5, title="Preview theme toggle", status="deployed",
                         summary="A dark/light toggle in the preview header.",
                         update_available=True, can_deploy=False)
        self.add_feature(6, title="Rolled-back experiment", status="rolled_back",
                         summary="Tried something; rolled it back.")
        self.add_feature(7, title="Discarded idea", status="discarded",
                         summary="An idea we abandoned.")
        # Sample files + a drift entry.
        self.files["owui/code_mode.py"] = (
            '"""\ntitle: Code Mode\ndescription: The Code pipe.\n'
            'version: 0.7.0\n"""\n'
            "print('code mode v1')\n")
        self.files["owui/code-toggle.js"] = (
            "(function(){if(window.__ct)return;window.__ct={};})()\n")
        self.files["owui/desktop_code_tool.py"] = (
            '"""\ntitle: Desktop and Code\ndescription: use the desktop, '
            'switch to Code.\nversion: 0.2.0\n"""\n'
            "print('tool v1')\n")
        self.drift_entry = {"component": "code-mode", "to": "function code_mode",
                            "expected": "0" * 64, "actual": "1" * 64}

    # ---- op lifecycle ----------------------------------------------------
    def _make_op(self, kind, feature, title, changed, failed=False):
        op_id = "20261006-%04x-%04x" % (_now() & 0xffff,
                                        int.from_bytes(os.urandom(2), "little") & 0xffff)
        op = {
            "id": op_id, "kind": kind, "feature": feature, "title": title,
            "status": "running", "error": None, "started": _now(), "ended": None,
            "steps": [{"name": "Stage " + str(feature), "status": "running",
                       "started": _now(), "ended": None, "detail": None}],
            "commits": {}, "installed": [], "restarted": [],
            "remote": {"rev": "c" * 40, "changed": changed},
        }
        log = "[%s] op %s: %s feature #%s\n" % (
            time.strftime("%H:%M:%S"), op_id, kind, feature)
        self.ops[op_id] = op
        self.logs[op_id] = log
        with self.lock:
            self.busy = op_id
        # The op becomes ok (or failed) after a short delay; the service polls.
        t = threading.Thread(target=self._age, args=(op_id, failed), daemon=True)
        t.start()
        return op_id

    def _age(self, op_id, failed):
        time.sleep(0.5)
        with self.lock:
            op = self.ops[op_id]
            op["steps"][0]["status"] = "ok"
            op["steps"][0]["ended"] = _now()
            if failed or (self.fail_feature is not None
                          and op.get("feature") == self.fail_feature):
                op["status"] = "failed"
                op["error"] = "a component test failed on the manager"
            else:
                op["status"] = "ok"
            op["ended"] = _now()
            self.busy = None

    # ---- handler ---------------------------------------------------------
    def handler_class(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = (json.dumps(obj) + "\n").encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _raw(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _auth(self):
                hdr = self.headers.get("Authorization", "")
                return hdr == "Bearer " + fake.token

            def _body(self):
                n = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw) if raw else {}
                except ValueError:
                    return {}

            def _record(self, method, path):
                fake.calls.append((method, path))

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def _dispatch(self, method):
                self._record(method, self.path)
                parsed = urlparse(self.path)
                path = parsed.path
                if not self._auth():
                    return self._send(401, {"error": "Unauthorized"})
                query = parse_qs(parsed.query)
                if path == "/api/health":
                    return self._send(200, {"ok": True, "busy": fake.busy})
                if path == "/api/features" and method == "GET":
                    feats = list(fake.features.values())
                    feats.sort(key=lambda f: f["n"], reverse=True)
                    return self._send(200, {"features": feats})
                if path == "/api/features" and method == "POST":
                    b = self._body()
                    v = fake.add_feature(chat_id=b.get("chat_id", "chat"))
                    return self._send(201, {"feature": v})
                if path == "/api/sync" and method == "POST":
                    return self._op_action("sync", None)
                parts = [p for p in path.strip("/").split("/") if p != ""]
                # REST routes use "/api/..." segments; match on the rest.
                if parts[:1] == ["api"]:
                    parts = parts[1:]
                if parts[:1] == ["features"]:
                    try:
                        n = int(parts[1])
                    except (ValueError, IndexError):
                        return self._send(404, {"error": "No such feature"})
                    if len(parts) == 2:
                        v = fake.features.get(n)
                        if not v:
                            return self._send(404, {"error": "No such feature"})
                        return self._send(200, {"feature": v})
                    if parts[2] == "diff":
                        v = fake.features.get(n)
                        if not v:
                            return self._send(404, {"error": "No such feature"})
                        files = [{"component": "code-mode", "file": "owui/code_mode.py",
                                  "additions": 3, "deletions": 1, "patch": "+++ a\n@@ -1 +1 @@\n-x\n+y"}]
                        return self._send(200, {"files": files})
                    if len(parts) == 3 and parts[2] in ("deploy", "rollback", "discard"):
                        return self._op_action(parts[2], n)
                    return self._send(404, {"error": "Not found"})
                if len(parts) == 2 and parts[:1] == ["ops"]:
                    op = fake.ops.get(parts[1])
                    if not op:
                        return self._send(404, {"error": "No such op"})
                    return self._send(200, {"op": op, "log": fake.logs.get(parts[1], "")})
                if parts[:1] == ["ops"]:
                    limit = int((query.get("limit") or ["30"])[0])
                    ops = list(fake.ops.values())
                    ops.sort(key=lambda o: o.get("started", 0), reverse=True)
                    return self._send(200, {"ops": ops[:limit]})
                if parts[:2] == ["files", "code-mode"] and len(parts) >= 3:
                    rev = parts[2]
                    p = "/".join(parts[3:]) if len(parts) > 3 else ""
                    if ".." in p or not p:
                        return self._send(404, {"error": "Not found"})
                    if p in fake.files:
                        data = fake.files[p]
                        if p in fake.raise_on_load_paths:
                            data = "RAISE_ON_LOAD -- this content will not load\n" + data
                        data = data.encode("utf-8") if isinstance(data, str) else data
                        return self._raw(200, data)
                    return self._send(404, {"error": "Not found"})
                if path == "/api/drift":
                    return self._send(200, {"drift": [fake.drift_entry]})
                if path.startswith("/api/checklist/") and method == "GET":
                    chat = path[len("/api/checklist/"):]
                    if chat == "chat-down":
                        return self._send(500, {"error": "boom"})
                    if chat == "chat-check":
                        return self._send(200, {"exists": True, "kind": "feature", "feature": 3, "done": 1,
                                                "total": 2, "now": 1, "items": [], "open": [], "title": "t"})
                    return self._send(200, {"exists": False})
                return self._send(404, {"error": "Not found"})

            def _op_action(self, kind, n):
                if fake.reject_feature is not None and n == fake.reject_feature:
                    return self._send(400, {"error": "not ready to deploy"})
                with fake.lock:
                    if fake.busy:
                        return self._send(409, {"error": "busy", "op": fake.busy})
                v = fake.features.get(n) if n is not None else None
                title = (v or {}).get("title", "") if v else ""
                changed = []
                if (v or {}).get("remote_touched"):
                    changed = list((v or {}).get("remote_touched"))
                elif kind == "sync":
                    changed = list(fake.files.keys())
                op_id = fake._make_op(kind, n, title, changed, failed=False)
                # A feature flagged in fail_feature ends its op failed; a served
                # file listed in raise_on_load_paths fails when installed.
                return self._send(202, {"op": op_id})

        return H

    def start(self, port):
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self.handler_class())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()


# --------------------------------------------------------------------------
# Fake Open WebUI
# --------------------------------------------------------------------------
ADMIN_TOKEN = "owui-admin-token"
USER_TOKEN = "owui-user-token"
USER_ID = "user-uid"
ADMIN_EMAIL = "admin@owui.local"
ADMIN_PASS = "secret-pass"
# Sign in with this email "succeeds" at the signin endpoint but OWUI's
# /api/v1/auths/ rejects the returned token. Used to check that
# /auth/signin refuses to set a cookie when verify_token fails on the token.
INVALID_SIGNIN_EMAIL = "invalid@owui.local"


class FakeOpenWebUI:
    def __init__(self, function_content="print('code mode v1')",
                 tool_content="print('tool v1')", admin_id="admin-uid",
                 key_file=None):
        self.func_content = function_content
        self.tool_content = tool_content
        self.admin_id = admin_id
        self.key_file = key_file
        self.calls = []
        self.update_calls = []   # (path, body) for the update endpoints
        self.lock = threading.Lock()
        self.dir = tempfile.mkdtemp(prefix="fake-owui-")
        self.db = os.path.join(self.dir, "webui.db")
        self._init_db()
        self.server = None
        self.thread = None

    def _init_db(self):
        conn = sqlite3.connect(self.db)
        conn.execute("create table function (id text, name text, content text)")
        conn.execute("create table tool (id text, name text, content text)")
        conn.execute("create table user (id text, email text, name text, role text, created_at real)")
        conn.execute("create table chat (id text, user_id text, chat text, updated_at real)")
        conn.execute("insert into function values ('code_mode','Code Mode',?)",
                     (self.func_content,))
        conn.execute("insert into tool values ('desktop_code','Desktop and Code',?)",
                     (self.tool_content,))
        conn.execute("insert into user values (?,?,?,?,?)",
                     (self.admin_id, ADMIN_EMAIL, "Admin User", "admin", 0))
        conn.commit()
        conn.close()

    def add_chat(self, chat_id, user_id, messages=None):
        """A chat row like Open WebUI's: messages = {id: {...}} for history.messages."""
        with self.lock:
            conn = sqlite3.connect(self.db)
            conn.execute("delete from chat where id = ?", (chat_id,))
            conn.execute("insert into chat values (?,?,?,?)",
                         (chat_id, user_id, json.dumps({"history": {"messages": messages or {}}}), time.time()))
            conn.commit()
            conn.close()

    def _read(self, sql, args=()):
        conn = sqlite3.connect("file:%s?mode=ro" % self.db, uri=True)
        try:
            row = conn.execute(sql, args).fetchone()
        finally:
            conn.close()
        return row

    def handler_class(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = (json.dumps(obj) + "\n").encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _bearer_role(self):
                hdr = self.headers.get("Authorization", "")
                if not hdr.startswith("Bearer "):
                    return None
                tok = hdr[len("Bearer "):]
                if tok == ADMIN_TOKEN:
                    return "admin"
                if tok == USER_TOKEN:
                    return "user"
                if fake.key_file:
                    payload = verify_jwt(tok, open(fake.key_file).read().strip().encode())
                    if payload is not None:
                        return "admin"
                return None

            def _body(self):
                n = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw) if raw else {}
                except ValueError:
                    return {}

            def _record(self, method, path):
                fake.calls.append((method, path))

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

            def _dispatch(self, method):
                self._record(method, self.path)
                parsed = urlparse(self.path)
                path = parsed.path
                role = self._bearer_role()
                if path == "/api/v1/auths/":
                    if role != "admin" and role != "user":
                        return self._send(401, {"detail": "invalid token"})
                    return self._send(200, {"id": fake.admin_id if role == "admin" else USER_ID,
                                            "email": ADMIN_EMAIL, "name": "Admin User", "role": role})
                if path == "/api/v1/auths/signin":
                    b = self._body()
                    if b.get("email") == ADMIN_EMAIL and b.get("password") == ADMIN_PASS:
                        return self._send(200, {"token": ADMIN_TOKEN, "id": fake.admin_id,
                                                "email": ADMIN_EMAIL, "name": "Admin User",
                                                "role": "admin"})
                    if b.get("email") == INVALID_SIGNIN_EMAIL:
                        return self._send(200, {"token": "bogus-token",
                                                "id": fake.admin_id,
                                                "email": INVALID_SIGNIN_EMAIL,
                                                "name": "Nobody", "role": "user"})
                    return self._send(400, {"detail": "invalid credentials"})
                if path == "/api/v1/functions/id/code_mode/update":
                    if role != "admin":
                        return self._send(401, {"detail": "admin only"})
                    b = self._body()
                    content = b.get("content", "")
                    if "RAISE_ON_LOAD" in content:
                        return self._send(400, {"detail": "import failed: boom"})
                    fake.update_calls.append({"path": path, "body": b,
                                              "auth": self.headers.get("Authorization")})
                    with fake.lock:
                        conn = sqlite3.connect(fake.db)
                        conn.execute("update function set content=?, name=? where id='code_mode'",
                                     (content, b.get("name", "Code Mode")))
                        conn.commit()
                        conn.close()
                    return self._send(200, {"id": "code_mode", "name": b.get("name"),
                                            "content": content})
                if path == "/api/v1/tools/id/desktop_code/update":
                    if role != "admin":
                        return self._send(401, {"detail": "admin only"})
                    b = self._body()
                    content = b.get("content", "")
                    if "RAISE_ON_LOAD" in content:
                        return self._send(400, {"detail": "import failed: tool boom"})
                    fake.update_calls.append({"path": path, "body": b,
                                              "auth": self.headers.get("Authorization")})
                    with fake.lock:
                        conn = sqlite3.connect(fake.db)
                        conn.execute("update tool set content=?, name=? where id='desktop_code'",
                                     (content, b.get("name", "Desktop and Code")))
                        conn.commit()
                        conn.close()
                    return self._send(200, {"id": "desktop_code", "name": b.get("name"),
                                            "content": content})
                return self._send(404, {"detail": "not found"})

        return H

    def start(self, port):
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self.handler_class())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()


# --------------------------------------------------------------------------
# Loader targets + key files
# --------------------------------------------------------------------------
def make_loader_targets():
    d = tempfile.mkdtemp(prefix="fake-loaders-")
    p = os.path.join(d, "loader.js")
    with open(p, "w") as fh:
        fh.write("(function(){window.__ct={};})();\n")
    return [p]


def make_secret_key():
    d = tempfile.mkdtemp(prefix="fake-key-")
    p = os.path.join(d, "secret_key")
    with open(p, "w") as fh:
        fh.write("a-very-secret-signing-key\n")
    return p
