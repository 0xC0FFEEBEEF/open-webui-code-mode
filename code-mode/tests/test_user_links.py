"""The app's address in a reply (pipe 0.19.0). The agent writes "open http://localhost:8000", which is the coding
machine's localhost and means nothing in the user's browser. The reply names the same app at an address the user can
open (http://<the Open WebUI host>/previews/<chat id>/), and only the chat's owner is told where that leads."""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "owui"))
sys.path.insert(0, here)
from code_mode import Pipe, url_port, user_links  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402

LINK = "http://cryoai/previews/chat-1/"


def test_the_apps_address_becomes_the_users_link():
    assert user_links("Open http://localhost:8000 in your browser.", LINK, {8000}) == f"Open {LINK} in your browser."
    assert user_links("It runs on localhost:8000 now", LINK, {8000}) == f"It runs on {LINK} now"
    assert user_links("see http://127.0.0.1:8000/admin?tab=2#top", LINK, {8000}) == f"see {LINK}admin?tab=2#top"
    assert user_links("or http://0.0.0.0:8000/", LINK, [8000]) == f"or {LINK}"


def test_other_ports_are_left_alone():
    said = "The app is at http://localhost:8000. Ollama must be running at http://localhost:11434."
    assert user_links(said, LINK, {8000}) == f"The app is at {LINK}. Ollama must be running at http://localhost:11434."
    assert user_links("http://localhost:8000", LINK, set()) == "http://localhost:8000"      # no app known: no guess
    assert user_links("http://localhost:8000", "", {8000}) == "http://localhost:8000"


def test_sentence_marks_and_markdown_stay_outside_the_link():
    assert user_links("Go to http://localhost:8000/admin.", LINK, {8000}) == f"Go to {LINK}admin."
    assert user_links("[the app](http://localhost:8000/)", LINK, {8000}) == f"[the app]({LINK})"
    assert user_links("**http://localhost:8000**, then", LINK, {8000}) == f"**{LINK}**, then"
    assert user_links("`http://localhost:8000/api/state`", LINK, {8000}) == f"`{LINK}api/state`"


def test_commands_in_code_blocks_and_other_schemes_stay():
    said = "Check it:\n```bash\ncurl http://localhost:8000/api/health\n```\nThen open http://localhost:8000"
    assert user_links(said, LINK, {8000}) == f"Check it:\n```bash\ncurl http://localhost:8000/api/health\n```\nThen open {LINK}"
    assert user_links("ws://localhost:8000/ws and me@localhost:8000", LINK, {8000}) == "ws://localhost:8000/ws and me@localhost:8000"
    assert user_links("https://mylocalhost:8000/", LINK, {8000}) == "https://mylocalhost:8000/"


def test_the_port_of_an_address():
    assert url_port("http://localhost:8000/x") == 8000 and url_port("localhost:3000") == 3000
    assert url_port("https://example.com/") is None and url_port("") is None


class Req:
    def __init__(self, host):
        self.headers = {"host": host}


def test_the_link_is_on_the_host_the_browser_used():
    p = Pipe()
    p.valves.PREVIEW_SECRET = "s3"
    assert p._friendly_url("chat-1", Req("cryoai:8080")) == "http://cryoai/previews/chat-1/"
    assert p._friendly_url("chat-1", Req("cryoai:8080")) == "http://cryoai/previews/chat-1/"
    assert p._friendly_url("chat-1", Req("cryoai")) == "http://cryoai/previews/chat-1/"
    for not_a_browser in (Req("127.0.0.1:8080"), Req("localhost:8080"), Req(""), Req("x/y?z"), None):
        assert p._friendly_url("chat-1", not_a_browser) == "http://cryoai/previews/chat-1/"
    p.valves.PREVIEWS_URL = ""
    assert p._friendly_url("chat-1", Req("cryoai:8080")) == ""
    p.valves.PREVIEWS_URL, p.valves.PREVIEW_SECRET = "http://{host}/previews", ""
    assert p._friendly_url("chat-1", Req("cryoai:8080")) == ""                          # no preview, no address


def test_only_the_chats_owner_is_told_where_its_preview_is():
    p = Pipe()
    p.valves.PREVIEW_SECRET = "s3"
    p._owns_chat = lambda chat, user: chat == "abcd1234-chat" and (user or {}).get("id") == "u1"
    got = p._preview_op({"code_preview": {"chat": "abcd1234-chat"}}, {"id": "u1"})
    assert got == {"ok": True, "url": p._preview_url("abcd1234-chat")} and "/p/abcd1234-chat/" in got["url"]
    assert p._preview_op({"code_preview": {"chat": "abcd1234-chat"}}, {"id": "u2"}) == {"ok": False}
    assert p._preview_op({"code_preview": {"chat": "abcd1234-other"}}, {"id": "u1"}) == {"ok": False}
    for bad in ({"chat": "../../etc/passwd"}, {"chat": ""}, {}, "abcd1234-chat", None):
        assert p._preview_op({"code_preview": bad}, {"id": "u1"}) == {"ok": False}
    p.valves.PREVIEW_SECRET = ""
    assert p._preview_op({"code_preview": {"chat": "abcd1234-chat"}}, {"id": "u1"}) == {"ok": False, "url": ""}


def test_a_preview_call_is_answered_before_anything_else():
    """code-previews sends code_github along so that an older pipe stops at "unknown GitHub op"; this one must not."""
    p = Pipe()
    p.valves.PREVIEW_SECRET = "s3"
    p._owns_chat = lambda chat, user: True
    body = {"model": "code_mode.x", "messages": [{"role": "user", "content": "preview"}],
            "code_preview": {"chat": "abcd1234-chat"}, "code_github": {"op": "preview"}}
    got = asyncio.run(p.pipe(body, {"id": "u1", "role": "user"}, None))
    assert got == {"ok": True, "url": p._preview_url("abcd1234-chat")}


def test_where_open_webui_is_missing_nobody_owns_a_chat():
    assert Pipe()._owns_chat("abcd1234-chat", {"id": "u1"}) is False


# ---- a whole turn against the fake opencode, with a preview server that reports the app's port

class Preview(BaseHTTPRequestHandler):
    app = 8000

    def do_GET(self):
        body = json.dumps({"version": "v1", "files": 2, "entry": None, "app": self.app}).encode()
        self.send_response(200 if self.path.endswith("/__version") else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def turn(tmp_path, said, friendly="http://cryoai/previews/chat1/", app=8000, preview_up=True):
    home = tmp_path / "home"
    fake = FakeOpencode(home)
    fake.agents, fake.script, fake.diff = {"build"}, (lambda session, text: [("text", said)]), []
    fake.start()
    Preview.app = app
    server = HTTPServer(("127.0.0.1", 0), Preview)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not preview_up:
        server.shutdown()
        server.server_close()
    p = Pipe()
    v = p.valves
    v.OPENCODE_URL, v.OPENCODE_PASSWORD, v.PROJECTS_ROOT = fake.url, "x", str(home / "projects")
    v.ASK_FIRST, v.LIVE_URL, v.VIEW_TOKEN, v.FEATURES_TOKEN, v.FIXER_TOKEN = False, "", "", "", ""
    v.PREVIEW_URL, v.PREVIEW_SECRET = f"http://127.0.0.1:{port}", "s3"
    os.makedirs(v.PROJECTS_ROOT, exist_ok=True)

    async def emit(e):
        pass

    async def go():
        return "".join([c async for c in p._run([{"role": "user", "content": "no questions, add a page"}], "chat1",
                                                {"id": "m", "providerID": "slerm"}, emit, None, friendly=friendly,
                                                picked="slerm-turing.m")])
    try:
        return asyncio.run(asyncio.wait_for(go(), 60))
    finally:
        fake.stop()
        if preview_up:
            server.shutdown()
            server.server_close()


SAID = "Done. Open http://localhost:8000 to see it. Your Ollama has to be running at http://localhost:11434."


def test_a_reply_names_the_app_where_the_user_can_open_it(tmp_path):
    text = turn(tmp_path, SAID)
    assert "Open http://cryoai/previews/chat1/ to see it." in text
    assert "localhost:8000" not in text and "running at http://localhost:11434." in text
    assert text.startswith("[Preview](http://127.0.0.1:")                                   # the Preview link is still first


def test_a_port_that_is_not_the_chats_app_stays(tmp_path):
    assert "Open http://localhost:8000 to see it." in turn(tmp_path, SAID, app=3000)
    assert "Open http://localhost:8000 to see it." in turn(tmp_path, SAID, app=None)


def test_no_address_to_give_or_no_preview_server_changes_nothing(tmp_path):
    assert "Open http://localhost:8000 to see it." in turn(tmp_path, SAID, friendly="")
    assert "Open http://localhost:8000 to see it." in turn(tmp_path, SAID, preview_up=False)
