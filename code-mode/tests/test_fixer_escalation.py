"""The Fixer view's Cancel and Ask Claude buttons (pipe 0.29.0) go through the pipe's code_fixer ops, which hold the
fixer's token: only for the asking admin's own chat, only for that chat's report. While the fixer hands a report to
Claude (status "escalated") a paused chat keeps waiting and says Claude is on it; the agent is then told whose
verdict it got. A cancel before the turn paused for it leaves the turn running."""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "owui"))
import code_mode  # noqa: E402
from code_mode import FIXER_HOLDS, Pipe, fixer_result_text  # noqa: E402

TOKEN = "fixer-token-123"
ADMIN = {"id": "u1", "role": "admin"}


class Fixer:
    def __init__(self):
        self.records, self.posts = {}, []
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
                parts = path.strip("/").split("/")
                if path == "/api/active":
                    live = [r for r in fake.records.values() if r["status"] != "done"]
                    return self._send(200, {"id": live[-1]["id"], "view": live[-1]["view"]}) if live else \
                        self._send(404, {"error": "none"})
                if parts[:2] == ["api", "incidents"] and parts[2] in fake.records:
                    if parts[-1] == "wait":
                        time.sleep(0.15)
                    return self._send(200, fake.records[parts[2]])
                self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                parts = self.path.strip("/").split("/")
                fake.posts.append((self.path, body))
                rec = fake.records.get(parts[2]) if len(parts) > 3 else None
                if rec is None:
                    return self._send(404, {"error": "unknown incident"})
                if parts[3] == "cancel":
                    if rec["status"] == "done":
                        return self._send(409, {"error": "This report is already finished."})
                    rec.update(status="done", verdict="cancelled", summary="Cancelled by you.")
                    return self._send(200, {"status": "cancelled"})
                if parts[3] == "escalate":
                    rec.update(status="escalated", escalation={"state": "waiting", "by": "user",
                                                               "fixer": {"verdict": rec.get("verdict")}})
                    return self._send(200, {"status": "escalated"})
                self._send(404, {"error": "?"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def add(self, rid, chat, status="investigating", **rec):
        self.records[rid] = dict({"id": rid, "chat_id": chat, "status": status, "source": "user", "title": "T",
                                  "view": "/f/%s/%s/" % (rid, "a" * 32)}, **rec)
        return rid

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def pipe(fixer):
    p = Pipe()
    p.valves.FIXER_URL, p.valves.FIXER_TOKEN = fixer.url, TOKEN
    p._owns_chat = lambda chat_id, user: chat_id in ("chat1", "chat2")
    FIXER_HOLDS.clear()
    return p


def op(p, args, user=ADMIN):
    return asyncio.run(p._fixer_op({"code_fixer": args}, user))


def test_cancel_reaches_only_the_chats_own_report():
    fx = Fixer()
    try:
        p = pipe(fx)
        fx.add("20261026-1000-aaaa", "chat1")
        fx.add("20261026-1001-bbbb", "chat2")
        r = op(p, {"op": "cancel", "chat_id": "chat1", "id": "20261026-1001-bbbb"})
        assert not r["ok"] and "another chat" in r["error"]
        assert not [x for x in fx.posts if x[0].endswith("/cancel")], "nothing sent for another chat's report"
        r = op(p, {"op": "cancel", "chat_id": "chat1", "id": "20261026-1000-aaaa"})
        assert r == {"ok": True, "status": "cancelled"}
        assert fx.records["20261026-1000-aaaa"]["verdict"] == "cancelled"
        r = op(p, {"op": "cancel", "chat_id": "chat1", "id": "20261026-1000-aaaa"})
        assert not r["ok"] and "already finished" in r["error"]
        assert not op(p, {"op": "cancel", "chat_id": "chat1", "id": "../x"})["ok"]
        assert not op(p, {"op": "cancel", "chat_id": "chat1", "id": "20261026-1000-aaaa"}, {"id": "u1", "role": "user"})["ok"]
    finally:
        fx.stop()


def test_a_cancel_before_the_turn_paused_drops_the_hold():
    fx = Fixer()
    try:
        p = pipe(fx)
        fx.add("20261026-1000-aaaa", "chat1")
        FIXER_HOLDS["chat1"] = {"id": "20261026-1000-aaaa", "view": "v", "title": "T", "t": time.time()}
        assert op(p, {"op": "cancel", "chat_id": "chat1", "id": "20261026-1000-aaaa"})["ok"]
        assert "chat1" not in FIXER_HOLDS, "the turn must not pause for a report that is already cancelled"
    finally:
        fx.stop()


def test_ask_claude_reopens_a_finished_report_and_active_says_claude():
    fx = Fixer()
    try:
        p = pipe(fx)
        fx.add("20261026-1000-aaaa", "chat1", status="done", verdict="fixed")
        assert op(p, {"op": "active", "chat_id": "chat1"}) == {"ok": True, "active": ""}
        r = op(p, {"op": "escalate", "chat_id": "chat1", "id": "20261026-1000-aaaa"})
        assert r == {"ok": True, "status": "escalated"}
        a = op(p, {"op": "active", "chat_id": "chat1"})
        assert a["ok"] and a["phase"] == "claude" and a["id"] == "20261026-1000-aaaa" and a["active"].endswith("/")
    finally:
        fx.stop()


def test_a_paused_chat_waits_through_claude_and_says_so():
    fx = Fixer()
    try:
        p = pipe(fx)
        fx.add("20261026-1000-aaaa", "chat1", status="escalated",
               escalation={"state": "claimed", "fixer": {"verdict": "could_not_fix"}})
        said = []

        async def status(text, done=False):
            said.append(text)

        def finish():
            time.sleep(0.8)
            fx.records["20261026-1000-aaaa"].update(
                status="done", verdict="fixed", summary="Claude fixed the route",
                escalation={"state": "done", "fixer": {"verdict": "could_not_fix"}})
        threading.Thread(target=finish, daemon=True).start()
        rec = asyncio.run(p._fixer_wait("20261026-1000-aaaa", status, time.time()))
        assert rec["verdict"] == "fixed"
        assert any("Claude is looking into it" in s for s in said), said
        text = fixer_result_text(rec)
        assert "Verdict: fixed" in text and "Claude took over" in text and "could_not_fix" in text
    finally:
        fx.stop()


def test_the_wait_has_an_end():
    fx = Fixer()
    try:
        p = pipe(fx)
        fx.add("20261026-1000-aaaa", "chat1", status="escalated")
        old = code_mode.FIXER_WAIT_MAX_S
        code_mode.FIXER_WAIT_MAX_S = 0.5

        async def status(text, done=False):
            pass
        try:
            rec = asyncio.run(p._fixer_wait("20261026-1000-aaaa", status, time.time()))
        finally:
            code_mode.FIXER_WAIT_MAX_S = old
        assert "Stopped waiting" in rec["summary"]
    finally:
        fx.stop()


def test_what_the_agent_reads_after_a_cancel():
    text = fixer_result_text({"verdict": "cancelled", "summary": "Cancelled by you."})
    assert "Verdict: cancelled" in text and "carry on without the fixer" in text
