"""Tests for code-preview.py (run with: .venv/bin/pytest -q test_preview.py)."""

import hashlib
import hmac
import os
import socket
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "code-preview.py")
SECRET = "testsecret"
FRAME = "http://cryoai:8080 http://cryoai:8080"

EXPECTED_CSP = (
    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: https:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors " + FRAME
)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sig_for(chat):
    return hmac.new(SECRET.encode(), chat.encode(), hashlib.sha256).hexdigest()[:32]


def raw_request(port, path, method="GET"):
    """Send a raw request line. Deliberately NOT normalised by any client."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    req = "%s %s HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n" % (method, path)
    sock.sendall(req.encode("latin-1"))
    chunks = []
    while True:
        c = sock.recv(65536)
        if not c:
            break
        chunks.append(c)
    sock.close()
    data = b"".join(chunks)
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
    return status, headers, body


class Server:
    def __init__(self, root, secret, port, logpath, frame_ancestors=FRAME):
        self.env = os.environ.copy()
        self.env["PREVIEW_SECRET"] = secret
        self.env["PREVIEW_ROOT"] = str(root)
        self.env["PREVIEW_PORT"] = str(port)
        self.env["PREVIEW_FRAME_ANCESTORS"] = frame_ancestors
        self.port = port
        self.logpath = logpath
        self.proc = None
        self._errf = None
        self.root = root

    def start(self):
        self._errf = open(self.logpath, "w")
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=self._errf,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited early:\n" + self.read_log())
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("server did not start:\n" + self.read_log())

    def read_log(self):
        try:
            return open(self.logpath).read()
        except OSError:
            return ""

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._errf:
            self._errf.close()


@pytest.fixture
def srv(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    chat = root / "chat1"
    chat.mkdir()
    (chat / "index.html").write_text("<!doctype html><title>t</title>")
    (chat / "style.css").write_text("body{color:red}")
    (chat / "note.txt").write_text("hello")
    (chat / "data.bin").write_bytes(bytes(range(256)))
    sub = chat / "sub"
    sub.mkdir()
    (sub / "page.html").write_text("<p>hi</p>")
    (sub / "b.txt").write_text("bbb")
    (chat / ".hidden").write_text("secret")
    git = chat / ".git"
    git.mkdir()
    (git / "config").write_text("[core]")
    try:
        os.symlink("/etc/passwd", chat / "passwd")
    except OSError:
        pass
    os.symlink(".hidden", chat / "hiddenlink")
    os.symlink(".git", chat / "gitlink")

    port = free_port()
    s = Server(root, SECRET, port, tmp_path / "log.txt")
    s.start()
    yield s
    s.stop()


# --------------------------------------------------------------------------- #
# routing / auth
# --------------------------------------------------------------------------- #

def test_good_sig_serves_index(srv):
    st, hdr, body = raw_request(srv.port, "/p/chat1/%s/" % sig_for("chat1"))
    assert st == 200
    assert body == b"<!doctype html><title>t</title>"
    assert hdr["content-type"] == "text/html"


def test_bad_sig_403(srv):
    st, _, _ = raw_request(srv.port, "/p/chat1/%s/" % ("0" * 32))
    assert st == 403


def test_empty_sig_403(srv):
    st, _, _ = raw_request(srv.port, "/p/chat1//")
    assert st == 403


def test_bad_chat_id_404(srv):
    st, _, _ = raw_request(srv.port, "/p/bad-id!/%s/" % sig_for("chat1"))
    assert st == 404


def test_missing_folder_404_with_valid_sig(srv):
    st, _, _ = raw_request(srv.port, "/p/does-not-exist/%s/" % sig_for("does-not-exist"))
    assert st == 404


def test_dotdot_refused(srv):
    st, _, _ = raw_request(srv.port, "/p/chat1/%s/../index.html" % sig_for("chat1"))
    assert st == 404


def test_percent_encoded_dotdot_refused(srv):
    st, _, _ = raw_request(
        srv.port, "/p/chat1/%s/%%2e%%2e%%2f%%2e%%2e%%2fetc%%2fpasswd" % sig_for("chat1")
    )
    assert st == 404


def test_symlink_outside_refused(srv):
    st, _, body = raw_request(srv.port, "/p/chat1/%s/passwd" % sig_for("chat1"))
    assert st == 404
    assert b"root:" not in body


def test_dotfile_refused(srv):
    st, _, _ = raw_request(srv.port, "/p/chat1/%s/.hidden" % sig_for("chat1"))
    assert st == 404


def test_symlink_to_dot_entry_refused(srv):
    for path in ("hiddenlink", "gitlink/config"):
        st, _, body = raw_request(srv.port, "/p/chat1/%s/%s" % (sig_for("chat1"), path))
        assert st == 404, path
        assert b"secret" not in body and b"[core]" not in body


def test_git_dir_refused(srv):
    st, _, _ = raw_request(srv.port, "/p/chat1/%s/.git/config" % sig_for("chat1"))
    assert st == 404


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #

def test_listing_when_no_index(srv):
    st, hdr, body = raw_request(srv.port, "/p/chat1/%s/sub/" % sig_for("chat1"))
    assert st == 200
    txt = body.decode()
    assert "<title>Project files</title>" in txt
    assert "@media (prefers-color-scheme: dark)" in txt
    assert 'href="/p/chat1/%s/sub/page.html"' % sig_for("chat1") in txt
    assert "b.txt" in txt
    # dot entries hidden
    assert ".hidden" not in txt
    assert ".git" not in txt


def test_index_served_in_subdirectory(srv):
    # sub has no index.html; put one and confirm it is served
    (srv.root / "chat1" / "sub" / "index.html").write_text("<h1>sub home</h1>")
    st, _, body = raw_request(srv.port, "/p/chat1/%s/sub/" % sig_for("chat1"))
    assert st == 200
    assert body == b"<h1>sub home</h1>"


def test_listing_is_newest_first(srv):
    newf = srv.root / "chat1" / "sub" / "zzz_new.txt"
    newf.write_text("new")
    _, _, body = raw_request(srv.port, "/p/chat1/%s/sub/" % sig_for("chat1"))
    txt = body.decode()
    assert txt.index("zzz_new.txt") < txt.index("b.txt")


# --------------------------------------------------------------------------- #
# content types
# --------------------------------------------------------------------------- #

def test_known_content_types(srv):
    assert raw_request(srv.port, "/p/chat1/%s/index.html" % sig_for("chat1"))[1]["content-type"] == "text/html"
    assert raw_request(srv.port, "/p/chat1/%s/style.css" % sig_for("chat1"))[1]["content-type"].startswith("text/css")
    assert raw_request(srv.port, "/p/chat1/%s/note.txt" % sig_for("chat1"))[1]["content-type"].startswith("text/plain")
    assert raw_request(srv.port, "/p/chat1/%s/data.bin" % sig_for("chat1"))[1]["content-type"] == "application/octet-stream"


def test_unknown_extension_utf8(srv):
    (srv.root / "chat1" / "utf.zzz").write_text("h\xefllo 你好", encoding="utf-8")
    st, hdr, body = raw_request(srv.port, "/p/chat1/%s/utf.zzz" % sig_for("chat1"))
    assert st == 200
    assert hdr["content-type"] == "text/plain; charset=utf-8"
    assert body.decode("utf-8") == "h\xefllo 你好"


def test_unknown_extension_binary(srv):
    (srv.root / "chat1" / "bin.dat").write_bytes(bytes([0xff, 0xfe, 0x00, 0x80]))
    st, hdr, _ = raw_request(srv.port, "/p/chat1/%s/bin.dat" % sig_for("chat1"))
    assert st == 200
    assert hdr["content-type"] == "application/octet-stream"


# --------------------------------------------------------------------------- #
# headers
# --------------------------------------------------------------------------- #

def test_headers_on_200(srv):
    _, hdr, _ = raw_request(srv.port, "/p/chat1/%s/index.html" % sig_for("chat1"))
    assert hdr["cache-control"] == "no-store"
    assert hdr["x-content-type-options"] == "nosniff"
    assert hdr["referrer-policy"] == "no-referrer"
    assert hdr["content-security-policy"] == EXPECTED_CSP


def test_headers_on_403(srv):
    _, hdr, _ = raw_request(srv.port, "/p/chat1/%s/" % ("0" * 32))
    assert hdr["cache-control"] == "no-store"
    assert hdr["x-content-type-options"] == "nosniff"
    assert hdr["referrer-policy"] == "no-referrer"
    assert hdr["content-security-policy"] == EXPECTED_CSP


def test_headers_on_404(srv):
    _, hdr, _ = raw_request(srv.port, "/p/nope/%s/" % sig_for("nope"))
    assert hdr["cache-control"] == "no-store"
    assert hdr["x-content-type-options"] == "nosniff"
    assert hdr["referrer-policy"] == "no-referrer"
    assert hdr["content-security-policy"] == EXPECTED_CSP


# --------------------------------------------------------------------------- #
# methods
# --------------------------------------------------------------------------- #

def test_head_sends_headers_no_body(srv):
    st, hdr, body = raw_request(srv.port, "/p/chat1/%s/style.css" % sig_for("chat1"), method="HEAD")
    assert st == 200
    assert hdr["content-type"].startswith("text/css")
    assert "content-length" in hdr
    assert body == b""


def test_post_405(srv):
    st, _, _ = raw_request(srv.port, "/p/chat1/%s/" % sig_for("chat1"), method="POST")
    assert st == 405


def test_other_methods_405(srv):
    for m in ("PUT", "DELETE", "PATCH"):
        st, _, _ = raw_request(srv.port, "/p/chat1/%s/" % sig_for("chat1"), method=m)
        assert st == 405


# --------------------------------------------------------------------------- #
# __version
# --------------------------------------------------------------------------- #

def test_version_manifest(srv):
    st, hdr, body = raw_request(srv.port, "/p/chat1/%s/__version" % sig_for("chat1"))
    assert st == 200
    assert hdr["access-control-allow-origin"] == "*"
    data = __import__("json").loads(body.decode())
    assert data["entry"] == "index.html"
    assert isinstance(data["version"], str) and data["version"]
    assert data["files"] >= 4  # index.html, style.css, note.txt, sub/page.html (+data.bin)


def test_version_manifest_contract(srv):
    # Regression: a NameError (e.g. referencing an undefined name) inside
    # Handler._version drops the connection before any response is sent, so the
    # panel sees RemoteDisconnected instead of a JSON manifest. We assert here
    # that __version always returns a well-formed manifest with no body loss.
    st, hdr, body = raw_request(srv.port, "/p/chat1/%s/__version" % sig_for("chat1"))
    assert st == 200
    assert hdr["access-control-allow-origin"] == "*"
    assert hdr["content-type"].startswith("application/json")
    assert body, "empty body: the handler dropped the connection"
    data = __import__("json").loads(body.decode())
    assert set(data) == {"version", "files", "entry", "app"}
    assert data["app"] is None                                        # chat1 runs no server of its own
    assert isinstance(data["version"], str) and data["version"]
    assert isinstance(data["files"], int) and data["files"] >= 0
    assert data["entry"] in (None, "index.html")


def test_version_changes_on_write_and_delete(srv):
    _, _, b1 = raw_request(srv.port, "/p/chat1/%s/__version" % sig_for("chat1"))
    v1 = __import__("json").loads(b1.decode())["version"]
    n1 = __import__("json").loads(b1.decode())["files"]

    (srv.root / "chat1" / "added.txt").write_text("x")
    _, _, b2 = raw_request(srv.port, "/p/chat1/%s/__version" % sig_for("chat1"))
    v2 = __import__("json").loads(b2.decode())["version"]
    n2 = __import__("json").loads(b2.decode())["files"]
    assert v2 != v1
    assert n2 == n1 + 1

    (srv.root / "chat1" / "added.txt").unlink()
    _, _, b3 = raw_request(srv.port, "/p/chat1/%s/__version" % sig_for("chat1"))
    v3 = __import__("json").loads(b3.decode())["version"]
    n3 = __import__("json").loads(b3.decode())["files"]
    assert v3 != v2
    assert n3 == n2 - 1


def test_version_ignores_dot_files(srv):
    _, _, b1 = raw_request(srv.port, "/p/chat1/%s/__version" % sig_for("chat1"))
    v1 = __import__("json").loads(b1.decode())["version"]
    n1 = __import__("json").loads(b1.decode())["files"]
    # a dot file and a .git file must not count or change the version
    (srv.root / "chat1" / ".hidden").write_text("changed")
    (srv.root / "chat1" / ".git" / "config").write_text("changed")
    _, _, b2 = raw_request(srv.port, "/p/chat1/%s/__version" % sig_for("chat1"))
    v2 = __import__("json").loads(b2.decode())["version"]
    n2 = __import__("json").loads(b2.decode())["files"]
    assert v2 == v1
    assert n2 == n1


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #

def test_log_hides_sig(srv):
    raw_request(srv.port, "/p/chat1/%s/index.html" % sig_for("chat1"))
    log = srv.read_log()
    assert sig_for("chat1") not in log
    assert "***" in log
    assert " 200" in log


# --------------------------------------------------------------------------- #
# startup
# --------------------------------------------------------------------------- #

def test_no_secret_exits_2(tmp_path):
    env = os.environ.copy()
    env.pop("PREVIEW_SECRET", None)
    env["PREVIEW_PORT"] = str(free_port())
    r = subprocess.run(
        [sys.executable, SERVER], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )
    assert r.returncode == 2
    assert b"PREVIEW_SECRET" in r.stderr


def test_frame_ancestors_env_override(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    chat = root / "chat1"
    chat.mkdir()
    (chat / "index.html").write_text("<html>")
    port = free_port()
    s = Server(root, SECRET, port, tmp_path / "log.txt", frame_ancestors="https://example.com")
    s.start()
    try:
        _, hdr, _ = raw_request(port, "/p/chat1/%s/index.html" % sig_for("chat1"))
    finally:
        s.stop()
    assert "frame-ancestors https://example.com" in hdr["content-security-policy"]


# --------------------------------------------------------------------------- #
# the chat's own server (2026-10-09): requests pass through to it
# --------------------------------------------------------------------------- #

APP = r'''
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _out(self, code, body, ctype="application/json", extra=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src *")      # must be replaced by the preview's
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path.startswith("/api/hello"):
            self._out(200, json.dumps({"hello": "from the app", "path": self.path, "host": self.headers["Host"]}))
        elif self.path == "/go":
            self._out(302, "", extra={"Location": "/api/hello"})
        elif self.path.startswith("/api/sse"):                 # an event stream: no length, pieces over time
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src *")
            self.end_headers()
            for word in ("one", "two", "three"):
                self.wfile.write(("data: %s\n\n" % word).encode()); self.wfile.flush()
                time.sleep(0.7)
        elif self.path.startswith("/api/live"):                # the same, the way uvicorn sends it: HTTP/1.1, chunked
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for word in ("one", "two", "three"):
                piece = ("data: %s\n\n" % word).encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece)); self.wfile.flush()
                time.sleep(0.7)
            self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
            self.close_connection = True
        elif self.path == "/api/chunks":                       # an answer of unknown length (HTTP/1.0: ends at close)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"rows": [1, 2, '); self.wfile.flush()
            time.sleep(0.2)
            self.wfile.write(b'3]}')
        elif self.path == "/":
            self._out(200, "<p>served by the app</p>", "text/html")
        else:
            self._out(404, "")
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._out(200, json.dumps({"echo": self.rfile.read(n).decode()}))
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


def _start_app(cwd, port):
    proc = subprocess.Popen([sys.executable, "-c", APP, str(port)], cwd=str(cwd),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.05)
    time.sleep(2.2)        # the preview caches "no server here" for 2 s
    return proc


def raw_post(port, path, body):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    data = body.encode()
    sock.sendall(("POST %s HTTP/1.0\r\nHost: localhost\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                  "Connection: close\r\n\r\n" % (path, len(data))).encode() + data)
    chunks = []
    while True:
        c = sock.recv(65536)
        if not c:
            break
        chunks.append(c)
    sock.close()
    head, _, payload = b"".join(chunks).partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), payload


def test_app_server_in_chat_folder_is_used(srv):
    app = _start_app(srv.root / "chat1", free_port())
    try:
        base = "/p/chat1/%s" % sig_for("chat1")
        st, hdr, body = raw_request(srv.port, base + "/api/hello?x=1")
        assert st == 200, body
        got = __import__("json").loads(body)
        assert got["hello"] == "from the app" and got["path"] == "/api/hello?x=1"
        assert hdr["content-security-policy"] == EXPECTED_CSP          # the app's own CSP is replaced
        assert hdr["referrer-policy"] == "no-referrer"
        st, hdr, body = raw_request(srv.port, base + "/")
        assert body == b"<p>served by the app</p>"                    # not index.html from disk
        st, hdr, _ = raw_request(srv.port, base + "/go")
        assert st == 302 and hdr["location"] == base + "/api/hello"   # redirects stay inside the preview
        st, payload = raw_post(srv.port, base + "/api/echo", "ping")
        assert st == 200 and b"ping" in payload                       # an app's API may take POST
        assert "(app)" in srv.read_log()
    finally:
        app.terminate()


def test_an_event_stream_arrives_piece_by_piece(srv):
    """EventSource: the first event must reach the page while the stream is still open, not when it ends."""
    app = _start_app(srv.root / "chat1", free_port())
    try:
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        t0 = time.time()
        sock.sendall(("GET /p/chat1/%s/api/sse/room1?last_count=0 HTTP/1.0\r\nHost: localhost\r\n"
                      "Accept: text/event-stream\r\n\r\n" % sig_for("chat1")).encode())
        got = b""
        while b"data: one" not in got:
            piece = sock.recv(65536)
            assert piece, "the stream closed before the first event: %r" % got
            got += piece
        first = time.time() - t0
        assert first < 1.0, "the first event took %.1f s: the stream was held back until it ended" % first
        assert b"data: three" not in got
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            got += piece
        sock.close()
        head, _, body = got.partition(b"\r\n\r\n")
        assert body == b"data: one\n\ndata: two\n\ndata: three\n\n"
        headers = {k.strip().lower(): v.strip() for k, _, v in
                   (line.partition(":") for line in head.decode().split("\r\n")[1:])}
        assert head.split(b" ")[1] == b"200" and headers["content-type"].startswith("text/event-stream")
        assert headers["content-security-policy"] == EXPECTED_CSP          # the app's own CSP is replaced here too
        assert headers["cache-control"] == "no-store" and "content-length" not in headers
        assert "(app, stream)" in srv.read_log() and sig_for("chat1") not in srv.read_log()
    finally:
        app.terminate()


def test_a_chunked_event_stream_arrives_piece_by_piece_and_unwrapped(srv):
    app = _start_app(srv.root / "chat1", free_port())
    try:
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        t0 = time.time()
        sock.sendall(("GET /p/chat1/%s/api/live HTTP/1.0\r\nHost: localhost\r\n\r\n" % sig_for("chat1")).encode())
        got = b""
        while b"data: one" not in got:
            piece = sock.recv(65536)
            assert piece, "the stream closed before the first event: %r" % got
            got += piece
        assert time.time() - t0 < 1.0 and b"data: three" not in got
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            got += piece
        sock.close()
        head, _, body = got.partition(b"\r\n\r\n")
        assert body == b"data: one\n\ndata: two\n\ndata: three\n\n"   # the chunk framing is the hop's, not the page's
        assert b"transfer-encoding" not in head.lower()
    finally:
        app.terminate()


def test_an_answer_of_unknown_length_passes_whole(srv):
    app = _start_app(srv.root / "chat1", free_port())
    try:
        st, hdr, body = raw_request(srv.port, "/p/chat1/%s/api/chunks" % sig_for("chat1"))
        assert st == 200 and __import__("json").loads(body) == {"rows": [1, 2, 3]}
        assert hdr["content-security-policy"] == EXPECTED_CSP
        st, hdr, body = raw_request(srv.port, "/p/chat1/%s/api/hello" % sig_for("chat1"))
        assert st == 200 and hdr["content-length"] == str(len(body))      # a plain answer still has its length
    finally:
        app.terminate()


def test_a_browser_that_leaves_a_stream_does_not_break_the_server(srv):
    app = _start_app(srv.root / "chat1", free_port())
    try:
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        sock.sendall(("GET /p/chat1/%s/api/sse/room1 HTTP/1.0\r\nHost: localhost\r\n\r\n" % sig_for("chat1")).encode())
        assert sock.recv(65536)
        sock.close()                                                       # the page was closed mid-stream
        time.sleep(2.5)                                                    # the test app is single-threaded
        st, _, body = raw_request(srv.port, "/p/chat1/%s/api/hello" % sig_for("chat1"))
        assert st == 200 and b"from the app" in body
    finally:
        app.terminate()


def test_version_names_the_apps_port(srv):
    port = free_port()
    app = _start_app(srv.root / "chat1", port)
    try:
        _, _, body = raw_request(srv.port, "/p/chat1/%s/__version" % sig_for("chat1"))
        assert __import__("json").loads(body)["app"] == port
    finally:
        app.terminate()


def test_app_still_cannot_reach_dot_paths(srv):
    app = _start_app(srv.root / "chat1", free_port())
    try:
        st, _, _ = raw_request(srv.port, "/p/chat1/%s/.git/config" % sig_for("chat1"))
        assert st == 404
    finally:
        app.terminate()


def test_server_outside_the_chat_folder_is_not_used(srv, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    app = _start_app(other, free_port())
    try:
        st, hdr, body = raw_request(srv.port, "/p/chat1/%s/" % sig_for("chat1"))
        assert st == 200 and body == b"<!doctype html><title>t</title>"   # files from disk
        st, _, _ = raw_request(srv.port, "/p/chat1/%s/" % sig_for("chat1"), method="POST")
        assert st == 405
    finally:
        app.terminate()


def test_another_chats_server_is_not_used(srv):
    (srv.root / "chat2").mkdir()
    app = _start_app(srv.root / "chat2", free_port())
    try:
        st, hdr, body = raw_request(srv.port, "/p/chat1/%s/" % sig_for("chat1"))
        assert body == b"<!doctype html><title>t</title>"
    finally:
        app.terminate()
