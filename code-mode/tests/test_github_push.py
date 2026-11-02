"""Stage 3 of the GitHub button: pushing a chat's project (to a new or existing branch, or a chat's own project into
an empty repo), the history, and rolling back. Real git on tests/fake_opencode.py against FakeGitHub.make_remotes()."""
import asyncio
import os
import subprocess

from test_github_attach import Env, run

import pytest
import json

from fake_github import TOKEN


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.fake.stop()
    e.vm.stop()
    assert TOKEN not in json.dumps(e.vm.commands), "the token reached the VM in a command"
    assert TOKEN not in json.dumps(e.answers), "an answer contained the token"
    assert e.key_folders() == [], "a key folder was left on the VM"
    assert all(not keys for keys in e.fake.keys.values()), "a deploy key was left on GitHub"
    assert len([k for k in e.fake.key_log if k[0] == "add"]) == len([k for k in e.fake.key_log if k[0] == "delete"])


def bare(env, repo, *args):
    """git, in the repo as GitHub has it."""
    return run("git", *args, cwd=env.remotes + repo + ".git")


def write(folder, name, text):
    with open(os.path.join(folder, name), "w") as f:
        f.write(text)


def test_a_chats_own_project_goes_into_an_empty_repo(env):
    folder = env.chat("chat-1")
    write(folder, "app.py", "print('launch')\n")
    write(folder, ".env", "API_KEY=secret\n")
    full = env.attach("chat-1", "octo/alpha")
    assert not full["ok"] and "is not empty" in full["error"] and run("git", "remote", cwd=folder) == ""
    res = env.attach("chat-1", "octo/gamma")  # gamma has nothing in it yet
    assert res["ok"] and res["own"] is True and res["fresh"] is False
    assert env.fake.key_log == []  # nothing was fetched, so no key yet
    assert open(os.path.join(folder, "app.py")).read() == "print('launch')\n"  # the project is untouched
    status = env.op("chat_status", chat_id="chat-1")
    assert status["linked"] and status["repo"] == "octo/gamma" and status["changed"] == 1  # .env is not counted
    assert env.op("branches", chat_id="chat-1")["branches"] == [] and env.op("branches", chat_id="chat-1")["default"] == "main"
    pushed = env.op("push", chat_id="chat-1", branch="main", message="First version")
    assert pushed["ok"] and pushed["pushed"] == "main" and pushed["changed"] == 0 and pushed["ahead"] == 0
    assert pushed["url"].endswith("/octo/gamma/tree/main")
    assert bare(env, "octo/gamma", "ls-tree", "-r", "--name-only", "main") == "app.py"  # and no .env on GitHub
    assert bare(env, "octo/gamma", "log", "-1", "--format=%s|%an|%ae", "main") == "First version|octo|583231+octo@users.noreply.github.com"
    assert env.fake.key_log == [("add", "octo/gamma", False), ("delete", "octo/gamma", False)]  # one WRITE key, gone again
    assert run("git", "branch", "--show-current", cwd=folder) == "main"
    assert env.op("branches", chat_id="chat-1")["branches"] == ["main"]


def test_push_to_a_new_branch_then_to_main(env):
    folder = env.chat("chat-1")
    env.attach("chat-1", "octo/alpha")
    write(folder, "notes.txt", "hello\n")
    open(os.path.join(folder, "README.md"), "a").write("more\n")
    changes = env.op("changes", chat_id="chat-1")
    assert sorted((f["status"], f["path"]) for f in changes["files"]) == [("??", "notes.txt"), ("M", "README.md")] and changes["more"] == 0
    assert env.op("branches", chat_id="chat-1") == {"ok": True, "branches": ["main", "dev"], "default": "main", "current": "main"}
    res = env.op("push", chat_id="chat-1", branch="feature/notes", message="Add   notes\nand more")
    assert res["ok"] and res["branch"] == "feature/notes" and res["changed"] == 0 and res["ahead"] == 0
    assert bare(env, "octo/alpha", "log", "-1", "--format=%s", "feature/notes") == "Add notes and more"  # one line
    assert "notes.txt" in bare(env, "octo/alpha", "ls-tree", "-r", "--name-only", "feature/notes")
    assert "notes.txt" not in bare(env, "octo/alpha", "ls-tree", "-r", "--name-only", "main")  # main is untouched
    assert run("git", "rev-parse", "--abbrev-ref", "feature/notes@{upstream}", cwd=folder) == "origin/feature/notes"
    assert env.op("branches", chat_id="chat-1")["current"] == "feature/notes"
    assert env.op("push", chat_id="chat-1", branch="feature/notes")["ok"]  # nothing new: still fine
    assert env.op("push", chat_id="chat-1", branch="main")["ok"]  # the same work onto main: a fast-forward
    assert "notes.txt" in bare(env, "octo/alpha", "ls-tree", "-r", "--name-only", "main")
    assert all(k[2] is False for k in env.fake.key_log[2:])  # after the clone, every key was a write key


def test_push_never_overwrites_what_github_has(env):
    folder = env.chat("chat-1")
    env.attach("chat-1", "octo/alpha")
    env.push_commit("octo/alpha", "theirs.txt", "from someone else")
    write(folder, "mine.txt", "my work\n")
    before = bare(env, "octo/alpha", "rev-parse", "main")
    res = env.op("push", chat_id="chat-1", branch="main", message="My work")
    assert not res["ok"] and "Pull first, or push to a new branch" in res["error"]
    assert bare(env, "octo/alpha", "rev-parse", "main") == before  # GitHub is unchanged
    assert open(os.path.join(folder, "mine.txt")).read() == "my work\n"  # and so is the work, now committed here
    state = env.op("chat_status", chat_id="chat-1")
    assert state["branch"] == "main" and state["changed"] == 0 and state["ahead"] == 1
    side = env.op("push", chat_id="chat-1", branch="my-work")
    assert side["ok"] and "mine.txt" in bare(env, "octo/alpha", "ls-tree", "-r", "--name-only", "my-work")


def test_history_and_rollback_add_commits_and_lose_nothing(env):
    folder = env.chat("chat-1")
    env.attach("chat-1", "octo/alpha")
    write(folder, "a.txt", "one\n")
    assert env.op("push", chat_id="chat-1", branch="main", message="Add a")["ok"]
    write(folder, "a.txt", "two\n")
    write(folder, "b.txt", "bee\n")
    assert env.op("push", chat_id="chat-1", branch="main", message="Change a, add b")["ok"]
    hist = env.op("history", chat_id="chat-1")["commits"]
    assert [c["subject"] for c in hist] == ["Change a, add b", "Add a", "First commit"] and all(c["pushed"] for c in hist)
    assert hist[0]["author"] == "octo" and len(hist[0]["sha"]) == 40 and hist[0]["short"] == hist[0]["sha"][:7]
    add_a = hist[1]["sha"]

    write(folder, "c.txt", "not committed yet\n")
    write(folder, ".env", "KEY=1\n")
    res = env.op("rollback", chat_id="chat-1", sha=add_a)
    assert res["ok"] and res["saved"] is True and res["same"] is False and res["rolled"] == add_a[:7]
    assert open(os.path.join(folder, "a.txt")).read() == "one\n"
    assert sorted(f for f in os.listdir(folder) if f != ".git") == [".env", "README.md", "a.txt"]  # b and c went; .env is not git's
    hist2 = env.op("history", chat_id="chat-1")["commits"]
    assert [c["subject"] for c in hist2][:2] == [f"Roll back to {add_a[:7]}: Add a", "Work in progress, saved before rolling back"]
    assert [c["pushed"] for c in hist2] == [False, False, True, True, True] and res["ahead"] == 2
    assert bare(env, "octo/alpha", "log", "-1", "--format=%s", "main") == "Change a, add b"  # nothing on GitHub moved

    async def notes():
        async with env.pipe._vm() as c:
            return await env.pipe._gh_take_notes(c, "chat-1"), await env.pipe._gh_take_notes(c, "chat-1")
    (new, told), again = asyncio.run(notes())
    assert new is True and len(told) == 1 and add_a[:7] in told[0] and "rolled" in told[0]  # the agent hears about it once
    assert again == (False, [])
    assert env.op("rollback", chat_id="chat-1", sha=add_a)["same"] is True  # already there: no new commit
    assert len(env.op("history", chat_id="chat-1")["commits"]) == 5

    wip = hist2[1]["sha"]  # rolling "forward" again is just another roll back: the saved work comes back
    assert env.op("rollback", chat_id="chat-1", sha=wip[:10])["ok"]
    assert open(os.path.join(folder, "a.txt")).read() == "two\n" and open(os.path.join(folder, "c.txt")).read() == "not committed yet\n"
    assert env.op("push", chat_id="chat-1", branch="main")["ok"]  # history only grew: no force needed
    assert bare(env, "octo/alpha", "rev-parse", "main") == run("git", "rev-parse", "HEAD", cwd=folder)
    assert not env.op("rollback", chat_id="chat-1", sha="deadbeef")["ok"]  # no such version


def test_bad_input_and_chats_without_a_repo(env):
    env.chat("chat-1")
    env.attach("chat-1", "octo/alpha")
    env.chat("chat-2")
    n = len(env.vm.commands)
    for branch in ("", "has space", "-lead", "a..b", "x;rm -rf ~", "$(id)"):
        assert not env.op("push", chat_id="chat-1", branch=branch)["ok"], branch
    for sha in ("", "zzzzzzz", "abc", "HEAD", "main; id"):
        assert not env.op("rollback", chat_id="chat-1", sha=sha)["ok"], sha
    for op in ("changes", "branches", "history"):
        assert not env.op(op, chat_id="../chat-1")["ok"], op
    assert not env.op("push", chat_id="../chat-1", branch="main")["ok"]
    assert len(env.vm.commands) == n  # none of that reached the VM
    for op, args in (("changes", {}), ("branches", {}), ("history", {}), ("push", {"branch": "main"}), ("rollback", {"sha": "abcdef1"})):
        res = env.op(op, chat_id="chat-2", **args)
        assert not res["ok"] and "not working on a GitHub repository" in res["error"], op
    assert env.fake.key_log == [("add", "octo/alpha", True), ("delete", "octo/alpha", True)]  # only the clone's key


def test_suggest_message_from_the_small_model_or_the_file_names(env):
    """The spark beside "What changed". The model call goes through Open WebUI, which is not here: a stand-in plays it."""
    folder = env.chat("chat-1")
    env.attach("chat-1", "octo/alpha")
    assert not env.op("suggest_message", chat_id="chat-1")["ok"]  # nothing changed yet
    write(folder, "launches.js", "export const pads = [];\n")
    write(folder, ".env", "API_KEY=secret\n")
    open(os.path.join(folder, "README.md"), "a").write("Countdown added\n" + "x" * 9000 + "\n")
    asked, answer = [], {"text": '**Commit message:** "Add a countdown to the launch page."\n\nHere is why...'}

    async def model(request, user, system, prompt):
        asked.append((system, prompt))
        if answer["text"] is None:
            raise RuntimeError("no model")
        return answer["text"]
    env.pipe._gh_ask_model = model
    res = env.op("suggest_message", chat_id="chat-1")
    assert res == {"ok": True, "message": "Add a countdown to the launch page", "from_model": True}
    system, prompt = asked[0]
    assert "ONE short line" in system and "launches.js" in prompt and "README.md" in prompt
    assert "Countdown added" in prompt and "API_KEY" not in prompt and len(prompt) < 3500  # a short piece, no secrets
    for text in ("", None, "<think>long thoughts\nmore</think>", "Sure!"):  # nothing usable: the file names will do
        answer["text"] = text
        res = env.op("suggest_message", chat_id="chat-1")
        assert res == {"ok": True, "message": "Add launches.js; Update README.md", "from_model": False}, text
    answer["text"] = "<think>hmm</think>\nUpdate README and add launch data"
    assert env.op("suggest_message", chat_id="chat-1")["message"] == "Update README and add launch data"
    mine = env.chat("chat-2")  # a chat's own project, not on GitHub yet, can be named as well
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
        write(mine, name, "1\n")
    answer["text"] = None
    assert env.op("suggest_message", chat_id="chat-2")["message"] == "Add a.py, b.py, c.py and 2 more"
    n = len(env.vm.commands)
    assert not env.op("suggest_message", chat_id="../chat-1")["ok"] and len(env.vm.commands) == n
    assert not env.op("suggest_message", chat_id="never-made")["ok"]


def test_the_one_line_request_turns_thinking_off_where_it_can(tmp_path):
    """A thinking model spends a short answer's allowance on thinking and returns nothing, so the request for a
    version name turns thinking off when the router says the model allows it."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from code_mode import Pipe

    models = {"data": [
        {"id": "qwen3.6-35b-a3b-ud-q5kxl", "slerm": {"status": "loaded", "thinking": {"levels": ["none", "on"], "default": "on"}}},
        {"id": "gpt-oss-20b-mxfp4", "slerm": {"status": "unloaded", "thinking": {"levels": ["low", "medium", "high"]}}},
        {"id": "tasks-llama3.2-1b", "slerm": {"status": "sidecar", "thinking": None}}]}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps(models).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        pipe = Pipe()
        pipe.valves.TASK_ROUTER_MODELS = "http://127.0.0.1:%d/v1/models" % srv.server_address[1]
        levels = lambda m: asyncio.run(pipe._task_thinking(m))  # noqa: E731
        assert levels("slerm-turing.qwen3.6-35b-a3b-ud-q5kxl") == ["none", "on"]
        assert levels("slerm-turing.gpt-oss-20b-mxfp4") == ["low", "medium", "high"]
        assert levels("slerm-turing.tasks-llama3.2-1b") == [] and levels("slerm-turing.unknown") == []
        pipe.valves.TASK_ROUTER_MODELS = "http://127.0.0.1:1/nothing-here"
        assert levels("slerm-turing.qwen3.6-35b-a3b-ud-q5kxl") == []  # router unreachable: no guessing
    finally:
        srv.shutdown()
    off = Pipe._gh_model_form("m", ["none", "on"], "sys", "ask")
    assert off["reasoning_effort"] == "none" and off["max_tokens"] == 120 and off["stream"] is False
    assert [x["role"] for x in off["messages"]] == ["system", "user"]
    low = Pipe._gh_model_form("m", ["low", "medium", "high"], "sys", "ask")
    assert low["reasoning_effort"] == "low" and low["max_tokens"] == 900  # cannot be turned off: least, with room
    plain = Pipe._gh_model_form("m", [], "sys", "ask")
    assert "reasoning_effort" not in plain and plain["max_tokens"] == 120
