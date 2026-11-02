"""code-claude's runner against a fake code-fixer API and a fake `claude` that streams like `claude -p
--output-format stream-json --verbose`: a normal run (events, redaction, result.json -> resolve), Claude not signed
in (the fixer's verdict stands, with a note), a Cancel mid-run (Claude is stopped, nothing resolved) and a run that
writes no result.json."""
import importlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "runner-test-token-1234567890"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
def out(o):
    print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "model": "claude-test"})
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the fixer's notes. token=abcdef1234567890xyz"}]}})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash",
     "input": {"command": "ssh vm129 journalctl -u code-preview"}}]}})
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "all good"}]}})
if mode == "slow":
    time.sleep(60)
if mode == "ok":
    with open("result.json", "w") as fh:
        json.dump({"verdict": "fixed", "summary": "Fixed the route", "changed": [{"path": "x.py", "why": "y"}],
                   "for_the_chat": "retry"}, fh)
out({"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 3,
     "total_cost_usd": 0.12})
'''


class Fixer:
    def __init__(self):
        self.escalations, self.events, self.resolved, self.claimed = [], [], [], []
        self.record = {"status": "escalated", "escalation": {"state": "claimed",
                       "fixer": {"verdict": "could_not_fix", "summary": "The fixer could not find it."}}}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, st, obj, raw=False):
                b = (obj if raw else json.dumps(obj)).encode()
                self.send_response(st)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                assert self.headers.get("Authorization") == "Bearer " + TOKEN
                if self.path == "/api/escalations":
                    return self._send(200, {"escalations": fake.escalations})
                if "/file/" in self.path:
                    return self._send(200, "evidence " + self.path.rsplit("/", 1)[1], raw=True)
                return self._send(200, fake.record)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                op = self.path.rsplit("/", 1)[1]
                if op == "claim":
                    fake.claimed.append(body)
                    fake.escalations = []
                    return self._send(200, fake.record)
                if op == "events":
                    if fake.record["status"] != "escalated":
                        return self._send(409, {"error": "closed"})
                    fake.events.extend(body["events"])
                    return self._send(200, {"ok": True})
                if op == "resolve":
                    fake.resolved.append(body)
                    return self._send(200, {"ok": True})
                self._send(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    fx = Fixer()
    fx.escalations = [{"id": "20261026-1200-abcd", "chat_id": "chat1", "title": "preview broken", "by": "auto",
                       "fixer_verdict": "could_not_fix"}]
    claude = tmp_path / "claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)
    monkeypatch.setenv("CODE_CLAUDE_ENV", str(tmp_path / "none.env"))
    monkeypatch.setenv("FIXER_URL", fx.url)
    monkeypatch.setenv("FIXER_TOKEN", TOKEN)
    monkeypatch.setenv("CLAUDE_BIN", str(claude))
    monkeypatch.setenv("CODE_CLAUDE_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    monkeypatch.setenv("HOME", str(tmp_path))
    sys.path.insert(0, HERE)
    import runner
    runner = importlib.reload(runner)
    os.makedirs(str(tmp_path / "work" / "runs"), exist_ok=True)
    yield fx, runner, tmp_path
    fx.stop()
    sys.path.remove(HERE)


def test_a_run_streams_claudes_steps_and_resolves_with_its_result(env):
    fx, runner, tmp = env
    assert runner.handle_one()
    assert fx.claimed and fx.claimed[0]["runner"].startswith("cryoai-")
    kinds = [(e["type"], e.get("state")) for e in fx.events]
    assert ("start", None) in kinds and ("say", None) in kinds
    assert ("tool", "running") in kinds and ("tool", "ok") in kinds
    said = " ".join(str(e.get("text", "")) for e in fx.events)
    assert "abcdef1234567890xyz" not in said and "token=***" in said, "secrets never reach the viewer"
    r = fx.resolved[0]
    assert r["verdict"] == "fixed" and r["summary"] == "Fixed the route" and r["turns"] == 3 and r["model"] == "claude-test"
    run_dir = tmp / "work" / "runs" / "20261026-1200-abcd"
    assert (run_dir / "evidence" / "report.json").read_text() == "evidence report.json"
    assert (run_dir / "stream.jsonl").exists()


def test_not_signed_in_keeps_the_fixers_verdict_and_says_how_to_sign_in(env, monkeypatch):
    fx, runner, tmp = env
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    runner = importlib.reload(runner)
    assert runner.handle_one()
    r = fx.resolved[0]
    assert r["verdict"] == "could_not_fix" and "The fixer could not find it." in r["summary"]
    assert "ssh -t cryoai claude" in r["summary"]
    assert any(e["type"] == "error" for e in fx.events)


def test_a_cancel_stops_claude_and_nothing_is_resolved(env, monkeypatch):
    fx, runner, tmp = env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")

    def cancel():
        time.sleep(1.5)
        fx.record = {"status": "done", "verdict": "cancelled", "escalation": {"state": "cancelled"}}
    threading.Thread(target=cancel, daemon=True).start()
    t0 = time.time()
    assert runner.handle_one()
    assert time.time() - t0 < 20, "Claude is stopped within seconds of the cancel"
    assert fx.resolved == []


def test_no_result_json_is_needs_human_with_its_last_words(env, monkeypatch):
    fx, runner, tmp = env
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "noresult")
    assert runner.handle_one()
    r = fx.resolved[0]
    assert r["verdict"] == "needs_human" and "without writing result.json" in r["summary"]


def test_nothing_waiting_does_nothing(env):
    fx, runner, tmp = env
    fx.escalations = []
    assert runner.handle_one() is False and fx.claimed == []
