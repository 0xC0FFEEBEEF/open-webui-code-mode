"""The Fixer button (pipe 0.25.0): the user opens the bug fixer on a chat by hand. The page calls the pipe with
code_fixer = {op, chat_id, details}; the pipe asks code-fixer (a fake one here) with its own token and files the
report with the chat's messages. Only an admin, only on their own chat, and the token never reaches the page."""
import asyncio
import copy
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "owui"))
from code_mode import FIXER_CONTEXT_CHARS, Pipe, chat_branch, fixer_context  # noqa: E402

TOKEN = "fixer-token-123"
ADMIN = {"id": "u1", "role": "admin"}
VIEW = "/f/20261020-1200-ab12/" + "c" * 32 + "/"


class FakeFixer:
    """Answers like code-fixer: /api/active, /api/pick, /api/report (Bearer), and records every request."""

    def __init__(self):
        self.requests, self.active, self.pick, self.report_status = [], None, {"model": "qwen3.8-27b", "loaded": True}, 201
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, obj):
                b = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _note(self, body=None):
                fake.requests.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                                      "body": body})
                return self.headers.get("Authorization") == "Bearer " + TOKEN

            def do_GET(self):
                if not self._note():
                    return self._send(401, {"error": "unauthorized"})
                if self.path.startswith("/api/active"):
                    return self._send(200, {"id": "x", "view": fake.active}) if fake.active else self._send(404, {})
                if self.path == "/api/pick" and fake.pick is not None:
                    return self._send(200, fake.pick)
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not self._note(body):
                    return self._send(401, {"error": "unauthorized"})
                if self.path == "/api/report":
                    if fake.report_status != 201:
                        return self._send(fake.report_status, {"error": "source must be 'chat' or 'code'"})
                    return self._send(201, {"id": "20261020-1200-ab12", "view": VIEW, "status": "queued"})
                self._send(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def stop(self):
        self.srv.shutdown()


CHAT = {"history": {"currentId": "m4", "messages": {
    "m1": {"id": "m1", "parentId": None, "role": "user", "content": "build me a clock"},
    "m2": {"id": "m2", "parentId": "m1", "role": "assistant", "content": "Done. [Preview](http://x/p/)"},
    "m2b": {"id": "m2b", "parentId": "m1", "role": "assistant", "content": "an older answer, regenerated away"},
    "m3": {"id": "m3", "parentId": "m2", "role": "user", "content": "the preview is blank"},
    "m4": {"id": "m4", "parentId": "m3", "role": "assistant", "content": "x" * 20000},
}}}


def pipe_with(fake, owns=lambda chat, user: chat == "chat-1" and user.get("id") == "u1"):
    p = Pipe()
    p.valves.FIXER_URL, p.valves.FIXER_TOKEN = fake.base, TOKEN
    p._owns_chat = owns

    async def chat_json(chat_id, user):
        return CHAT if owns(chat_id, user) else {}
    p._chat_json = chat_json
    return p


def call(p, args, user=ADMIN):
    body = {"model": "code_mode.x", "stream": False, "messages": [{"role": "user", "content": "fixer"}],
            "code_fixer": args, "code_github": {"op": "fixer"}}
    return asyncio.run(p.pipe(body, user, None))


def test_status_says_which_model_and_that_none_is_running():
    fake = FakeFixer()
    try:
        got = call(pipe_with(fake), {"op": "status", "chat_id": "chat-1"})
        assert got == {"ok": True, "active": "", "model": "qwen3.8-27b", "loaded": True}
        assert all(r["auth"] == "Bearer " + TOKEN for r in fake.requests)
        assert TOKEN not in json.dumps(got)
        fake.pick = {"model": "ornith-1.5-35b-a3b", "loaded": False}
        assert call(pipe_with(fake), {"op": "status", "chat_id": "chat-1"})["loaded"] is False
    finally:
        fake.stop()


def test_status_hands_back_the_running_report():
    fake = FakeFixer()
    fake.active = VIEW
    try:
        got = call(pipe_with(fake), {"op": "status", "chat_id": "chat-1"})
        assert got["ok"] and got["active"] == fake.base + VIEW
    finally:
        fake.stop()


def test_status_with_an_older_fixer_has_no_model_line():
    fake = FakeFixer()
    fake.pick = None  # no /api/pick there
    try:
        got = call(pipe_with(fake), {"op": "status", "chat_id": "chat-1"})
        assert got == {"ok": True, "active": "", "model": "", "loaded": None}
    finally:
        fake.stop()


def test_report_files_the_chat_as_the_user():
    fake = FakeFixer()
    try:
        got = call(pipe_with(fake), {"op": "report", "chat_id": "chat-1",
                                     "details": "The preview stays blank\nafter the last turn"})
        assert got == {"ok": True, "view": fake.base + VIEW, "id": "20261020-1200-ab12"}
        [post] = fake.posts()
        b = post["body"]
        assert post["path"] == "/api/report" and post["auth"] == "Bearer " + TOKEN
        assert b["chat_id"] == "chat-1" and b["source"] == "user" and b["reporter_model"] == "you"
        assert b["title"] == "The preview stays blank" and b["details"] == "The preview stays blank\nafter the last turn"
        texts = [m["content"] for m in b["context"]]
        assert texts[:3] == ["build me a clock", "Done. [Preview](http://x/p/)", "the preview is blank"]
        assert "an older answer, regenerated away" not in json.dumps(b["context"])   # current branch only
        assert len(texts[3]) <= FIXER_CONTEXT_CHARS + 40 and "[... cut ...]" in texts[3]
    finally:
        fake.stop()


def test_report_with_nothing_written_still_goes():
    fake = FakeFixer()
    try:
        assert call(pipe_with(fake), {"op": "report", "chat_id": "chat-1", "details": "  "})["ok"]
        b = fake.posts()[0]["body"]
        assert b["title"] == "Look into this chat" and b["details"] == ""
    finally:
        fake.stop()


def test_a_long_first_line_makes_a_short_title():
    fake = FakeFixer()
    try:
        call(pipe_with(fake), {"op": "report", "chat_id": "chat-1", "details": "word " * 100})
        title = fake.posts()[0]["body"]["title"]
        assert len(title) == 120 and title.endswith("...")
    finally:
        fake.stop()


def test_report_while_one_runs_opens_that_one():
    fake = FakeFixer()
    fake.active = VIEW
    try:
        got = call(pipe_with(fake), {"op": "report", "chat_id": "chat-1", "details": "again"})
        assert got == {"ok": True, "view": fake.base + VIEW, "existing": True}
        assert fake.posts() == []
    finally:
        fake.stop()


def test_refused_report_is_said_plainly():
    fake = FakeFixer()
    fake.report_status = 400
    try:
        got = call(pipe_with(fake), {"op": "report", "chat_id": "chat-1", "details": "x"})
        assert got["ok"] is False and "HTTP 400" in got["error"]
    finally:
        fake.stop()


def test_only_admins_on_their_own_chats():
    fake = FakeFixer()
    try:
        p = pipe_with(fake)
        got = call(p, {"op": "report", "chat_id": "chat-1", "details": "x"}, user={"id": "u1", "role": "user"})
        assert got == {"ok": False, "error": "The Fixer is for admins only."}
        assert call(p, {"op": "report", "chat_id": "chat-2", "details": "x"})["error"] == "This chat can't be found."
        for bad in ("../../etc", "", "a" * 65, None):
            assert call(p, {"op": "status", "chat_id": bad})["ok"] is False
        assert call(p, {"op": "nope", "chat_id": "chat-1"})["error"] == "Unknown Fixer op: nope"
        assert call(p, "status")["ok"] is False
        assert fake.requests == []   # none of these reached the fixer
    finally:
        fake.stop()


def test_not_set_up_and_unreachable():
    p = Pipe()
    p._owns_chat = lambda chat, user: True
    p.valves.FIXER_TOKEN = ""
    assert "isn't set up" in call(p, {"op": "status", "chat_id": "chat-1"})["error"]
    p.valves.FIXER_URL, p.valves.FIXER_TOKEN = "http://127.0.0.1:9", TOKEN
    got = call(p, {"op": "status", "chat_id": "chat-1"})
    assert got["ok"] is False and "can't be reached" in got["error"]


def test_the_chat_branch_and_its_fallbacks():
    assert [m["id"] for m in chat_branch(CHAT)] == ["m1", "m2", "m3", "m4"]
    assert chat_branch({"messages": [{"role": "user", "content": "hi"}]}) == [{"role": "user", "content": "hi"}]
    assert chat_branch({}) == [] and chat_branch(None) == []
    loop = {"history": {"currentId": "a", "messages": {"a": {"parentId": "b", "role": "user", "content": "1"},
                                                        "b": {"parentId": "a", "role": "user", "content": "2"}}}}
    assert len(chat_branch(loop)) == 2   # a broken parent chain ends instead of looping
    parts = {"messages": [{"role": "user", "content": [{"type": "text", "text": "see"}, {"type": "image_url"}]},
                          {"role": "system", "content": "not this"}]}
    assert fixer_context(parts) == [{"role": "user", "content": "see"}]


def test_a_broken_chain_still_gives_the_fixer_the_chat():
    """Chat 00086b8e on 2026-10-20: the current message was a stub with no role and no parent."""
    broken = copy.deepcopy(CHAT)
    broken["history"]["messages"]["stub"] = {"content": "[Preview](x)", "output": []}
    broken["history"]["currentId"] = "stub"
    broken["messages"] = [broken["history"]["messages"][k] for k in ("m1", "m2", "m3")]
    assert [m["content"] for m in fixer_context(broken)] == ["build me a clock", "Done. [Preview](http://x/p/)",
                                                             "the preview is blank"]
    del broken["messages"]   # no flat list either: every message by time
    by_time = copy.deepcopy(broken)
    for k, ts in (("m1", 100), ("m2", 101), ("m2b", 101.5), ("m3", 102), ("m4", 103)):
        by_time["history"]["messages"][k]["timestamp"] = ts
    got = [m["content"][:12] for m in fixer_context(by_time)]   # the stub has no role: left out
    assert got == ["build me a c", "Done. [Previ", "an older ans", "the preview ", "xxxxxxxxxxxx"]
    one = {"history": {"currentId": "a", "messages": {"a": {"role": "user", "content": "hi", "parentId": None}}}}
    assert fixer_context(one) == [{"role": "user", "content": "hi"}]   # a one-message chat is not "broken"
