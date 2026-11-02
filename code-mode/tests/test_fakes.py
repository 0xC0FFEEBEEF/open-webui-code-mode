"""The fakes themselves: tests/fake_opencode.py and the git/deploy-key side of tests/fake_github.py behave as their
docstrings say. Also the shortest example of running a command on the fake VM the way the pipe has to."""
import os
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fake_github import TOKEN, FakeGitHub  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402


def sh(c, command, cwd=None, timeout_ms=20000):
    """Run one command through the shell API and wait for it: (status, exit, output)."""
    body = {"command": command, "timeout": timeout_ms}
    if cwd:
        body["cwd"] = cwd
    r = c.post("/api/shell", json=body)
    r.raise_for_status()
    job = r.json()["data"]
    while job["status"] == "running":
        time.sleep(0.05)
        job = c.get(f"/api/shell/{job['id']}").json()["data"]
    out = c.get(f"/api/shell/{job['id']}/output").json()["data"]["output"]
    assert c.delete(f"/api/shell/{job['id']}").status_code == 204
    return job["status"], job["exit"], out


def test_fake_opencode_shell_api(tmp_path):
    vm = FakeOpencode(tmp_path / "home").start()
    try:
        assert httpx.post(vm.url + "/api/shell", json={"command": "true"}).status_code == 401  # no auth
        with httpx.Client(base_url=vm.url, auth=("opencode", "x")) as c:
            first = c.post("/api/shell", json={"command": "sleep 0.2; echo hi"}).json()["data"]
            assert first["status"] == "running" and first["exit"] is None  # it never waits for the command
            assert sh(c, "echo out; echo err >&2; exit 3") == ("exited", 3, "out\nerr\n")
            assert sh(c, "cd \"$HOME\" && pwd")[2].strip() == vm.home
            assert c.post("/api/shell", json={"command": "pwd", "cwd": str(tmp_path / "missing")}).status_code == 500
            assert sh(c, "mkdir -p projects/chats/abc && cd projects/chats/abc && pwd")[1] == 0
            assert sh(c, "pwd", cwd=os.path.join(vm.home, "projects/chats/abc"))[2].strip().endswith("/chats/abc")
            assert sh(c, "echo start; sleep 5", timeout_ms=300)[:2] == ("timeout", None)
            assert c.get("/api/shell/sh_nope").status_code == 404
        assert len(vm.deleted) == 5 and vm.commands[1]["output"] == "out\nerr\n"
    finally:
        vm.stop()


def test_fake_github_deploy_keys_meta_and_branches():
    gh = FakeGitHub().start()
    try:
        assert len(httpx.get(gh.url + "/meta").json()["ssh_keys"]) == 3  # no token needed
        with httpx.Client(base_url=gh.url, headers={"Authorization": f"Bearer {TOKEN}"}) as c:
            key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGtestkeytestkeytestkeytestkeytestkeytestkey code-mode"
            assert c.post("/repos/octo/alpha/keys", json={"title": "t", "key": "not a key"}).status_code == 422
            made = c.post("/repos/octo/alpha/keys", json={"title": "t", "key": key, "read_only": True})
            assert made.status_code == 201 and made.json()["read_only"] is True
            again = c.post("/repos/octo/beta/keys", json={"title": "t", "key": key, "read_only": True})
            assert again.status_code == 422 and again.json()["errors"][0]["message"] == "key is already in use"
            assert [k["id"] for k in c.get("/repos/octo/alpha/keys").json()] == [made.json()["id"]]
            gone = c.delete(f"/repos/octo/alpha/keys/{made.json()['id']}")
            assert gone.status_code == 204 and gone.content == b""
            assert gh.key_log == [("add", "octo/alpha", True), ("delete", "octo/alpha", True)] and gh.keys["octo/alpha"] == []
            assert [b["name"] for b in c.get("/repos/octo/alpha/branches").json()] == ["main", "dev"]
            assert c.get("/repos/octo/gamma/branches").json() == []  # an empty repo
            gh.deploy_keys_allowed = False
            assert c.post("/repos/octo/alpha/keys", json={"title": "t", "key": key}).status_code == 404
    finally:
        gh.stop()


def test_fake_github_remotes_are_real_git(tmp_path):
    gh = FakeGitHub()
    prefix = gh.make_remotes(tmp_path / "remotes")
    run = lambda *a, cwd: subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout  # noqa: E731
    work = tmp_path / "work"
    run("git", "clone", "-q", prefix + "octo/alpha.git", str(work), cwd=tmp_path)
    assert (work / "README.md").read_text().startswith("# alpha")
    assert "origin/dev" in run("git", "branch", "-r", cwd=work)
    assert run("git", "ls-remote", prefix + "octo/gamma.git", cwd=tmp_path) == ""  # empty: nothing to fetch
