"""Runs the REAL pipe behind POST /api/chat/completions, with GitHub faked (tests/fake_github.py), for the page tests.
harness.cjs starts it; nothing here needs Open WebUI.

It answers a direct call the way Open WebUI does: the dict the pipe returned as JSON, or `null` when the pipe raised
or returned anything else. Test controls:
    POST /_test {"github": {"pending": 2, ...}, "role": "user"}   set fake-GitHub attributes / the caller's role
    POST /_test {"push": {"repo": "octo/alpha", "file": "x.txt", "text": "..."}}   someone else pushes a commit there
    POST /_test {"remote": {"repo": "octo/alpha", "branch": "main"}}   what 'GitHub' has: {"remote": {"branches", "files", "subject"}}
    POST /_test {"attach": {"chat": id, "repo": "octo/alpha"}}    make that chat's folder and attach the repo to it with
                   the pipe's _gh_attach (as the chat's first turn does); answers {"attach": its result}
    GET  /_test    {"repos", "github_requests", "state_file", "state_mode", "keys", "key_log", "vm_commands",
                    "projects"}: the fake GitHub's repos, what it was asked, the saved state file and its mode, deploy
                   keys now and their add/delete log, the commands run on the fake VM, and the VM's projects folder
The fake VM (tests/fake_opencode.py) really runs the pipe's shell commands under a throwaway home; with a GITHUB_GIT
valve the pipe's git reaches real bare repositories on disk (fake.make_remotes), so clone, pull and push are real.
First line of stdout: {"port": N, "token": "<the fake token>"}.
"""
import asyncio
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "owui"))
from fake_github import FakeGitHub  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402
from code_mode import Pipe  # noqa: E402

tmp = tempfile.mkdtemp(prefix="gh-bridge-")
fake = FakeGitHub().start()
vm = FakeOpencode(os.path.join(tmp, "vm-home")).start()  # plays the sandbox VM: commands really run, under this home
state_file = os.path.join(tmp, "cfg", "github.json")
projects = os.path.join(vm.home, "projects")
os.makedirs(os.path.join(projects, "chats"))
pipe = Pipe()
for valve, value in (("GITHUB_API", fake.url), ("GITHUB_WEB", fake.url), ("GITHUB_STATE", state_file)):
    if not hasattr(pipe.valves, valve):
        sys.exit(f"pipe_bridge: the pipe has no {valve} valve")
    setattr(pipe.valves, valve, value)
pipe.valves.OPENCODE_URL, pipe.valves.OPENCODE_PASSWORD, pipe.valves.PROJECTS_ROOT = vm.url, "test", projects
if hasattr(pipe.valves, "GITHUB_GIT"):  # where git reaches the repos: real bare repositories on disk here
    pipe.valves.GITHUB_GIT = fake.make_remotes(os.path.join(tmp, "remotes"))
role = {"value": "admin"}
asked = []  # what the pipe asked "the model" (suggest_message): (system, prompt)


async def fake_model(request, user, system, prompt):
    """Stands in for Open WebUI's model call, answering the way small models do: quoted, with chatter after."""
    asked.append((system, prompt))
    return '"Tidy the launch page."\n\nThis message describes the change to app.py.'


pipe._gh_ask_model = fake_model


def attach(chat_id, full_name):
    """Make the chat's project folder the way the pipe's _session does (mkdir + git init), then attach the repo with
    the pipe's own _gh_attach(c, chat_id, full_name). Needs a signed-in state file (sign in through the panel)."""
    import subprocess

    import httpx

    folder = os.path.join(projects, "chats", chat_id)
    os.makedirs(folder, exist_ok=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", folder], check=True)

    async def go():
        async with httpx.AsyncClient(base_url=vm.url, auth=("opencode", "test"), timeout=60) as c:
            return await pipe._gh_attach(c, chat_id, full_name)
    return asyncio.run(go())


def push(repo, file, text, branch="main"):
    """A commit that adds `file` lands on the repo's branch on 'GitHub' (the bare repository git fetches from)."""
    import subprocess

    work = tempfile.mkdtemp(prefix="push-", dir=tmp)
    git = lambda *a: subprocess.run(["git", "-c", "user.name=octo", "-c", "user.email=o@example.com", *a], cwd=work,  # noqa: E731
                                    check=True, capture_output=True)
    git("clone", "-q", "-b", branch, pipe.valves.GITHUB_GIT + repo + ".git", ".")
    with open(os.path.join(work, file), "w") as f:
        f.write(text)
    git("add", "-A")
    git("commit", "-q", "-m", "add " + file)
    git("push", "-q", "origin", branch)
    return True


def remote(repo, branch="main"):
    """The repo as 'GitHub' has it: its branches, and the files and last commit subject of one branch."""
    import subprocess

    bare = pipe.valves.GITHUB_GIT + repo + ".git"
    git = lambda *a: subprocess.run(["git", *a], cwd=bare, capture_output=True, text=True).stdout.strip()  # noqa: E731
    branches = git("for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    if branch not in branches:
        return {"branches": branches, "files": None, "subject": None}
    return {"branches": branches, "files": git("ls-tree", "-r", "--name-only", branch).split("\n"),
            "subject": git("log", "-1", "--format=%s", branch)}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            with open(state_file) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = None
        self._send({"repos": fake.repos, "github_requests": fake.requests, "state_file": saved,
                    "state_mode": oct(os.stat(state_file).st_mode & 0o777) if os.path.exists(state_file) else None,
                    "keys": fake.keys, "key_log": fake.key_log, "projects": projects, "asked": asked,
                    "vm_commands": [{k: c[k] for k in ("command", "cwd", "status", "exit", "output")} for c in vm.commands]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode() or "{}")
        if self.path == "/_test":
            for k, v in (body.get("github") or {}).items():
                if not hasattr(fake, k) or k.startswith("_"):
                    return self._send({"error": f"fake GitHub has no setting {k}"})
                setattr(fake, k, v)
            role["value"] = body.get("role") or role["value"]
            if body.get("remote"):  # {"repo": "octo/alpha", "branch": "main"}: what GitHub has there
                return self._send({"ok": True, "remote": remote(**body["remote"])})
            if body.get("push"):  # {"repo": "octo/alpha", "file": "x.txt", "text": "..."}: someone else pushes a commit
                return self._send({"ok": True, "push": push(**body["push"])})
            if body.get("attach"):  # {"chat": id, "repo": "octo/alpha"}: a chat that works on a repo, as after its first turn
                return self._send({"ok": True, "attach": attach(body["attach"]["chat"], body["attach"]["repo"])})
            return self._send({"ok": True})
        try:
            res = asyncio.run(pipe.pipe(body, {"id": "u1", "role": role["value"]}, None))
        except Exception as e:  # noqa: BLE001 - Open WebUI answers null here
            print(f"pipe raised: {e.__class__.__name__}: {e}", file=sys.stderr, flush=True)
            res = None
        self._send(res if isinstance(res, dict) else None)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
print(json.dumps({"port": srv.server_address[1], "token": fake.token}), flush=True)
srv.serve_forever()
