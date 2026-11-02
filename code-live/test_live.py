#!/usr/bin/env python3
"""Tests for code-live.py against a fake opencode server (no network, no real sessions).

    python3 -m unittest -v test_live.py
"""
import base64
import importlib.util
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SECRET = "test-secret"
CHAT = "chat-1"
SID = "ses_abc"
OTHER_SID = "ses_other"
DIFF_SID = "ses_diff"  # a session that has a project directory, so its edits can be diffed
REVIEW_SID = "ses_review1"      # a review session Code mode's reviewer ran for this chat
FOREIGN_REVIEW_SID = "ses_review9"  # a review session of another chat
REVIEW_ROUND = 2
M1, M2 = "msg_0001user", "msg_0005user"

FAKE_DIR = os.path.realpath(tempfile.mkdtemp())  # what the agent has on disk in the diff tests

# ---------------------------------------------------------------- fake opencode

STATE = {"active": {SID: {"type": "running"}}, "messages": [], "events": queue.Queue(), "event_conns": 0}


def assistant(mid, parts, done=True):
    t = {"created": 1000}
    if done:
        t["completed"] = 2000
    return {"id": mid, "type": "assistant", "time": t, "model": {"id": "qwen-test"}, "content": parts,
            "tokens": {"input": 10, "output": 5, "cache": {"read": 100}}}


def reset_messages():
    STATE["messages"] = [
        {"id": "msg_0000user", "type": "user", "time": {"created": 500}},
        {"id": M1, "type": "user", "time": {"created": 900}},
        assistant("msg_0002", [
            {"type": "reasoning", "text": "plan it", "time": {"created": 1000, "completed": 1100}},
            {"type": "text", "text": "Writing the page."},
            {"type": "tool", "id": "call_w", "name": "write", "state": {"status": "completed",
             "input": {"path": "/home/coder/projects/chats/chat-1/index.html", "content": "<h1>x</h1>"},
             "content": [{"type": "text", "text": "Wrote file"}, {"type": "file", "uri": "data:image/png;base64,AAAA"}]}},
        ]),
        assistant("msg_0003", [
            {"type": "tool", "id": "call_s", "name": "shell", "state": {"status": "error", "input": {"command": "false"},
             "error": {"message": "exit 1"}}},
        ]),
        {"id": "msg_0004idle", "type": "idle", "time": {"created": 2500}},
        {"id": M2, "type": "user", "time": {"created": 3000}},
        assistant("msg_0006", [
            {"type": "reasoning", "text": "still thinking", "time": {"created": 3100}},
            {"type": "tool", "id": "call_x", "name": "write", "state": {"status": "streaming", "input": '{"path": "a.py", "content": "pri'}},
        ], done=False),
    ]


def decode_cursor(c):
    return json.loads(base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)))


def rows_of(diff):
    return [row for h in diff["hunks"] for row in h["lines"]]


def check_rows(tc, diff):
    """What the page needs from every diff row: a kind, a line number on the right side of it, and word marks
    that stay inside the line they belong to. A diff may have no hunks at all (nothing typed has landed yet),
    in which case the page falls back to the plain view and shows the note."""
    for h in diff["hunks"]:
        tc.assertTrue(h["lines"], "a hunk with no rows should have been dropped")
        for kind, old_no, new_no, text, marks in h["lines"]:
            tc.assertIn(kind, " +-")
            tc.assertEqual(kind == " ", old_no is not None and new_no is not None,
                           "an unchanged line carries both numbers, a changed one carries one")
            tc.assertIsInstance(text, str)
            for a, b in marks:
                tc.assertLess(a, b, text)
                tc.assertLessEqual(b, len(text), text)


class FakeOpencode(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        b = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        assert self.headers.get("Authorization", "").startswith("Basic ")
        path, _, qs = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(qs).items()}
        parts = path.strip("/").split("/")
        if path == "/api/session/active":
            return self._json({"data": STATE["active"]})
        if path == "/api/mcp":
            return self._json({"data": STATE.get("mcp", [])})
        if path == "/api/event":
            STATE["event_conns"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"type":"server.connected","data":{}}\n\n')
            self.wfile.flush()
            while True:
                e = STATE["events"].get()
                if e is None:
                    return
                self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n")
                self.wfile.flush()
        if len(parts) == 3 and parts[:2] == ["api", "session"]:
            if parts[2] == DIFF_SID:  # the session whose files the diff tests read
                return self._json({"data": {"id": parts[2], "metadata": {"owui_chat_id": CHAT},
                                            "directory": FAKE_DIR}})
            if parts[2] in (REVIEW_SID, FOREIGN_REVIEW_SID):  # one of a chat's review sessions
                review_of = CHAT if parts[2] == REVIEW_SID else "someone-else"
                return self._json({"data": {"id": parts[2], "metadata": {"owui_review_of": review_of,
                                                                        "owui_review_round": REVIEW_ROUND},
                                            "directory": FAKE_DIR}})
            if parts[2] == "ses_cleared":  # deleted, as Code mode does with an earlier turn's review sessions
                return self._json({"_tag": "SessionNotFoundError", "sessionID": parts[2],
                                   "message": "Session not found: " + parts[2]}, 404)
            if parts[2] == "ses_located":  # how opencode 2.0.18 answers: the directory under "location"
                return self._json({"data": {"id": parts[2], "metadata": {"owui_chat_id": CHAT},
                                            "location": {"directory": FAKE_DIR}}})
            if parts[2] == "ses_nobody":  # a session that belongs to no chat at all
                return self._json({"data": {"id": parts[2], "metadata": {}}})
            meta = {"owui_chat_id": CHAT} if parts[2] == SID else {"owui_chat_id": "someone-else"}
            return self._json({"data": {"id": parts[2], "metadata": meta}})
        if len(parts) >= 4 and parts[3] == "message":
            msgs = STATE["messages"]
            if len(parts) == 5:
                hit = [m for m in msgs if m["id"] == parts[4]]
                return self._json({"data": hit[0]} if hit else {"error": "nf"}, 200 if hit else 404)
            if q.get("type"):
                msgs = [m for m in msgs if m["type"] == q["type"]]
            limit = int(q.get("limit", 50))
            if "cursor" in q:
                c = decode_cursor(q["cursor"])
                assert c["order"] == "asc" and c["direction"] == "next"
                after = [m for m in msgs if m["id"] > c["id"]]
                page = after[:limit]
                nxt = base64.urlsafe_b64encode(json.dumps({"id": page[-1]["id"], "order": "asc", "direction": "next"}).encode()).decode() if page else None
                return self._json({"data": page, "cursor": {"next": nxt}})
            if q.get("order") == "desc":
                return self._json({"data": list(reversed(msgs))[:limit], "cursor": {}})
            if q.get("order") == "asc":   # the first page, oldest first
                page = msgs[:limit]
                nxt = base64.urlsafe_b64encode(json.dumps({"id": page[-1]["id"], "order": "asc", "direction": "next"}).encode()).decode() if page else None
                return self._json({"data": page, "cursor": {"next": nxt}})
        self._json({"error": "unknown " + path}, 404)


# ---------------------------------------------------------------- module under test

def load_module(port):
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "service.json")
    with open(cfg, "w") as fh:
        json.dump({"password": "pw", "port": port}, fh)
    os.environ.update(PREVIEW_SECRET=SECRET, OPENCODE_URL="http://127.0.0.1:%d" % port, OPENCODE_CONFIG=cfg)
    spec = importlib.util.spec_from_file_location("code_live", os.path.join(HERE, "code-live.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpencode)
        cls.fake.daemon_threads = True
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.mod = load_module(cls.fake.server_address[1])
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls.default_diff_every = cls.mod.DIFF_EVERY

    def setUp(self):
        reset_messages()
        STATE["active"] = {SID: {"type": "running"}}
        self.mod._owner.clear()
        self.mod._dirs.clear()
        self.mod._review.clear()
        self.mod.BEFORE.clear()
        self.mod.DIFF_EVERY = self.default_diff_every
        with open(os.path.join(FAKE_DIR, "a.py"), "w") as fh:
            fh.write("def a():\n    return 0\n\n# tail\n")
        with open(os.path.join(FAKE_DIR, "b.py"), "w") as fh:
            fh.write("alpha\nbeta\ngamma\ndelta\n")

    def url(self, chat=CHAT, sid=SID, m=M2, events=False, full=False, sig=None):
        sig = sig or self.mod.live_sig(chat, SECRET)
        return "%s/w/%s/%s/%s?s=%s&m=%s%s" % (self.base, chat, sig, "events" if events else "", sid, m, "&full=1" if full else "")

    def get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def events(self, url, until="end", limit=50, timeout=10):
        """Yield SSE messages as they arrive, until one has k == until (or the stream ends)."""
        n = 0
        with urllib.request.urlopen(url, timeout=timeout) as r:
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    ev = json.loads(line[5:])
                    yield ev
                    n += 1
                    if ev["k"] == until or n >= limit:
                        return

    def stream(self, url, **kw):
        return list(self.events(url, **kw))

    # ---- signing and routing
    def test_sig_is_bound_to_chat_and_not_the_preview_sig(self):
        import hashlib, hmac
        preview = hmac.new(SECRET.encode(), CHAT.encode(), hashlib.sha256).hexdigest()[:32]
        self.assertNotEqual(self.mod.live_sig(CHAT, SECRET), preview)
        self.assertTrue(self.mod.valid_sig(CHAT, self.mod.live_sig(CHAT, SECRET)))
        self.assertFalse(self.mod.valid_sig("chat-2", self.mod.live_sig(CHAT, SECRET)))

    def test_routes(self):
        self.assertEqual(self.get(self.base + "/health")[0], 200)
        self.assertEqual(self.get(self.url(sig="0" * 32))[0], 403)
        self.assertEqual(self.get(self.url(sid="nope"))[0], 404)
        self.assertEqual(self.get(self.url(m="x"))[0], 404)
        self.assertEqual(self.get(self.url(sid=OTHER_SID))[0], 404)  # the session belongs to another chat
        status, headers, body = self.get(self.url())
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("frame-ancestors", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self' http://127.0.0.1:", headers["Content-Security-Policy"])
        self.assertIn("frame-src http://127.0.0.1:*;", headers["Content-Security-Policy"])
        self.assertIn("EventSource", body)

    # ---- review sessions (the reviewer's own opencode session, opened on this chat's window)
    def test_a_review_session_of_this_chat_opens(self):
        status, headers, body = self.get(self.url(sid=REVIEW_SID))
        self.assertEqual(status, 200)
        self.assertIn("EventSource", body)

    def test_a_session_opencode_no_longer_has_says_so(self):
        # an earlier turn's review session is deleted when the next turn starts; its box in the chat still links here
        status, headers, body = self.get(self.url(sid="ses_cleared"))
        self.assertEqual(status, 404)
        self.assertIn("no longer on the coding machine", body)
        self.assertNotIn("not answering", body)
        self.assertEqual(self.get(self.url(sid="ses_cleared", events=True))[0], 404)
        # the chat's box shows one compact line that sizes itself, not a tall frame of plain text
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn('"iframe:height"', body)
        self.assertIn("live: false", body)   # the Code page folds it with the other finished rounds

    def test_a_review_session_of_another_chat_is_refused(self):
        self.assertEqual(self.get(self.url(sid=FOREIGN_REVIEW_SID))[0], 404)

    def test_a_review_link_signed_for_another_chat_is_refused(self):
        self.assertEqual(self.get(self.url(sid=REVIEW_SID, sig=self.mod.live_sig("chat-2", SECRET)))[0], 403)

    def test_session_chat_names_the_chat_a_review_session_belongs_to(self):
        self.assertEqual(self.mod.session_chat(REVIEW_SID), CHAT)
        self.assertEqual(self.mod.session_chat(FOREIGN_REVIEW_SID), "someone-else")
        self.assertIsNone(self.mod.session_chat("ses_nobody"))  # a session that belongs to no chat

    def test_a_review_snapshot_names_its_round(self):
        ev = self.stream(self.url(sid=REVIEW_SID, m=M1, events=True, full=True), until="snap")
        self.assertEqual((ev[0]["k"], ev[0]["review"]), ("snap", {"round": REVIEW_ROUND}))

    def test_the_chat_own_session_snapshot_is_not_a_review(self):
        ev = self.stream(self.url(sid=SID, m=M1, events=True, full=True), until="snap")
        self.assertIn(ev[0].get("review"), (None, {}))

    def test_review_info_reads_the_round(self):
        ri = self.mod.review_info
        self.assertEqual(ri({"owui_review_of": CHAT, "owui_review_round": 3}), {"round": 3})
        self.assertEqual(ri({"owui_review_of": CHAT}), {"round": 0})                # no round recorded
        self.assertEqual(ri({"owui_review_of": CHAT, "owui_review_round": "4"}), {"round": 4})
        self.assertEqual(ri({"owui_review_of": CHAT, "owui_review_round": "?"}), {"round": 0})
        self.assertIsNone(ri({"owui_chat_id": CHAT}))                               # the chat's own session
        self.assertIsNone(ri({}))

    # ---- shaping
    def test_step_from_message(self):
        st = self.mod.step_from_message(STATE["messages"][2])
        kinds = [(p["p"], p.get("o")) for p in st["parts"]]
        self.assertEqual(kinds, [("think", 0), ("text", 0), ("tool", None)])
        tool = st["parts"][2]
        self.assertEqual(tool["out"], "Wrote file\n[image]")  # no data: URI reaches the browser
        err = self.mod.step_from_message(STATE["messages"][3])["parts"][0]
        self.assertEqual((err["status"], err["err"]), ("error", "exit 1"))

    def test_caps(self):
        big = "x" * (self.mod.INPUT_CAP + 10)
        self.assertLess(len(self.mod.slim_input({"content": big})["content"]), self.mod.INPUT_CAP + 100)
        out = self.mod.content_text([{"type": "text", "text": "a" * 10000 + "TAIL"}])
        self.assertTrue(out.endswith("TAIL"))
        self.assertLess(len(out), self.mod.OUTPUT_CAP + 100)

    def test_shape_event(self):
        se = self.mod.shape_event
        self.assertEqual(se({"type": "session.reasoning.delta", "data": {"assistantMessageID": "m", "ordinal": 1, "delta": "hm"}}),
                         {"k": "think+", "id": "m", "o": 1, "d": "hm"})
        self.assertEqual(se({"type": "session.tool.input.delta", "data": {"assistantMessageID": "m", "id": "c", "delta": "{\"pa"}})["d"], "{\"pa")
        self.assertEqual(se({"type": "session.tool.input.started", "data": {"id": "c", "name": "write"}})["status"], "streaming")
        self.assertEqual(se({"type": "session.tool.failed", "data": {"id": "c", "error": {"message": "nope"}}})["err"], "nope")
        self.assertEqual(se({"type": "session.tool.progress", "data": {"id": "c", "metadata": {"output": "line"}}})["out"], "line")
        self.assertIsNone(se({"type": "session.tool.progress", "data": {"id": "c", "metadata": {}}}))
        self.assertIsNone(se({"type": "session.step.failed", "data": {"error": {"type": "aborted"}}}))
        self.assertEqual(se({"type": "session.execution.interrupted", "data": {}}), {"k": "end", "how": "interrupted"})
        self.assertEqual(se({"type": "permission.asked", "data": {"action": "shell", "resources": ["rm -rf b"]}})["level"], "ask")
        self.assertEqual(se({"type": "form.created", "data": {"form": {"fields": [{"description": "Which?"}]}}})["level"], "ask")
        self.assertIsNone(se({"type": "session.usage.updated", "data": {}}))

    # ---- history
    def test_turn_messages_stops_at_idle_and_next_prompt(self):
        msgs, ended = self.mod.turn_messages(SID, M1)
        self.assertEqual([m["id"] for m in msgs], ["msg_0002", "msg_0003"])
        self.assertTrue(ended)
        msgs, ended = self.mod.turn_messages(SID, M2)
        self.assertEqual([m["id"] for m in msgs], ["msg_0006"])
        self.assertFalse(ended)

    def test_paging(self):
        self.mod.PAGE = 1
        try:
            msgs, ended = self.mod.turn_messages(SID, M1)
            self.assertEqual(len(msgs), 2)
            self.assertTrue(ended)
        finally:
            self.mod.PAGE = 50

    def test_turn_is_live(self):
        self.assertTrue(self.mod.turn_is_live(SID, M2))
        self.assertFalse(self.mod.turn_is_live(SID, M1))          # an older prompt
        self.assertTrue(self.mod.turn_is_live(SID, "msg_0009new"))  # filed a moment from now
        STATE["active"] = {}
        self.assertFalse(self.mod.turn_is_live(SID, M2))

    # ---- the stream
    def test_past_turn_is_lazy_then_replays_in_full(self):
        out = self.stream(self.url(m=M1, events=True), until="past")
        self.assertEqual(out, [{"k": "past"}])
        out = self.stream(self.url(m=M1, events=True, full=True))
        self.assertEqual(out[0]["k"], "snap")
        self.assertFalse(out[0]["running"])
        self.assertEqual(out[0]["since"], 900)
        self.assertEqual(len(out[0]["steps"]), 2)
        self.assertEqual(out[-1], {"k": "end", "how": "done"})

    def test_live_turn_streams_until_the_run_ends(self):
        got = queue.Queue()

        def reader():
            for ev in self.events(self.url(events=True)):
                got.put(ev)
            got.put(None)
        threading.Thread(target=reader, daemon=True).start()
        snap = got.get(timeout=10)
        self.assertEqual(snap["k"], "snap")
        self.assertTrue(snap["running"])
        self.assertEqual(snap["steps"][0]["parts"][1]["status"], "streaming")
        self.assertEqual(snap["steps"][0]["parts"][1]["input"], '{"path": "a.py", "content": "pri')
        ev = STATE["events"]
        ev.put({"type": "session.tool.input.delta", "data": {"sessionID": OTHER_SID, "id": "zz", "delta": "other chat"}})
        ev.put({"type": "session.tool.input.delta", "data": {"sessionID": SID, "assistantMessageID": "msg_0006", "id": "call_x", "delta": "nt(1)"}})
        ev.put({"type": "session.usage.updated", "data": {"sessionID": SID}})
        ev.put({"type": "session.tool.success", "data": {"sessionID": SID, "id": "call_x", "content": [{"type": "text", "text": "ok"}]}})
        ev.put({"type": "session.execution.succeeded", "data": {"sessionID": SID}})
        a, b, c = got.get(timeout=10), got.get(timeout=10), got.get(timeout=10)
        self.assertEqual((a["k"], a["d"]), ("tool+", "nt(1)"))  # the other session's delta never arrives
        self.assertEqual((b["k"], b["status"], b["out"]), ("tool", "completed", "ok"))
        self.assertEqual(c, {"k": "end", "how": "succeeded"})
        self.assertIsNone(got.get(timeout=10))

    # ---- compaction overlay (10-25): the window's compact/cprog events
    def test_session_dir_reads_opencode_2_location(self):
        # opencode 2.0.18 nests it; reading only "directory" left every real session without diffs or file views
        self.assertEqual(self.mod.session_dir("ses_located"), FAKE_DIR)
        self.assertEqual(self.mod.session_dir(DIFF_SID), FAKE_DIR)

    def test_compaction_events_are_shaped_for_the_overlay(self):
        se = self.mod.shape_event
        self.assertEqual(se({"type": "session.compaction.started", "created": 7, "data": {"sessionID": SID, "reason": "auto"}}),
                         {"k": "compact", "ph": "start", "reason": "auto", "t0": 7})
        self.assertEqual(se({"type": "session.compaction.delta", "data": {"sessionID": SID, "text": "## Obj"}}),
                         {"k": "compact", "ph": "delta", "d": "## Obj"})
        end = se({"type": "session.compaction.ended", "created": 9, "data": {"sessionID": SID, "text": "## Objective\n- x",
                  "recent": "abc" * 10, "tokens": {"input": 5, "output": 3}}})
        self.assertEqual(end, {"k": "compact", "ph": "end", "text": "## Objective\n- x", "tokens": {"input": 5, "output": 3},
                               "recent": 30, "t1": 9})
        self.assertEqual(se({"type": "session.compaction.failed", "data": {"sessionID": SID, "error": {"message": "boom"}}}),
                         {"k": "compact", "ph": "fail", "err": "boom"})

    def test_compaction_state_of_a_running_and_a_finished_compaction(self):
        cs = self.mod.compaction_state
        self.assertEqual(cs({"type": "compaction", "status": "running", "time": {"created": 5}}), {"ph": "start", "t0": 5})
        done = cs({"type": "compaction", "status": "completed", "time": {"created": 5}, "summary": "## S", "recent": "r" * 8,
                   "tokens": {"output": 2}})
        self.assertEqual((done["ph"], done["text"], done["recent"], done["tokens"]), ("end", "## S", 8, {"output": 2}))

    def compaction_history(self):
        """A session whose history has an older, finished compaction, work after it, and one running now."""
        STATE["messages"] = [
            {"id": "msg_0000user", "type": "user", "text": "before the last compaction", "time": {"created": 100}},
            {"id": "msg_0001cmp", "type": "compaction", "status": "completed", "time": {"created": 200},
             "summary": "## Objective\n- old", "recent": "kept"},
            {"id": M1, "type": "user", "text": "continue\nplease", "time": {"created": 900}},
            assistant("msg_0002", [
                {"type": "reasoning", "text": "plan"}, {"type": "reasoning", "text": " more"},
                {"type": "text", "text": "Building it now."},
                {"type": "tool", "id": "c1", "name": "shell", "state": {"status": "completed",
                 "input": {"command": "cd /home/coder/projects/x && cargo test\n--lib"}, "content": [{"type": "text", "text": "ok " * 50}]}},
                {"type": "tool", "id": "c2", "name": "edit", "state": {"status": "completed",
                 "input": {"filePath": FAKE_DIR + "/src/a.rs", "oldString": "a", "newString": "b"}}},
            ]),
            {"id": M2, "type": "user", "text": "go on", "time": {"created": 3000}},
            {"id": "msg_0006cmp", "type": "compaction", "status": "running", "time": {"created": 3100}},
        ]
        self.mod._dirs[SID] = (FAKE_DIR, time.time())
        self.mod._owner[SID] = (CHAT, time.time())

    def test_the_compaction_tape_reads_back_to_the_last_finished_compaction(self):
        self.compaction_history()
        lines, model = self.mod.compaction_tape(SID)
        self.assertEqual(model, "qwen-test")
        kinds = [(x["k"], x["s"]) for x in lines]
        self.assertEqual(kinds, [("sum", "The summary from the last compaction"), ("you", "continue"),
                                 ("think", "plan"), ("says", "Building it now."), ("sh", "cargo test"),
                                 ("edit", "src/a.rs"), ("you", "go on")])
        self.assertNotIn("before the last compaction", json.dumps(lines))   # older than the last summary
        self.assertEqual(lines[2]["w"], len("plan") + len(" more"))      # one thinking line per run, its size summed
        self.assertGreater(lines[4]["w"], 150)                            # a call weighs its input and its output

    def panel(self, doc):
        """The slerm panel's /api/ai/live, answering doc."""
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                b = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d/api/ai/live" % srv.server_address[1]

    def test_strata_live_reads_the_engine_progress_from_the_panel(self):
        url = self.panel({"servers": [
            {"engine": "llama", "loaded": "other-model", "monitor": {"live": {"state": "idle"}}},
            {"engine": "strata", "loaded": "STRATA-x", "slots": [{"prompt_cached": 117392}],
             "monitor": {"live": {"state": "reading", "prompt_read": 120000, "prompt_total": 199335, "prompt_tokens": 199335,
                                  "generated": None, "tok_s": None, "prefill_tok_s_mean": 358.1}}}]})
        got = self.mod.strata_live("STRATA-x", url)
        self.assertEqual(got, {"state": "reading", "read": 120000, "total": 199335, "prompt": 199335, "cached": 117392,
                               "gen": None, "rate": None, "pps": 358.1})
        self.assertEqual(self.mod.strata_live(None, url)["total"], 199335)   # no model known yet: the Strata server
        self.assertIsNone(self.mod.strata_live("nobody", url))
        self.assertIsNone(self.mod.strata_live("STRATA-x", "http://127.0.0.1:9/nothing"))

    def fake_engine(self, live):
        """The engine's progress from this function instead of the panel, polled every 50 ms."""
        self.addCleanup(setattr, self.mod, "strata_live", self.mod.strata_live)
        self.addCleanup(setattr, self.mod, "COMPACT_POLL", self.mod.COMPACT_POLL)
        self.mod.strata_live = live
        self.mod.COMPACT_POLL = 0.05

    def test_a_compaction_streams_its_tape_progress_summary_and_done(self):
        self.compaction_history()
        STATE["messages"].pop()               # the compaction starts while the window watches
        STATE["messages"].append(assistant("msg_0007", [{"type": "text", "text": "ok"}], done=False))
        polls = []
        self.fake_engine(lambda model: polls.append(model) or {"state": "reading", "read": len(polls) * 1000, "total": 9000})
        got = queue.Queue()

        def reader():
            for e in self.events(self.url(events=True), limit=400):
                got.put(e)
            got.put(None)
        threading.Thread(target=reader, daemon=True).start()
        self.assertEqual(got.get(timeout=10)["k"], "snap")
        ev = STATE["events"]

        def take(pred, what):
            end = time.time() + 10
            while time.time() < end:
                e = got.get(timeout=10)
                if e is None:
                    self.fail("stream ended before " + what)
                if pred(e):
                    return e
            self.fail("no " + what)

        ev.put({"type": "session.compaction.started", "created": 50, "data": {"sessionID": SID, "reason": "auto"}})
        self.assertEqual(take(lambda e: e["k"] == "compact", "start")["ph"], "start")
        tape = take(lambda e: e["k"] == "compact", "tape")
        self.assertEqual(tape["ph"], "tape")
        self.assertEqual(tape["lines"][0]["k"], "sum")
        prog = take(lambda e: e["k"] == "cprog", "progress")
        self.assertEqual((prog["state"], prog["total"]), ("reading", 9000))
        self.assertEqual(polls[0], "qwen-test")           # the model of the session's newest step
        ev.put({"type": "session.compaction.delta", "data": {"sessionID": SID, "text": "## Obj"}})
        self.assertEqual(take(lambda e: e["k"] == "compact", "delta")["d"], "## Obj")
        ev.put({"type": "session.compaction.ended", "created": 60, "data": {"sessionID": SID, "text": "## Obj", "recent": "r"}})
        self.assertEqual(take(lambda e: e["k"] == "compact", "end")["ph"], "end")
        take(lambda e: e["k"] == "cprog", "progress while the new context is read")
        ev.put({"type": "session.text.started", "created": 70, "data": {"sessionID": SID, "assistantMessageID": "msg_0008"}})
        done = take(lambda e: e["k"] in ("compact", "text0"), "done")
        self.assertEqual(done, {"k": "compact", "ph": "done"})   # before the first word of the next step
        self.assertEqual(take(lambda e: e["k"] != "cprog", "the text")["k"], "text0")
        n = len(polls)
        time.sleep(0.3)
        self.assertLessEqual(len(polls) - n, 1, "the progress polls stop once the new context is read")
        ev.put({"type": "session.execution.succeeded", "data": {"sessionID": SID}})
        take(lambda e: e["k"] == "end", "end")

    def test_a_window_opened_during_a_compaction_starts_from_it(self):
        self.compaction_history()
        self.fake_engine(lambda model: {"state": "reading", "read": 5, "total": 10})
        evs = self.stream(self.url(events=True), until="cprog", limit=20)
        self.assertEqual(evs[0]["compacting"], {"ph": "start", "t0": 3100})
        self.assertEqual([e["k"] for e in evs[1:]][-1], "cprog")
        self.assertIn({"k": "compact", "ph": "tape"}, [{"k": e["k"], "ph": e.get("ph")} for e in evs[1:]])
        # finished, and the step after it has not said anything yet: the new context is being read
        STATE["messages"][-1] = {"id": "msg_0006cmp", "type": "compaction", "status": "completed", "time": {"created": 3100},
                                 "summary": "## New", "recent": "rr", "tokens": {"output": 4}}
        STATE["messages"].append(assistant("msg_0007", [], done=False))
        snap = self.stream(self.url(events=True), until="snap")[0]
        self.assertEqual((snap["compacting"]["ph"], snap["compacting"]["text"]), ("end", "## New"))
        STATE["messages"][-1] = assistant("msg_0007", [{"type": "text", "text": "back at work"}], done=False)
        self.assertIsNone(self.stream(self.url(events=True), until="snap")[0]["compacting"])

    # ---- review overlay (10-25): a review window's brief, its reading progress, and the builder's handback
    BRIEF = ("[Code mode review - you are the reviewer here, not the engineer who did the work]\n\n"
             "Conversation context, oldest first (earlier requests are background, not a new checklist):\n1. strip ChatGPT\n2. continue\n\n"
             "Current task to review:\ncontinue\n\nThe engineer's closing summary (claims to check, not facts):\nAll done.\n\n"
             "Reply in the form your instructions give.")
    HANDBACK = ("[Independent review by Code mode - not a message from the user] A reviewer that did not watch you work "
                "compared the project before and after this turn with what the user asked for. It found:\n\n"
                "- [not built] ChatGPT stripped from everything (half built) - core/src/auth.rs still has AuthMode::ChatGPT\n"
                "- [removed] The settings menu (gone)\n\nGo through them one by one. Check each yourself first.\n"
                "- removed: put it back, working and easy to find (visible, with a label).\n")

    def test_handback_findings_are_the_review_lines_only(self):
        self.assertEqual(self.mod.handback_findings(self.HANDBACK),
                         ["[not built] ChatGPT stripped from everything (half built) - core/src/auth.rs still has AuthMode::ChatGPT",
                          "[removed] The settings menu (gone)"])
        self.assertEqual(self.mod.handback_findings("continue"), [])

    def test_a_review_window_gets_its_brief_and_the_engine_progress_until_the_reviewer_starts(self):
        STATE["messages"] = [{"id": M1, "type": "user", "text": self.BRIEF, "time": {"created": 900}},
                             assistant("msg_0002", [], done=False)]
        STATE["active"] = {REVIEW_SID: {"type": "running"}}
        polls = []
        self.fake_engine(lambda model: polls.append(model) or {"state": "reading", "read": 4000, "total": 12617})
        got = queue.Queue()

        def reader():
            for e in self.events(self.url(sid=REVIEW_SID, m=M1, events=True), limit=200):
                got.put(e)
            got.put(None)
        threading.Thread(target=reader, daemon=True).start()
        snap = got.get(timeout=10)
        self.assertEqual((snap["k"], snap["review"], snap["brief"]), ("snap", {"round": REVIEW_ROUND}, self.BRIEF))
        self.assertNotIn("handback", snap)
        prog = got.get(timeout=10)
        self.assertEqual((prog["k"], prog["read"], prog["total"]), ("cprog", 4000, 12617))
        STATE["events"].put({"type": "session.tool.input.started", "created": 5,
                             "data": {"sessionID": REVIEW_SID, "id": "r1", "name": "shell", "assistantMessageID": "msg_0002"}})
        while True:
            e = got.get(timeout=10)
            if e["k"] == "tool":
                break
        n = len(polls)
        time.sleep(0.3)
        self.assertLessEqual(len(polls) - n, 1, "no more progress polls once the reviewer has started")
        STATE["events"].put({"type": "session.execution.succeeded", "data": {"sessionID": REVIEW_SID}})
        while got.get(timeout=10)["k"] != "end":
            pass

    def test_the_builder_run_after_a_review_gets_the_findings_it_was_sent(self):
        STATE["messages"] = [{"id": M1, "type": "user", "text": self.HANDBACK, "time": {"created": 900}},
                             assistant("msg_0002", [], done=False)]
        snap = self.stream(self.url(m=M1, events=True), until="snap")[0]
        self.assertIsNone(snap["review"])
        self.assertNotIn("brief", snap)
        self.assertEqual(len(snap["handback"]["findings"]), 2)
        self.assertTrue(snap["handback"]["findings"][1].startswith("[removed] The settings menu"))

    # ---- Watch mode (10-25): the engine, from the slerm panel, through code-live
    PANEL = {"servers": [{"engine": "strata", "loaded": "STRATA-x", "node": "slerm-node04",
                          "monitor": {"engine": {"max_context": 262144},
                                      "live": {"state": "generating", "phase": "thinking", "tok_s": 23.4, "tok_s_mean": 25.5,
                                               "generated": 670, "prompt_tokens": 210269, "prompt_read": None, "prompt_total": None,
                                               "prefill_tok_s_mean": 50.7},
                                      "feed": {"gpus": [{"name": "Tesla P100", "util_pct": 81, "mem_used_mb": 15308, "mem_total_mb": 16384,
                                                         "temp_c": 60, "power_w": 106.35, "pcie_rx_mb_s": 783.7}],
                                               "host_stats": {"mem_used_mb": 38328, "mem_total_mb": 44913, "cpu_pct": 37.6, "cpus": 36,
                                                              "disk_read_mb_s": 2.14}},
                                      "requests": [{"prompt_tokens": 209984, "reused": 209955, "prompt_read": 29, "output_tokens": 257,
                                                    "decode_tok_s": 31.9, "duration_s": 8.7, "time": 5}] * 15}}]}

    def test_engine_view_keeps_what_watch_mode_shows(self):
        v = self.mod.engine_view(self.PANEL)
        self.assertEqual((v["model"], v["node"], v["state"], v["phase"], v["tok_s"], v["max_context"]),
                         ("STRATA-x", "slerm-node04", "generating", "thinking", 23.4, 262144))
        self.assertEqual(v["gpus"][0], {"name": "Tesla P100", "util": 81, "mem_used": 15308, "mem_total": 16384, "temp": 60,
                                        "power": 106.35, "pcie_rx": 783.7})
        self.assertEqual(v["host"]["cpus"], 36)
        self.assertEqual(len(v["requests"]), 12)
        self.assertEqual(v["requests"][0], {"prompt": 209984, "reused": 209955, "read": 29, "out": 257, "tok_s": 31.9, "secs": 8.7, "t": 5})
        self.assertIsNone(self.mod.engine_view({"servers": []}))

    def test_engine_now_asks_the_panel_once_per_moment(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(inner):
                hits.append(1)
                b = json.dumps(self.PANEL).encode()
                inner.send_response(200)
                inner.send_header("Content-Length", str(len(b)))
                inner.end_headers()
                inner.wfile.write(b)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:%d/api/ai/live" % srv.server_address[1]
        self.mod._engine_cache.update(at=0.0, doc=None)
        self.assertEqual(self.mod.engine_now(url)["tok_s"], 23.4)
        self.assertEqual(self.mod.engine_now(url)["tok_s"], 23.4)
        self.assertEqual(len(hits), 1, "the second ask within the moment is answered from memory")
        self.mod._engine_cache.update(at=0.0, doc=None)
        self.assertIsNone(self.mod.engine_now("http://127.0.0.1:9/nothing"))

    def test_the_engine_route_answers_a_signed_link_with_cors(self):
        self.addCleanup(setattr, self.mod, "engine_now", self.mod.engine_now)
        self.mod.engine_now = lambda url=None: {"state": "idle"}
        status, headers, body = self.get("%s/w/%s/%s/engine" % (self.base, CHAT, self.mod.live_sig(CHAT, SECRET)))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(json.loads(body)["engine"], {"state": "idle"})
        status, _, _ = self.get("%s/w/%s/%s/engine" % (self.base, CHAT, "f" * 32))
        self.assertEqual(status, 403)

    # ---- Watch mode's Tools tab (10-25): calls by tool over the session, MCP servers, review send-backs
    def tool_history(self, n_shell=3):
        msgs = [{"id": "msg_0000user", "type": "user", "text": "build it", "time": {"created": 1}}]
        for i in range(n_shell):
            msgs.append(assistant("msg_%04dasst" % (i + 1), [{"type": "tool", "id": "s%d" % i, "name": "shell", "state": {"status": "completed"}},
                                                              {"type": "tool", "id": "e%d" % i, "name": "edit", "state": {"status": "completed"}}]))
        msgs.append(assistant("msg_0090asst", [{"type": "tool", "id": "m1", "name": "context7_resolve-library-id", "state": {}}]))
        msgs.append({"id": "msg_0091user", "type": "user", "time": {"created": 2}, "text": self.HANDBACK})
        return msgs

    def test_session_stats_count_once_then_only_new_messages(self):
        STATE["messages"] = self.tool_history(3)
        self.mod._stats.clear()
        self.addCleanup(setattr, self.mod, "STATS_TTL", self.mod.STATS_TTL)
        self.mod.STATS_TTL = 0
        st = self.mod.session_stats(SID)
        self.assertEqual(st["tools"], {"shell": 3, "edit": 3, "context7_resolve-library-id": 1})
        self.assertEqual((st["steps"], st["sendbacks"], st["kinds"]), (4, 1, {"not built": 1, "removed": 1}))
        STATE["messages"].append(assistant("msg_0092asst", [{"type": "tool", "id": "s9", "name": "shell", "state": {}}]))
        st = self.mod.session_stats(SID)
        self.assertEqual((st["tools"]["shell"], st["steps"], st["sendbacks"]), (4, 5, 1), "only the new message is added")

    def test_the_tools_route_names_mcp_servers_and_their_calls(self):
        STATE["messages"] = self.tool_history(2) + [{"id": M2, "type": "user", "time": {"created": 3}, "text": "go on"}]
        STATE["mcp"] = [{"name": "context7", "status": {"status": "connected"}}, {"name": "exa", "status": {"status": "connected"}}]
        self.addCleanup(STATE.pop, "mcp", None)
        self.mod._stats.clear()
        status, headers, body = self.get(self.url(m=M2).replace("/?", "/tools?"))
        self.assertEqual((status, headers["Access-Control-Allow-Origin"]), (200, "*"))
        doc = json.loads(body)
        self.assertEqual(doc["tools"][:2], [["edit", 2], ["shell", 2]])
        self.assertEqual((doc["calls"], doc["steps"], doc["sendbacks"]), (5, 3, 1))
        self.assertEqual(doc["mcp"], [{"name": "context7", "status": "connected", "calls": 1}, {"name": "exa", "status": "connected", "calls": 0}])
        self.assertEqual(doc["kinds"], [["not built", 1], ["removed", 1]])

    def test_finished_live_turn_ends_right_after_the_snapshot(self):
        STATE["messages"].append({"id": "msg_0007idle", "type": "idle", "time": {"created": 4000}})
        out = self.stream(self.url(events=True, full=True))
        self.assertFalse(out[0]["running"])
        self.assertEqual(out[1]["k"], "end")

    # ---- live diffs

    def open_events(self, sid, m):
        """Open the event stream in a reader thread and hand back (queue, feed)."""
        got = queue.Queue()

        def reader():
            for ev in self.events(self.url(sid=sid, m=m, events=True)):
                got.put(ev)
            got.put(None)

        threading.Thread(target=reader, daemon=True).start()
        out = []

        def feed(t, data):
            STATE["events"].put({"type": t, "data": dict(data, sessionID=sid)})

        def take(until="end"):
            while True:
                ev = got.get(timeout=10)
                if ev is None:
                    self.fail("the event stream ended early: " + repr(out))
                out.append(ev)
                if ev["k"] == until:
                    return out

        return out, feed, take

    # ---- live git diffs

    def test_a_live_git_diff_arrives_as_a_gitdiff_event(self):
        diff = "\n".join([
            "diff --git a/a.py b/a.py", "--- a/a.py", "+++ b/a.py",
            "@@ -1,4 +1,4 @@",
            " def a():",
            "-    return 0",
            "+    return 1",
            "     # end",
            "diff --git a/b.py b/b.py", "--- /dev/null", "+++ b/b.py",
            "@@ -0,0 +1,2 @@",
            "+print('b')",
            "+x = 1",
        ])
        STATE["messages"] = [{"id": M1, "type": "user", "time": {"created": 3000}}]
        STATE["active"] = {REVIEW_SID: {"type": "running"}}
        out, feed, take = self.open_events(REVIEW_SID, M1)
        snap = take("snap")[0]
        self.assertEqual((snap["k"], snap["review"]), ("snap", {"round": REVIEW_ROUND}))
        call = {"id": "call_g1", "assistantMessageID": "msg_0006"}
        feed("session.tool.input.started", dict(call, name="shell"))
        feed("session.tool.called", dict(call, input={"command": "git diff --cached"}))
        feed("session.tool.success", dict(call, content=[{"type": "text", "text": diff}]))
        feed("session.execution.succeeded", {})
        take()
        kinds = [ev["k"] for ev in out]
        self.assertEqual(kinds.count("gitdiff"), 1)
        self.assertLess(kinds.index("gitdiff"), kinds.index("end"))
        gd = out[kinds.index("gitdiff")]
        self.assertEqual(gd["id"], "call_g1")  # it belongs to the call it follows
        self.assertEqual(out[kinds.index("gitdiff") - 1].get("status"), "completed")
        self.assertEqual(sorted(f["path"] for f in gd["files"]), ["a.py", "b.py"])
        rows = [r for h in gd["files"][0]["hunks"] for r in h["lines"]]
        self.assertIn(["-", 2, None, "    return 0", [[11, 12]]], rows)
        self.assertIn(["+", None, 2, "    return 1", [[11, 12]]], rows)

    def test_a_live_cat_of_a_diff_file_gets_no_gitdiff_event(self):
        diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        STATE["messages"] = [{"id": M1, "type": "user", "time": {"created": 3000}}]
        STATE["active"] = {REVIEW_SID: {"type": "running"}}
        out, feed, take = self.open_events(REVIEW_SID, M1)
        take("snap")
        call = {"id": "call_c1", "assistantMessageID": "msg_0006"}
        feed("session.tool.input.started", dict(call, name="shell"))
        feed("session.tool.called", dict(call, input={"command": "cat changes.diff"}))
        feed("session.tool.success", dict(call, content=[{"type": "text", "text": diff}]))
        feed("session.execution.succeeded", {})
        take()
        self.assertNotIn("gitdiff", [ev["k"] for ev in out])

    def test_live_edit_streams_real_diffs_as_it_is_typed(self):
        self.mod.DIFF_EVERY = 0.0  # every delta recomputes: the test wants each step of the growth
        STATE["messages"] = [{"id": M2, "type": "user", "time": {"created": 3000}}]
        STATE["active"] = {DIFF_SID: {"type": "running"}}
        out, feed, take = self.open_events(DIFF_SID, M2)
        self.assertEqual(take("snap")[0]["k"], "snap")
        call = {"id": "call_e"}
        feed("session.tool.input.started", dict(call, name="edit", assistantMessageID="msg_0006"))
        for d in ('{"path": "a.py", "oldStr',
                  'ing": "    return 0", "newString": "    ret',
                  'urn 1"}'):            feed("session.tool.input.delta", dict(call, delta=d, assistantMessageID="msg_0006"))
        feed("session.tool.input.ended", dict(call, text='{"path": "a.py", "oldString": "    return 0", '
                                                       '"newString": "    return 1"}', assistantMessageID="msg_0006"))
        feed("session.tool.called", dict(call, input={"path": "a.py", "oldString": "    return 0",
                                                      "newString": "    return 1"}, assistantMessageID="msg_0006"))
        feed("session.tool.success", dict(call, content=[{"type": "text", "text": "Edited a.py"}],
                                          assistantMessageID="msg_0006"))
        feed("session.execution.succeeded", {})
        take()

        kinds = [ev["k"] for ev in out]
        self.assertEqual(kinds[1], "tool")            # the call appears before its first diff
        self.assertGreaterEqual(kinds.count("diff"), 3)  # a diff per typed piece, and the final one
        self.assertLess(kinds.index("tool+"), kinds.index("diff"))  # the typing reaches the page first
        self.assertLess(kinds.index("diff"), kinds.index("end"))
        diffs = [ev for ev in out if ev["k"] == "diff"]
        self.assertEqual({d["id"] for d in diffs}, {"call_e"})
        self.assertEqual({d["path"] for d in diffs}, {"a.py"})
        self.assertTrue(all(d["hunks"] for d in diffs))
        for d in diffs:
            check_rows(self, d)
        self.assertEqual(diffs[-1], out[-2])  # the finished diff is the last thing before the turn ends
        rows = [r for h in diffs[-1]["hunks"] for r in h["lines"]]
        self.assertIn(["-", 2, None, "    return 0", [[11, 12]]], rows)
        self.assertIn(["+", None, 2, "    return 1", [[11, 12]]], rows)
        self.assertIn([" ", 1, 1, "def a():", []], rows)      # context
        self.assertIn([" ", 3, 3, "", []], rows)
        self.assertEqual(diffs[-1]["more_old"], 0)
        # mid-typing the green line was only as long as what had been typed, marked on that whole piece
        self.assertIn(["+", None, 2, "    ret", [[4, 7]]], [r for h in diffs[0]["hunks"] for r in h["lines"]])

        # a replay of the same turn shows the same diff: the window kept the file it read
        STATE["active"] = {}  # the turn is over
        STATE["messages"].append(assistant("msg_0006", [
            {"type": "tool", "id": "call_e", "name": "edit", "state": {"status": "completed",
             "input": {"path": "a.py", "oldString": "    return 0", "newString": "    return 1"},
             "content": [{"type": "text", "text": "Edited a.py"}]}}]))
        replay = self.stream(self.url(sid=DIFF_SID, m=M2, events=True, full=True))
        part = replay[0]["steps"][0]["parts"][0]
        self.assertEqual([r[:4] for r in part["diff"]["hunks"][0]["lines"]][:2],
                         [[" ", 1, 1, "def a():"], ["-", 2, None, "    return 0"]])

    def test_live_write_folds_the_old_tail_until_it_is_typed_over(self):
        self.mod.DIFF_EVERY = 0.0  # every delta recomputes: the test wants each step of the growth
        STATE["messages"] = [{"id": M2, "type": "user", "time": {"created": 3000}}]
        STATE["active"] = {DIFF_SID: {"type": "running"}}
        out, feed, take = self.open_events(DIFF_SID, M2)
        take("snap")
        call = {"id": "call_w", "assistantMessageID": "msg_0006"}
        feed("session.tool.input.started", dict(call, name="write"))
        for d in ('{"path": "b.py", "content": "alpha', "\\nBETA", "\\n\"}"):
            feed("session.tool.input.delta", dict(call, delta=d))
        feed("session.tool.success", dict(call, content=[{"type": "text", "text": "Wrote b.py"}]))
        feed("session.execution.succeeded", {})
        take()

        diffs = [ev for ev in out if ev["k"] == "diff"]
        for d in diffs:
            check_rows(self, d)
        self.assertEqual([d["more_old"] for d in diffs], [3, 2, 0, 0])
        # while typing, old lines the model has not reached yet are folded out, not shown as deletions
        self.assertFalse([r for h in diffs[0]["hunks"] for r in h["lines"] if r[0] == "-"])
        self.assertEqual([r[:4] for r in rows_of(diffs[-1])],
                         [[" ", 1, 1, "alpha"], ["-", 2, None, "beta"], ["+", None, 2, "BETA"],
                          ["-", 3, None, "gamma"], ["-", 4, None, "delta"]])
        self.assertEqual(diffs[-1], out[-2])  # and it lands right after the completed tool call

    def test_edits_outside_the_session_directory_get_no_diff(self):
        self.mod.DIFF_EVERY = 0.0
        STATE["messages"] = [{"id": M2, "type": "user", "time": {"created": 3000}}]
        STATE["active"] = {DIFF_SID: {"type": "running"}}
        out, feed, take = self.open_events(DIFF_SID, M2)
        take("snap")
        call = {"id": "call_p", "assistantMessageID": "msg_0006"}
        feed("session.tool.input.started", dict(call, name="edit"))
        feed("session.tool.input.delta", dict(call, delta='{"path": "/etc/crontab", "oldString": "a", '
                                                           '"newString": "b"}'))
        feed("session.tool.success", dict(call, content=[{"type": "text", "text": "no"}]))
        feed("session.execution.succeeded", {})
        take()
        self.assertFalse([ev for ev in out if ev["k"] == "diff"])  # nothing is read outside the chat's own folder
        self.assertTrue(self.mod.BEFORE.has("call_p"))  # it remembers the refusal and does not retry
        self.assertIsNone(self.mod.BEFORE.get("call_p"))

    def test_a_path_that_is_still_being_typed_is_not_read_as_a_file(self):
        # {"path": "a is not a file name yet. Reading it would cache the wrong "before" (an absent file reads as
        # empty) and every diff after it would say the edit could not be placed in the file.
        self.mod.DIFF_EVERY = 0.0
        STATE["messages"] = [{"id": M2, "type": "user", "time": {"created": 3000}}]
        STATE["active"] = {DIFF_SID: {"type": "running"}}
        out, feed, take = self.open_events(DIFF_SID, M2)
        take("snap")
        call = {"id": "call_h", "assistantMessageID": "msg_0006"}
        feed("session.tool.input.started", dict(call, name="edit"))
        for d in ('{"path": "a', '.py", "oldString": "    return 0", "newString": "    ret', 'urn 1"}'):
            feed("session.tool.input.delta", dict(call, delta=d))
        feed("session.tool.success", dict(call, content=[{"type": "text", "text": "Edited a.py"}]))
        feed("session.execution.succeeded", {})
        take()

        diffs = [ev for ev in out if ev["k"] == "diff"]
        self.assertTrue(diffs)
        self.assertEqual({d["path"] for d in diffs}, {"a.py"}, "no diff is named for a half-typed path")
        for d in diffs:
            check_rows(self, d)
            self.assertIsNone(d["note"], "the file was read, so there is nothing to apologise about")
        self.assertEqual(self.mod.BEFORE.get("call_h"), "def a():\n    return 0\n\n# tail\n",
                         "the before is the file the closed path names")
        rows = [r[:4] for r in rows_of(diffs[-1])]
        self.assertIn(["-", 2, None, "    return 0"], rows)
        self.assertIn(["+", None, 2, "    return 1"], rows)


class FixtureWindowTests(unittest.TestCase):
    """Fixture mode end to end: the captured learnlab turn served by this same code, with its edits drawn from the
    file each call actually found. Its own module, because fixture mode replaces how the module reaches opencode."""

    @classmethod
    def setUpClass(cls):
        cls.secret = os.environ.get("PREVIEW_SECRET")
        cls.mod = load_module(0)  # fixture mode never calls opencode
        cls.adding = 0
        cls.path = os.path.join(HERE, "fixtures", "learnlab-iq3-turn.json")
        cls.fx, cls.chat, cls.start, cls.calls, cls.seeded = cls.mod.fixture_prep(cls.path)
        cls.sid = cls.fx["session"]["id"]
        cls.srv = cls.mod.fixture_server(cls.fx, 0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        if cls.secret is None:
            os.environ.pop("PREVIEW_SECRET", None)
        else:
            os.environ["PREVIEW_SECRET"] = cls.secret  # the other tests talk to the fake opencode
        cls.mod.HUB.connected.clear()

    def url(self, events=False, full=True, sig=None, sid=None):
        sig = sig or self.mod.live_sig(self.chat)
        return "%s/w/%s/%s/%s?s=%s&m=%s%s" % (self.base, self.chat, sig, "events" if events else "",
                                              sid or self.sid, self.start, "&full=1" if full else "")

    def get(self, url):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def stream(self, url, until="end", limit=200, timeout=15):
        out, n = [], 0
        with urllib.request.urlopen(url, timeout=timeout) as r:
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    ev = json.loads(line[5:])
                    out.append(ev)
                    n += 1
                    if ev["k"] == until or n >= limit:
                        break
        return out

    def test_the_fixture_answers_for_its_own_chat(self):
        self.assertEqual(self.chat, os.path.basename(self.fx["session"]["directory"].rstrip("/")))
        self.assertTrue(self.mod.valid_sig(self.chat, self.mod.live_sig(self.chat)))
        self.assertFalse(self.mod.valid_sig("some-other-chat", self.mod.live_sig(self.chat)))
        self.assertEqual(self.get(self.base + "/health")[0], 200)
        status, headers, body = self.get(self.url())
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("EventSource", body)  # the window's own page, not a copy
        self.assertEqual(self.get(self.url(sig="0" * 32))[0], 403)
        # a session the fixture does not have is not answerable: the window says so instead of inventing one
        self.assertEqual(self.get(self.url(sid="ses_nope"))[0], 502)

    def test_the_captured_turn_replays_as_a_finished_turn_with_real_diffs(self):
        out = self.stream(self.url(events=True))
        snap = out[0]
        self.assertEqual(snap["k"], "snap")
        self.assertFalse(snap["running"])
        self.assertEqual(out[-1], {"k": "end", "how": "done"})
        self.assertEqual(len(out), 2)  # a fixture has no event stream to follow
        steps = [p for st in snap["steps"] for p in st["parts"] if p["p"] == "tool"]
        edited = [p for p in steps if p["name"] in ("edit", "write")]
        self.assertEqual(len(edited), self.calls)
        with_diff = [p for p in edited if p.get("diff")]
        self.assertEqual(len(with_diff), self.seeded)
        self.assertGreaterEqual(len(with_diff), 8)
        removing = adding = 0
        for p in with_diff:
            self.assertTrue(p["diff"]["hunks"], p["id"])
            check_rows(self, p["diff"])
            self.assertIn(p["diff"]["path"], self.fx["files"], p["id"])
            kinds = [r[0] for r in rows_of(p["diff"])]
            self.assertTrue("+" in kinds or "-" in kinds, p["id"])  # a diff that changes nothing would be a bug
            removing += "-" in kinds
            adding += "+" in kinds
        self.assertGreaterEqual(removing, 6, "most of the capture's edits remove lines")
        self.assertGreaterEqual(adding, 6, "most of them add lines too")
        dropped = [p for p in edited if not p.get("diff")]
        self.assertEqual(len(dropped), self.calls - self.seeded)  # a call whose input the capture dropped gets none

    def test_a_finished_fixture_turn_is_past_without_full(self):
        out = self.stream(self.url(events=True, full=False), until="past")
        self.assertEqual(out, [{"k": "past"}])

    def test_a_bad_fixture_is_refused(self):
        bad = os.path.join(tempfile.mkdtemp(), "not-a-turn.json")
        with open(bad, "w") as fh:
            fh.write('{"session": {"id": "nope"}, "messages": []}')
        with self.assertRaises(ValueError):
            self.mod.fixture_prep(bad)
        with self.assertRaises((OSError, ValueError)):
            self.mod.fixture_prep(os.path.join(HERE, "fixtures", "does-not-exist.json"))


class FixtureReplayTests(unittest.TestCase):
    """--fixture --live: the captured turn comes back through the live event stream instead of as history. What
    the finished window shows (FixtureWindowTests, and the view tests in test_read.py) must arrive over that
    stream too - the same file views, the same diffs, the same calls - and the window opens on an empty Follow
    stage rather than a filled Log. The replay starts when a window connects, so every window watches the whole
    turn from its start, whenever it opens."""

    @classmethod
    def setUpClass(cls):
        cls.secret = os.environ.get("PREVIEW_SECRET")
        cls.path = os.path.join(HERE, "fixtures", "learnlab-iq3-turn.json")
        cls.mod = load_module(0)  # fixture mode never calls opencode
        cls.fx, cls.chat, cls.start, cls.calls, cls.seeded = cls.mod.fixture_prep(cls.path)
        cls.sid = cls.fx["session"]["id"]
        cls.fin = load_module(0)  # the same capture served as a finished window: what the stream must match
        cls.fin_fx = cls.fin.fixture_prep(cls.path)[0]
        finished = cls.fin.fixture_server(cls.fin_fx, 0)
        finished.server_close()  # only to point that module at the capture; nothing listens
        cls.snap = cls.fin.snapshot(cls.sid, cls.start, settle=True)
        cls.mod.REPLAY_GAP = 0.01  # tests want the replay's content, not its real speed: keep it short
        cls.srv = cls.mod.fixture_server(cls.fx, 0, live=True, pace=1000)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls.events = cls.stream_of(cls.base, cls.chat, cls.sid, cls.start, cls.mod)  # the first window's replay

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        if cls.secret is None:
            os.environ.pop("PREVIEW_SECRET", None)
        else:
            os.environ["PREVIEW_SECRET"] = cls.secret

    @staticmethod
    def stream_of(base, chat, sid, start, mod):
        out = []
        url = "%s/w/%s/%s/events?s=%s&m=%s" % (base, chat, mod.live_sig(chat), sid, start)
        with urllib.request.urlopen(url, timeout=30) as r:  # no full=1: this must go through the live path
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    ev = json.loads(line[5:])
                    out.append(ev)
                    if ev["k"] == "end":
                        break
        return out

    @staticmethod
    def view_map(events):
        """The stream's view events, keyed by the call they belong to and the file/how they show."""
        return {(e["id"], e["path"], e["how"]): {k: v for k, v in e.items() if k not in ("k", "id")}
                for e in events if e["k"] == "view"}

    @staticmethod
    def snap_view_map(snap):
        out = {}
        for st in snap["steps"]:
            for p in st["parts"]:
                for v in p.get("views") or []:
                    out[(p["id"], v["path"], v["how"])] = dict(v)
        return out

    def test_the_stream_opens_on_an_empty_follow_stage_and_ends(self):
        first, last = self.events[0], self.events[-1]
        self.assertEqual(first["k"], "snap")
        self.assertTrue(first["running"])
        self.assertEqual(first["steps"], [])  # everything arrives as events, not as history
        self.assertEqual(last, {"k": "end", "how": "succeeded"})

    def test_a_second_window_gets_its_own_replay_from_the_start(self):
        """A window opened after another has watched the turn still sees the whole turn, from its start."""
        again = self.stream_of(self.base, self.chat, self.sid, self.start, self.mod)
        self.assertEqual(again[0]["k"], "snap")
        self.assertTrue(again[0]["running"])
        self.assertEqual(again[0]["steps"], [])  # it too opens on an empty Follow stage, not on history
        self.assertEqual(again[-1], {"k": "end", "how": "succeeded"})
        self.assertEqual(self.view_map(again), self.snap_view_map(self.snap))  # and every view arrives again

    def test_the_replay_starts_when_a_window_connects_not_when_the_server_starts(self):
        """Connect long after the whole replay would have been over, and the window still watches the turn
        event by event from its start."""
        srv = self.mod.fixture_server(self.fx, 0, live=True, pace=1000)  # the whole turn replays in a few seconds
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            time.sleep(8)  # a replay started with the server would be finished long before this connect
            late = self.stream_of("http://127.0.0.1:%d" % srv.server_address[1], self.chat, self.sid,
                                  self.start, self.mod)
            self.assertEqual(late[0]["k"], "snap")
            self.assertTrue(late[0]["running"])
            self.assertEqual(late[0]["steps"], [])  # the late window opens on an empty stage like any other
            self.assertEqual(late[-1], {"k": "end", "how": "succeeded"})
            self.assertEqual(self.view_map(late), self.snap_view_map(self.snap))  # and it watched all of it
        finally:
            srv.shutdown()
            srv.server_close()

    def test_every_file_view_of_the_finished_turn_arrives_over_the_stream(self):
        got, want = self.view_map(self.events), self.snap_view_map(self.snap)
        self.assertEqual(len(want), 18)  # the capture's ground truth: 18 views over 14 read/shell calls
        self.assertEqual(got, want)      # same files, same lines, same marks, same windows, same text

    def test_the_views_arrive_grouped_by_call_in_the_capture_order(self):
        """Each call's views arrive together, right after that call's tool event, in the order the capture made
        the calls - the same grouping the finished snapshot puts in its items."""
        got = []
        for e in self.events:
            if e["k"] == "view":
                if not got or got[-1][0] != e["id"]:
                    got.append([e["id"], 0])
                got[-1][1] += 1
        want = [[p["id"], len(p["views"])] for st in self.snap["steps"] for p in st["parts"] if p.get("views")]
        self.assertEqual(got, want)
        self.assertEqual(sum(c for _, c in got), 18)

    def test_every_edit_diff_of_the_finished_turn_arrives_over_the_stream(self):
        last = {}  # the stream emits growing diffs per call; only the last one per call should match
        for e in self.events:
            if e["k"] == "diff":
                last[e["id"]] = e
        want = {p["id"]: p["diff"] for st in self.snap["steps"] for p in st["parts"]
                if p["p"] == "tool" and p.get("diff")}
        self.assertEqual(len(want), self.seeded)
        self.assertEqual(set(last), set(want))
        for cid, d in want.items():
            for key in ("path", "hunks", "more_old", "note"):
                self.assertEqual(last[cid][key], d[key], "%s %s" % (cid, key))

    def test_every_tool_call_streams_with_its_name_and_final_status(self):
        got = {}
        for e in self.events:
            if e["k"] == "tool" and e.get("id"):
                got[e["id"]] = e
        want = {p["id"]: p for st in self.snap["steps"] for p in st["parts"] if p["p"] == "tool"}
        self.assertEqual(set(got), set(want))
        for cid, item in want.items():
            self.assertEqual(got[cid]["status"], item["status"], cid)

    def test_the_stream_has_the_capture_steps(self):
        self.assertEqual(sum(1 for e in self.events if e["k"] == "step"), len(self.snap["steps"]))
        self.assertTrue(any(e["k"] in ("text", "text.", "text+", "text0") for e in self.events))
        self.assertTrue(any(e["k"] in ("think", "think.", "think+", "think0") for e in self.events))

    def test_live_without_a_fixture_is_refused(self):
        argv = sys.argv
        try:
            sys.argv = ["code-live.py", "--live"]
            with self.assertRaises(SystemExit) as cm:
                self.mod.main()
            self.assertEqual(cm.exception.code, 2)
        finally:
            sys.argv = argv


if __name__ == "__main__":
    unittest.main(verbosity=2)
