"""The Desktop and Upload buttons (pipe 0.30.0, Feature #31): the page asks the pipe for the chat's links with
code_desktop = {"op": "links", "chat_id"} and never starts a coding turn. The upload link is signed over
"upload:" + chat id (code-live checks it), which is not the live window's key. Admins only, own chats only."""
import asyncio
import hashlib
import hmac
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "owui"))
from code_mode import Pipe  # noqa: E402

ADMIN = {"id": "u1", "role": "admin"}
CHAT = "7bd11809-9de2-4093-b599-0b161b957b14"
SECRET = "preview-secret-xyz"


def pipe(owns=True):
    p = Pipe()
    p._owns_chat = lambda chat, user: owns
    p.valves.VIEW_TOKEN, p.valves.DESKTOP_URL = "view-tok", "http://ai-sandbox:6080/"
    p.valves.PREVIEW_SECRET, p.valves.LIVE_URL = SECRET, "http://ai-sandbox:4099"
    return p


def call(p, args, user=ADMIN):
    return asyncio.run(p.pipe({"model": "code_mode.x", "code_desktop": args, "code_github": {"op": "desktop"}},
                              user, None))


def test_links_for_an_admin_on_their_own_chat():
    out = call(pipe(), {"op": "links", "chat_id": CHAT})
    assert out["ok"] is True
    assert out["view"] == "http://ai-sandbox:6080/view?t=view-tok&s=" + CHAT
    sig = hmac.new(SECRET.encode(), ("upload:" + CHAT).encode(), hashlib.sha256).hexdigest()[:32]
    assert out["upload"] == "http://ai-sandbox:4099/u/%s/%s/file" % (CHAT, sig)
    live = hmac.new(SECRET.encode(), ("live:" + CHAT).encode(), hashlib.sha256).hexdigest()[:32]
    assert live not in out["upload"]
    assert out["folder"] == "uploads/"


def test_only_admins_and_only_their_own_chats():
    assert call(pipe(), {"op": "links", "chat_id": CHAT}, user={"id": "u2", "role": "user"})["ok"] is False
    out = call(pipe(owns=False), {"op": "links", "chat_id": CHAT})
    assert out["ok"] is False and "view" not in out
    assert call(pipe(), {"op": "links", "chat_id": "../etc"})["ok"] is False
    assert call(pipe(), {"op": "nope", "chat_id": CHAT})["ok"] is False
    assert call(pipe(), "not a dict")["ok"] is False


def test_unset_valves_give_blank_links():
    p = pipe()
    p.valves.VIEW_TOKEN = ""
    p.valves.PREVIEW_SECRET = ""
    out = call(p, {"op": "links", "chat_id": CHAT})
    assert out == {"ok": True, "view": "", "upload": "", "folder": "uploads/"}
