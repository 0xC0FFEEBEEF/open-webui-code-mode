"""Tests for code-fixer.py.

Run with:  .venv/bin/pytest -q test_fixer.py

A fake opencode server runs in a thread (session create, prompt, a scripted
/api/event stream, permission/form replies, session list/messages) and a fake
code-mode-logs / systemctl via FIXER_LOGS_CMD / FIXER_RESTART_CMD. The real
code-fixer.py is spawned as a subprocess against that fake.
"""

import base64
import http.server
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXER_SRC = os.path.join(HERE, "code-fixer.py")
REPORT_SRC = os.path.join(HERE, "report-bug")
SECRET = "testsecret"
TOKEN = "testtoken"
FRAME = "http://cryoai:8080 http://cryoai:8080"

# A scripted fixer run: reasoning, text, two tools, one refused permission,
# one answered form, then success.
SCRIPT = [
    {"type": "session.reasoning.started", "data": {}},
    {"type": "session.reasoning.ended", "data": {"text": "let me look"}},
    {"type": "session.text.delta", "data": {"delta": "hi"}},
    {"type": "session.text.ended", "data": {"text": "I'll check the file."}},
    {"type": "session.tool.input.started", "data": {"id": "t1", "name": "shell"}},
    {"type": "session.tool.called", "data": {"id": "t1", "input": {"command": "ls -la"}}},
    {"type": "session.tool.success", "data": {"id": "t1",
          "content": [{"type": "text", "text": "index.html"}]}},
    {"type": "permission.asked", "data": {"id": "p1", "action": "shell",
          "resources": ["shell: rm -rf build"]}},
    {"type": "form.created", "data": {"form": {"id": "f1",
          "fields": [{"key": "mode", "name": "Mode"}]}}},
    {"type": "session.tool.input.started", "data": {"id": "t2", "name": "edit"}},
    {"type": "session.tool.called", "data": {"id": "t2",
          "input": {"path": "index.html", "text": "<!-- fixed -->"}}},
    {"type": "session.tool.success", "data": {"id": "t2",
          "content": [{"type": "text", "text": "saved"}]}},
    {"type": "session.execution.succeeded", "data": {}},
]

# The opencode "run finished" event types, mirrored here so the fake can omit
# them when asked to run forever (cap test).
FAKE_END_TYPES = {"session.execution.succeeded",
                  "session.execution.failed",
                  "session.execution.interrupted"}


def _opencode_basic():
    """Expected 'Authorization: Basic ...' header the fake requires. It must
    match the password in the fixer credentials file (FIXER_OPENCODE_CREDENTIALS)
    so the suite catches a fixer that doesn't send opencode's Basic auth."""
    path = os.environ.get("FIXER_OPENCODE_CREDENTIALS", "")
    try:
        with open(path) as fh:
            pw = json.load(fh).get("password", "")
    except OSError:
        pw = ""
    token = base64.b64encode(("opencode:%s" % pw).encode()).decode()
    return "Basic " + token


# --------------------------------------------------------------------------- #
# Fake opencode server
# --------------------------------------------------------------------------- #

class FakeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.session_id = None
        self.prompt_event = threading.Event()
        self.interrupted = threading.Event()   # POST .../interrupt: the run ends with session.execution.interrupted
        self.permission_replies = []
        self.created = []          # bodies of POST /api/session
        self.router_models = []    # what the gateway's /v1/models lists
        self.router_auth = []      # Authorization header each /v1/models request carried
        self.form_replies = []
        self._n = 0


class FakeOpencodeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _require_auth(self):
        """Reject any request that isn't opencode's Basic auth, so a fixer that
        forgets to send it gets 401 (and the run fails to start)."""
        got = self.headers.get("Authorization", "")
        if got != _opencode_basic():
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":  # stands in for the slerm gateway (no auth there)
            self.server.state.router_auth.append(self.headers.get("Authorization"))
            return self._json(200, {"object": "list", "data": self.server.state.router_models})
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, parsed.query
        if path == "/api/event":
            return self._sse()
        if path == "/api/session":
            return self._list_sessions(query)
        if path.startswith("/api/session/") and path.endswith("/message"):
            return self._messages()
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        data = json.loads(body) if body else {}
        if path == "/api/session":
            return self._create_session(data)
        if path.endswith("/prompt"):
            return self._prompt(data)
        if re.search(r"/permission/[^/]+/reply$", path):
            self.server.state.permission_replies.append(data)
            return self._json(200, {"data": {"ok": True}})
        if re.search(r"/form/[^/]+/reply$", path):
            self.server.state.form_replies.append(data)
            return self._json(200, {"data": {"ok": True}})
        if path.endswith("/interrupt"):
            self.server.state.interrupted.set()
            return self._json(200, {"interrupted": True})
        self._json(404, {"error": "not found"})

    # -- handlers ---------------------------------------------------------- #
    def _create_session(self, data):
        with self.server.state.lock:
            self.server.state.created.append(data)
            self.server.state._n += 1
            self.server.state.session_id = "ses_fake_%d" % self.server.state._n
            self.server.state.prompt_event.clear()
            self.server.state.interrupted.clear()
        self._json(200, {"data": {"id": self.server.state.session_id}})

    def _prompt(self, data):
        text = data.get("text", "")
        with self.server.state.lock:
            self.server.state.prompt_text = text
        self.server.state.prompt_event.set()
        iid = re.search(r"(/\S+/fixes/[0-9A-Za-z_-]+)", text)
        if iid and os.environ.get("FAKE_WRITE_RESULT", "1") != "0":
            result = {
                "verdict": os.environ.get("FAKE_VERDICT", "fixed"),
                "summary": "simulated fix of the bug",
                "root_cause": "simulated root cause",
                "changed": [{"path": "index.html", "why": "fixed the link"}],
                "tests": "1 test passed",
                "restart": [x for x in os.environ.get("FAKE_RESTART", "").split(",")
                            if x],
                "for_the_chat": "retry the failed command",
            }
            dirpath = iid.group(1)
            try:
                os.makedirs(dirpath, exist_ok=True)
                with open(os.path.join(dirpath, "result.json"), "w") as fh:
                    json.dump(result, fh)
            except OSError:
                pass
        self._json(200, {"data": {"id": "msg_fake"}})

    def _list_sessions(self, query):
        params = urllib.parse.parse_qs(query)
        directory = params.get("directory", [""])[0]
        chat_id = directory.strip("/").split("/")[-1]
        if chat_id.startswith("nosession"):  # a chat with no Code turns: no folder, so opencode lists nothing
            return self._json(200, {"data": []})
        self._json(200, {"data": [{"id": "ses_chat",
                                   "metadata": {"owui_chat_id": chat_id}}]})

    def _messages(self):
        self._json(200, {"data": [
            {"id": "msg_1", "type": "assistant", "content": [
                {"type": "reasoning", "text": "let me look at the file"},
                {"type": "tool", "name": "shell", "state": {
                    "status": "completed",
                    "input": {"command": "cat index.html"},
                    "content": [{"type": "text",
                                 "text": "<html><head></head><body>hi</body>"
                                         "</html>"}]}},
            ]},
            {"id": "msg_2", "type": "assistant", "content": [
                {"type": "text", "text": "Found the problem and fixed it."},
            ]},
        ]})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b'data: {"type": "server.connected", "data": {}}\n')
        self.wfile.flush()
        if not self.server.state.prompt_event.wait(30):
            return
        sid = self.server.state.session_id
        delay = float(os.environ.get("FAKE_DELAY", "0.01"))
        never_end = os.environ.get("FAKE_MAX_NEVER") == "1"
        silence = float(os.environ.get("FAKE_SILENCE", "0"))
        for i, ev in enumerate(SCRIPT):
            if never_end and ev["type"] in FAKE_END_TYPES:
                continue
            d = dict(ev.get("data", {}))
            d["sessionID"] = sid
            pkt = {"type": ev["type"], "data": d}
            self.wfile.write(("data: %s\n" % json.dumps(pkt)).encode())
            self.wfile.flush()
            if i == 0 and silence:
                time.sleep(silence)   # a genuine mid-script gap
            time.sleep(delay)
            if self.server.state.interrupted.is_set():
                break
        if never_end or self.server.state.interrupted.is_set():
            # a run that never ends by itself: it ends when interrupted (Cancel, the time cap)
            while not self.server.state.interrupted.wait(0.1):
                pass
            pkt = {"type": "session.execution.interrupted", "data": {"sessionID": sid}}
            try:
                self.wfile.write(("data: %s\n" % json.dumps(pkt)).encode())
                self.wfile.flush()
            except OSError:
                pass


class FakeOpencode:
    def __init__(self):
        self.state = FakeState()
        self.port = _free_port()
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), FakeOpencodeHandler)
        self.server.state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------- #
# code-fixer service runner (subprocess)
# --------------------------------------------------------------------------- #

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FixerServer:
    def __init__(self, tmp_path, fake, port=None, env=None):
        env = env or {}
        self.env = {
            "FIXER_SECRET": SECRET,
            "FIXER_TOKEN": TOKEN,
            "FIXER_PORT": str(port or _free_port()),
            "FIXER_DIR": str(tmp_path / "fixes"),
            "FIXER_PROJECTS": str(tmp_path / "projects"),
            "FIXER_MODEL": "slerm/ornith-1.5-35b-a3b",
            "FIXER_ROUTER_MODELS": "http://127.0.0.1:%d/v1/models" % fake.port,
            "FIXER_OPENCODE_URL": "http://127.0.0.1:%d" % fake.port,
            "FIXER_EVENT_TIMEOUT": "30",
            "FIXER_RESTART_LOG": str(tmp_path / "restart.log"),
            # Default restart needs sudo (unavailable in tests); a harmless
            # no-op that exits 0 so restart_outcome reads "ok (exit 0)".
            "FIXER_RESTART_CMD": "true",
            # The fixer reads opencode's Basic-auth password from this file.
            "FIXER_OPENCODE_CREDENTIALS": str(tmp_path / "service.json"),
            # Handing a give-up verdict to Claude is off here: tests that want it turn it on.
            "FIXER_ESCALATE": "0",
            "FIXER_WATCHDOG_S": "0.3",
        }
        # opencode credentials file: {"password": "<pw>"}. The in-process fake
        # requires the same Basic auth, so a fixer that doesn't send it gets 401.
        (tmp_path / "service.json").write_text(
            json.dumps({"password": "fakeopencodepw"}))
        # fake logs: a file we pre-populate
        self.fakelogs = tmp_path / "fakelogs.txt"
        self.fakelogs.write_text("===== fake code-mode-logs =====\nlog line 1\n"
                                 "log line 2\n")
        self.env["FIXER_LOGS_CMD"] = "cat %s" % self.fakelogs
        for k, v in env.items():
            self.env[k] = str(v)
        self.proc = None
        self.errf = None
        self._saved_env = {}

    @property
    def port(self):
        return int(self.env["FIXER_PORT"])

    def start(self):
        os.makedirs(self.env["FIXER_DIR"], exist_ok=True)
        os.makedirs(self.env["FIXER_PROJECTS"], exist_ok=True)
        # The fake opencode server runs in *this* process and reads its knobs
        # (FAKE_RESTART, FAKE_MAX_NEVER, ...) from os.environ, so the overrides
        # the test passes here must be visible to us, not only to the service
        # subprocess. Export them now and restore on stop().
        for key, value in self.env.items():
            if key.startswith("FAKE_") \
                    or key == "FIXER_OPENCODE_CREDENTIALS":
                self._saved_env[key] = os.environ.get(key)
                os.environ[key] = value
        self.errf = open(os.path.join(self.env["FIXER_DIR"], ".service.log"), "w")
        # FIXER_PROJECTS must exist before spawn (capture_diff lists it)
        self.proc = subprocess.Popen(
            [sys.executable, FIXER_SRC], env=self.env,
            stdout=subprocess.DEVNULL, stderr=self.errf)
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("service exited early:\n" + self.read_log())
            try:
                socket.create_connection(("127.0.0.1", int(self.env["FIXER_PORT"])),
                                         timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("service did not start:\n" + self.read_log())

    def read_log(self):
        try:
            return open(self.env["FIXER_DIR"] + "/.service.log").read()
        except OSError:
            return ""

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.errf:
            self.errf.close()
        for key, orig in self._saved_env.items():
            if orig is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig
        self._saved_env = {}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def sig_for(incident_id):
    return hmac.new(SECRET.encode(), ("f:" + incident_id).encode(),
                    hashlib.sha256).hexdigest()[:32]




def log_sig():
    return hmac.new(SECRET.encode(), b"log", hashlib.sha256).hexdigest()[:32]


def _body(raw):
    """Decode an HTTP response body: JSON when the server sent JSON, otherwise
    the raw text. The viewer, diff and log endpoints return HTML/plain text,
    not JSON, so the API helper must not choke on them."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def api(port, method, path, body=None, bearer=None, timeout=30):
    url = "http://127.0.0.1:%d%s" % (port, path)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, \
                dict((k.lower(), v) for k, v in resp.getheaders()), \
                _body(raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, \
            dict((k.lower(), v) for k, v in e.headers.items()), \
            _body(raw)


def _parse_sse_body(body):
    """Parse an SSE stream the way a browser's EventSource does: split on blank
    lines, then read the 'data:' line of each block."""
    events = []
    for block in body.split("\n\n"):
        for line in block.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                events.append(json.loads(payload))
            except ValueError:
                pass
    return events


def read_sse(port, path, timeout=20):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    # A browser only dispatches an event at a blank line, so the stream must
    # end with one; assert it so bad framing fails loudly instead of silently
    # dropping the final event.
    assert body.endswith("\n\n"), "SSE stream must end with a blank line"
    return _parse_sse_body(body)


def wait_done(port, incident_id, timeout=40):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            st, _, rec = api(port, "GET",
                             "/api/incidents/%s" % incident_id, bearer=TOKEN)
            if st == 200 and rec.get("status") == "done":
                return rec
            last = rec
        except urllib.error.URLError:
            pass
        time.sleep(0.2)
    return last


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake():
    f = FakeOpencode()
    f.start()
    yield f
    f.stop()


@pytest.fixture
def srv(tmp_path, fake):
    s = FixerServer(tmp_path, fake)
    s.start()
    yield s
    s.stop()


def post_report(port, chat_id="chat-1", source="code", title="T", details="D",
                body_extra=None):
    body = {"chat_id": chat_id, "source": source, "reporter_model": "test agent",
            "title": title, "details": details,
            "context": [{"role": "user", "content": "the page is broken"}]}
    if body_extra:
        body.update(body_extra)
    st, hdr, resp = api(port, "POST", "/api/report", body=body, bearer=TOKEN)
    return st, hdr, resp


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #

def test_report_requires_bearer(fake, srv):
    st, _, _ = api(srv.port, "POST", "/api/report", body={}, bearer="wrong")
    assert st == 401
    st, _, _ = api(srv.port, "POST", "/api/report", body={})
    assert st == 401


def test_api_incident_requires_bearer(fake, srv):
    post_report(srv.port)
    st, _, _ = api(srv.port, "GET", "/api/incidents/x")
    assert st == 401


def test_opencode_basic_auth_required(fake, tmp_path):
    # The fake opencode server requires opencode's Basic auth, matching the
    # password in the fixer credentials file. A request without it is 401;
    # with the right password it is 200. This is what makes the suite catch a
    # fixer that forgets to send opencode's Basic auth.
    creds = tmp_path / "service.json"
    creds.write_text(json.dumps({"password": "fakeopencodepw"}))
    saved = os.environ.get("FIXER_OPENCODE_CREDENTIALS")
    os.environ["FIXER_OPENCODE_CREDENTIALS"] = str(creds)
    try:
        port = fake.port
        no_auth_url = "http://127.0.0.1:%d/api/session" % port
        try:
            urllib.request.urlopen(no_auth_url, timeout=5)
            got = 200
        except urllib.error.HTTPError as exc:
            got = exc.code
        assert got == 401, "expected 401 without opencode Basic auth"
        pw = base64.b64encode(b"opencode:fakeopencodepw").decode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/session" % port,
            headers={"Authorization": "Basic " + pw}, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
    finally:
        if saved is None:
            os.environ.pop("FIXER_OPENCODE_CREDENTIALS", None)
        else:
            os.environ["FIXER_OPENCODE_CREDENTIALS"] = saved


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def test_bad_chat_id(fake, srv):
    st, _, _ = post_report(srv.port, chat_id="bad id!")
    assert st == 400


def test_title_too_long(fake, srv):
    st, _, _ = post_report(srv.port, title="x" * 201)
    assert st == 400


def test_details_too_long(fake, srv):
    st, _, _ = post_report(srv.port, details="y" * 20001)
    assert st == 400


def test_bad_source(fake, srv):
    st, _, _ = post_report(srv.port, source="weird")
    assert st == 400


def test_report_credentials_unreadable(fake, srv, tmp_path):
    # Point the fixer at a credentials file that does not exist. The incident
    # must be refused (could_not_fix) with a credentials message -- it must
    # NOT fall back to the insecure default 'password' and hit the fake.
    missing = str(tmp_path / "nope" / "service.json")
    s = FixerServer(tmp_path, fake, env={"FIXER_OPENCODE_CREDENTIALS": missing})
    s.start()
    try:
        st, _, resp = post_report(s.port)
        assert st == 201
        rec = wait_done(s.port, resp["id"])
        assert rec["status"] == "done"
        assert rec["verdict"] == "could_not_fix"
        assert "credentials" in (rec.get("summary") or "").lower()
        log = s.read_log()
        assert "opencode credentials" in log or "refusing" in log
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
# incident files + queued response
# --------------------------------------------------------------------------- #

def test_report_queued_and_files(fake, srv, tmp_path):
    st, _, resp = post_report(srv.port, chat_id="chat-1")
    assert st == 201
    iid = resp["id"]
    assert resp["status"] == "queued"
    assert resp["view"] == "/f/%s/%s/" % (iid, sig_for(iid))
    rec = wait_done(srv.port, iid)
    assert rec["status"] == "done"
    d = tmp_path / "fixes" / iid
    for name in ("report.json", "context.md", "logs.txt", "session.md",
                 "events.jsonl", "incident.json"):
        assert (d / name).exists(), name
    # report.json echoes the report
    rj = json.loads((d / "report.json").read_text())
    assert rj["chat_id"] == "chat-1" and rj["source"] == "code"
    # logs.txt captured the fake logs
    assert "log line 1" in (d / "logs.txt").read_text()
    # index.jsonl got a line
    idx = json.loads((tmp_path / "fixes" / "index.jsonl").read_text().splitlines()[0])
    assert idx["id"] == iid and idx["status"] == "done"


def test_context_md(fake, srv, tmp_path):
    st, _, resp = post_report(srv.port, chat_id="c1",
                              body_extra={"context": [
                                  {"role": "user", "content": "hello there"}]})
    iid = resp["id"]; wait_done(srv.port, iid)
    ctx = (tmp_path / "fixes" / iid / "context.md").read_text()
    assert "hello there" in ctx and "## user" in ctx


def test_session_md_from_chat(fake, srv, tmp_path):
    # source chat so the service queries the opencode session
    st, _, resp = post_report(srv.port, chat_id="chat-9", source="chat")
    iid = resp["id"]; wait_done(srv.port, iid)
    sm = (tmp_path / "fixes" / iid / "session.md").read_text()
    assert "cat index.html" in sm and "Found the problem" in sm


def test_session_md_code_mode_reads_the_session(fake, srv, tmp_path):
    # A Code-mode report comes from the chat's own coding session: its steps are the best evidence there is
    # (this used to be skipped for source "code", against the brief).
    st, _, resp = post_report(srv.port, chat_id="chat-9", source="code")
    iid = resp["id"]; wait_done(srv.port, iid)
    sm = (tmp_path / "fixes" / iid / "session.md").read_text()
    assert "cat index.html" in sm and "Found the problem" in sm


def test_session_md_user_report_reads_the_session(fake, srv, tmp_path):
    st, _, resp = post_report(srv.port, chat_id="chat-9", source="user", details="")
    iid = resp["id"]; wait_done(srv.port, iid)
    sm = (tmp_path / "fixes" / iid / "session.md").read_text()
    assert "cat index.html" in sm


def test_session_md_chat_without_code_turns(fake, srv, tmp_path):
    st, _, resp = post_report(srv.port, chat_id="nosession-1", source="user")
    iid = resp["id"]; wait_done(srv.port, iid)
    sm = (tmp_path / "fixes" / iid / "session.md").read_text()
    assert "no coding session" in sm and "nosession-1" in sm


# --------------------------------------------------------------------------- #
# the Fixer button: a report the user files from the chat's pill
# --------------------------------------------------------------------------- #

def test_user_report_prompt(fake, srv):
    st, _, resp = post_report(srv.port, chat_id="chat-5", source="user", title="Look into this chat", details="",
                              body_extra={"reporter_model": "you"})
    assert st == 201
    rec = wait_done(srv.port, resp["id"])
    text = fake.state.prompt_text
    assert "The user opened you from one of their chats" in text
    assert "A model in a chat reported" not in text and "Reporter:" not in text
    assert "find the problem yourself" in text            # nothing written: it has to look for itself
    assert "the user reads this, not a model" in text      # for_the_chat is for the user
    assert "/home/coder/projects/chats/chat-5" in text and "%s/result.json" % resp["id"] in text
    assert rec["source"] == "user" and rec["reporter"] == "you"


def test_user_report_prompt_carries_what_they_wrote(fake, srv):
    st, _, resp = post_report(srv.port, chat_id="chat-5", source="user", title="Preview stays blank",
                              details="Preview stays blank after the last turn")
    wait_done(srv.port, resp["id"])
    text = fake.state.prompt_text
    assert "What the user wrote: Preview stays blank after the last turn" in text
    assert "find the problem yourself" not in text


def test_model_report_prompt_unchanged(fake, srv):
    st, _, resp = post_report(srv.port, chat_id="chat-5", source="code", title="desktop hover broken",
                              details="exit 2")
    wait_done(srv.port, resp["id"])
    text = fake.state.prompt_text
    assert "A model in a chat reported a bug." in text
    assert "Title: desktop hover broken\nDetails: exit 2\nReporter: test agent (code mode), chat chat-5." in text
    assert "retry the failed command" in text and "%s/result.json" % resp["id"] in text


def test_pick_requires_bearer(fake, srv):
    st, _, _ = api(srv.port, "GET", "/api/pick")
    assert st == 401


def test_pick_says_which_model_and_whether_it_is_loaded(fake, srv):
    fake.state.router_models = _router(("ornith-1.5-35b-a3b", "unloaded"), ("qwen3.8-27b", "loaded"))
    st, _, got = api(srv.port, "GET", "/api/pick", bearer=TOKEN)
    assert st == 200 and got == {"model": "qwen3.8-27b", "loaded": True}
    fake.state.router_models = _router(("llama3.2-1b", "loaded"))
    st, _, got = api(srv.port, "GET", "/api/pick", bearer=TOKEN)
    assert got == {"model": "ornith-1.5-35b-a3b", "loaded": False}   # the run would load Ornith


def test_pick_router_unreachable_is_unknown(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env={"FIXER_ROUTER_MODELS": "http://127.0.0.1:%d/v1/models" % _free_port()})
    s.start()
    try:
        st, _, got = api(s.port, "GET", "/api/pick", bearer=TOKEN)
        assert st == 200 and got == {"model": "ornith-1.5-35b-a3b", "loaded": None}
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
# events: recording, replay, live
# --------------------------------------------------------------------------- #

def test_events_recorded(fake, srv, tmp_path):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]; rec = wait_done(srv.port, iid)
    events = [json.loads(l) for l in
              (tmp_path / "fixes" / iid / "events.jsonl").read_text().splitlines()]
    kinds = {e["kind"] for e in events}
    assert {"status", "thinking", "say", "tool", "permission", "done"} <= kinds
    # each event carries a unix-seconds "t"
    assert all("t" in e for e in events)
    # a thinking end event has a seconds count
    assert any(e["kind"] == "thinking" and e["state"] == "end"
               and "seconds" in e for e in events)
    # permission refused
    perm = [e for e in events if e["kind"] == "permission"]
    assert perm and perm[0]["decision"] == "refused"
    # done carries the final record
    done = [e for e in events if e["kind"] == "done"][0]
    assert done["record"]["verdict"] == "fixed"


def test_sse_replay(fake, srv):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]; wait_done(srv.port, iid)
    events = read_sse(srv.port, "/f/%s/%s/events" % (iid, sig_for(iid)))
    kinds = {e["kind"] for e in events}
    assert "done" in kinds and "tool" in kinds
    assert events[-1]["kind"] == "done"


def test_sse_live_and_content_type(fake, srv):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]
    globals_ = {"chunks": []}

    def reader():
        req = urllib.request.Request(
            "http://127.0.0.1:%d/f/%s/%s/events" % (srv.port, iid, sig_for(iid)))
        try:
            with urllib.request.urlopen(req, timeout=20) as resp_e:
                globals_["ctype"] = resp_e.headers.get("Content-Type", "")
                for line in resp_e:
                    globals_["chunks"].append(line)
        except Exception:
            pass
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    # wait for completion via the API while the SSE reader streams live
    rec = wait_done(srv.port, iid)
    assert rec["status"] == "done"
    t.join(timeout=10)
    body = b"".join(globals_["chunks"]).decode("utf-8", "replace")
    assert body.endswith("\n\n"), "live SSE stream must end with a blank line"
    got = _parse_sse_body(body)
    assert "text/event-stream" in globals_.get("ctype", "")
    kinds = {e["kind"] for e in got}
    assert {"tool", "permission", "say"} <= kinds
    assert got[-1]["kind"] == "done"


# --------------------------------------------------------------------------- #
# permission refused with the message
# --------------------------------------------------------------------------- #

def test_permission_refused_with_message(fake, srv, tmp_path):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]; rec = wait_done(srv.port, iid)
    # the fake recorded the reject reply with the message
    assert fake.state.permission_replies, "no permission reply recorded"
    reply = fake.state.permission_replies[0]
    assert reply["decision"] == "reject"
    assert "Not allowed in the fixer" in reply["message"]
    # and it was surfaced to the viewer
    events = [json.loads(l) for l in
              (tmp_path / "fixes" / iid / "events.jsonl").read_text().splitlines()]
    perm = [e for e in events if e["kind"] == "permission"][0]
    assert "rm -rf build" in perm["what"] and perm["decision"] == "refused"


# --------------------------------------------------------------------------- #
# form answered
# --------------------------------------------------------------------------- #

def test_form_answered(fake, srv):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]; wait_done(srv.port, iid)
    assert fake.state.form_replies, "no form reply recorded"
    ans = fake.state.form_replies[0]["answer"]
    assert ans.get("mode") == "Use your best judgement."


# --------------------------------------------------------------------------- #
# result.json read / missing -> could_not_fix
# --------------------------------------------------------------------------- #

def test_result_json_read(fake, srv, tmp_path):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]; rec = wait_done(srv.port, iid)
    assert rec["verdict"] == "fixed"
    assert rec["summary"] == "simulated fix of the bug"
    assert rec["changed"][0]["path"] == "index.html"
    assert rec["for_the_chat"] == "retry the failed command"


def test_missing_result_could_not_fix(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env={"FAKE_WRITE_RESULT": "0"})
    s.start()
    try:
        st, _, resp = post_report(s.port)
        iid = resp["id"]; rec = wait_done(s.port, iid)
        assert rec["verdict"] == "could_not_fix"
        assert rec["summary"]  # some fallback text
        assert (tmp_path / "fixes" / iid / "events.jsonl").exists()
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
# restart called only when asked
# --------------------------------------------------------------------------- #

def test_restart_when_asked(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env={"FAKE_RESTART": "code-preview"})
    s.start()
    try:
        st, _, resp = post_report(s.port)
        iid = resp["id"]; wait_done(s.port, iid)
        log = tmp_path / "restart.log"
        assert log.exists() and "called" in log.read_text()
        rec = json.loads((tmp_path / "fixes" / iid / "incident.json").read_text())
        assert rec["restart_outcome"] and rec["restart_outcome"].startswith("ok")
    finally:
        s.stop()


def test_restart_not_asked(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env={"FAKE_RESTART": ""})
    s.start()
    try:
        st, _, resp = post_report(s.port)
        iid = resp["id"]; wait_done(s.port, iid)
        log = tmp_path / "restart.log"
        assert not log.exists() or "called" not in log.read_text()
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
# the queue: two reports run one after the other
# --------------------------------------------------------------------------- #

def test_queue_two_sequential(fake, srv, tmp_path):
    _, _, r1 = post_report(srv.port, chat_id="chat-a")
    _, _, r2 = post_report(srv.port, chat_id="chat-b")
    d1 = wait_done(srv.port, r1["id"])
    d2 = wait_done(srv.port, r2["id"])
    assert d1["status"] == "done" and d2["status"] == "done"
    # both recorded events
    assert (tmp_path / "fixes" / r1["id"] / "events.jsonl").stat().st_size > 0
    assert (tmp_path / "fixes" / r2["id"] / "events.jsonl").stat().st_size > 0


# --------------------------------------------------------------------------- #
# the loop survives long silences and honors FIXER_MAX_MINUTES
# --------------------------------------------------------------------------- #

def test_silence_mid_script(fake, tmp_path):
    # The fake goes silent for 2 s mid-script; the loop must keep waiting and
    # then finish the run normally (not end early on the gap).
    s = FixerServer(tmp_path, fake, env={"FAKE_SILENCE": "2"})
    s.start()
    try:
        st, _, resp = post_report(s.port)
        iid = resp["id"]
        # It should be investigating during the gap and never finish early.
        deadline = time.time() + 5
        while time.time() < deadline:
            st, _, rec = api(s.port, "GET", "/api/incidents/%s" % iid,
                             bearer=TOKEN)
            if rec.get("status") == "done":
                raise AssertionError("ended before the silence finished")
            if rec.get("status") == "investigating":
                break
            time.sleep(0.1)
        rec = wait_done(s.port, iid)
        assert rec["status"] == "done" and rec["verdict"] == "fixed"
    finally:
        s.stop()


def test_stop_after_max_minutes(fake, tmp_path):
    # A run that never emits an end event must be stopped at the cap and
    # finalized as could_not_fix.
    s = FixerServer(tmp_path, fake,
                    env={"FAKE_MAX_NEVER": "1", "FIXER_MAX_MINUTES": "0.05"})
    s.start()
    try:
        st, _, resp = post_report(s.port)
        iid = resp["id"]
        rec = wait_done(s.port, iid, timeout=30)
        assert rec["status"] == "done"
        assert rec["verdict"] == "could_not_fix"
        assert "Stopped after" in (rec.get("summary") or "")
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
# diff capture from a temp git repo
# --------------------------------------------------------------------------- #

def test_diff_capture(fake, tmp_path):
    # a git repo under the projects dir with a future-dated commit
    repo = tmp_path / "projects" / "chatproj"
    repo.mkdir(parents=True)
    (repo / "index.html").write_text("<html>broken</html>")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "index.html").write_text("<html>fixed</html>")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = "t@t"
    env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    subprocess.run(["git", "-C", str(repo), "add", "-A"], env=env, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fix the page"],
                   env=env, check=True)

    s = FixerServer(tmp_path, fake)
    s.start()
    try:
        st, _, resp = post_report(s.port)
        iid = resp["id"]; wait_done(s.port, iid)
        diff = (tmp_path / "fixes" / iid / "diff.patch").read_text()
        assert "fix the page" in diff
        assert "fixed" in diff
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
# wait / active endpoints
# --------------------------------------------------------------------------- #

def test_wait_endpoint(fake, srv):
    st, _, resp = post_report(srv.port, chat_id="waits")
    iid = resp["id"]
    st, _, rec = api(srv.port, "GET",
                     "/api/incidents/%s/wait?timeout=30" % iid, bearer=TOKEN)
    assert st == 200 and rec["status"] == "done"


def test_wait_unknown_404(fake, srv):
    st, _, _ = api(srv.port, "GET", "/api/incidents/nope/wait?timeout=5", bearer=TOKEN)
    assert st == 404


def test_active_endpoint(fake, srv, tmp_path):
    st, _, resp = post_report(srv.port, chat_id="active-chat")
    # NOTE: FixerServer env override not used here.
    iid = resp["id"]
    # immediately query active (should still be unfinished)
    st, _, active = api(srv.port, "GET", "/api/active?chat=active-chat",
                        bearer=TOKEN)
    assert st == 200 and active["id"] == iid
    assert active["view"] == "/f/%s/%s/" % (iid, sig_for(iid))
    wait_done(srv.port, iid)
    # once done, active is 404
    st, _, _ = api(srv.port, "GET", "/api/active?chat=active-chat", bearer=TOKEN)
    assert st == 404


def test_active_unknown_chat_404(fake, srv):
    st, _, _ = api(srv.port, "GET", "/api/active?chat=none", bearer=TOKEN)
    assert st == 404


# --------------------------------------------------------------------------- #
# viewer / log / diff sigs
# --------------------------------------------------------------------------- #

def test_viewer_bad_sig_403(fake, srv):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]
    st, _, _ = api(srv.port, "GET", "/f/%s/%s/" % (iid, "0" * 32))
    assert st == 403


def test_log_bad_sig_403(fake, srv):
    st, _, _ = api(srv.port, "GET", "/log/%s/" % ("0" * 32))
    assert st == 403


def test_viewer_and_diff_good_sig(fake, srv, tmp_path):
    # a git repo under FIXER_PROJECTS so capture_diff has a patch to serve
    repo = tmp_path / "projects" / "chatproj"
    repo.mkdir(parents=True)
    (repo / "index.html").write_text("<html>broken</html>")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "index.html").write_text("<html>fixed</html>")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = "t@t"
    env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    subprocess.run(["git", "-C", str(repo), "add", "-A"], env=env, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fix the page"],
                   env=env, check=True)

    st, _, resp = post_report(srv.port)
    iid = resp["id"]; rec = wait_done(srv.port, iid)
    st, hdr, _ = api(srv.port, "GET", "/f/%s/%s/" % (iid, sig_for(iid)))
    assert st == 200
    st, _, diff = api(srv.port, "GET", "/f/%s/%s/diff" % (iid, sig_for(iid)))
    assert st == 200 and "fix the page" in (diff or "")
    st, _, logjson = api(srv.port, "GET", "/log/%s/incidents" % log_sig())
    assert st == 200 and any(e["id"] == iid for e in logjson)


def test_log_page_html(fake, srv):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]; wait_done(srv.port, iid)
    st, _, body = api(srv.port, "GET", "/log/%s/" % log_sig())
    assert st == 200 and "Bug log" in body


def test_viewer_logs_no_sig(fake, srv):
    # A signed viewer request (/f/<id>/<32-hex-sig>/) must be logged masked.
    # The raw 32-hex signature must never land in the service's stderr.
    st, _, resp = post_report(srv.port)
    iid = resp["id"]; rec = wait_done(srv.port, iid)
    st, _, body = api(srv.port, "GET", "/f/%s/%s/" % (iid, sig_for(iid)))
    assert st == 200 and body.startswith("<")
    log = srv.read_log()
    assert sig_for(iid) not in log, "raw signature leaked to stderr"
    # The masked line is present, so we know the request was logged.
    assert ("/f/%s/" % iid) in log


def test_unknown_incident_404(fake, srv):
    st, _, _ = api(srv.port, "GET", "/api/incidents/does-not-exist", bearer=TOKEN)
    assert st == 404


# --------------------------------------------------------------------------- #
# headers
# --------------------------------------------------------------------------- #

def test_security_headers(fake, srv):
    st, _, resp = post_report(srv.port)
    iid = resp["id"]
    _, hdr, _ = api(srv.port, "GET", "/f/%s/%s/" % (iid, sig_for(iid)))
    assert hdr["cache-control"] == "no-store"
    assert hdr["x-content-type-options"] == "nosniff"
    assert hdr["referrer-policy"] == "no-referrer"
    assert hdr["content-security-policy"].startswith("default-src 'self' 'unsafe-inline'")
    assert FRAME in hdr["content-security-policy"]


def test_security_headers_on_403(fake, srv):
    _, hdr, _ = api(srv.port, "GET", "/log/%s/" % ("0" * 32))
    assert hdr["cache-control"] == "no-store"
    assert hdr["x-content-type-options"] == "nosniff"
    assert hdr["referrer-policy"] == "no-referrer"


# --------------------------------------------------------------------------- #
# report-bug CLI against the test server
# --------------------------------------------------------------------------- #

def test_report_bug_cli(fake, srv, tmp_path):
    chatdir = tmp_path / "chats" / "cli-chat"
    chatdir.mkdir(parents=True)
    env = os.environ.copy()
    env["FIXER_PORT"] = str(srv.port)
    env["FIXER_API_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["FIXER_PUBLIC_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["REPORTER_MODEL"] = "test agent"
    env["FIXER_TOKEN"] = TOKEN
    env["PROJECTS_ROOT"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, REPORT_SRC, "Page unstyled",
         "index.html loads without its stylesheet"],
        cwd=str(chatdir), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verdict: fixed" in r.stdout
    assert "retry the failed command" in r.stdout
    assert "Fixer: http://" in r.stdout


def test_report_bug_cli_features_dir(fake, srv, tmp_path):
    # report-bug must accept features/<chat id>/... as its source of the chat id,
    # the same way it accepts chats/<chat id>/...
    chatdir = tmp_path / "features" / "feat-chat"
    chatdir.mkdir(parents=True)
    env = os.environ.copy()
    env["FIXER_PORT"] = str(srv.port)
    env["FIXER_API_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["FIXER_PUBLIC_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["REPORTER_MODEL"] = "test agent"
    env["FIXER_TOKEN"] = TOKEN
    env["PROJECTS_ROOT"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, REPORT_SRC, "Bug in the features page",
         "the panel does not open"],
        cwd=str(chatdir), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verdict: fixed" in r.stdout


def test_report_bug_exit1_when_not_fixed(fake, tmp_path):
    # a not_a_bug verdict -> report-bug exits 1
    s = FixerServer(tmp_path, fake, env={"FAKE_VERDICT": "not_a_bug"})
    s.start()
    try:
        chatdir = tmp_path / "chats" / "cc"
        chatdir.mkdir(parents=True)
        env = os.environ.copy()
        env["FIXER_PORT"] = str(s.port)
        env["FIXER_API_BASE"] = "http://127.0.0.1:%d" % s.port
        env["FIXER_PUBLIC_BASE"] = "http://127.0.0.1:%d" % s.port
        env["FIXER_TOKEN"] = TOKEN
        env["PROJECTS_ROOT"] = str(tmp_path)
        r = subprocess.run(
            [sys.executable, REPORT_SRC, "Not a bug", "works fine as-is"],
            cwd=str(chatdir), env=env, capture_output=True, text=True, timeout=120)
        # not_a_bug is a valid outcome -> exit 0 per the brief
        assert r.returncode == 0
    finally:
        s.stop()


def test_report_bug_exit2_no_chat(fake, srv, tmp_path):
    env = os.environ.copy()
    env["FIXER_PORT"] = str(srv.port)
    env["FIXER_API_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["FIXER_TOKEN"] = TOKEN
    env["PROJECTS_ROOT"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, REPORT_SRC, "T", "D"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2


def test_report_bug_cli_reads_token_file(fake, srv, tmp_path):
    # No FIXER_TOKEN in the environment; it must be read from FIXER_ENV_FILE.
    token_file = tmp_path / "fixer.env"
    token_file.write_text("FIXER_SECRET=%s\nFIXER_TOKEN=%s\nOTHER=skipme\n"
                          % (SECRET, TOKEN))
    chatdir = tmp_path / "chats" / "tok-chat"
    chatdir.mkdir(parents=True)
    env = os.environ.copy()
    env.pop("FIXER_TOKEN", None)
    env["FIXER_PORT"] = str(srv.port)
    env["FIXER_API_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["FIXER_PUBLIC_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["FIXER_ENV_FILE"] = str(token_file)
    env["PROJECTS_ROOT"] = str(tmp_path)
    r = subprocess.run(
        [sys.executable, REPORT_SRC, "Page unstyled",
         "index.html loads without its stylesheet"],
        cwd=str(chatdir), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verdict: fixed" in r.stdout


def test_report_bug_wait(fake, srv, tmp_path):
    # Post a report to obtain an id, then re-check it with --wait (no new post).
    st, hdr, resp = post_report(srv.port)
    iid = resp["id"]
    env = os.environ.copy()
    env["FIXER_PORT"] = str(srv.port)
    env["FIXER_API_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["FIXER_PUBLIC_BASE"] = "http://127.0.0.1:%d" % srv.port
    env["FIXER_TOKEN"] = TOKEN
    r = subprocess.run(
        [sys.executable, REPORT_SRC, "--wait", iid],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Verdict: fixed" in r.stdout
    assert "Fixer: http://" not in r.stdout


def test_report_bug_exit3_slow(fake, tmp_path):
    # A fake that never finishes; REPORT_WAIT=1 -> exit 3 with the keep-waiting
    # message (no new report filed).
    s = FixerServer(tmp_path, fake, env={"FAKE_MAX_NEVER": "1", "REPORT_WAIT": "1"})
    s.start()
    try:
        chatdir = tmp_path / "chats" / "slow"
        chatdir.mkdir(parents=True)
        env = os.environ.copy()
        env["FIXER_PORT"] = str(s.port)
        env["FIXER_API_BASE"] = "http://127.0.0.1:%d" % s.port
        env["FIXER_PUBLIC_BASE"] = "http://127.0.0.1:%d" % s.port
        env["FIXER_TOKEN"] = TOKEN
        env["REPORT_WAIT"] = "1"
        env["PROJECTS_ROOT"] = str(tmp_path)
        r = subprocess.run(
            [sys.executable, REPORT_SRC, "Slow", "takes a while"],
            cwd=str(chatdir), env=env, capture_output=True, text=True,
            timeout=60)
        assert r.returncode == 3, r.stdout + r.stderr
        assert ("Still investigating (the user is watching it live). "
                "Keep waiting with: report-bug --wait") in r.stdout
    finally:
        s.stop()



def test_report_bug_exit3_after_not_done_answer(fake, tmp_path):
    # REPORT_WAIT=5: the server answers /wait with a not-done record inside the window;
    # that must still end in exit 3, not a printed verdict of None (seen live 2026-10-06).
    s = FixerServer(tmp_path, fake, env={"FAKE_MAX_NEVER": "1", "REPORT_WAIT": "5"})
    s.start()
    try:
        chatdir = tmp_path / "chats" / "slow"
        chatdir.mkdir(parents=True)
        env = os.environ.copy()
        env["FIXER_PORT"] = str(s.port)
        env["FIXER_API_BASE"] = "http://127.0.0.1:%d" % s.port
        env["FIXER_PUBLIC_BASE"] = "http://127.0.0.1:%d" % s.port
        env["FIXER_TOKEN"] = TOKEN
        env["REPORT_WAIT"] = "5"
        env["PROJECTS_ROOT"] = str(tmp_path)
        r = subprocess.run(
            [sys.executable, REPORT_SRC, "Slow", "takes a while"],
            cwd=str(chatdir), env=env, capture_output=True, text=True,
            timeout=60)
        assert r.returncode == 3, r.stdout + r.stderr
        assert "Verdict:" not in r.stdout
        assert ("Still investigating (the user is watching it live). "
                "Keep waiting with: report-bug --wait") in r.stdout
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
# model choice: the router's loaded coding model, else FIXER_MODEL
# --------------------------------------------------------------------------- #

def _router(*pairs):
    return [{"id": mid, "object": "model", "owned_by": "slerm", "slerm": {"status": st}} for mid, st in pairs]


def test_fixer_uses_loaded_capable_model(fake, srv):
    fake.state.router_models = _router(("ornith-1.5-35b-a3b", "unloaded"), ("qwen3.8-27b", "loaded"))
    st, _, resp = post_report(srv.port)
    rec = wait_done(srv.port, resp["id"])
    assert fake.state.created[-1]["model"] == {"id": "qwen3.8-27b", "providerID": "slerm"}
    assert rec.get("fixer_model") == "qwen3.8-27b"
    assert fake.state.router_auth and not any(fake.state.router_auth), "opencode auth sent to the gateway"


def test_fixer_falls_back_when_loaded_model_is_not_capable(fake, srv):
    fake.state.router_models = _router(("llama3.2-1b", "loaded"), ("qwen3.8-27b", "unloaded"))
    st, _, resp = post_report(srv.port)
    wait_done(srv.port, resp["id"])
    assert fake.state.created[-1]["model"] == {"id": "ornith-1.5-35b-a3b", "providerID": "slerm"}


def test_fixer_falls_back_when_router_unreachable(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env={"FIXER_ROUTER_MODELS": "http://127.0.0.1:%d/v1/models" % _free_port()})
    s.start()
    try:
        st, _, resp = post_report(s.port)
        wait_done(s.port, resp["id"])
        assert fake.state.created[-1]["model"] == {"id": "ornith-1.5-35b-a3b", "providerID": "slerm"}
    finally:
        s.stop()


# --------------------------------------------------------------------------- #
# Escalation to Claude, and Cancel (2026-10-26)
# --------------------------------------------------------------------------- #

ESC = {"FIXER_ESCALATE": "1"}


def wait_status(port, incident_id, status, timeout=30):
    deadline = time.time() + timeout
    rec = None
    while time.time() < deadline:
        st, _, rec = api(port, "GET", "/api/incidents/%s" % incident_id, bearer=TOKEN)
        if st == 200 and rec.get("status") == status:
            return rec
        time.sleep(0.1)
    raise AssertionError("never reached %s: %s" % (status, rec))


def test_a_give_up_goes_to_claude_and_the_runner_resolves_it(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env=dict(ESC, FAKE_VERDICT="could_not_fix"))
    s.start()
    try:
        _, _, resp = post_report(s.port, chat_id="chat-esc")
        iid = resp["id"]
        rec = wait_status(s.port, iid, "escalated")
        assert rec["escalation"]["state"] == "waiting" and rec["escalation"]["by"] == "auto"
        assert rec["escalation"]["fixer"]["verdict"] == "could_not_fix"
        st, _, act = api(s.port, "GET", "/api/active?chat=chat-esc", bearer=TOKEN)
        assert st == 200 and act["id"] == iid, "the chat keeps waiting while Claude looks"
        st, _, lst = api(s.port, "GET", "/api/escalations", bearer=TOKEN)
        assert [e["id"] for e in lst["escalations"]] == [iid] and lst["escalations"][0]["fixer_verdict"] == "could_not_fix"
        assert api(s.port, "GET", "/api/escalations")[0] == 401
        st, _, got = api(s.port, "POST", "/api/incidents/%s/claim" % iid, body={"runner": "r1"}, bearer=TOKEN)
        assert st == 200 and got["escalation"]["state"] == "claimed"
        assert api(s.port, "POST", "/api/incidents/%s/claim" % iid, body={"runner": "r2"}, bearer=TOKEN)[0] == 409
        st, _, txt = api(s.port, "GET", "/api/incidents/%s/file/report.json" % iid, bearer=TOKEN)
        assert st == 200 and "chat-esc" in str(txt)
        assert api(s.port, "GET", "/api/incidents/%s/file/sig.txt" % iid, bearer=TOKEN)[0] == 404
        st, _, _ = api(s.port, "POST", "/api/incidents/%s/events" % iid, bearer=TOKEN, body={"runner": "r1", "events": [
            {"type": "say", "text": "Looking at the fixer's notes."},
            {"type": "tool", "id": "c1", "name": "Bash", "summary": "ssh vm129 journalctl -u code-preview", "state": "running"},
            {"type": "tool", "id": "c1", "name": "Bash", "state": "ok", "output": "ok"},
            {"type": "nonsense", "text": "dropped"}]})
        assert st == 200
        assert api(s.port, "POST", "/api/incidents/%s/events" % iid, bearer=TOKEN,
                   body={"runner": "r2", "events": []})[0] == 409, "only the runner that claimed it"
        st, _, _ = api(s.port, "POST", "/api/incidents/%s/resolve" % iid, bearer=TOKEN, body={
            "runner": "r1", "verdict": "fixed", "summary": "Claude fixed the preview route",
            "changed": [{"path": "code-preview.py", "why": "route"}], "seconds": 300})
        assert st == 200
        rec = wait_done(s.port, iid)
        assert rec["verdict"] == "fixed" and rec["summary"] == "Claude fixed the preview route"
        assert rec["escalation"]["state"] == "done" and rec["escalation"]["fixer"]["verdict"] == "could_not_fix"
        evs = read_sse(s.port, "/f/%s/%s/events" % (iid, sig_for(iid)))
        kinds = [e["kind"] for e in evs]
        assert "escalate" in kinds and kinds[-1] == "done"
        claude = [e for e in evs if e["kind"] == "claude"]
        assert [e["type"] for e in claude] == ["status", "say", "tool", "tool"], claude
        assert api(s.port, "GET", "/api/active?chat=chat-esc", bearer=TOKEN)[0] == 404
    finally:
        s.stop()


def test_unclaimed_it_closes_with_the_fixers_verdict(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env=dict(ESC, FAKE_VERDICT="needs_human", FIXER_ESCALATE_CLAIM_S="1"))
    s.start()
    try:
        _, _, resp = post_report(s.port)
        rec = wait_done(s.port, resp["id"], timeout=30)
        assert rec["verdict"] == "needs_human" and rec["summary"] == "simulated fix of the bug"
        assert rec["escalation"]["state"] == "unclaimed" and "code-claude" in rec["escalation"]["note"]
    finally:
        s.stop()


def test_a_fixed_report_is_not_escalated_but_the_button_can_reopen_it(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env=ESC)
    s.start()
    try:
        _, _, resp = post_report(s.port, chat_id="chat-btn")
        iid = resp["id"]
        rec = wait_done(s.port, iid)
        assert rec["verdict"] == "fixed" and not rec.get("escalation")
        st, _, got = api(s.port, "POST", "/api/incidents/%s/escalate" % iid, body={"by": "user"}, bearer=TOKEN)
        assert st == 200 and got["status"] == "escalated"
        rec = wait_status(s.port, iid, "escalated")
        assert rec["escalation"]["by"] == "user" and rec["escalation"]["fixer"]["verdict"] == "fixed"
        assert api(s.port, "GET", "/api/active?chat=chat-btn", bearer=TOKEN)[0] == 200
        api(s.port, "POST", "/api/incidents/%s/claim" % iid, body={"runner": "r1"}, bearer=TOKEN)
        api(s.port, "POST", "/api/incidents/%s/resolve" % iid, body={"runner": "r1", "verdict": "not_a_bug",
                                                                    "summary": "all fine"}, bearer=TOKEN)
        rec = wait_done(s.port, iid)
        assert rec["verdict"] == "not_a_bug" and rec["escalation"]["state"] == "done"
    finally:
        s.stop()


def test_cancel_stops_the_fixer_and_nothing_goes_to_claude(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env=dict(ESC, FAKE_MAX_NEVER="1"))
    s.start()
    try:
        _, _, resp = post_report(s.port)
        iid = resp["id"]
        wait_status(s.port, iid, "investigating")
        assert fake.state.prompt_event.wait(10)
        time.sleep(0.3)
        st, _, got = api(s.port, "POST", "/api/incidents/%s/cancel" % iid, body={}, bearer=TOKEN)
        assert st == 202 and got["status"] == "cancelling"
        rec = wait_done(s.port, iid, timeout=20)
        assert rec["verdict"] == "cancelled" and "Cancelled by you" in rec["summary"]
        assert not rec.get("escalation"), "a cancel never hands it to Claude"
        assert fake.state.interrupted.is_set()
        assert api(s.port, "POST", "/api/incidents/%s/cancel" % iid, body={}, bearer=TOKEN)[0] == 409
    finally:
        s.stop()


def test_cancel_while_claude_looks_closes_it_and_the_runner_is_refused(fake, tmp_path):
    s = FixerServer(tmp_path, fake, env=dict(ESC, FAKE_VERDICT="could_not_fix"))
    s.start()
    try:
        _, _, resp = post_report(s.port)
        iid = resp["id"]
        wait_status(s.port, iid, "escalated")
        api(s.port, "POST", "/api/incidents/%s/claim" % iid, body={"runner": "r1"}, bearer=TOKEN)
        assert api(s.port, "POST", "/api/incidents/%s/cancel" % iid, body={}, bearer=TOKEN)[0] == 200
        rec = wait_done(s.port, iid)
        assert rec["verdict"] == "cancelled" and rec["escalation"]["state"] == "cancelled"
        assert api(s.port, "POST", "/api/incidents/%s/events" % iid, bearer=TOKEN,
                   body={"runner": "r1", "events": [{"type": "say", "text": "x"}]})[0] == 409
    finally:
        s.stop()


def test_a_restart_closes_reports_it_cut_off(fake, tmp_path):
    fixes = tmp_path / "fixes"
    os.makedirs(fixes / "20261026-0100-aaaa", exist_ok=True)
    os.makedirs(fixes / "20261026-0101-bbbb", exist_ok=True)
    (fixes / "index.jsonl").write_text(
        json.dumps({"id": "20261026-0100-aaaa", "chat_id": "c1", "status": "queued"}) + "\n" +
        json.dumps({"id": "20261026-0101-bbbb", "chat_id": "c2", "status": "escalated"}) + "\n")
    (fixes / "20261026-0101-bbbb" / "incident.json").write_text(json.dumps({
        "id": "20261026-0101-bbbb", "chat_id": "c2", "status": "escalated", "created": 1,
        "escalation": {"state": "claimed", "fixer": {"verdict": "could_not_fix", "summary": "gave up"}}}))
    s = FixerServer(tmp_path, fake)
    s.start()
    try:
        a = wait_done(s.port, "20261026-0100-aaaa")
        b = wait_done(s.port, "20261026-0101-bbbb")
        assert a["verdict"] == "could_not_fix" and "restarted" in a["summary"]
        assert b["verdict"] == "could_not_fix" and b["escalation"]["state"] == "restarted"
        assert api(s.port, "GET", "/api/active?chat=c2", bearer=TOKEN)[0] == 404
    finally:
        s.stop()
