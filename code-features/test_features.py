#!/usr/bin/env python3
"""End-to-end tests for code-features: real git in temp dirs, real server.

One server is started on a free port in a thread per test (function-scoped
fixture), and each test creates its own feature (unique chat id) so they do not
interfere. The API client sends the Bearer token, and the server enforces it
exactly as in production.
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

# Sentinel: "no token argument given" vs an explicit None (meaning "no header").
_NO_TOKEN = object()

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "code-features.py")
FEATURE_CLI = os.path.join(HERE, "feature")
WS_AGENTS = os.path.join(HERE, "workspace-AGENTS.md")

CHECK_SRC = '''import os, sys
path = sys.argv[1]
try:
    with open(path) as f:
        content = f.read()
except OSError:
    content = ""
if "SLOW" in content:
    import time
    time.sleep(5)
if "BROKEN" in content:
    sys.exit(1)
print("ok")
'''

RESTART_SH = "#!/bin/sh\necho \"$1\" >> %s\n"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Harness:
    def __init__(self, base):
        self.base = base
        self.projects = os.path.join(base, "projects")
        self.home = os.path.join(base, "home")
        self.dir = os.path.join(base, "features")
        self.port = _free_port()
        self.token = "tok-secret"
        self.restart_log = os.path.join(base, "restarts.log")
        self.fixer_index = os.path.join(base, "fixes.jsonl")
        self.check_script = os.path.join(base, "check_broken.py")
        self.comps = os.path.join(base, "comps.json")
        self.restart_cmd = "sh " + os.path.join(base, "restart.sh")
        self._repos = {}
        self._server = None

    # -- filesystem / git helpers ----------------------------------------
    @staticmethod
    def _write(path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def _git(self, cwd, *args):
        return subprocess.run(["git", "-C", cwd, *args],
                              capture_output=True, text=True)

    def build_repo(self, name, branch, files):
        repo = os.path.join(self.projects, name)
        for rel, content in files.items():
            self._write(os.path.join(repo, rel), content)
        self._git(repo, "init", "-q", "-b", branch)
        self._git(repo, "config", "user.email", "t@t")
        self._git(repo, "config", "user.name", "t")
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", "init")
        self._repos[name] = (repo, branch)
        return repo

    def build(self):
        self._write(self.check_script, CHECK_SRC)
        os.chmod(self.check_script, 0o755)
        comps = [
            {"name": "desktop-cli", "label": "Desktop tool", "branch": "master",
             "test": ["python3", self.check_script, "desktop"],
             "install": [{"from": "desktop", "to": "~/.local/bin/desktop",
                          "mode": "0755"}], "restart": []},
            {"name": "code-preview", "label": "Preview server", "branch": "master",
             "test": ["python3", self.check_script, "code-preview.py"],
             "install": [{"from": "code-preview.py",
                          "to": "~/.local/lib/code-mode/code-preview.py",
                          "mode": "0644"}],
             "restart": ["code-preview"]},
            {"name": "code-fixer", "label": "Bug fixer", "branch": "main",
             "test": ["python3", self.check_script, "code-fixer.py"],
             "install": [{"from": "code-fixer.py",
                          "to": "~/.local/lib/code-mode/code-fixer.py",
                          "mode": "0644"},
                        {"from": "viewer.html",
                         "to": "~/.local/lib/code-mode/viewer.html",
                         "mode": "0644"},
                        {"from": "log.html",
                         "to": "~/.local/lib/code-mode/log.html", "mode": "0644"},
                        {"from": "report-bug", "to": "~/.local/bin/report-bug",
                         "mode": "0755"}],
             "restart": ["code-fixer"]},
            {"name": "code-mode", "label": "Open WebUI side", "branch": "main",
             "test": ["python3", self.check_script, "agents/AGENTS.md"],
             "install": [{"from": "agents/AGENTS.md",
                          "to": "~/.config/opencode/AGENTS.md", "mode": "0644"}],
             "restart": [], "remote": "owui/"},
        ]
        with open(self.comps, "w") as fh:
            json.dump(comps, fh)
        self.build_repo("desktop-cli", "master", {"desktop": "v1\n"})
        self.build_repo("code-preview", "master",
                        {"code-preview.py": "# preview v1\n"})
        self.build_repo("code-fixer", "main",
                        {"code-fixer.py": "# fixer v1\n", "viewer.html": "v",
                         "log.html": "v", "report-bug": "#!/bin/sh\n"})
        self.build_repo("code-mode", "main",
                        {"agents/AGENTS.md": "agents v1\n",
                         "owui/code_mode.py": "# code_mode v1\n"})
        open(self.fixer_index, "w").close()
        self._write(os.path.join(self.base, "restart.sh"),
                    RESTART_SH % self.restart_log)
        os.chmod(os.path.join(self.base, "restart.sh"), 0o755)

    def env(self):
        e = dict(os.environ)
        e.update(FEATURES_TOKEN=self.token, FEATURES_PORT=str(self.port),
                 FEATURES_DIR=self.dir, FEATURES_PROJECTS=self.projects,
                 FEATURES_HOME=self.home, FEATURES_COMPONENTS=self.comps,
                 FEATURES_RESTART_CMD=self.restart_cmd,
                 FEATURES_FIXER_INDEX=self.fixer_index)
        return e

    def start(self):
        os.makedirs(self.dir, exist_ok=True)
        self._server = subprocess.Popen(
            [sys.executable, SERVER], env=self.env(),
            stdout=open(os.path.join(self.base, "srv.out"), "w"),
            stderr=subprocess.STDOUT)
        for _ in range(100):
            if self._server.poll() is not None:
                raise RuntimeError("server exited early:\n"
                                   + open(os.path.join(self.base, "srv.out")).read())
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/health" % self.port,
                        timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not start:\n"
                           + open(os.path.join(self.base, "srv.out")).read())

    def stop(self):
        if self._server is not None:
            self._server.terminate()
            try:
                self._server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._server.kill()

    # -- API + git helpers -----------------------------------------------
    def api(self, method, path, body=None, token=_NO_TOKEN):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        tok = self.token if token is _NO_TOKEN else token
        if tok is not None:
            req.add_header("Authorization", "Bearer " + tok)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
        except Exception as e:
            return None, {"err": repr(e)}

    def create(self, chat_id):
        return self.api("POST", "/api/features", {"chat_id": chat_id})

    def main_rev(self, name, ref="HEAD"):
        repo, _ = self._repos[name]
        return self._git(repo, "rev-parse", ref).stdout.strip()

    def main_is_unchanged(self, name, before):
        return self.main_rev(name) == before

    def commit_worktree(self, worktree, filename, content, msg,
                        user="dev", email="dev@example.com"):
        with open(os.path.join(worktree, filename), "a") as f:
            f.write(content)
        g = self._git
        g(worktree, "config", "user.email", email)
        g(worktree, "config", "user.name", user)
        g(worktree, "add", "-A")
        g(worktree, "commit", "-q", "-m", msg)

    def add_untracked(self, worktree, filename, content):
        with open(os.path.join(worktree, filename), "w") as f:
            f.write(content)

    def write(self, worktree, path, content):
        # Write `content` to `worktree/path` WITHOUT committing, leaving a
        # tracked file dirty (uncommitted change).
        self._write(os.path.join(worktree, path), content)

    def wait_op(self, op_id, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            st, o = self.api("GET", "/api/ops/%s" % op_id)
            if st == 200 and o["op"]["status"] != "running":
                return o["op"]
            time.sleep(0.1)
        raise TimeoutError("op %s still running" % op_id)

    def install_target(self, comp, i=0):
        with open(self.comps) as fh:
            c = [c for c in json.load(fh) if c["name"] == comp][0]
        to = c["install"][i]["to"]
        return os.path.join(self.home, to[2:]) if to.startswith("~/") else to

    def restarts(self):
        if not os.path.exists(self.restart_log):
            return []
        with open(self.restart_log) as fh:
            return [l.strip() for l in fh if l.strip()]

    # -- CLI -------------------------------------------------------------
    def run_cli(self, *args, cwd, extra_env=None):
        env = self.env()
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, FEATURE_CLI, *args],
                              capture_output=True, text=True, env=env, cwd=cwd)


@pytest.fixture
def h(tmp_path):
    base = str(tmp_path)
    hx = Harness(base)
    hx.build()
    hx.start()
    yield hx
    hx.stop()


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def test_auth_open_health_enforced_other(h):
    st, _ = h.api("GET", "/api/health", token=None)
    assert st == 200
    st, _ = h.api("GET", "/api/features", token=None)
    assert st == 401
    st, _ = h.api("GET", "/api/features", token="wrong")
    assert st == 401
    st, body = h.api("GET", "/api/features")
    assert st == 200 and "features" in body


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def test_create_worktrees_base_agents_json_idempotent(h):
    st, body = h.create("chat-create")
    assert st == 201
    f = body["feature"]
    assert f["status"] == "draft"
    assert f["branch"] == "feature/1"
    d = f["dir"]
    for name in ("desktop-cli", "code-preview", "code-fixer", "code-mode"):
        assert os.path.isdir(os.path.join(d, name)), name
    # base (per-component main sha at creation) is recorded on the feature:
    rec = json.load(open(os.path.join(h.dir, "features", "%d.json" % f["n"])))
    for name in ("desktop-cli", "code-preview", "code-fixer", "code-mode"):
        assert name in rec["base"]
        assert rec["base"][name]
    agents = open(os.path.join(d, "AGENTS.md")).read()
    assert "feature/1" in agents
    featjson = json.load(open(os.path.join(d, ".feature.json")))
    assert featjson == {"n": 1, "chat_id": "chat-create"}
    # idempotent per chat
    st2, body2 = h.create("chat-create")
    assert st2 == 200 and body2["feature"]["n"] == 1
    # n increments for a new chat
    st3, body3 = h.create("chat-other")
    assert st3 == 201 and body3["feature"]["branch"] == "feature/2"


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------
def test_view_ahead_commits_dirty_remote_can(h):
    st, body = h.create("chat-view")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    wt = os.path.join(d, "desktop-cli")
    h.commit_worktree(wt, "desktop", "# feature line\n", "branch commit")
    wt2 = os.path.join(d, "code-mode")
    h.commit_worktree(wt2, "owui/code_mode.py", "# remote change\n", "remote")
    st, body = h.api("GET", "/api/features/%d" % n)
    f = body["feature"]
    dc = [c for c in f["components"] if c["name"] == "desktop-cli"][0]
    assert dc["ahead"] == 1
    assert dc["commits"][0]["short"]
    assert dc["commits"][0]["subject"] == "branch commit"
    assert dc["label"] == "Desktop tool"
    rt = f.get("remote_touched") or []
    assert "owui/code_mode.py" in rt
    assert "desktop-cli" in f["touched"]
    # clean worktree + ahead>0 -> deployable
    assert f["can_deploy"] is True and f["can_rollback"] is False
    assert f["blocked"] is None
    # blocked when dirty worktree: an uncommitted tracked change (h.write) +
    # an untracked file (notes.txt)
    h.write(wt, "desktop", "uncommitted\n")
    h.add_untracked(wt, "notes.txt", "scratch")
    st, body = h.api("GET", "/api/features/%d" % n)
    f = body["feature"]
    dc = [c for c in f["components"] if c["name"] == "desktop-cli"][0]
    assert "desktop" in dc["dirty"] and "notes.txt" in dc["dirty"]
    assert f["can_deploy"] is False
    assert f["blocked"] and "Uncommitted changes" in f["blocked"]


# ---------------------------------------------------------------------------
# ready validation
# ---------------------------------------------------------------------------
def test_ready_validation(h):
    st, body = h.create("chat-ready")
    n = body["feature"]["n"]
    st, _ = h.api("POST", "/api/features/%d/ready" % n, {"title": "", "summary": "x"})
    assert st == 400
    st, _ = h.api("POST", "/api/features/%d/ready" % n,
                  {"title": "t" * 200, "summary": "x"})
    assert st == 400
    st, body = h.api("POST", "/api/features/%d/ready" % n,
                     {"title": "Good title", "summary": "a summary"})
    assert st == 200 and body["feature"]["status"] == "ready"
    assert body["feature"]["ready_at"]


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------
def test_deploy_installs_backs_up_and_records_applied(h):
    st, body = h.create("chat-deploy")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    before = h.main_rev("desktop-cli")
    # pre-create the install target so a backup is made
    tgt = h.install_target("desktop-cli")
    os.makedirs(os.path.dirname(tgt), exist_ok=True)
    with open(tgt, "w") as f:
        f.write("old live content\n")
    old_sha = hashlib.sha256(open(tgt, "rb").read()).hexdigest()
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# new desktop\n", "deploy change")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    assert st == 202
    op = h.wait_op(op["op"])
    assert op["status"] == "ok", op.get("error")
    # main advanced with a merge commit
    assert h.main_rev("desktop-cli") != before
    installed = open(tgt).read()
    assert "# new desktop" in installed
    ins = op["installed"][0]
    assert ins["sha_before"] == old_sha
    assert ins["sha_after"]
    backup = os.path.join(h.dir, "ops", op["id"], "backup", tgt.lstrip("/"))
    assert os.path.exists(backup)
    assert open(backup).read() == "old live content\n"
    # code-mode remote change -> remote.changed
    h.commit_worktree(os.path.join(d, "code-mode"), "owui/code_mode.py",
                      "# remote\n", "remote")
    st, op2 = h.api("POST", "/api/features/%d/deploy" % n)
    op2 = h.wait_op(op2["op"])
    assert op2["remote"]["changed"] == ["owui/code_mode.py"]
    assert "code-mode" in op2["commits"] and op2["commits"]["code-mode"]


def test_deploy_restarts_only_relevant_units(h):
    st, body = h.create("chat-restart")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "code-preview"), "code-preview.py",
                      "# preview change\n", "preview change")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    op = h.wait_op(op["op"])
    assert op["status"] == "ok"
    assert op["restarted"] == [{"unit": "code-preview", "ok": True,
                                "output": ""}]
    assert h.restarts() == ["code-preview"]


def test_deploy_blocked_dirty_worktree(h):
    st, body = h.create("chat-block1")
    n = body["feature"]["n"]
    wt = os.path.join(body["feature"]["dir"], "desktop-cli")
    h.commit_worktree(wt, "desktop", "committed\n", "committed")
    h.write(wt, "desktop", "uncommitted\n")
    st, body = h.api("GET", "/api/features/%d" % n)
    assert body["feature"]["can_deploy"] is False


def test_deploy_blocked_dirty_main(h):
    st, body = h.create("chat-block2")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# change\n", "change")
    # uncommitted change in the main checkout
    main_wt = os.path.join(h.projects, "desktop-cli")
    with open(os.path.join(main_wt, "desktop"), "a") as f:
        f.write("uncommitted in main\n")
    st, resp = h.api("POST", "/api/features/%d/deploy" % n)
    assert st == 400 and "desktop-cli" in resp["error"]


def test_deploy_blocked_busy_fixer_then_old_record_does_not_block(h):
    st, body = h.create("chat-busy")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# change\n", "change")
    # recent non-done record -> busy
    now = int(time.time())
    with open(h.fixer_index, "w") as f:
        f.write(json.dumps({"id": "x1", "created": now, "status": "working"}) + "\n")
    st, resp = h.api("POST", "/api/features/%d/deploy" % n)
    assert st == 400 and "fixer" in resp["error"].lower()
    # an old non-done record (> 2h) does NOT block
    with open(h.fixer_index, "w") as f:
        f.write(json.dumps(
            {"id": "x1", "created": now - 10000, "status": "working"}) + "\n")
    st, resp = h.api("POST", "/api/features/%d/deploy" % n)
    assert st == 202
    op = h.wait_op(resp["op"])
    assert op["status"] == "ok"
    open(h.fixer_index, "w").close()


def test_deploy_already_running_409(h):
    st, body = h.create("chat-409")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# SLOW line\n", "slow change")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    assert st == 202
    # wait until the op is actually running (holds the lock)
    for _ in range(200):
        _, o = h.api("GET", "/api/ops/%s" % op["op"])
        if o["op"]["status"] == "running":
            break
        time.sleep(0.1)
    st, resp = h.api("POST", "/api/features/%d/deploy" % n)
    assert st == 409 and resp["error"] == "busy" and "op" in resp
    op = h.wait_op(op["op"])
    assert op["status"] == "ok"


def test_deploy_failing_tests_leave_unchanged(h):
    st, body = h.create("chat-fail")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    before = h.main_rev("desktop-cli")
    tgt = h.install_target("desktop-cli")
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# BROKEN change\n", "broken")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    op = h.wait_op(op["op"])
    assert op["status"] == "failed"
    assert op["error"]
    # nothing promoted, nothing installed
    assert h.main_rev("desktop-cli") == before
    assert not os.path.exists(tgt) or "BROKEN" not in open(tgt).read()


def test_deploy_merge_conflict_names_files_unchanged(h):
    st, body = h.create("chat-conflict")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    wt = os.path.join(d, "desktop-cli")
    h.commit_worktree(wt, "desktop", "# branch content A\n", "branch")
    # a conflicting change landed on main after the branch point
    main_wt = os.path.join(h.projects, "desktop-cli")
    h.commit_worktree(main_wt, "desktop", "# main content B\n", "main change")
    before = h.main_rev("desktop-cli")   # main's HEAD just before the deploy
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    op = h.wait_op(op["op"])
    assert op["status"] == "failed"
    assert "desktop" in op["error"]
    # the failed merge left main exactly where it was (not promoted)
    assert h.main_rev("desktop-cli") == before
    assert not os.path.exists(h.install_target("desktop-cli"))


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------
def test_rollback_restores_old_content(h):
    st, body = h.create("chat-rollback")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    tgt = h.install_target("desktop-cli")
    os.makedirs(os.path.dirname(tgt), exist_ok=True)
    with open(tgt, "w") as f:
        f.write("previous\n")
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# deployed change\n", "deploy")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    op = h.wait_op(op["op"])
    assert op["status"] == "ok"
    assert "# deployed change" in open(tgt).read()
    # rollback
    st, op = h.api("POST", "/api/features/%d/rollback" % n)
    op = h.wait_op(op["op"])
    assert op["status"] == "ok", op.get("error")
    st, body = h.api("GET", "/api/features/%d" % n)
    assert body["feature"]["status"] == "rolled_back"
    assert "# deployed change" not in open(tgt).read()
    assert open(tgt).read().strip() == "v1"   # main reverted, then reinstalled
    assert op["commits"]  # revert commits recorded on the op


def test_rollback_conflict_names_later_commit_changes_nothing(h):
    st, body = h.create("chat-rb-conflict")
    n1 = body["feature"]["n"]
    d1 = body["feature"]["dir"]
    tgt = h.install_target("desktop-cli")
    h.commit_worktree(os.path.join(d1, "desktop-cli"), "desktop",
                      "# feature A change\n", "A")
    st, op = h.api("POST", "/api/features/%d/deploy" % n1)
    h.wait_op(op["op"])
    # a later change that overwrites the same file A deployed
    main_wt = os.path.join(h.projects, "desktop-cli")
    with open(os.path.join(main_wt, "desktop"), "w") as f:
        f.write("# different main content\n")
    h._git(main_wt, "config", "user.email", "m@m")
    h._git(main_wt, "config", "user.name", "m")
    h._git(main_wt, "add", "-A")
    h._git(main_wt, "commit", "-q", "-m", "direct change")
    st, op = h.api("POST", "/api/features/%d/rollback" % n1)
    op = h.wait_op(op["op"])
    assert op["status"] == "failed"
    assert "later" in op["error"].lower()
    st, body = h.api("GET", "/api/features/%d" % n1)
    # nothing changed: still deployed
    assert body["feature"]["status"] == "deployed"


def test_redeploy_after_rollback_restores_and_includes_new(h):
    st, body = h.create("chat-redeploy")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    tgt = h.install_target("desktop-cli")
    os.makedirs(os.path.dirname(tgt), exist_ok=True)
    with open(tgt, "w") as f:
        f.write("baseline\n")
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# first change\n", "first")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    h.wait_op(op["op"])
    st, op = h.api("POST", "/api/features/%d/rollback" % n)
    h.wait_op(op["op"])
    st, body = h.api("GET", "/api/features/%d" % n)
    assert body["feature"]["status"] == "rolled_back"
    # a new change on the branch in addition to the original
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# second change\n", "second")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    op = h.wait_op(op["op"])
    assert op["status"] == "ok", op.get("error")
    content = open(tgt).read()
    assert "# first change" in content  # restored
    assert "# second change" in content  # new branch commit merged
    st, body = h.api("GET", "/api/features/%d" % n)
    assert body["feature"]["status"] == "deployed"


def test_update_deploy_of_deployed_feature(h):
    st, body = h.create("chat-update")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# v1 change\n", "v1")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    h.wait_op(op["op"])
    st, body = h.api("GET", "/api/features/%d" % n)
    assert body["feature"]["update_available"] is False
    # more commits on the branch
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# v2 change\n", "v2")
    st, body = h.api("GET", "/api/features/%d" % n)
    assert body["feature"]["update_available"] is True
    assert body["feature"]["can_deploy"] is True
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    op = h.wait_op(op["op"])
    assert op["status"] == "ok"
    assert "# v2 change" in open(h.install_target("desktop-cli")).read()


# ---------------------------------------------------------------------------
# discard
# ---------------------------------------------------------------------------
def test_discard_removes_worktrees(h):
    st, body = h.create("chat-discard")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop", "# change\n", "c")
    st, op = h.api("POST", "/api/features/%d/discard" % n)
    op = h.wait_op(op["op"])
    assert op["status"] == "ok"
    st, body = h.api("GET", "/api/features/%d" % n)
    assert body["feature"]["status"] == "discarded"
    assert not os.path.isdir(os.path.join(d, "desktop-cli"))


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------
def test_sync_installs_and_restarts(h):
    st, op = h.api("POST", "/api/sync")
    assert st == 202
    op = h.wait_op(op["op"])
    assert op["status"] == "ok"
    # code-preview and code-fixer have restart units
    assert "code-preview" in h.restarts()
    assert "code-fixer" in h.restarts()
    assert os.path.exists(h.install_target("desktop-cli"))


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------
def test_drift_detects_modified_and_missing(h):
    st, body = h.create("chat-drift")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# change\n", "c")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    h.wait_op(op["op"])
    tgt = h.install_target("desktop-cli")
    os.makedirs(os.path.dirname(tgt), exist_ok=True)
    with open(tgt, "w") as f:
        f.write("drifted\n")
    st, body = h.api("GET", "/api/drift")
    drift = {x["component"]: x for x in body["drift"]}
    assert "desktop-cli" in drift
    assert drift["desktop-cli"]["actual"] != drift["desktop-cli"]["expected"]


# ---------------------------------------------------------------------------
# files endpoint
# ---------------------------------------------------------------------------
def test_files_endpoint_content_bad_rev_and_dotdot(h):
    st, body = h.create("chat-files")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# change\n", "c")
    h.wait_op(h.api("POST", "/api/features/%d/deploy" % n)[1]["op"])
    rev = h.main_rev("desktop-cli")
    url = "/api/files/desktop-cli/%s/desktop" % rev
    st, raw = _raw_api(h.port, url)
    assert st == 200 and b"# change" in raw
    # bad rev -> 404
    st, _ = h.api("GET", "/api/files/desktop-cli/ZZZ/desktop")
    assert st == 404
    # dotdot rejected -> 404
    st, _ = h.api("GET", "/api/files/desktop-cli/%s/..%%2F%%2Fetc" % rev[:8])
    assert st == 404


def _raw_api(port, url, token="tok-secret"):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, url),
                                 method="GET")
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read()


# ---------------------------------------------------------------------------
# diff pending / deployed
# ---------------------------------------------------------------------------
def test_diff_pending_includes_untracked_and_deployed(h):
    st, body = h.create("chat-diff")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    wt = os.path.join(d, "desktop-cli")
    h.commit_worktree(wt, "desktop", "# change\n", "c")
    h.add_untracked(wt, "brand_new.txt", "new file\n")
    st, body = h.api("GET", "/api/features/%d/diff?what=pending" % n)
    files = {f["file"]: f for f in body["files"]}
    assert "desktop" in files and files["desktop"]["additions"] >= 1
    assert "brand_new.txt" in files  # untracked reported
    # commit the untracked file so the deploy isn't blocked by a dirty worktree
    h.commit_worktree(wt, "brand_new.txt", "new file\n", "add brand_new")
    # deploy, then diff deployed
    h.wait_op(h.api("POST", "/api/features/%d/deploy" % n)[1]["op"])
    st, body = h.api("GET", "/api/features/%d/diff?what=deployed" % n)
    files = body["files"]
    assert files  # applied commit diffs present


# ---------------------------------------------------------------------------
# ops list + log
# ---------------------------------------------------------------------------
def test_ops_list_and_log_content(h):
    st, body = h.create("chat-ops")
    n = body["feature"]["n"]
    d = body["feature"]["dir"]
    h.commit_worktree(os.path.join(d, "desktop-cli"), "desktop",
                      "# change\n", "c")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    opid = op["op"]
    op = h.wait_op(opid)
    st, body = h.api("GET", "/api/ops")
    ids = [o["id"] for o in body["ops"]]
    assert opid in ids
    st, body = h.api("GET", "/api/ops/%s" % opid)
    assert st == 200
    assert "$ git" in body["log"] or "git merge" in body["log"]
    assert "-> exit" in body["log"]


# ---------------------------------------------------------------------------
# the feature CLI
# ---------------------------------------------------------------------------
def test_cli_status_test_ready_and_errors(h):
    st, body = h.create("chat-cli")
    n = body["feature"]["n"]
    ws = body["feature"]["dir"]
    wt = os.path.join(ws, "desktop-cli")
    h.commit_worktree(wt, "desktop", "# cli change\n", "c")
    # status from inside the workspace
    r = h.run_cli("status", cwd=ws)
    assert r.returncode == 0 and "Feature #%d" % n in r.stdout
    # diff
    r = h.run_cli("diff", cwd=ws)
    assert r.returncode == 0 and "desktop" in r.stdout
    # test passes
    r = h.run_cli("test", cwd=ws)
    assert r.returncode == 0 and "PASS desktop-cli" in r.stdout
    # ready succeeds
    r = h.run_cli("ready", "Title", "summary", cwd=ws)
    assert r.returncode == 0 and "is ready" in r.stdout
    # ready again fails: dirty worktree (an uncommitted tracked change)
    h.write(wt, "desktop", "uncommitted\n")
    r = h.run_cli("ready", "T", "S", cwd=ws)
    assert r.returncode == 1 and "uncommitted" in (r.stdout + r.stderr).lower()
    # failing test -> exit 1
    r = h.run_cli("test", cwd=ws, extra_env={})
    # introduce a BROKEN change and re-test
    h.commit_worktree(wt, "desktop", "# BROKEN\n", "broken", user="b")
    r = h.run_cli("test", cwd=ws)
    assert r.returncode == 1 and "FAIL desktop-cli" in r.stdout
    # outside a workspace -> exit 2
    r = h.run_cli("status", cwd="/tmp")
    assert r.returncode == 2
    assert "Not in a feature workspace" in r.stdout


# ---------------------------------------------------------------------------
# input types and ready-after-deploy (added in Claude's review)
# ---------------------------------------------------------------------------
def test_bad_input_types_are_400_not_500(h):
    st, _ = h.api("POST", "/api/features", {"chat_id": [1]})
    assert st == 400
    st, body = h.create("chat-types")
    n = body["feature"]["n"]
    st, _ = h.api("POST", "/api/features/%d/ready" % n, {"title": 5, "summary": "x"})
    assert st == 400
    st, _ = h.api("POST", "/api/features/%d/ready" % n, {"title": "ok", "summary": ["x"]})
    assert st == 400
    st, _ = h.api("POST", "/api/features/%d/tests" % n, {"nosuch": {"ok": True}})
    assert st == 400
    st, _ = h.api("POST", "/api/features/%d/tests" % n, {"desktop-cli": "yes"})
    assert st == 400


def test_ready_after_deploy_keeps_it_deployed_and_rollbackable(h):
    st, body = h.create("chat-ready-again")
    n = body["feature"]["n"]
    h.commit_worktree(os.path.join(body["feature"]["dir"], "desktop-cli"), "desktop",
                      "# live\n", "live change")
    st, op = h.api("POST", "/api/features/%d/deploy" % n)
    assert st == 202
    assert h.wait_op(op["op"])["status"] == "ok"
    st, body = h.api("POST", "/api/features/%d/ready" % n, {"title": "Renamed", "summary": "s"})
    assert st == 200
    f = body["feature"]
    assert f["status"] == "deployed" and f["can_rollback"] is True and f["title"] == "Renamed"
    st, op = h.api("POST", "/api/features/%d/rollback" % n)
    assert st == 202
    assert h.wait_op(op["op"])["status"] == "ok"


def test_ops_list_reads_disk_and_includes_sync(h):
    st, op = h.api("POST", "/api/sync")
    assert st == 202
    assert h.wait_op(op["op"])["status"] == "ok"
    st, body = h.api("GET", "/api/ops")
    assert st == 200 and op["op"] in [o["id"] for o in body["ops"]]
    # a restarted service still lists it (history lives on disk)
    h.stop()
    h.start()
    st, body = h.api("GET", "/api/ops")
    assert op["op"] in [o["id"] for o in body["ops"]]
    st, _ = h.api("GET", "/api/ops/..")
    assert st == 404


def test_cli_reads_the_token_from_the_env_file(h, tmp_path):
    # In real use FEATURES_TOKEN is only in /etc/code-mode/features.env, never in the environment
    # (a bug read the wrong dict and every call got 401; the suite missed it by passing the token in env).
    st, body = h.create("chat-envfile")
    ws = body["feature"]["dir"]
    envfile = tmp_path / "features.env"
    envfile.write_text("FEATURES_TOKEN=%s\nFEATURES_PORT=%d\n" % (h.token, h.port))
    env = h.env()
    del env["FEATURES_TOKEN"]
    del env["FEATURES_PORT"]
    env["FEATURES_ENV_FILE"] = str(envfile)
    r = subprocess.run([sys.executable, FEATURE_CLI, "status"], capture_output=True, text=True, env=env, cwd=ws)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Unauthorized" not in r.stdout + r.stderr


def test_features_survive_a_restart_and_unknown_is_404(h):
    st, body = h.create("chat-restart")
    n = body["feature"]["n"]
    h.stop()
    h.start()
    st, body = h.api("GET", "/api/features/%d" % n)
    assert st == 200 and body["feature"]["chat_id"] == "chat-restart"
    st, body = h.create("chat-restart")          # the same chat gets the same feature back
    assert st == 200 and body["feature"]["n"] == n
    st, _ = h.api("GET", "/api/features/999")
    assert st == 404
    st, _ = h.api("GET", "/api/features/999/diff")
    assert st == 404
    st, _ = h.api("GET", "/api/features/%d/ready" % n)
    assert st == 405


# ---------------------------------------------------------------------------
# checklist (the Code page's Checklist tab reads the builder's CHECKLIST.md)
# ---------------------------------------------------------------------------
CHECKLIST = """# Feature #7 — stage 3a

## Acceptance
- [x] A.1 review session of this chat opens — 200 — commit ec0d6d9; acceptance passes
- [x] **A.2** another chat's review refused — 404
- [ ] A.8 page: header says "Review · round N"
- [ ] own tests added; all existing tests still pass

## Open
- (none yet)
- the page test for the header is flaky

## Next
build the git-diff parser
"""


def test_checklist_of_a_feature_chat(h):
    st, body = h.create("chat-check")
    feat = body["feature"]
    with open(os.path.join(feat["dir"], "CHECKLIST.md"), "w") as f:
        f.write(CHECKLIST)
    st, c = h.api("GET", "/api/checklist/chat-check")
    assert st == 200 and c["exists"] and c["kind"] == "feature" and c["feature"] == feat["n"]
    assert c["title"] == "Feature #7 — stage 3a"
    assert [it["id"] for it in c["items"]] == ["A.1", "A.2", "A.8", ""]
    assert [it["done"] for it in c["items"]] == [True, True, False, False]
    assert (c["done"], c["total"], c["now"]) == (2, 4, 2)       # "now" = the first box not ticked, not the Next line
    a1 = c["items"][0]
    assert a1["label"] == "review session of this chat opens"
    assert a1["detail"].startswith("200") and a1["sha"] == "ec0d6d9" and a1["section"] == "Acceptance"
    assert c["items"][3]["label"] == "own tests added; all existing tests still pass"
    assert c["open"] == ["the page test for the header is flaky"]
    assert isinstance(c["updated"], int) and isinstance(c["server_time"], int)


def test_checklist_of_a_project_chat_missing_bad_and_unauthenticated(h):
    proj = os.path.join(h.projects, "chats", "chat-proj", ".code-mode")
    os.makedirs(proj)
    with open(os.path.join(proj, "CHECKLIST.md"), "w") as f:
        f.write("- [ ] one\n- [x] two\n")
    st, c = h.api("GET", "/api/checklist/chat-proj")
    assert st == 200 and c["exists"] and c["kind"] == "project" and c["feature"] is None
    assert (c["done"], c["total"], c["now"]) == (1, 2, 0)
    st, c = h.api("GET", "/api/checklist/chat-nothing")
    assert st == 200 and c["exists"] is False
    for bad in ("a.b", "..", "x" * 65):
        st, _ = h.api("GET", "/api/checklist/" + bad)
        assert st == 404, bad
    st, _ = h.api("GET", "/api/checklist/chat-proj", token=None)
    assert st == 401


def test_a_long_checklist_is_read_to_its_last_box(h):
    # long builds keep notes in CHECKLIST.md; at 128 KiB the newest boxes at the bottom fell off the tab
    proj = os.path.join(h.projects, "chats", "chat-long", ".code-mode")
    os.makedirs(proj)
    notes = "".join("note %05d about what was tried and what it showed\n" % i for i in range(4000))
    with open(os.path.join(proj, "CHECKLIST.md"), "w") as f:
        f.write("- [x] first\n## Notes\n" + notes + "## Acceptance\n- [ ] last box\n")
    assert os.path.getsize(os.path.join(proj, "CHECKLIST.md")) > 128 * 1024
    st, c = h.api("GET", "/api/checklist/chat-long")
    assert st == 200 and c["total"] == 2 and c["items"][-1]["label"] == "last box" and c["now"] == 1


def test_the_builders_now_lines_say_which_task_it_is_on(h):
    # the Code page shows them above the running live window (10-24, the user: "a little summary as to what it is
    # currently doing for each task ... when it moves to the next task it would update that")
    proj = os.path.join(h.projects, "chats", "chat-now", ".code-mode")
    os.makedirs(proj)
    with open(os.path.join(proj, "CHECKLIST.md"), "w") as f:
        f.write("# Owl\n\n## Now\n- **Task:** B4 ChatGPT auth chain\nDoing: tracing who still reads `chatgpt_base_url`\n\n"
                "## Acceptance\n- [x] A.1 one - done\n- [ ] B4 ChatGPT auth chain\n")
    st, c = h.api("GET", "/api/checklist/chat-now")
    assert st == 200 and c["current"] == {"task": "B4 ChatGPT auth chain", "doing": "tracing who still reads `chatgpt_base_url`"}
    assert (c["done"], c["total"]) == (1, 2)  # the Now lines are not boxes
    with open(os.path.join(proj, "CHECKLIST.md"), "w") as f:
        f.write("# Owl\n## Next\nsomething\n- [ ] one\n")
    st, c = h.api("GET", "/api/checklist/chat-now")
    assert st == 200 and c["current"] is None  # no Now section: the page falls back to the first open box


def test_checklist_symlinked_out_of_the_projects_is_not_read(h, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("- [x] should never be served\n")
    proj = os.path.join(h.projects, "chats", "chat-link", ".code-mode")
    os.makedirs(proj)
    os.symlink(str(outside), os.path.join(proj, "CHECKLIST.md"))
    st, c = h.api("GET", "/api/checklist/chat-link")
    assert st == 200 and c["exists"] is False and "items" not in c
