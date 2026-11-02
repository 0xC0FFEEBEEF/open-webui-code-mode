"""code-previews forwards the owner of a Code chat to its preview and tells nobody else where it is."""
import importlib.util
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("code_previews", os.path.join(here, "code-previews.py"))
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

SIGNED = "http://ai-sandbox:4097/p/chat-0001/" + "ab" * 16 + "/"


class FakeOwui(BaseHTTPRequestHandler):
    """Answers the way Open WebUI does for a pipe call: the pipe's dict, 401 without a valid sign-in."""
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        FakeOwui.seen.append({"auth": self.headers.get("Authorization"), "body": body})
        token = (self.headers.get("Authorization") or "")[7:]
        if token not in ("owner", "someone-else"):
            self.send_response(401)
            self.end_headers()
            return
        owns = token == "owner" and body["code_preview"]["chat"] == "chat-0001"
        out = json.dumps({"ok": True, "url": SIGNED} if owns else {"ok": False}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def srv(monkeypatch, capfd):
    FakeOwui.seen = []
    owui = HTTPServer(("127.0.0.1", 0), FakeOwui)
    threading.Thread(target=owui.serve_forever, daemon=True).start()
    monkeypatch.setattr(cp, "OWUI_URL", "http://127.0.0.1:%d" % owui.server_address[1])
    server = ThreadingHTTPServer(("127.0.0.1", 0), cp.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.owui = owui
    yield server
    server.shutdown()
    server.server_close()
    owui.shutdown()
    owui.server_close()


def get(server, path, cookie=None, method="GET"):
    sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=15)
    head = "%s %s HTTP/1.0\r\nHost: cryoai\r\n" % (method, path)
    if cookie:
        head += "Cookie: %s\r\n" % cookie
    sock.sendall((head + "\r\n").encode())
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    sock.close()
    top, _, body = data.partition(b"\r\n\r\n")
    lines = top.decode().split("\r\n")
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:])}
    return int(lines[0].split(" ")[1]), headers, body.decode()


def test_the_owner_is_sent_on_to_the_preview(srv):
    status, headers, body = get(srv, "/previews/chat-0001/", "theme=dark; token=owner")
    assert status == 302 and headers["location"] == SIGNED and body == ""
    assert headers["cache-control"] == "no-store" and headers["referrer-policy"] == "no-referrer"
    assert FakeOwui.seen[-1]["auth"] == "Bearer owner"
    sent = FakeOwui.seen[-1]["body"]
    assert sent["code_preview"] == {"chat": "chat-0001"} and sent["stream"] is False
    assert sent["code_github"] == {"op": "preview"}                 # an older pipe stops at this instead of starting a turn
    assert not {"chat_id", "id", "session_id"} & set(sent)          # with one of these Open WebUI runs a chat turn


def test_the_page_and_query_come_along(srv):
    assert get(srv, "/previews/chat-0001", "token=owner")[1]["location"] == SIGNED
    assert get(srv, "/previews/chat-0001/admin/users?tab=2&q=a%20b", "token=owner")[1]["location"] == SIGNED + "admin/users?tab=2&q=a%20b"
    assert get(srv, "/previews/chat-0001/", "token=owner", method="HEAD")[0] == 302


def test_no_sign_in_gets_a_sign_in_page_and_open_webui_is_not_asked(srv):
    status, headers, body = get(srv, "/previews/chat-0001/")
    assert status == 401 and "Sign in to Open WebUI first" in body
    assert 'href="http://cryoai:8080/"' in body and "location" not in headers
    assert FakeOwui.seen == []
    assert "ai-sandbox" not in body and "ab" * 16 not in body


def test_a_sign_in_open_webui_refuses_gets_the_sign_in_page(srv):
    status, _, body = get(srv, "/previews/chat-0001/", "token=expired")
    assert status == 401 and "Sign in to Open WebUI first" in body


def test_someone_elses_chat_and_unknown_chats_are_not_found(srv):
    for path, cookie in (("/previews/chat-0001/", "token=someone-else"), ("/previews/chat-9999/", "token=owner")):
        status, headers, body = get(srv, path, cookie)
        assert status == 404 and "location" not in headers and "ab" * 16 not in body


def test_other_addresses_and_other_verbs(srv):
    for path in ("/", "/previews/", "/previews/short/", "/previews/../etc/passwd", "/p/chat-0001/x/", "/api/chats"):
        assert get(srv, path, "token=owner")[0] == 404
    assert get(srv, "/previews/chat-0001/", "token=owner", method="POST")[0] == 405
    assert len(FakeOwui.seen) == 0


def test_open_webui_down(srv):
    srv.owui.shutdown()
    srv.owui.server_close()
    status, _, body = get(srv, "/previews/chat-0001/", "token=owner")
    assert status == 502 and "did not answer" in body


def test_the_log_names_the_chat_but_never_the_sign_in_or_the_signed_link(srv, capfd):
    get(srv, "/previews/chat-0001/x?secret=1", "token=owner")
    err = capfd.readouterr().err
    assert "GET /previews/chat-0001/x 302" in err
    assert "owner" not in err and "ab" * 16 not in err and "secret" not in err


def test_an_answer_that_is_not_a_link_is_not_followed(srv, monkeypatch):
    for bad in ({"ok": True, "url": "javascript:alert(1)"}, {"ok": True, "url": 5}, {"ok": True}, None, "x", {"ok": False, "url": SIGNED}):
        class Odd(FakeOwui):
            def do_POST(self, bad=bad):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                out = json.dumps(bad).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        odd = HTTPServer(("127.0.0.1", 0), Odd)
        threading.Thread(target=odd.serve_forever, daemon=True).start()
        monkeypatch.setattr(cp, "OWUI_URL", "http://127.0.0.1:%d" % odd.server_address[1])
        assert get(srv, "/previews/chat-0001/", "token=owner")[0] == 404
        odd.shutdown()
        odd.server_close()
