"""Tests for the `desktop` CLI tool.

A fake desktop API server (http.server in a thread) records requests and
answers like the real one. The tool is invoked as a subprocess so we can
check real exit codes and stdout.
"""

import base64
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import uuid
import shutil
import tempfile
import atexit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "desktop")

# A valid 1x1 PNG, base64 encoded, used for screenshot tests.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class State:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        # observe() body served for both /api/observe and screenshot
        self.observe_body = {
            "session": "desktop-cli-test",
            "url": "http://localhost:8765/",
            "title": "Test page",
            "tabs": [{"title": "Test page", "url": "http://localhost:8765/"}],
            "screen": {"w": 1280, "h": 800},
            "elements": [],
            "text": "Hello world, typed value here",
            "screenshot": PNG_B64,
            "user_control": False,
            "todo": [],
        }
        self.act_status = 200
        self.act_resp = {"ok": True}
        self.hold_until = 0          # user_control True for first N health polls
        self.health_calls = 0
        self.act_409_times = 0       # first N act calls return 409
        self.act_count = 0
        # observe() may be overridden, e.g. observe_override = (503, {"detail": ...})
        self.observe_override = None
        self.park_status = 200
        self.park_resp = {"ok": True}
        # /api/upload
        self.upload_status = 200
        self.upload_resp = {"ok": True, "how": "the file chooser the click opened", "attached": ["lesson.pdf"],
                            "left_out": [], "closed_dialogs": 0,
                            "input": {"names": ["lesson.pdf"], "multiple": False, "accept": ".pdf",
                                      "label": "Lesson plan"}}
        self.upload_409_times = 0
        self.upload_count = 0
        # /api/diag (page errors): None = the endpoint answers 404, like an old desktop API
        self.diag_body = {"watching": True, "url": "http://localhost:8765/", "title": "Test page",
                          "settled": True, "log": {"url": "http://localhost:8765/", "age_s": 2.0, "requests": 3,
                                                   "by_type": {"Document": 1, "Script": 1, "Image": 1},
                                                   "pending": 0, "counts": {"errors": 0, "console_errors": 0, "failed": 0},
                                                   "errors": [], "console_errors": [], "failed": [], "recent": []}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json_body(self):
        import json
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode()) if raw else {}
        except Exception:
            return {}

    def _record(self, method, parsed):
        state = self.server.state
        with state.lock:
            state.requests.append({
                "method": method,
                "path": parsed.path,
                "query": urllib.parse.parse_qs(parsed.query),
                "headers": dict(self.headers),
                "body": None,
            })

    def _json(self, code, obj):
        self._respond(code, obj)

    def do_POST(self):
        state = self.server.state
        parsed = urllib.parse.urlsplit(self.path)
        body = self._json_body()
        with state.lock:
            state.requests.append({
                "method": "POST",
                "path": parsed.path,
                "query": urllib.parse.parse_qs(parsed.query),
                "headers": dict(self.headers),
                "body": body,
            })
        if parsed.path == "/api/observe":
            if state.observe_override is not None:
                code, obj = state.observe_override
                self._respond(code, obj)
            else:
                self._json(200, state.observe_body)
        elif parsed.path == "/api/act":
            with state.lock:
                state.act_count += 1
                if state.act_409_times and state.act_count <= state.act_409_times:
                    code, resp = 409, {"ok": False, "error": "user has control"}
                else:
                    code, resp = state.act_status, state.act_resp
            self._respond(code, resp)
        elif parsed.path == "/api/upload":
            with state.lock:
                state.upload_count += 1
                if state.upload_count <= state.upload_409_times:
                    code, resp = 409, {"ok": False, "error": "the user has control of the desktop"}
                else:
                    code, resp = state.upload_status, state.upload_resp
            self._respond(code, resp)
        elif parsed.path == "/api/caption":
            self._json(200, {"ok": True})
        elif parsed.path == "/api/park":
            self._respond(state.park_status, state.park_resp)
        elif parsed.path == "/api/diag":
            if state.diag_body is None:
                self._json(404, {"detail": "Not Found"})
            else:
                self._json(200, state.diag_body)
        else:
            self._json(404, {"ok": False, "error": "nope"})

    def do_GET(self):
        state = self.server.state
        parsed = urllib.parse.urlsplit(self.path)
        self._record("GET", parsed)
        if parsed.path == "/health":
            with state.lock:
                state.health_calls += 1
                uc = state.health_calls <= state.hold_until
            self._json(200, {"session": "desktop-cli-test", "user_control": uc})
        else:
            self._json(404, {"ok": False})

    def _respond(self, code, obj):
        import json
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.state = State()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    return srv, "http://%s:%d" % (host, port)


# --------------------------------------------------------------------------
# fake vision server (OpenAI-compatible /chat/completions)
# --------------------------------------------------------------------------
class VisionState:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        self.script = []          # list of full OpenAI-shaped response bodies
        self.idx = 0

    def next_response(self):
        with self.lock:
            if self.idx < len(self.script):
                resp = self.script[self.idx]
                self.idx += 1
                return resp
            return self.script[-1] if self.script else self.fallback()

    @staticmethod
    def fallback():
        return {"choices": [{"message": {"content": '{"action": "done", "report": "done"}'}}]}


class VisionHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        state = self.server.state
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode()) if raw else {}
        except Exception:
            body = {}
        with state.lock:
            state.requests.append({
                "method": "POST",
                "path": parsed.path,
                "query": urllib.parse.parse_qs(parsed.query),
                "headers": dict(self.headers),
                "body": body,
            })
        resp = state.next_response()
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_vision_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), VisionHandler)
    srv.state = VisionState()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    host, port = srv.server_address
    return srv, "http://%s:%d" % (host, port)


def _vision_resp(content):
    """Wrap model content (a string) in an OpenAI-shaped response body."""
    return {"choices": [{"message": {"content": content}}]}


def _vision_reasoning(content, reasoning):
    return {"choices": [{"message": {"content": content, "reasoning_content": reasoning}}]}


def _action_resp(obj):
    return _vision_resp(json.dumps(obj))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def run(args, **env):
    e = dict(os.environ)
    e.pop("CODE_DESKTOP", None)
    e["CODE_DESKTOP"] = env.pop("CODE_DESKTOP", "desktop-cli-test")
    e["DESKTOP_API"] = env.pop("DESKTOP_API", BASE)
    e["CONTROL_TOKEN"] = env.pop("CONTROL_TOKEN", "test-token")
    e.pop("DESKTOP_WAIT_MAX", None)
    e.pop("DESKTOP_POLL", None)
    e.pop("DESKTOP_ENV_FILE", None)
    e["VISION_API"] = env.pop("VISION_API", BASE_V)
    e["VISION_MODEL"] = env.pop("VISION_MODEL", "test-vision-model")
    e.update(env)
    proc = subprocess.run(
        [sys.executable, TOOL, *args],
        env=e, capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_raw(args, cwd=None, extra=None, no_token=False):
    """Run the tool with a fully custom environment (no defaults)."""
    e = dict(os.environ)
    for k in ("CODE_DESKTOP", "DESKTOP_API", "CONTROL_TOKEN",
              "DESKTOP_WAIT_MAX", "DESKTOP_POLL", "DESKTOP_ENV_FILE"):
        e.pop(k, None)
    e["DESKTOP_API"] = BASE
    if not no_token:
        e["CONTROL_TOKEN"] = "test-token"
    if extra:
        e.update(extra)
    proc = subprocess.run([sys.executable, TOOL, *args], env=e, cwd=cwd,
                          capture_output=True, text=True, timeout=60)
    return proc


def last(state, path):
    matches = [r for r in state.requests if r["path"] == path]
    return matches[-1] if matches else None


def acts(state):
    return [r for r in state.requests if r["path"] == "/api/act"]


def obses(state):
    return [r for r in state.requests if r["path"] == "/api/observe"]


BASE = None
SRV = None
BASE_V = None
SRV_V = None


def setup_module(module):
    global BASE, SRV, BASE_V, SRV_V
    SRV, BASE = start_server()
    SRV_V, BASE_V = start_vision_server()


def cleanup_module(module):
    if SRV:
        SRV.shutdown()
    if SRV_V:
        SRV_V.shutdown()


# --------------------------------------------------------------------------
# session selection
# --------------------------------------------------------------------------
def test_session_from_env():
    code, out, err = run(["look"])
    assert code == 0, err
    assert "Desktop session: desktop-cli-test" in out


def test_session_refuses_default():
    code, out, err = run(["look"], CODE_DESKTOP="default")
    assert code == 2
    assert "default" in (out + err).lower()


def test_session_refuses_empty():
    code, out, err = run(["look"], CODE_DESKTOP="!!!@@@")
    assert code == 2
    assert "empty" in (out + err).lower()


def test_session_sanitise():
    code, out, err = run(["look"], CODE_DESKTOP="my bad session!")
    assert code == 0, err
    assert "Desktop session: mybadsession" in out


def _make_chats_dir(path_components):
    # The chats rule uses the component directly under /home/coder/projects/
    # chats/<id>/; the first path component is therefore the session id.
    base = "/home/coder/projects/chats"
    full = os.path.join(base, *path_components)
    os.makedirs(full, exist_ok=True)
    return full


def test_session_chats_depth():
    deep = _make_chats_dir(["abc123", "sub", "more"])
    proc = run_raw(["look"], cwd=deep)
    assert proc.returncode == 0, proc.stderr
    assert "Desktop session: abc123" in proc.stdout


def test_session_chats_path_shallow():
    p = _make_chats_dir(["chatid99"])
    proc = run_raw(["look"], cwd=p)
    assert proc.returncode == 0, proc.stderr
    assert "Desktop session: chatid99" in proc.stdout


def _make_features_dir(path_components):
    # The features rule mirrors the chats rule: session name is the chat id,
    # i.e. the first path component under /home/coder/projects/features/. The
    # `desktop` code checks that exact prefix, so we build the dir there. We
    # register removal of that first path component on interpreter exit, so
    # running the suite leaves nothing behind.
    base = "/home/coder/projects/features"
    full = os.path.join(base, *path_components)
    os.makedirs(full, exist_ok=True)
    atexit.register(shutil.rmtree, os.path.join(base, path_components[0]),
                    ignore_errors=True)
    return full


def test_session_features_dir_deep():
    deep = _make_features_dir(["feat-42", "work", "sub"])
    proc = run_raw(["look"], cwd=deep)
    assert proc.returncode == 0, proc.stderr
    assert "Desktop session: feat-42" in proc.stdout


def test_session_features_dir_shallow():
    p = _make_features_dir(["feat-99"])
    proc = run_raw(["look"], cwd=p)
    assert proc.returncode == 0, proc.stderr
    assert "Desktop session: feat-99" in proc.stdout


def test_session_git_root_fallback():
    work = _make_chats_dir([])  # any tmp dir without a git root
    # temporarily point a fresh repo: use a subdir with .git
    import tempfile
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "testrepo", ".git"), exist_ok=True)
    proc = run_raw(["look"], cwd=os.path.join(d, "testrepo"))
    assert proc.returncode == 0, proc.stderr
    assert "Desktop session: code-testrepo" in proc.stdout


def test_session_git_root_basename_long():
    import tempfile
    name = "x" * 80
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, name, ".git"), exist_ok=True)
    proc = run_raw(["look"], cwd=os.path.join(d, name))
    assert proc.returncode == 0, proc.stderr
    sess = proc.stdout.splitlines()[0].replace("Desktop session: ", "")
    assert sess.startswith("code-")
    assert len(sess) == 64                  # whole "code-<name>" cut to 64
    assert sess[5:].replace("x", "") == ""  # only x's after the prefix


def test_credentials_from_env_file(tmp_path):
    envf = tmp_path / "desktop.env"
    envf.write_text("DESKTOP_API=%s\nCONTROL_TOKEN=file-token\n" % BASE)
    # No env token: the tool must read credentials from the file. `look` needs
    # the token for /api/observe, so success proves the file was used.
    before = len(SRV.state.requests)
    proc = run_raw(["look"], extra={"DESKTOP_ENV_FILE": str(envf)}, no_token=True)
    assert proc.returncode == 0, proc.stderr
    assert "Page text:" in proc.stdout
    # headers on this run's observe request carry the file token
    obs = [r for r in SRV.state.requests[before:] if r["path"] == "/api/observe"]
    assert obs and obs[-1]["headers"]["Authorization"] == "Bearer file-token"


# --------------------------------------------------------------------------
# request shapes and headers
# --------------------------------------------------------------------------
def test_open_navigate_shape():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["open", "http://localhost:8000"])
    assert code == 0, err
    act = last(s, "/api/act")
    assert act["body"] == {"action": "navigate", "url": "http://localhost:8000"}
    assert act["headers"]["Authorization"] == "Bearer test-token"
    assert act["headers"]["Content-Type"] == "application/json"
    assert act["query"]["s"] == ["desktop-cli-test"]


def test_open_new_tab():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["open", "http://x/", "--new-tab"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "new_tab", "url": "http://x/"}


def test_click():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["click", "7"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "click", "id": 7}


def test_type_with_id_enter_noclear():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["type", "3", "hello", "--enter", "--no-clear"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {
        "action": "type", "id": 3, "text": "hello",
        "enter": True, "clear": False,
    }


def test_type_focused_default():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["type", "world"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {
        "action": "type", "text": "world",
        "enter": False, "clear": True,
    }


def test_key():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["key", "ctrl+a"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "key", "keys": "ctrl+a"}


def test_scroll_default_and_explicit():
    s = SRV.state
    code, out, err = run(["scroll"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "scroll", "direction": "down", "amount": 5}
    code, out, err = run(["scroll", "up", "3"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "scroll", "direction": "up", "amount": 3}


def test_back():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["back"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "back"}


def test_wait_max_cap():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["wait", "99"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "wait", "seconds": 10}


def test_hover_by_element():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["hover", "5"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "hover", "id": 5}


def test_hover_by_xy():
    s = SRV.state
    s.requests.clear()
    code, out, err = run(["hover", "--xy", "640", "400"])
    assert code == 0, err
    assert last(s, "/api/act")["body"] == {"action": "hover", "x": 640, "y": 400}


def test_delegate_uses_hover():
    # The delegate loop may drive hover (e.g. to reveal a tooltip).
    _reset_handback()
    SRV_V.state.script = [
        _action_resp({"thought": "reveal the menu", "action": "hover", "id": 4}),
        _action_resp({"thought": "done", "action": "done", "report": "hovered to reveal the menu"}),
    ]
    code, out, err = run(["delegate", "reveal the menu", "--steps", "2"])
    assert code == 0, err
    a = acts(SRV.state)
    assert len(a) == 1
    assert a[0]["body"] == {"action": "hover", "id": 4}
    assert 'step 1: hover [4]' in out
    assert "hovered to reveal the menu" in out


def test_delegate_hover_xy_converts_to_pixels():
    # screen is 1280x800; 0-1000 x=250,y=500 -> 320,400
    _reset_handback()
    SRV_V.state.script = [
        _action_resp({"thought": "hover a spot", "action": "hover", "x": 250, "y": 500}),
        _action_resp({"thought": "done", "action": "done", "report": "done"}),
    ]
    code, out, err = run(["delegate", "hover something", "--steps", "2"])
    assert code == 0, err
    a = acts(SRV.state)
    assert a[0]["body"] == {"action": "hover", "x": 320, "y": 400}



def test_observe_after_act_and_caption():
    s = SRV.state
    s.requests.clear()
    run(["click", "1"])
    obs = last(s, "/api/observe")
    assert obs is not None
    assert obs["body"] == {"marks": False, "max_width": 320}
    captions = [r for r in s.requests if r["path"] == "/api/caption"]
    assert len(captions) == 2
    assert captions[0]["body"] == {"text": "opencode: click element 1", "busy": True}
    assert captions[1]["body"] == {"text": "", "busy": False}


# --------------------------------------------------------------------------
# output format
# --------------------------------------------------------------------------
def _make_many_elements():
    els = []
    for i in range(1, 59):                       # 58 non-drawn buttons
        els.append({"id": i, "tag": "button", "role": "", "text": "B%d" % i,
                    "x": 0, "y": 0, "w": 10, "h": 10})
    els.append({"id": 100, "tag": "drawn", "role": "", "text": "D0",
                "x": 0, "y": 0, "w": 10, "h": 10})  # skipped
    els.append({"id": 200, "tag": "a", "role": "link", "text": "Home",
                "x": 0, "y": 0, "w": 10, "h": 10})  # non-drawn #59
    for j in range(5):                            # 5 drawn skipped
        els.append({"id": 101 + j, "tag": "drawn", "role": "", "text": "D%d" % j,
                    "x": 0, "y": 0, "w": 10, "h": 10})
    for i in range(300, 303):                     # non-drawn #60, #61, #62
        els.append({"id": i, "tag": "button", "role": "", "text": "L%d" % i,
                    "x": 0, "y": 0, "w": 10, "h": 10})
    return els


def test_output_format_more_and_drawn():
    srv = SRV
    srv.state.observe_body["elements"] = _make_many_elements()
    srv.state.observe_body["text"] = "the visible page text"
    srv.state.observe_body["url"] = "http://localhost:8765/"
    srv.state.observe_body["title"] = "Title"
    srv.state.observe_body["tabs"] = [{"title": "t", "url": "u"}] * 3
    code, out, err = run(["look"])
    assert code == 0, err
    assert "Desktop session: desktop-cli-test" in out
    assert "Page: Title — http://localhost:8765/" in out
    assert "Tabs open: 3" in out
    assert "Page text:" in out
    assert "the visible page text" in out
    # 62 non-drawn -> 60 shown, +2 more; 6 drawn skipped
    assert "(+2 more further down; scroll to reach them)" in out
    assert '[58] button "B58"' in out
    assert '[200] a/link "Home"' in out        # the 59th non-drawn element
    assert 'button "L300"' in out              # the 60th
    assert "L301" not in out                   # beyond the 60 limit
    assert "D0" not in out                     # drawn elements never shown
    numbered = [l for l in out.splitlines() if l.startswith("[")]
    assert len(numbered) == 60


def test_output_no_page():
    srv = SRV
    srv.state.observe_body["url"] = ""
    srv.state.observe_body["text"] = ""
    srv.state.observe_body["elements"] = []
    code, out, err = run(["look"])
    assert code == 0, err
    assert "Page: (no page open)" in out
    assert "(none)" in out


def test_output_element_text_cuts_to_80():
    srv = SRV
    srv.state.observe_body["text"] = "p" * 5000
    srv.state.observe_body["elements"] = [
        {"id": 1, "tag": "button", "role": "",
         "text": "q" * 200, "x": 0, "y": 0, "w": 1, "h": 1}
    ]
    code, out, err = run(["look"])
    assert code == 0, err
    assert 'button "%s"' % ("q" * 80) in out
    assert "q" * 81 not in out


# --------------------------------------------------------------------------
# HTTP 400 -> exit 1
# --------------------------------------------------------------------------
def test_http400_exit1():
    srv = SRV
    srv.state.act_status = 400
    srv.state.act_resp = {"ok": False, "error": "unknown element 99"}
    code, out, err = run(["click", "99"])
    assert code == 1
    assert "unknown element 99" in out
    assert "run `desktop look` to see the current page" in out
    srv.state.act_status = 200
    srv.state.act_resp = {"ok": True}


# --------------------------------------------------------------------------
# user control wait
# --------------------------------------------------------------------------
def test_usercontrol_handback_after_two_polls():
    srv = SRV
    srv.state.health_calls = 0
    srv.state.hold_until = 2   # user_control True for first 2 health polls
    code, out, err = run(["click", "1"], DESKTOP_POLL="0.01",
                         DESKTOP_WAIT_MAX="10")
    assert code == 0, err
    assert "waiting for them to hand it back" in out


def test_usercontrol_never_exit3():
    srv = SRV
    srv.state.health_calls = 0
    srv.state.hold_until = 100000  # never hands back
    code, out, err = run(["click", "1"], DESKTOP_POLL="0.01",
                         DESKTOP_WAIT_MAX="0.05")
    assert code == 3
    assert "waiting for them to hand it back" in out


# --------------------------------------------------------------------------
# 409 then retry
# --------------------------------------------------------------------------
def test_409_then_retry():
    srv = SRV
    srv.state.requests.clear()
    srv.state.health_calls = 0
    srv.state.act_count = 0
    srv.state.act_409_times = 1  # first act -> 409, second -> 200
    srv.state.hold_until = 0     # pre-check: no control, proceed
    srv.state.act_resp = {"ok": True}
    code, out, err = run(["click", "5"], DESKTOP_POLL="0.01",
                         DESKTOP_WAIT_MAX="5")
    assert code == 0, err
    a = acts(srv.state)
    assert len(a) == 2
    assert a[0]["body"] == {"action": "click", "id": 5}
    assert a[1]["body"] == {"action": "click", "id": 5}


# --------------------------------------------------------------------------
# screenshot
# --------------------------------------------------------------------------
def test_screenshot_written(tmp_path):
    outp = tmp_path / "shot.png"
    code, out, err = run(["screenshot", str(outp)])
    assert code == 0, err
    assert out == ""          # screenshot prints nothing
    obs = last(SRV.state, "/api/observe")
    assert obs["body"] == {"marks": True}
    assert outp.read_bytes() == base64.b64decode(PNG_B64)


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def test_status():
    srv = SRV
    srv.state.health_calls = 0
    srv.state.hold_until = 0
    code, out, err = run(["status"])
    assert code == 0, err
    assert "Desktop session: desktop-cli-test" in out
    assert "User control: false" in out
    srv.state.health_calls = 0
    srv.state.hold_until = 1
    code, out, err = run(["status"])
    assert code == 0
    assert "User control: true" in out


# --------------------------------------------------------------------------
# change 1+2: connection failures and non-200 responses no longer traceback,
# and every error message includes the server's reason
# --------------------------------------------------------------------------
def test_observe_503_detail_exit1():
    srv = SRV
    srv.state.observe_override = (503,
        {"detail": "all 2 extra desktops are busy - try again later"})
    code, out, err = run(["look"])
    assert code == 1, err
    assert "all 2 extra desktops are busy - try again later" in err
    assert err.startswith("desktop:")
    assert "Traceback" not in err
    srv.state.observe_override = None


def test_act_503_detail_exit1():
    srv = SRV
    srv.state.health_calls = 0
    srv.state.act_409_times = 0
    srv.state.act_status = 503
    srv.state.act_resp = {"detail": "server is restarting"}
    code, out, err = run(["click", "1"], DESKTOP_POLL="0.01",
                         DESKTOP_WAIT_MAX="5")
    assert code == 1, err
    assert "server is restarting" in out
    assert "run `desktop look`" in out
    assert "Traceback" not in err
    srv.state.act_status = 200
    srv.state.act_resp = {"ok": True}


def test_connection_closed_exit1_no_traceback():
    # Point at a closed port so the connection is refused.
    code, out, err = run(["look"], DESKTOP_API="http://127.0.0.1:1")
    assert code == 1
    assert "Traceback" not in err
    assert err.startswith("desktop:")


# --------------------------------------------------------------------------
# change 3: `desktop done` parks the session (POST /api/park)
# --------------------------------------------------------------------------
def test_done_200():
    srv = SRV
    srv.state.park_status = 200
    srv.state.park_resp = {"ok": True}
    code, out, err = run(["done"])
    assert code == 0, err
    assert "Desktop desktop-cli-test shut down." in out
    # park is called with the token and no body
    p = last(SRV.state, "/api/park")
    assert p["query"]["s"] == ["desktop-cli-test"]
    assert p["headers"]["Authorization"] == "Bearer test-token"


def test_done_404():
    srv = SRV
    srv.state.park_status = 404
    srv.state.park_resp = {"ok": False}
    code, out, err = run(["done"])
    assert code == 0, err
    assert "No desktop running for desktop-cli-test." in out


# --------------------------------------------------------------------------
# vision: `see` and `delegate`
# --------------------------------------------------------------------------
def last_v(path):
    return last(SRV_V.state, path)


def test_see_request_shape_and_output():
    SRV_V.state.requests.clear()
    SRV_V.state.script = [_vision_resp("The Save button is red, bottom-right, 4px low.")]
    code, out, err = run(["see", "what colour is the Save button?"])
    assert code == 0, err
    assert "Asking test-vision-model (it may need a few minutes to load)..." in out
    assert "Desktop session: desktop-cli-test" in out
    assert "Vision model: test-vision-model" in out
    assert "Answer:" in out
    assert "The Save button is red, bottom-right, 4px low." in out

    body = last_v("/chat/completions")["body"]
    assert body["model"] == "test-vision-model"
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 1500
    # `see` must NOT send response_format
    assert "response_format" not in body
    user = body["messages"][1]
    assert user["role"] == "user"
    texts = [b["text"] for b in user["content"] if b["type"] == "text"]
    assert "what colour is the Save button?" in texts[0]
    imgs = [b for b in user["content"] if b["type"] == "image_url"]
    assert imgs and imgs[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_delegate_sends_response_format_only():
    SRV_V.state.script = [_action_resp({"thought": "done", "action": "done",
                                        "report": "all done"})]
    code, out, err = run(["delegate", "click the button", "--steps", "1"])
    assert code == 0, err
    body = last_v("/chat/completions")["body"]
    assert "response_format" in body
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "action"
    # the task appears in the user text
    texts = [b["text"] for b in body["messages"][1]["content"] if b["type"] == "text"]
    assert "click the button" in "\n".join(texts)
    assert "all done" in out
    assert "Steps: 1" in out


def _reset_handback():
    SRV.state.requests.clear()
    SRV.state.health_calls = 0
    SRV.state.hold_until = 0
    SRV.state.act_409_times = 0
    SRV_V.state.idx = 0


def test_delegate_click_type_done_exit0():
    _reset_handback()
    SRV_V.state.script = [
        _action_resp({"thought": "click the input", "action": "click", "id": 1}),
        _action_resp({"thought": "type hello", "action": "type", "id": 1, "text": "hello"}),
        _action_resp({"thought": "finished", "action": "done",
                      "report": "clicked and typed hello"}),
    ]
    code, out, err = run(["delegate", "type hello and finish"])
    assert code == 0, err
    a = acts(SRV.state)
    assert len(a) == 2
    assert a[0]["body"] == {"action": "click", "id": 1}
    assert a[1]["body"] == {"action": "type", "id": 1, "text": "hello",
                            "enter": False, "clear": True}
    assert "step 1: click [1]" in out
    assert 'step 2: type "hello" into [1]' in out
    assert "clicked and typed hello" in out
    assert "Steps: 3" in out


def test_delegate_xy_converts_to_pixels():
    _reset_handback()
    # screen is 1280x800; 0-1000 x=640,y=400 -> 819,320
    SRV_V.state.script = [
        _action_resp({"thought": "click a spot", "action": "click", "x": 640, "y": 400}),
        _action_resp({"thought": "done", "action": "done", "report": "clicked"}),
    ]
    code, out, err = run(["delegate", "click the middle", "--steps", "2"])
    assert code == 0, err
    a = acts(SRV.state)
    assert a[0]["body"] == {"action": "click_xy", "x": 819, "y": 320}


def test_delegate_repeat_guard_refuses_third():
    _reset_handback()
    SRV_V.state.script = [
        _action_resp({"thought": "tab", "action": "key", "keys": "Tab"}),
        _action_resp({"thought": "tab", "action": "key", "keys": "Tab"}),
        _action_resp({"thought": "tab", "action": "key", "keys": "Tab"}),
        _action_resp({"thought": "done", "action": "done", "report": "stopped"}),
    ]
    code, out, err = run(["delegate", "press tab a couple of times", "--steps", "4"])
    assert code == 0, err
    a = acts(SRV.state)
    # only the first two Tab actions are acted; the third identical one is refused
    assert len(a) == 2
    assert a[0]["body"] == {"action": "key", "keys": "Tab"}
    assert a[1]["body"] == {"action": "key", "keys": "Tab"}
    assert "refused repeat" in out
    assert "Steps: 4" in out


def test_delegate_three_bad_replies_exit1():
    _reset_handback()
    SRV_V.state.script = [
        _vision_resp("I can't answer that as JSON"),
        _vision_resp("still not a JSON object"),
        _vision_resp("nope"),
    ]
    code, out, err = run(["delegate", "do something", "--steps", "10"])
    assert code == 1, err
    assert not acts(SRV.state)              # nothing was ever acted
    assert "(stopped before finishing: 3 unparseable replies in a row)" in out
    assert "Steps: 3" in out


def test_delegate_step_cap():
    _reset_handback()
    SRV_V.state.script = [
        _action_resp({"thought": "t1", "action": "type", "id": 1, "text": "a"}),
        _action_resp({"thought": "t2", "action": "type", "id": 1, "text": "b"}),
        _action_resp({"thought": "t3", "action": "type", "id": 1, "text": "c"}),
    ]
    code, out, err = run(["delegate", "type stuff", "--steps", "3"])
    assert code == 1, err
    assert len(acts(SRV.state)) == 3
    assert "(stopped before finishing: the step limit was reached)" in out
    assert "Steps: 3" in out


def test_delegate_reasoning_content_used_when_content_empty():
    _reset_handback()
    SRV_V.state.script = [
        _vision_reasoning("", '{"thought": "via thinking", "action": "done", "report": "from reasoning"}'),
    ]
    code, out, err = run(["delegate", "finish via reasoning", "--steps", "1"])
    assert code == 0, err
    assert "from reasoning" in out
    assert "Steps: 1" in out


def test_vision_server_error_exit1_no_traceback():
    # Vision API unreachable -> clean message, exit 1, no traceback.
    code, out, err = run(["see", "what is here?"], VISION_API="http://127.0.0.1:1")
    assert code == 1, err
    assert "Traceback" not in err
    assert err.startswith("desktop:")



# --------------------------------------------------------------------------
# page problems, errors, verify (2026-10-09)
# --------------------------------------------------------------------------
import copy


def _bad_log():
    return {"url": "http://localhost:8765/", "age_s": 3.0, "requests": 40,
            "by_type": {"Document": 1, "Script": 2, "Image": 30, "Fetch": 7}, "pending": 0,
            "counts": {"errors": 1, "console_errors": 1, "failed": 12},
            "first": ["TypeError: this.initMap is not a function", "403 https://tile.example/1.png"],
            "errors": [{"text": "TypeError: this.initMap is not a function\n    at init", "where": "http://localhost:8765/app.js:102"}],
            "console_errors": [{"type": "error", "text": "Error loading storms: Failed to fetch"}],
            "failed": [{"url": "https://tile.example/%d.png" % i, "status": 403, "type": "Image", "error": "Forbidden"} for i in range(10)]
                      + [{"url": "https://api.example/storms", "status": None, "type": "Fetch", "error": "net::ERR_FAILED (MissingAllowOriginHeader)"},
                         {"url": "http://localhost:8765/favicon.ico", "status": 404, "type": "Other", "error": "Not Found"}],
            "recent": [{"type": "log", "text": "Refreshing data..."}]}


def _with(**changes):
    """Temporarily change the fake server's state; returns a restore function."""
    st = SRV.state
    saved = {k: copy.deepcopy(getattr(st, k)) for k in changes}
    for k, v in changes.items():
        setattr(st, k, v)
    return lambda: [setattr(st, k, v) for k, v in saved.items()]


def test_look_shows_no_problems_line():
    restore = _with(observe_body={**SRV.state.observe_body, "url": "http://localhost:8765/", "watching": True,
                                  "diag": {"counts": {"errors": 0, "console_errors": 0, "failed": 0}, "first": []}})
    try:
        code, out, _ = run(["look"])
    finally:
        restore()
    assert code == 0
    assert "Page problems since it loaded: none" in out


def test_look_shows_problem_counts():
    log = _bad_log()
    restore = _with(observe_body={**SRV.state.observe_body, "url": "http://localhost:8765/", "watching": True,
                                  "diag": {"counts": log["counts"], "first": log["first"]}})
    try:
        code, out, _ = run(["look"])
    finally:
        restore()
    assert "1 JavaScript error, 1 console error, 12 failed requests" in out
    assert "desktop errors" in out
    assert "TypeError: this.initMap is not a function" in out


def test_look_old_api_prints_no_problem_line():
    code, out, _ = run(["look"])      # the default fake observe has no watching/diag keys
    assert code == 0
    assert "Page problems" not in out


def test_errors_lists_everything():
    restore = _with(diag_body={"watching": True, "url": "http://localhost:8765/", "title": "T", "settled": True, "log": _bad_log()})
    try:
        code, out, _ = run(["errors"])
    finally:
        restore()
    assert code == 0
    assert "JavaScript errors (uncaught):" in out and "app.js:102" in out
    assert "Console errors:" in out and "Failed to fetch" in out
    assert "403 Image https://tile.example/0.png" in out
    assert "MissingAllowOriginHeader" in out
    assert "[log] Refreshing data..." in out


def test_errors_clean_page():
    code, out, _ = run(["errors"])
    assert code == 0
    assert "No JavaScript errors, console errors or failed requests" in out


def _review(obj):
    SRV_V.state.script = [_vision_resp(json.dumps(obj))]
    SRV_V.state.idx = 0


def test_verify_pass_reloads_and_reviews():
    _review({"broken": False, "problems": [], "missing": []})
    before = len(SRV.state.requests)
    nv = len(SRV_V.state.requests)
    code, out, _ = run(["verify", "--expect", "a map and a storm list"])
    assert code == 0, out
    assert "VERIFY: PASS" in out
    new = SRV.state.requests[before:]
    assert [r["body"] for r in new if r["path"] == "/api/act"] == [{"action": "reload"}]
    d = [r["body"] for r in new if r["path"] == "/api/diag"]
    assert d and d[0]["settle"] == 20
    req = SRV_V.state.requests[nv]["body"]
    assert req["response_format"]["json_schema"]["name"] == "review"
    assert "a map and a storm list" in req["messages"][1]["content"][0]["text"]


def test_verify_url_navigates_with_scheme():
    _review({"broken": False, "problems": [], "missing": []})
    before = len(SRV.state.requests)
    code, out, _ = run(["verify", "localhost:8001"])
    assert code == 0
    assert [r["body"] for r in SRV.state.requests[before:] if r["path"] == "/api/act"] == [
        {"action": "navigate", "url": "http://localhost:8001"}]


def test_verify_fails_on_errors_and_blocked_images():
    _review({"broken": True, "problems": ["the map is grey with Access blocked text"], "missing": ["storm list"]})
    restore = _with(diag_body={"watching": True, "url": "http://localhost:8765/", "title": "T", "settled": True, "log": _bad_log()})
    try:
        code, out, _ = run(["verify"])
    finally:
        restore()
    assert code == 4
    assert "VERIFY: FAIL" in out
    assert "JavaScript error: TypeError: this.initMap is not a function (http://localhost:8765/app.js:102)" in out
    assert "Console error: Error loading storms" in out
    assert "10 of 30 images failed to load" in out
    assert "Request failed: net::ERR_FAILED (MissingAllowOriginHeader) Fetch https://api.example/storms" in out
    assert "favicon" not in out
    assert "Looks wrong: the map is grey" in out and "Not visible: storm list" in out
    assert "Don't hide errors" in out


def test_verify_few_missing_tiles_is_only_a_warning():
    _review({"broken": False, "problems": [], "missing": []})
    log = _bad_log()
    log.update(errors=[], console_errors=[], failed=log["failed"][:2], by_type={"Image": 100})
    restore = _with(diag_body={"watching": True, "url": "u", "title": "T", "settled": True, "log": log})
    try:
        code, out, _ = run(["verify"])
    finally:
        restore()
    assert code == 0, out
    assert "VERIFY: PASS" in out and "2 of 100 images failed" in out and "Warnings:" in out


def test_verify_broken_text():
    _review({"broken": False, "problems": [], "missing": []})
    restore = _with(observe_body={**SRV.state.observe_body, "text": "Wind speed: undefined mph, pressure NaN mb"})
    try:
        code, out, _ = run(["verify"])
    finally:
        restore()
    assert code == 4
    assert 'the page shows "undefined"' in out


def test_verify_not_watching_fails():
    restore = _with(diag_body={"watching": False, "url": "u", "title": "", "settled": False, "log": None})
    try:
        code, out, _ = run(["verify"])
    finally:
        restore()
    assert code == 4
    assert "not recording page errors" in out and "report-bug" in out


def test_verify_without_vision_model_warns():
    before = len(SRV_V.state.requests)
    code, out, _ = run(["verify"], VISION_MODEL="", VISION_MODELS="")
    assert code == 0, out
    assert len(SRV_V.state.requests) == before         # nobody was asked
    assert "nobody looked at the screenshot" in out


def test_verify_here_does_not_reload():
    _review({"broken": False, "problems": [], "missing": []})
    before = len(SRV.state.requests)
    code, out, _ = run(["verify", "--here", "--expect", "the details panel"])
    assert code == 0, out
    assert "VERIFY (as it is now, not reloaded): PASS" in out
    assert not [r for r in SRV.state.requests[before:] if r["path"] == "/api/act"]


def test_verify_here_refuses_url():
    code, out, err = run(["verify", "--here", "http://localhost:1"])
    assert code == 2 and "give no URL" in err


def test_verify_missing_item_hints_here():
    _review({"broken": False, "problems": [], "missing": ["details panel"]})
    code, out, _ = run(["verify"])
    assert code == 4
    assert "desktop verify --here" in out


def test_verify_goes_through_the_preview(tmp_path):
    _review({"broken": False, "problems": [], "missing": []})
    (tmp_path / "desktop-cli-test").write_text("/p/desktop-cli-test/" + "a" * 32 + "/")
    before = len(SRV.state.requests)
    code, out, _ = run(["verify", "http://localhost:8000/sub/page.html?x=1"], DESKTOP_PREVIEW_STATE=str(tmp_path))
    assert code == 0, out
    assert [r["body"] for r in SRV.state.requests[before:] if r["path"] == "/api/act"] == [
        {"action": "navigate", "url": "http://127.0.0.1:4097/p/desktop-cli-test/" + "a" * 32 + "/sub/page.html?x=1"}]
    assert "the way the user's Preview panel shows it" in out


def test_verify_direct_skips_the_preview(tmp_path):
    _review({"broken": False, "problems": [], "missing": []})
    (tmp_path / "desktop-cli-test").write_text("/p/desktop-cli-test/" + "a" * 32 + "/")
    before = len(SRV.state.requests)
    code, out, _ = run(["verify", "--direct", "localhost:8000"], DESKTOP_PREVIEW_STATE=str(tmp_path))
    assert [r["body"] for r in SRV.state.requests[before:] if r["path"] == "/api/act"] == [
        {"action": "navigate", "url": "http://localhost:8000"}]


def test_verify_ignores_a_bad_preview_file(tmp_path):
    _review({"broken": False, "problems": [], "missing": []})
    (tmp_path / "desktop-cli-test").write_text("http://evil.example/")
    before = len(SRV.state.requests)
    run(["verify", "localhost:8000"], DESKTOP_PREVIEW_STATE=str(tmp_path))
    assert [r["body"] for r in SRV.state.requests[before:] if r["path"] == "/api/act"] == [
        {"action": "navigate", "url": "http://localhost:8000"}]


def test_verify_explains_root_relative_paths(tmp_path):
    _review({"broken": False, "problems": [], "missing": []})
    sig = "b" * 32
    (tmp_path / "desktop-cli-test").write_text("/p/desktop-cli-test/%s/" % sig)
    log = _bad_log()
    log.update(errors=[], console_errors=[], failed=[
        {"url": "http://127.0.0.1:4097/api/storms", "status": 404, "type": "Fetch", "error": "Not Found"},
        {"url": "http://127.0.0.1:4097/p/desktop-cli-test/%s/fine.png" % sig, "status": 404, "type": "Image", "error": "x"}])
    restore = _with(diag_body={"watching": True, "url": "u", "title": "T", "settled": True, "log": log})
    try:
        code, out, _ = run(["verify", "localhost:8000"], DESKTOP_PREVIEW_STATE=str(tmp_path))
    finally:
        restore()
    assert code == 4
    assert 'Why some requests failed: /api/storms starts with "/"' in out
    assert "/p/desktop-cli-test/%s/" % sig in out and 'fetch("api/storms")' in out
    assert "fine.png" not in out.split("Why some requests failed")[1].split("\n")[0]


def test_verify_accepts_the_preview_address_itself(tmp_path):
    _review({"broken": False, "problems": [], "missing": []})
    sig = "c" * 32
    (tmp_path / "desktop-cli-test").write_text("/p/desktop-cli-test/%s/" % sig)
    before = len(SRV.state.requests)
    run(["verify", "http://localhost:4097/p/desktop-cli-test/%s/index.html" % sig], DESKTOP_PREVIEW_STATE=str(tmp_path))
    assert [r["body"] for r in SRV.state.requests[before:] if r["path"] == "/api/act"] == [
        {"action": "navigate", "url": "http://127.0.0.1:4097/p/desktop-cli-test/%s/index.html" % sig}]


# --------------------------------------------------------------------------
# upload: files travel as bytes, attached without Chrome's file picker
# --------------------------------------------------------------------------

def _upload_reset():
    s = SRV.state
    s.requests.clear()
    s.upload_status, s.upload_409_times, s.upload_count = 200, 0, 0
    s.upload_resp = State().upload_resp
    return s


def test_upload_to_element_sends_the_bytes(tmp_path):
    s = _upload_reset()
    f = tmp_path / "lesson.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    code, out, err = run(["upload", "16", str(f)])
    assert code == 0, err
    body = last(s, "/api/upload")["body"]
    assert body["id"] == 16
    assert body["files"] == [{"name": "lesson.pdf", "data": base64.b64encode(b"%PDF-1.4 fake").decode()}]
    assert "Attached to Lesson plan: lesson.pdf" in out
    assert "The input accepts: .pdf" in out
    assert "Page text:" in out                       # the page is shown afterwards
    assert last(s, "/api/upload")["headers"].get("Authorization") == "Bearer test-token"


def test_upload_without_element_and_several_files(tmp_path):
    s = _upload_reset()
    s.upload_resp = dict(s.upload_resp, attached=["a.png"], left_out=["b.png"], closed_dialogs=1)
    for n in ("a.png", "b.png"):
        (tmp_path / n).write_bytes(b"x")
    code, out, err = run(["upload", str(tmp_path / "a.png"), str(tmp_path / "b.png")])
    assert code == 0, err
    body = last(s, "/api/upload")["body"]
    assert "id" not in body and [f["name"] for f in body["files"]] == ["a.png", "b.png"]
    assert "Closed the file picker that was open." in out
    assert "Left out (that input takes one file): b.png" in out


def test_upload_a_file_named_like_a_number_is_a_file(tmp_path):
    s = _upload_reset()
    (tmp_path / "3").write_bytes(b"three")
    (tmp_path / "x.txt").write_bytes(b"x")
    proc = run_raw(["upload", "3", "x.txt"], cwd=str(tmp_path), extra={"CODE_DESKTOP": "desktop-cli-test"})
    assert proc.returncode == 0, proc.stderr
    body = last(s, "/api/upload")["body"]
    assert "id" not in body and [f["name"] for f in body["files"]] == ["3", "x.txt"]


def test_upload_missing_file_exit2(tmp_path):
    s = _upload_reset()
    code, out, err = run(["upload", "4", str(tmp_path / "nope.pdf")])
    assert code == 2
    assert "no such file" in err
    assert last(s, "/api/upload") is None


def test_upload_too_big_exit2(tmp_path):
    s = _upload_reset()
    big = tmp_path / "big.bin"
    with open(big, "wb") as fh:
        fh.truncate(50 * 2**20 + 1)
    code, out, err = run(["upload", str(big)])
    assert code == 2 and "more than 50 MB" in err
    assert last(s, "/api/upload") is None


def test_upload_refused_says_why(tmp_path):
    s = _upload_reset()
    s.upload_status, s.upload_resp = 400, {"ok": False, "error": "this page has no file input"}
    (tmp_path / "a.txt").write_bytes(b"a")
    code, out, err = run(["upload", str(tmp_path / "a.txt")])
    assert code == 1
    assert "upload failed: this page has no file input" in out


def test_upload_old_desktop_api(tmp_path):
    s = _upload_reset()
    s.upload_status, s.upload_resp = 404, {"detail": "Not Found"}
    (tmp_path / "a.txt").write_bytes(b"a")
    code, out, err = run(["upload", str(tmp_path / "a.txt")])
    assert code == 1 and "no upload yet" in err


def test_upload_waits_out_user_control(tmp_path):
    s = _upload_reset()
    s.upload_409_times = 1
    (tmp_path / "a.txt").write_bytes(b"a")
    code, out, err = run(["upload", str(tmp_path / "a.txt")], DESKTOP_POLL="0.01")
    assert code == 0, err
    assert s.upload_count == 2


def test_page_hints_for_file_inputs_and_an_open_picker():
    s = SRV.state
    saved = s.observe_body
    s.observe_body = dict(saved, file_dialog=True,
                          elements=[{"id": 1, "tag": "input[file]", "role": "", "text": "file"}])
    try:
        code, out, err = run(["look"])
    finally:
        s.observe_body = saved
    assert code == 0, err
    assert "Chrome's file picker is open" in out
    assert "desktop upload N FILE" in out
    code, out, err = run(["look"])
    assert "file picker" not in out and "desktop upload" not in out
