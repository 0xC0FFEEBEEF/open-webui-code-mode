"""A report filed with the Fixer button pauses the chat (pipe 0.28.0), the way the agent's own `report-bug` does: the
running turn is stopped between steps (the command in flight finishes first), waits for the fixer's verdict, and
then carries on with it. A turn that starts while such a report still runs waits for it first. The agent's own
reports are not waited on here: its turn already waits inside that command."""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
sys.path.insert(0, os.path.join(here, "..", "owui"))
from code_mode import FIXER_HOLDS, Pipe  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402

TOKEN = "fixer-token-123"
ADMIN = {"id": "u1", "role": "admin"}


class FakeFixer:
    """code-fixer's report API: /api/report files one (running until .finish()), /api/active, /api/incidents/<id>
    and its /wait (which answers at once here, done or not)."""

    def __init__(self, source="user"):
        self.records, self.source = {}, source
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

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self._send(401, {"error": "unauthorized"})
                path = self.path.split("?")[0]
                if path == "/api/active":
                    live = [r for r in fake.records.values() if r["status"] != "done"]
                    return self._send(200, {"id": live[-1]["id"], "view": live[-1]["view"]}) if live else \
                        self._send(404, {"error": "no active incident"})
                parts = path.strip("/").split("/")
                if parts[:2] == ["api", "incidents"] and parts[2] in fake.records:
                    if parts[-1] == "wait":
                        time.sleep(0.2)
                    return self._send(200, fake.records[parts[2]])
                self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                n = len(fake.records) + 1
                rid = "20261026-0400-%04d" % n
                fake.records[rid] = {"id": rid, "status": "running", "verdict": None, "source": body.get("source"),
                                     "title": body.get("title"), "view": "/f/%s/%s/" % (rid, "a" * 32)}
                self._send(201, {"id": rid, "view": fake.records[rid]["view"], "status": "queued"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def file(self, title, source=None):
        rid = "20261026-0300-%04d" % (len(self.records) + 1)
        self.records[rid] = {"id": rid, "status": "running", "verdict": None, "source": source or self.source,
                             "title": title, "view": "/f/%s/%s/" % (rid, "b" * 32)}
        return rid

    def finish(self, rid=None, **rec):
        rid = rid or list(self.records)[-1]
        self.records[rid].update(dict({"status": "done", "verdict": "fixed", "summary": "The preview reloads now.",
                                       "changed": [{"path": "code-preview.py", "why": "stale frame"}]}, **rec))

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def make(tmp_path, script, fixer):
    fake = FakeOpencode(tmp_path / "home")
    fake.script = script
    fake.start()
    p = Pipe()
    v = p.valves
    v.OPENCODE_URL, v.OPENCODE_PASSWORD, v.PROJECTS_ROOT = fake.url, "x", str(tmp_path / "home" / "projects")
    v.ASK_FIRST, v.PREVIEW_SECRET, v.LIVE_URL, v.VIEW_TOKEN, v.FEATURES_TOKEN = False, "", "", "", ""
    v.AUTO_REVIEW, v.AUTO_VERIFY = 0, 0
    v.FIXER_URL, v.FIXER_TOKEN = fixer.url, TOKEN
    os.makedirs(v.PROJECTS_ROOT, exist_ok=True)
    p._owns_chat = lambda chat_id, user: True

    async def chat_json(chat_id, user):
        return {"history": {"messages": {}, "currentId": None}}
    p._chat_json = chat_json
    FIXER_HOLDS.clear()
    return fake, p


def run(p, page=None):
    statuses = []

    async def emit(e):
        if e["type"] == "status":
            statuses.append(e["data"]["description"])

    async def go():
        out = []

        async def turn():
            async for chunk in p._run([{"role": "user", "content": "make the preview work"}], "chat1",
                                      {"id": "m", "providerID": "slerm"}, emit, None, picked="slerm-turing.m", user=ADMIN):
                out.append(chunk)
        task = asyncio.ensure_future(turn())
        if page:
            await page(p)
        await asyncio.wait_for(task, 30)
        return "".join(out)
    return asyncio.run(asyncio.wait_for(go(), 40)), statuses


def test_the_fixer_button_pauses_the_turn_after_the_command_in_flight_and_it_carries_on_with_the_verdict(tmp_path):
    fixer = FakeFixer()
    seen = []

    def script(session, text):
        seen.append(text)
        if len(seen) == 1:
            return [("tool", "shell", {"command": "cargo test"}, "ok", 1.5),  # the command in flight when it is filed
                    ("sleep", 0.5),  # the model writing its next step
                    ("tool", "shell", {"command": "desktop open http://localhost:8001"}, "ok"),
                    ("text", "Opened it.")]
        return [("text", "Back at it: the preview works now.")]
    fake, p = make(tmp_path, script, fixer)

    async def page(p):
        await asyncio.sleep(0.5)  # cargo test is running
        r = await p._fixer_op({"code_fixer": {"op": "report", "chat_id": "chat1",
                                              "details": "The preview shows the Code mode page"}}, ADMIN)
        assert r["ok"] and "chat1" in FIXER_HOLDS
        await asyncio.sleep(2.5)
        assert len(seen) == 1, "still paused while the fixer works"
        fixer.finish()
    try:
        text, statuses = run(p, page)
    finally:
        fake.stop()
        fixer.stop()
    commands = [c for c in fake.replies]  # no approvals in this turn
    assert commands == []
    assert "Paused for the Fixer:** The preview shows the Code mode page" in text
    assert "Fixer:** fixed" in text and "Back at it" in text
    assert any("Paused - the Fixer is investigating" in s for s in statuses)
    assert len(seen) == 2 and "<fixer_result>" in seen[1] and "Verdict: fixed" in seen[1]
    assert "code-preview.py: stale frame" in seen[1] and "Carry on with what you were doing" in seen[1]
    assert "Opened it." not in text, "the step after the command in flight never ran"
    assert not FIXER_HOLDS


def test_a_turn_started_while_a_user_report_runs_waits_for_it_and_gets_the_verdict(tmp_path):
    fixer = FakeFixer()
    rid = fixer.file("The chat is stuck on loading")
    prompts = []

    def script(session, text):
        prompts.append(text)
        return [("text", "ok")]
    fake, p = make(tmp_path, script, fixer)

    async def page(p):
        await asyncio.sleep(1.0)
        assert prompts == [], "nothing goes to the agent before the fixer is done"
        fixer.finish(rid, verdict="not_a_bug", summary="It was the network.")
    try:
        text, statuses = run(p, page)
    finally:
        fake.stop()
        fixer.stop()
    assert "Waiting for the Fixer** to finish looking into: The chat is stuck on loading" in text
    assert "Fixer:** not_a_bug" in text
    assert "<fixer_result>" in prompts[0] and "Before this message" in prompts[0] and "It was the network." in prompts[0]


def test_the_agents_own_report_is_not_waited_on_at_turn_start(tmp_path):
    fixer = FakeFixer(source="code")
    fixer.file("desktop hover ignores --xy")
    prompts = []
    fake, p = make(tmp_path, lambda s, t: prompts.append(t) or [("text", "ok")], fixer)
    try:
        text, _ = run(p)
    finally:
        fake.stop()
        fixer.stop()
    assert "Waiting for the Fixer" not in text and "<fixer_result>" not in prompts[0]


def test_a_report_filed_with_no_turn_running_holds_nothing_over_the_next_turn_once_it_is_done(tmp_path):
    fixer = FakeFixer()
    fake, p = make(tmp_path, lambda s, t: [("tool", "shell", {"command": "ls"}, "ok"), ("text", "ok")], fixer)

    async def file_it():
        return await p._fixer_op({"code_fixer": {"op": "report", "chat_id": "chat1", "details": "x"}}, ADMIN)
    assert asyncio.run(file_it())["ok"] and "chat1" in FIXER_HOLDS
    fixer.finish()
    try:
        text, _ = run(p)
    finally:
        fake.stop()
        fixer.stop()
    assert "Paused for the Fixer" not in text and "Waiting for the Fixer" not in text and not FIXER_HOLDS


def test_with_the_pause_off_the_button_only_files(tmp_path):
    fixer = FakeFixer()
    fake, p = make(tmp_path, lambda s, t: [("text", "ok")], fixer)
    p.valves.FIXER_PAUSE = False

    async def file_it():
        return await p._fixer_op({"code_fixer": {"op": "report", "chat_id": "chat1", "details": "x"}}, ADMIN)
    try:
        assert asyncio.run(file_it())["ok"] and not FIXER_HOLDS
        text, _ = run(p)
    finally:
        fake.stop()
        fixer.stop()
    assert "Waiting for the Fixer" not in text
