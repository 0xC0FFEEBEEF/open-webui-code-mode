"""Working on a repo in a chat: the pipe's _gh_attach, chat_status and pull, run for real. tests/fake_opencode.py
plays the sandbox VM by really running the commands under a throwaway home, FakeGitHub.make_remotes gives git real
bare repositories to fetch from, and tests/fake_github.py plays the API (deploy keys, host keys)."""
import asyncio
import glob
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "owui"))
sys.path.insert(0, HERE)
from code_mode import Pipe  # noqa: E402
from fake_github import TOKEN, FakeGitHub  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402

ADMIN = {"id": "u1", "role": "admin"}
ADD, DELETE = ("add", "octo/alpha", True), ("delete", "octo/alpha", True)


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.fake = FakeGitHub().start()
        self.vm = FakeOpencode(tmp_path / "vm-home").start()
        self.remotes = self.fake.make_remotes(tmp_path / "remotes")
        self.pipe = Pipe()
        v = self.pipe.valves
        v.GITHUB_API = v.GITHUB_WEB = self.fake.url
        v.GITHUB_STATE = str(tmp_path / "cfg" / "github.json")
        v.GITHUB_GIT = self.remotes
        v.OPENCODE_URL, v.OPENCODE_PASSWORD = self.vm.url, "test"
        v.PROJECTS_ROOT = os.path.join(self.vm.home, "projects")
        self.answers = []
        for op, args in (("set_client", {"client_id": self.fake.client_id}), ("login_start", {}), ("login_poll", {})):
            assert self.op(op, **args)["ok"]

    def op(self, op, **args):
        res = asyncio.run(self.pipe.pipe({"model": "code_mode.x", "code_github": dict(args, op=op)}, ADMIN, None))
        self.answers.append(res)
        return res

    def folder(self, chat):
        return os.path.join(self.pipe.valves.PROJECTS_ROOT, "chats", chat)

    def chat(self, chat):
        """A chat's project folder as the pipe's _session leaves it: made, and `git init`-ed."""
        os.makedirs(self.folder(chat))
        run("git", "init", "-q", ".", cwd=self.folder(chat))
        return self.folder(chat)

    def attach(self, chat, repo):
        async def go():
            async with self.pipe._vm() as c:
                return await self.pipe._gh_attach(c, chat, repo)
        res = asyncio.run(go())
        self.answers.append(res)
        return res

    def key_folders(self):
        return glob.glob(os.path.join(self.vm.home, ".local/state/code-mode/gh/*"))

    def saved(self):
        with open(self.pipe.valves.GITHUB_STATE) as f:
            return json.load(f)

    def push_commit(self, repo, name, text, branch="main"):
        """Someone else pushes a commit to the repo on 'GitHub'."""
        work = self.tmp / ("push-" + name)
        run("git", "clone", "-q", "-b", branch, self.remotes + repo + ".git", str(work), cwd=self.tmp)
        (work / name).write_text(text)
        run("git", "add", "-A", cwd=work)
        run("git", "-c", "user.name=octo", "-c", "user.email=o@example.com", "commit", "-q", "-m", "add " + name, cwd=work)
        run("git", "push", "-q", "origin", branch, cwd=work)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.fake.stop()
    e.vm.stop()
    assert TOKEN not in json.dumps(e.vm.commands), "the token reached the VM in a command"
    assert TOKEN not in json.dumps(e.answers), "an answer contained the token"
    assert e.key_folders() == [], "a key folder was left on the VM"
    assert all(not keys for keys in e.fake.keys.values()) or e.fake.key_delete_fails, "a deploy key was left on GitHub"


def test_attach_clones_with_a_key_that_is_gone_afterwards(env):
    folder = env.chat("chat-1")
    res = env.attach("chat-1", "octo/alpha")
    assert res == {"ok": True, "repo": "octo/alpha", "branch": "main", "fresh": True}
    assert open(os.path.join(folder, "README.md")).read().startswith("# alpha")
    assert run("git", "remote", "get-url", "origin", cwd=folder) == env.remotes + "octo/alpha.git"
    assert run("git", "branch", "--show-current", cwd=folder) == "main"
    assert run("git", "rev-parse", "--abbrev-ref", "main@{upstream}", cwd=folder) == "origin/main"
    assert run("git", "config", "user.name", cwd=folder) == "octo"
    assert run("git", "config", "user.email", cwd=folder) == "583231+octo@users.noreply.github.com"
    exclude = open(os.path.join(folder, ".git/info/exclude")).read().splitlines()
    assert {".venv/", "node_modules/", ".env", ".env.*", "*.pyc"} <= set(exclude)
    assert env.fake.key_log == [ADD, DELETE]  # one read-only key, added and removed
    added = next(r for r in env.fake.requests if r["method"] == "POST" and r["path"].endswith("/keys"))
    assert added["json"]["read_only"] is True and added["json"]["key"].startswith("ssh-ed25519 ")
    assert run("git", "status", "--porcelain", cwd=folder) == ""  # a clean clone


def test_attach_again_changes_nothing(env):
    env.chat("chat-1")
    env.attach("chat-1", "octo/alpha")
    assert env.attach("chat-1", "octo/alpha") == {"ok": True, "repo": "octo/alpha", "branch": "main", "fresh": False}
    assert env.fake.key_log == [ADD, DELETE]  # no second key
    other = env.attach("chat-1", "octo/beta")
    assert not other["ok"] and "already works on octo/alpha" in other["error"]


def test_attach_an_empty_repo(env):
    folder = env.chat("chat-1")
    res = env.attach("chat-1", "octo/gamma")
    assert res["ok"] and res["branch"] == "main" and res["fresh"]
    assert run("git", "symbolic-ref", "HEAD", cwd=folder) == "refs/heads/main"  # ready for a first commit on main
    assert os.listdir(folder) == [".git"]
    assert env.op("chat_status", chat_id="chat-1")["linked"]


def test_a_chat_with_its_own_work_is_refused(env):
    folder = env.chat("chat-1")
    open(os.path.join(folder, "notes.txt"), "w").write("mine")
    res = env.attach("chat-1", "octo/alpha")
    assert not res["ok"] and "project of its own" in res["error"]
    assert env.fake.key_log == [] and run("git", "remote", cwd=folder) == ""
    assert not env.attach("no-such-chat", "octo/alpha")["ok"]


def test_no_admin_rights_gives_plain_words_and_leaves_nothing(env):
    folder = env.chat("chat-1")
    env.fake.deploy_keys_allowed = False
    res = env.attach("chat-1", "octo/alpha")
    assert not res["ok"] and "admin" in res["error"]
    assert run("git", "remote", cwd=folder) == "" and os.listdir(folder) == [".git"]
    env.fake.deploy_keys_allowed = True
    assert env.attach("chat-1", "octo/alpha")["fresh"]  # and a retry works


def test_a_failed_fetch_still_removes_its_key(env):
    folder = env.chat("chat-1")
    env.fake.repos.append(env.fake._repo("ghost", True))  # on GitHub, but git cannot reach it
    res = env.attach("chat-1", "octo/ghost")
    assert not res["ok"] and "Could not fetch octo/ghost" in res["error"]
    assert env.fake.key_log == [("add", "octo/ghost", True), ("delete", "octo/ghost", True)]
    assert run("git", "remote", cwd=folder) == ""  # so the next try starts clean
    assert not env.attach("chat-1", "octo/nowhere")["ok"]  # not on GitHub at all: no key is even tried
    assert len(env.fake.key_log) == 2


def test_attach_op_clones_into_a_chat_that_exists_right_away(env):
    """"Work on it" in a chat: the page calls op attach, the clone happens at once, even in a chat that never ran
    the coding agent (no folder yet). The chat's next turn is told once that the project is new."""
    res = env.op("attach", chat_id="chat-9", full_name="octo/alpha")
    assert res == {"ok": True, "linked": True, "repo": "octo/alpha", "branch": "main", "changed": 0, "ahead": 0,
                   "project": False, "fresh": True, "own": False}
    folder = env.folder("chat-9")
    assert open(os.path.join(folder, "README.md")).read().startswith("# alpha")
    assert run("git", "status", "--porcelain", cwd=folder) == ""  # the marker lives inside .git: not a change

    async def take():
        async with env.pipe._vm() as c:
            return (await env.pipe._gh_take_notes(c, "chat-9"))[0], (await env.pipe._gh_take_notes(c, "chat-9"))[0]
    assert asyncio.run(take()) == (True, False)  # "this project is new to the agent": said once
    again = env.op("attach", chat_id="chat-9", full_name="octo/alpha")
    assert again["ok"] and again["fresh"] is False and env.fake.key_log == [ADD, DELETE]
    assert asyncio.run(take()) == (False, False)
    mine = env.chat("chat-8")
    open(os.path.join(mine, "app.py"), "w").write("print(1)")
    res = env.op("attach", chat_id="chat-8", full_name="octo/alpha")  # its own project, and alpha is not empty
    assert not res["ok"] and "project of its own" in res["error"] and run("git", "remote", cwd=mine) == ""


def test_chat_status(env):
    folder = env.chat("chat-1")
    env.chat("chat-2")
    env.attach("chat-1", "octo/alpha")
    assert env.op("chat_status", chat_id="chat-1") == {"ok": True, "linked": True, "repo": "octo/alpha", "branch": "main",
                                                       "changed": 0, "ahead": 0, "project": False}
    open(os.path.join(folder, "new.txt"), "w").write("x")
    open(os.path.join(folder, "README.md"), "a").write("more\n")
    open(os.path.join(folder, ".env"), "w").write("SECRET=1")  # excluded: not a change
    assert env.op("chat_status", chat_id="chat-1")["changed"] == 2
    assert env.op("chat_status", chat_id="chat-2")["linked"] is False
    open(os.path.join(env.folder("chat-2"), "app.py"), "w").write("print(1)")
    own = env.op("chat_status", chat_id="chat-2")
    assert own["project"] is True and own["linked"] is False  # a project of its own, not on GitHub
    assert env.op("chat_status", chat_id="never-made")["linked"] is False


def test_pull_fast_forwards_or_says_why_not(env):
    folder = env.chat("chat-1")
    env.attach("chat-1", "octo/alpha")
    env.push_commit("octo/alpha", "from-github.txt", "hello")
    res = env.op("pull", chat_id="chat-1")
    assert res["ok"] and res["linked"] and res["changed"] == 0
    assert open(os.path.join(folder, "from-github.txt")).read() == "hello"
    assert env.fake.key_log == [ADD, DELETE, ADD, DELETE]  # pulling used its own read-only key
    open(os.path.join(folder, "local.txt"), "w").write("mine")  # now both sides have a commit of their own
    run("git", "add", "-A", cwd=folder)
    run("git", "commit", "-q", "-m", "local work", cwd=folder)
    env.push_commit("octo/alpha", "second.txt", "again")
    res = env.op("pull", chat_id="chat-1")
    assert not res["ok"] and res["error"].startswith("Could not pull:")
    assert open(os.path.join(folder, "local.txt")).read() == "mine"  # nothing lost
    assert not env.op("pull", chat_id="never-made")["ok"]


def test_bad_names_never_reach_the_vm(env):
    env.chat("chat-1")
    n = len(env.vm.commands)
    for chat, repo in (("../chat-1", "octo/alpha"), ("chat 1; rm -rf ~", "octo/alpha"), ("", "octo/alpha"),
                       ("chat-1", "octo/alpha; touch /tmp/x"), ("chat-1", "../.."), ("chat-1", "octo/$(id)"), ("chat-1", "")):
        assert not env.attach(chat, repo)["ok"], (chat, repo)
    for chat in ("../chat-1", "a b", "$(id)", ""):
        assert not env.op("chat_status", chat_id=chat)["ok"], chat
        assert not env.op("pull", chat_id=chat)["ok"], chat
        assert not env.op("attach", chat_id=chat, full_name="octo/alpha")["ok"], chat
    for repo in ("octo/alpha; touch /tmp/x", "../..", "octo/$(id)", ""):
        assert not env.op("attach", chat_id="chat-1", full_name=repo)["ok"], repo
    assert len(env.vm.commands) == n
    assert env.fake.key_log == []


def test_signed_out_says_so(env):
    env.chat("chat-1")
    env.op("logout")
    res = env.attach("chat-1", "octo/alpha")
    assert not res["ok"] and res["signed_out"] and "sign in" in res["error"]
    assert env.op("chat_status", chat_id="chat-1")["signed_out"] and env.op("pull", chat_id="chat-1")["signed_out"]
    assert env.fake.key_log == []


def test_a_key_github_would_not_delete_is_removed_next_time(env):
    env.chat("chat-1")
    env.fake.key_delete_fails = True
    assert env.attach("chat-1", "octo/alpha")["ok"]  # the clone itself is fine
    stale = env.saved()["stale_keys"]
    assert len(stale) == 1 and stale[0]["repo"] == "octo/alpha" and len(env.fake.keys["octo/alpha"]) == 1
    assert env.key_folders() == []  # its private half is gone from the VM, so it opens nothing
    env.fake.key_delete_fails = False
    assert env.op("pull", chat_id="chat-1")["ok"]
    assert "stale_keys" not in env.saved() and env.fake.keys["octo/alpha"] == []
