"""Performance handoff (2026-10-23), stages A, B and D: the status line names the step it is on and says what a quiet
turn waits on; the compaction backstop never queues behind opencode's own; per-turn timing counts useful progress and
survives Stop; a job that finishes while its chat's turn runs is handed to that turn, exactly once, and Stop holds it."""
import asyncio
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "owui"))
from code_mode import Pipe, TurnCheck, tool_phase, tool_status  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402
from test_review import run_turn  # noqa: E402


class FakeLater:
    """code-later's claim and hold API, as the pipe calls it: ready wakes per chat, claimed once."""

    def __init__(self):
        self.ready, self.held, self.calls, self.claimed = {}, set(), [], {}
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                fake.calls.append((self.path, body, self.headers.get("Authorization")))
                out = {}
                with fake.lock:
                    chat = body.get("chat")
                    if self.path == "/api/claim":
                        texts = [] if chat in fake.held else fake.ready.pop(chat, [])
                        ids = ["L%d" % (len(fake.claimed) + i) for i in range(len(texts))]
                        fake.claimed.update({i: (chat, t) for i, t in zip(ids, texts)})
                        out = {"ids": ids, "text": "\n\n".join(texts)}
                    elif self.path == "/api/unclaim":
                        for i in body.get("ids") or []:
                            c, t = fake.claimed.pop(i)
                            fake.ready.setdefault(c, []).append(t)
                        out = {"ready_again": len(body.get("ids") or [])}
                    elif self.path == "/api/hold":
                        (fake.held.add if body.get("hold") else fake.held.discard)(chat)
                        out = {"changed": 1}
                b = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def make_ready(self, chat, text):
        with self.lock:
            self.ready.setdefault(chat, []).append(text)

    def paths(self):
        return [(p, b.get("hold")) if p == "/api/hold" else p for p, b, _ in self.calls]

    def stop(self):
        self.srv.shutdown()


def timing_of(fake):
    [got] = [e["timing"] for e in fake.status_events if e.get("action") == "slerm_timing"]
    return got


# ---- stage A: phases and wait notes -------------------------------------------------------------------------------

def test_a_tool_call_is_named_by_the_step_it_is_part_of():
    cases = [
        ("read", {"filePath": "src/a.rs"}, "Investigating"),
        ("grep", {"pattern": "fn main"}, "Investigating"),
        ("edit", {"filePath": "src/a.rs"}, "Editing"),
        ("write", {"filePath": "x.py"}, "Editing"),
        ("task", {"prompt": "map the code"}, "Delegating to a helper agent"),
        ("shell", {"command": "cd codex-rs && cargo test -p codex-core"}, "Running checks"),
        ("shell", {"command": "/home/coder/projects/x/.venv/bin/python -m pytest -q"}, "Running checks"),
        ("shell", {"command": "bash codex-rs/check.sh"}, "Running checks"),
        ("shell", {"command": "check app/server.py"}, "Running checks"),
        ("shell", {"command": "desktop verify http://localhost:8000"}, "Running checks"),
        ("shell", {"command": "git checkout -b feature"}, "Running a command"),   # not the `check` linter
        ("shell", {"command": "cargo build --release"}, "Building"),
        ("shell", {"command": "slerm-job run --cpus 8 -- cargo build"}, "Waiting for a job"),
        ("shell", {"command": "later run -- sleep 60"}, "Waiting for a job"),
        ("shell", {"command": "cd owl && git status --short | head"}, "Investigating"),
        ("shell", {"command": "sed -n 1,80p src/main.rs"}, "Investigating"),
        ("shell", {"command": "sed -i 's/a/b/' src/main.rs"}, "Editing"),
        ("shell", {"command": "git commit -qm 'fix'"}, "Committing"),
        ("shell", {"command": "desktop click 3"}, "Using the desktop"),
    ]
    for name, args, want in cases:
        assert tool_phase(name, args) == want, (name, args)
    assert tool_status("read", {"filePath": "src/a.rs"}) == "Investigating · read: src/a.rs"


def test_status_lines_name_the_step(tmp_path):
    def script(session, text):
        return [("tool", "read", {"filePath": "a.py"}, "x = 1"),
                ("tool", "shell", {"command": "pytest -q"}, "1 passed"),
                ("text", "done")]
    _, statuses, _, _ = run_turn(tmp_path, script)
    assert "Investigating · read: a.py" in statuses
    assert "Running checks · shell: pytest -q" in statuses
    assert any(s.startswith("Working with m · reading context") for s in statuses)


def test_a_long_tool_gets_wait_notes_with_its_age_at_doubling_gaps(tmp_path):
    def script(session, text):
        return [("tool", "shell", {"command": "cargo test"}, "ok", 3.6), ("text", "done")]
    _, statuses, _, _ = run_turn(tmp_path, script, HEARTBEAT_SECONDS=1)
    notes = [s for s in statuses if "still running" in s]
    assert notes and all(re.fullmatch(r"Running checks · shell: cargo test — still running \(\ds\)", s) for s in notes)
    assert len(notes) <= 2          # at ~1 s and ~3 s, not once a second


def test_a_quiet_start_says_it_is_reading_the_context_not_thinking(tmp_path):
    def script(session, text):
        return [("sleep", 1.6), ("text", "done")]
    _, statuses, _, _ = run_turn(tmp_path, script, HEARTBEAT_SECONDS=1)
    assert any(re.fullmatch(r"Reading context \(\ds\)", s) for s in statuses)
    assert not any(s.startswith("Thinking") for s in statuses)


def test_after_a_compaction_it_says_it_reads_the_new_context(tmp_path):
    def script(session, text):
        return [("event", "session.compaction.started"), ("event", "session.compaction.ended"), ("sleep", 1.6),
                ("text", "done")]
    _, statuses, _, _ = run_turn(tmp_path, script, HEARTBEAT_SECONDS=1)
    assert any(s.startswith("Reading the compacted context (") for s in statuses)


def test_wait_notes_can_be_turned_off(tmp_path):
    def script(session, text):
        return [("tool", "shell", {"command": "cargo test"}, "ok", 1.5), ("text", "done")]
    _, statuses, _, _ = run_turn(tmp_path, script, HEARTBEAT_SECONDS=0)
    assert not any("still running" in s for s in statuses)


def test_a_json_file_alone_is_no_web_page_to_verify():
    c = TurnCheck()
    c.tool("edit", {"filePath": "codex-rs/core/config.schema.json"}, "ok", False)
    assert c.edited and not c.pending()
    c.tool("edit", {"filePath": "owl/index.html"}, "ok", False)
    assert c.pending() == "no `desktop verify` after the last change"


# ---- stage B: the compaction backstop -----------------------------------------------------------------------------

def test_the_backstop_asks_only_after_opencode_let_a_second_step_pass(tmp_path):
    def script(session, text):
        return [("usage", {"input": 30000, "cache": {"read": 36000}}),   # 66,000: the old max() saw only 36,000
                ("usage", {"input": 500, "cache": {"read": 69500}}),
                ("text", "done")]
    _, _, fake, _ = run_turn(tmp_path, script, CONTEXT_COMPACT_TOKENS=65536)
    assert fake.compactions and len(fake.compactions) == 1


def test_the_backstop_never_queues_behind_opencodes_own_compaction(tmp_path):
    def script(session, text):
        return [("usage", {"input": 100, "cache": {"read": 66000}}),
                ("event", "session.compaction.started"), ("event", "session.compaction.ended"),
                ("usage", {"input": 100, "cache": {"read": 66000}}),           # over again: a new first step
                ("usage", {"input": 100, "cache": {"read": 20000}}),
                ("text", "done")]
    _, _, fake, _ = run_turn(tmp_path, script, CONTEXT_COMPACT_TOKENS=65536)
    assert fake.compactions == []


# ---- stage A: what one turn did, saved with it --------------------------------------------------------------------

def test_turn_timing_counts_reads_repeats_edits_and_checks(tmp_path):
    def script(session, text):
        return [("tool", "read", {"filePath": "a.py"}, "x"),
                ("tool", "read", {"filePath": "a.py"}, "x"),                 # unchanged: a repeat
                ("tool", "edit", {"filePath": "a.py"}, "ok"),
                ("tool", "read", {"filePath": "a.py"}, "x"),                 # changed since: not a repeat
                ("tool", "read", {"filePath": "b.py"}, "y"),
                ("tool", "shell", {"command": "pytest -q"}, "1 passed"),
                ("tool", "shell", {"command": "cat a.py"}, "x"),
                ("text", "done")]
    _, _, fake, _ = run_turn(tmp_path, script)
    got = timing_of(fake)
    assert got["activity"] == {"reads": 4, "repeat_reads": 1, "edits": 1, "shell_commands": 2, "checks": 1,
                               "checks_failed": 0, "files_read": 2, "files_edited": 1}
    assert set(got["phase_seconds"]) == {"Investigating", "Editing", "Running checks"}
    assert got["stopped"] is False and got["wakes_delivered"] == 0 and got["compactions"] == 0


# ---- stage D: wake-ups reach the chat's own turn, once -----------------------------------------------------------

def test_a_wake_ready_when_the_turn_starts_goes_into_its_prompt(tmp_path):
    later = FakeLater()
    later.make_ready("chat1", "⏰ **Wake-up**: `cargo test` finished OK (exit 0) after 19s.")
    try:
        _, _, fake, _ = run_turn(tmp_path, None, LATER_URL=later.url, LATER_TOKEN="t")
    finally:
        later.stop()
    [s] = fake.sessions.values()
    assert "<background_results>" in s["prompts"][0] and "cargo test` finished OK" in s["prompts"][0]
    assert later.paths()[:2] == [("/api/hold", False), "/api/claim"]   # the user is back: held ones are released
    assert all(auth == "Bearer t" for _, _, auth in later.calls)
    assert timing_of(fake)["wakes_delivered"] == 1


def test_a_job_that_finishes_during_the_run_is_handed_over_in_the_same_turn(tmp_path):
    later = FakeLater()

    def script(session, text):
        if len(session["prompts"]) == 1:
            return [("tool", "shell", {"command": "later run -- cargo test"}, "set L1"),
                    ("do", lambda: later.make_ready("chat1", "WAKE: cargo test finished OK (exit 0)")),
                    ("text", "Waiting for the test run.")]
        return [("text", "The tests passed; done.")]
    try:
        text, statuses, fake, _ = run_turn(tmp_path, script, LATER_URL=later.url, LATER_TOKEN="t")
    finally:
        later.stop()
    [s] = fake.sessions.values()
    assert s["prompts"][1] == "WAKE: cargo test finished OK (exit 0)"
    assert "A background job finished - handing the result to the agent" in statuses
    assert "The tests passed; done." in text and "Wake-up:" in text
    assert later.ready == {}                                  # claimed: code-wake will not send it again
    assert timing_of(fake)["wakes_delivered"] == 1


def test_wake_rounds_per_turn_are_bounded(tmp_path):
    later = FakeLater()

    def script(session, text):
        n = len(session["prompts"])
        return [("do", lambda: later.make_ready("chat1", "WAKE %d" % n)), ("text", "run %d" % n)]
    try:
        _, _, fake, _ = run_turn(tmp_path, script, LATER_URL=later.url, LATER_TOKEN="t", WAKES_PER_TURN=2)
    finally:
        later.stop()
    [s] = fake.sessions.values()
    assert s["prompts"][1:] == ["WAKE 1", "WAKE 2"]
    assert later.ready == {"chat1": ["WAKE 3"]}                # left for code-wake after the turn


def test_without_a_later_token_nothing_is_asked_of_code_later(tmp_path):
    later = FakeLater()
    later.make_ready("chat1", "WAKE")
    try:
        _, _, fake, _ = run_turn(tmp_path, None, LATER_URL=later.url, LATER_TOKEN="")
    finally:
        later.stop()
    assert later.calls == [] and later.ready == {"chat1": ["WAKE"]}
    [s] = fake.sessions.values()
    assert "background_results" not in s["prompts"][0]


def test_opencode_stopping_holds_the_chats_wakes(tmp_path):
    later = FakeLater()

    def script(session, text):
        return [("event", "session.execution.interrupted")]
    try:
        text, _, _, _ = run_turn(tmp_path, script, LATER_URL=later.url, LATER_TOKEN="t")
    finally:
        later.stop()
    assert "opencode stopped" in text
    assert ("/api/hold", True) in later.paths()


def test_stop_keeps_the_partial_timing_and_holds_the_wakes(tmp_path):
    later = FakeLater()
    home = tmp_path / "home"
    fake = FakeOpencode(home)
    fake.agents = {"build", "ask", "review"}
    fake.script = lambda session, text: [("tool", "shell", {"command": "cargo test"}, "ok", 30)]
    fake.start()
    p = Pipe()
    v = p.valves
    v.OPENCODE_URL, v.OPENCODE_PASSWORD, v.PROJECTS_ROOT = fake.url, "x", str(home / "projects")
    v.ASK_FIRST, v.PREVIEW_SECRET, v.LIVE_URL, v.VIEW_TOKEN, v.FEATURES_TOKEN, v.FIXER_TOKEN = False, "", "", "", "", ""
    v.LATER_URL, v.LATER_TOKEN = later.url, "t"
    os.makedirs(v.PROJECTS_ROOT, exist_ok=True)
    events = []

    async def emit(e):
        if e["type"] == "status":
            events.append(e["data"])

    async def go():
        async def consume():
            async for _ in p._run([{"role": "user", "content": "run the tests"}], "chat1",
                                  {"id": "m", "providerID": "slerm"}, emit, None, picked="slerm-turing.m"):
                pass
        task = asyncio.create_task(consume())
        for _ in range(200):          # until the tool is running
            await asyncio.sleep(0.05)
            if any(d.get("description", "").startswith("Running checks") for d in events):
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(asyncio.wait_for(go(), 30))
    finally:
        fake.stop()
        later.stop()
    [timing] = [d["timing"] for d in events if d.get("action") == "slerm_timing"]
    assert timing["stopped"] is True and timing["incomplete"]["tools"] == 1
    assert ("/api/hold", True) in later.paths()


def test_a_claimed_wake_whose_prompt_never_reaches_opencode_is_given_back(tmp_path):
    later = FakeLater()
    later.make_ready("chat1", "WAKE: build finished")
    try:
        text, _, fake, _ = run_turn(tmp_path, None, LATER_URL=later.url, LATER_TOKEN="t", _prompt_status=500)
    finally:
        later.stop()
    assert "Lost contact with opencode" in text
    assert later.ready == {"chat1": ["WAKE: build finished"]}     # code-wake can still deliver it
