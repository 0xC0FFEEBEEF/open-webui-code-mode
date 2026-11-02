"""Approvals and questions wait in the page's box (pipe 0.28.0). Open WebUI's own pop-up went to the one tab that sent
the message and was gone for good once that tab reloaded or left the chat, while Open WebUI waited for its answer
forever (2026-10-26: a frozen reply, an agent waiting on an approval nobody could see). Now the turn lists each one
in PAGE_ASKS, the page fetches the list (code_ask {op: list}) in any tab or browser of the chat's owner, and answers
it (code_ask {op: answer}). A request no turn waits on any more is listed straight from opencode and answered there."""
import asyncio
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
sys.path.insert(0, os.path.join(here, "..", "owui"))
import code_mode  # noqa: E402
from code_mode import PAGE_ASKS, PageAsk, Pipe  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402

OWNER = {"id": "u1", "role": "admin"}
OTHER = {"id": "u2", "role": "user"}
TITLES = {"chat1": "Space weather"}


def make(tmp_path, script):
    fake = FakeOpencode(tmp_path / "home")
    fake.script = script
    fake.start()
    p = Pipe()
    v = p.valves
    v.OPENCODE_URL, v.OPENCODE_PASSWORD, v.PROJECTS_ROOT = fake.url, "x", str(tmp_path / "home" / "projects")
    v.ASK_FIRST, v.PREVIEW_SECRET, v.LIVE_URL, v.VIEW_TOKEN, v.FEATURES_TOKEN, v.FIXER_TOKEN = False, "", "", "", "", ""
    v.AUTO_REVIEW, v.AUTO_VERIFY = 0, 0
    os.makedirs(v.PROJECTS_ROOT, exist_ok=True)

    async def title(self, chat_id, user):  # Open WebUI's chat table: chat1 is u1's
        return TITLES.get(chat_id) if (user or {}).get("id") == "u1" else None
    p._chat_title = title.__get__(p)
    code_mode._direct_scan["t"] = 0.0
    PageAsk.POLL = 0.2
    return fake, p


def turn(p, user=OWNER, page=None):
    """Runs one turn in chat1 while `page(p)` (a coroutine) plays the page; (reply text, page's result)."""
    async def go():
        out = []

        async def run():
            async for chunk in p._run([{"role": "user", "content": "tidy the build folder"}], "chat1",
                                      {"id": "m", "providerID": "slerm"}, None, None, picked="slerm-turing.m", user=user):
                out.append(chunk)
        task = asyncio.ensure_future(run())
        got = await page(p) if page else None
        await asyncio.wait_for(task, 20)
        return "".join(out), got
    return asyncio.run(asyncio.wait_for(go(), 30))


async def until(fn, timeout=10.0):
    t = 0.0
    while t < timeout:
        got = await fn()
        if got:
            return got
        await asyncio.sleep(0.05)
        t += 0.05
    raise AssertionError("timed out")


async def listed(p, user=OWNER):
    r = await p._ask_op({"code_ask": {"op": "list"}}, user)
    assert r["ok"], r
    return r["asks"]


ASK = ("ask", {"action": "shell", "resources": ["rm -rf build"]})


def test_an_approval_waits_in_the_list_until_the_page_allows_it(tmp_path):
    fake, p = make(tmp_path, lambda s, t: [ASK, ("text", "Removed the build folder.")])

    async def page(p):
        asks = await until(lambda: listed(p))
        a = asks[0]
        assert a["kind"] == "permission" and a["chat_id"] == "chat1" and a["title"] == "Space weather"
        assert a["data"] == {"action": "shell", "resources": ["rm -rf build"]} and not a["direct"]
        assert await listed(p, OTHER) == [], "only the chat's owner sees it"
        refused = await p._ask_op({"code_ask": {"op": "answer", "id": a["id"], "value": {"allow": True}}}, OTHER)
        assert not refused["ok"]
        assert (await p._ask_op({"code_ask": {"op": "answer", "id": a["id"], "value": {"allow": True}}}, OWNER))["ok"]
        return a
    try:
        text, _ = turn(p, page=page)
    finally:
        fake.stop()
    assert fake.replies[0][0] == "permission" and fake.replies[0][2] == {"decision": "once"}
    assert "Allowed: `shell: rm -rf build`" in text
    assert not PAGE_ASKS, "nothing is left waiting"


def test_deny_with_a_reason_lets_it_carry_on_and_says_why(tmp_path):
    fake, p = make(tmp_path, lambda s, t: [ASK, ("text", "Kept it.")])

    async def page(p):
        a = (await until(lambda: listed(p)))[0]
        await p._ask_op({"code_ask": {"op": "answer", "id": a["id"],
                                      "value": {"allow": False, "message": "keep build, delete build/cache"}}}, OWNER)
    try:
        text, _ = turn(p, page=page)
    finally:
        fake.stop()
    assert fake.replies[0][2] == {"decision": "reject", "message": "keep build, delete build/cache"}
    assert "Refused: `shell: rm -rf build` (keep build, delete build/cache)" in text


def test_answered_somewhere_else_the_turn_goes_on_without_answering_twice(tmp_path):
    fake, p = make(tmp_path, lambda s, t: [ASK, ("text", "Done.")])

    async def page(p):
        await until(lambda: listed(p))
        fake.withdraw(next(iter(fake.permissions)))  # e.g. an older page answered it straight to opencode
    try:
        text, _ = turn(p, page=page)
    finally:
        fake.stop()
    assert fake.replies == [] and "Answered elsewhere: `shell: rm -rf build`" in text
    assert not PAGE_ASKS


def test_a_request_no_turn_waits_on_is_listed_from_opencode_and_answered_there(tmp_path):
    # e.g. Open WebUI restarted, or a turn of an older pipe: opencode still holds the request
    fake, p = make(tmp_path, lambda s, t: [ASK, ("text", "ok")])
    try:
        s = {"id": "ses_orphan1", "directory": str(tmp_path), "prompts": [], "prompt_files": [], "agent": "build",
             "metadata": {"owui_chat_id": "chat1"}, "model": {}}
        fake.sessions[s["id"]] = s
        import threading
        threading.Thread(target=fake._play, args=(s, "x"), daemon=True).start()

        async def go():
            asks = await until(lambda: listed(p))
            a = asks[0]
            assert a["direct"] and a["id"].startswith("per_") and a["sid"] == "ses_orphan1" and a["title"] == "Space weather"
            no = await p._ask_op({"code_ask": {"op": "answer", "id": a["id"], "sid": a["sid"],
                                               "value": {"allow": True}}}, OTHER)
            assert not no["ok"] and fake.replies == []
            ok = await p._ask_op({"code_ask": {"op": "answer", "id": a["id"], "sid": a["sid"],
                                               "value": {"allow": True}}}, OWNER)
            assert ok["ok"], ok
            again = await p._ask_op({"code_ask": {"op": "answer", "id": a["id"], "sid": a["sid"],
                                                  "value": {"allow": True}}}, OWNER)
            assert not again["ok"] and again.get("gone")
        asyncio.run(asyncio.wait_for(go(), 15))
    finally:
        fake.stop()
    assert fake.replies[0][0] == "permission" and fake.replies[0][2] == {"decision": "once"}


def test_a_question_waits_in_the_list_with_its_options_and_an_empty_answer_takes_the_first(tmp_path):
    fields = [{"key": "q0", "title": "Data", "description": "Which data?", "options": [
        {"label": "Kp and solar wind", "value": "kp"}, {"label": "Everything", "value": "all"}]}]
    fake, p = make(tmp_path, lambda s, t: [("form", {"fields": fields}), ("text", "Building it.")])

    async def page(p):
        a = (await until(lambda: listed(p)))[0]
        assert a["kind"] == "question" and a["data"]["question"] == "Which data?"
        assert a["data"]["options"] == ["Kp and solar wind", "Everything"]
        await p._ask_op({"code_ask": {"op": "answer", "id": a["id"], "value": ""}}, OWNER)
    try:
        text, _ = turn(p, page=page)
    finally:
        fake.stop()
    assert fake.replies[0][0] == "form" and fake.replies[0][2] == {"answer": {"q0": "kp"}}
    assert "Which data? — **kp**" in text


def test_skipping_a_question_leaves_it_for_the_next_message(tmp_path):
    fields = [{"key": "q0", "title": "Data", "description": "Which data?", "options": [{"label": "All", "value": "all"}]}]
    fake, p = make(tmp_path, lambda s, t: [("form", {"fields": fields}), ("text", "ok")])

    async def page(p):
        a = (await until(lambda: listed(p)))[0]
        await p._ask_op({"code_ask": {"op": "answer", "id": a["id"], "value": False}}, OWNER)
    try:
        text, _ = turn(p, page=page)
    finally:
        fake.stop()
    assert fake.replies == [("form-deleted", fake.replies[0][1], None)]
    assert "Not answered yet" in text and "1. Which data? (All)" in text


def test_with_the_page_box_off_the_old_pop_up_is_used():
    p = Pipe()
    p.valves.PAGE_ASKS = False
    shown = []

    class C:
        async def post(self, path, json=None):
            shown.append((path, json))

    async def call(e):
        shown.append(e["type"])
        return True

    async def status(t, done=False):
        pass
    text = asyncio.run(p._permission(C(), "ses_1", {"id": "per_1", "action": "shell", "resources": ["rm x"]}, call,
                                     status, "chat1", OWNER))
    assert shown[0] == "confirmation" and shown[1] == ("/api/session/ses_1/permission/per_1/reply", {"decision": "once"})
    assert "Allowed" in text and not PAGE_ASKS
