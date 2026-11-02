"""Tests for code-deploy.

Function-scoped fixtures: a fresh temp state dir, a new fake manager and fake
Open WebUI on free ports, and the real service on a free port, all wired
together. Every test starts from clean state.
"""

import base64
import hashlib
import io
import json
import os
import sys
import threading
import time
import contextlib
import types
from http.server import ThreadingHTTPServer

import pytest
import subprocess
import urllib.request
import urllib.error

import fakes
import importlib.util

_code_deploy_path = os.path.join(os.path.dirname(__file__), "code-deploy.py")
_spec = importlib.util.spec_from_file_location("code_deploy", _code_deploy_path)
code_deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(code_deploy)

NODE = "/usr/bin/node"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("FEATURES_TOKEN", raising=False)
    monkeypatch.delenv("OWUI_URL", raising=False)
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)

    mgr = fakes.FakeManager("mgr-secret-token")
    mgr.seed_demo()
    mgr_port = mgr.start(0)
    ow = fakes.FakeOpenWebUI(key_file=None)
    ow_port = ow.start(0)

    key_file = fakes.make_secret_key()
    loader = str(tmp_path / "loader.js")
    with open(loader, "w") as fh:
        fh.write("(function(){})();\n")

    state = tmp_path / "state"
    state.mkdir()

    cfg = code_deploy.Config()
    cfg.features_url = "http://127.0.0.1:%d" % mgr_port
    cfg.features_token = "mgr-secret-token"
    cfg.owui_url = "http://127.0.0.1:%d" % ow_port
    cfg.owui_db = ow.db
    cfg.secret_key_file = key_file
    cfg.state_dir = str(state)
    cfg.allowed_origins = ["http://test-origin.example"]
    cfg.loader_targets = [loader]
    cfg.node = NODE

    deployer = code_deploy.Deployer(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), code_deploy.make_handler(deployer))
    srv_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield types.SimpleNamespace(
        mgr=mgr, ow=ow, cfg=cfg, key_file=key_file, loader=loader,
        state=state, deployer=deployer,
        base="http://127.0.0.1:%d" % srv_port,
        admin=fakes.ADMIN_TOKEN, user=fakes.USER_TOKEN,
        mgr_token="mgr-secret-token",
    )
    server.shutdown()
    mgr.stop()
    ow.stop()


def call(env, method, path, headers=None, body=None):
    url = env.base + path
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=10)
        status, raw, hdrs = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read(), e.headers
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw
    return status, parsed, hdrs


def bearer(tok):
    return {"Authorization": "Bearer " + tok}


def add_feature(env, remote_touched=None, status="ready", **kw):
    return env.mgr.add_feature(remote_touched=remote_touched or [],
                               status=status, **kw)


def wait_op(env, manager_op_id, timeout=15):
    """Poll /api/ops/<manager_id> until the correlated local op is terminal."""
    end = time.time() + timeout
    while time.time() < end:
        status, body, _ = call(env, "GET", "/api/ops/%s" % manager_op_id,
                               bearer(env.admin))
        if status == 200 and body.get("local"):
            if body["local"].get("status") in ("ok", "failed"):
                return body
        time.sleep(0.2)
    return None


def installed_json(env):
    with open(os.path.join(env.cfg.state_dir, "installed.json")) as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def test_401_no_auth(env):
    status, body, _ = call(env, "GET", "/api/state")
    assert status == 401
    assert body == {"error": "sign in"}


def test_403_non_admin(env):
    status, body, _ = call(env, "GET", "/api/state", bearer(env.user))
    assert status == 403
    assert body == {"error": "sign in"}


def test_admin_ok(env):
    status, body, _ = call(env, "GET", "/api/state", bearer(env.admin))
    assert status == 200
    assert "features" in body and "ops" in body and "drift" in body


def test_auth_cache_one_call(env):
    call(env, "GET", "/api/state", bearer(env.admin))
    call(env, "GET", "/api/state", bearer(env.admin))
    auth_calls = [c for c in env.ow.calls
                  if c == ("GET", "/api/v1/auths/")]
    assert len(auth_calls) == 1  # second request served from the 60s cache


def test_non_admin_cache_not_promoted(env):
    # A non-admin token is rejected on the first request, and the 60s cache
    # must not promote it to admin on the cached second request.
    status, _, _ = call(env, "GET", "/api/state", bearer(env.user))
    assert status == 403
    status, _, _ = call(env, "GET", "/api/state", bearer(env.user))
    assert status == 403
    auth_calls = [c for c in env.ow.calls
                  if c == ("GET", "/api/v1/auths/")]
    assert len(auth_calls) == 1  # cached second call still returns 403, not 200


# --------------------------------------------------------------------------
# CORS + preflight
# --------------------------------------------------------------------------
def test_cors_allowed_origin(env):
    hdrs_in = dict(bearer(env.admin))
    hdrs_in["Origin"] = "http://test-origin.example"
    status, _, hdrs = call(env, "GET", "/api/chats", hdrs_in)
    assert status == 200
    assert hdrs.get("Access-Control-Allow-Origin") == "http://test-origin.example"
    assert hdrs.get("Vary") == "Origin"


def test_cors_other_origin(env):
    hdrs_in = dict(bearer(env.admin))
    hdrs_in["Origin"] = "http://evil.example"
    _, _, hdrs = call(env, "GET", "/api/chats", hdrs_in)
    assert hdrs.get("Access-Control-Allow-Origin") is None


def test_preflight_allowed(env):
    status, _, hdrs = call(env, "OPTIONS", "/api/chats",
                           {"Origin": "http://test-origin.example"})
    assert status == 204
    assert hdrs.get("Access-Control-Allow-Origin") == "http://test-origin.example"
    assert "GET" in hdrs.get("Access-Control-Allow-Methods", "")
    assert "X-Code-Deploy" in hdrs.get("Access-Control-Allow-Headers", "")


def test_preflight_other_no_cors(env):
    status, _, hdrs = call(env, "OPTIONS", "/api/chats",
                           {"Origin": "http://evil.example"})
    assert status == 204
    assert hdrs.get("Access-Control-Allow-Origin") is None


# --------------------------------------------------------------------------
# signin / signout / cookie auth
# --------------------------------------------------------------------------
def test_signin(env):
    status, body, hdrs = call(env, "POST", "/auth/signin",
                              {"Content-Type": "application/json"},
                              {"email": fakes.ADMIN_EMAIL, "password": fakes.ADMIN_PASS})
    assert status == 200
    assert body["ok"] is True
    assert "name" in body
    cookie = hdrs.get("Set-Cookie", "")
    assert cookie.startswith("cd_token=")
    assert "HttpOnly" in cookie and "Max-Age=43200" in cookie


def test_signin_bad_password(env):
    status, body, _ = call(env, "POST", "/auth/signin",
                           {"Content-Type": "application/json"},
                           {"email": fakes.ADMIN_EMAIL, "password": "wrong"})
    assert status == 401
    assert body["error"] == "invalid email or password"


def test_signin_refuses_cookie_when_token_rejected(env):
    # signin "succeeds" and returns a token that OWUI's /api/v1/auths/ rejects,
    # so verify_token fails (user None). The cookie must NOT be set.
    status, body, hdrs = call(env, "POST", "/auth/signin",
                              {"Content-Type": "application/json"},
                              {"email": fakes.INVALID_SIGNIN_EMAIL,
                               "password": "anything"})
    assert status == 403
    assert body["error"] == "not an admin"
    assert not hdrs.get("Set-Cookie")


def test_signout(env):
    status, _, hdrs = call(env, "POST", "/auth/signout")
    assert status == 200
    cookie = hdrs.get("Set-Cookie", "")
    assert "cd_token=" in cookie and "Max-Age=0" in cookie


def test_harness_requires_demo(env, monkeypatch):
    # /harness.html is served only in demo mode (DEPLOY_DEMO=1); otherwise it
    # must be 404, never an empty 200 body.
    monkeypatch.delenv("DEPLOY_DEMO", raising=False)
    status, body, _ = call(env, "GET", "/harness.html")
    assert status == 404
    assert body == {"error": "Not found"}


def test_cookie_requires_xcode_deploy(env):
    cookie = "cd_token=%s" % env.admin
    # POST with the cookie but without X-Code-Deploy -> 401.
    status, _, _ = call(env, "POST", "/api/features/4/rollback",
                        {"Cookie": cookie, "X-Code-Deploy": "0"})
    assert status == 401


def test_cookie_post_works_with_header(env):
    add_feature(env, remote_touched=["owui/code_mode.py"], status="ready")
    cookie = "cd_token=%s" % env.admin
    status, body, _ = call(env, "POST", "/api/features/100/deploy",
                           {"Cookie": cookie, "X-Code-Deploy": "1"})
    assert status == 202
    assert body["op"]


# --------------------------------------------------------------------------
# data proxies
# --------------------------------------------------------------------------
def test_chats(env):
    status, body, _ = call(env, "GET", "/api/chats", bearer(env.admin))
    assert status == 200
    chats = body["chats"]
    # discarded feature #7 is excluded.
    assert all(v["status"] != "discarded" for v in chats.values())
    assert any(v.get("update_available") for v in chats.values())


def test_state_shape(env):
    status, body, _ = call(env, "GET", "/api/state", bearer(env.admin))
    assert status == 200
    for key in ("user", "features", "ops", "busy", "drift", "installed"):
        assert key in body
    assert body["user"] == {"name": "Admin User", "email": fakes.ADMIN_EMAIL}
    assert isinstance(body["drift"], list) and len(body["drift"]) >= 1


def test_state_user_logged_in(env):
    status, body, _ = call(env, "GET", "/api/state", bearer(env.admin))
    assert body["user"]["email"] is not None or body["user"]["name"] is not None


# --------------------------------------------------------------------------
# Deploy: install
# --------------------------------------------------------------------------
def test_deploy_installs_function(env):
    feat = add_feature(env, remote_touched=["owui/code_mode.py"], status="ready")
    status, body, _ = call(env, "POST", "/api/features/%d/deploy" % feat["n"],
                           bearer(env.admin))
    assert status == 202
    op_id = body["op"]
    done = wait_op(env, op_id)
    assert done is not None and done["local"]["status"] == "ok"

    local = done["local"]
    assert local["installed"][0]["path"] == "owui/code_mode.py"
    assert local["installed"][0]["target"] == "function code_mode"
    sha_before = local["installed"][0]["sha_before"]
    sha_after = local["installed"][0]["sha_after"]
    assert sha_before != sha_after

    installed = installed_json(env)
    expected = hashlib.sha256(env.mgr.files["owui/code_mode.py"].encode()).hexdigest()
    assert installed["owui/code_mode.py"]["sha256"] == expected
    assert installed["owui/code_mode.py"]["op"] == local["id"]

    # The function was updated with the new content + frontmatter description.
    updates = [u for u in env.ow.update_calls
               if u["path"] == "/api/v1/functions/id/code_mode/update"
               and "RAISE_ON_LOAD" not in u["body"].get("content", "")]
    assert len(updates) == 1
    u = updates[0]
    assert u["body"]["id"] == "code_mode"
    assert u["body"]["name"] == "Code Mode"
    assert u["body"]["content"] == env.mgr.files["owui/code_mode.py"]
    assert u["body"]["meta"]["description"] == "The Code pipe."
    assert u["body"]["meta"]["manifest"] == {}
    # Not a deploy failure: no auto-rollback.
    assert local["auto_rollback"] is None


def test_deploy_installs_loader(env):
    feat = add_feature(env, remote_touched=["owui/code-toggle.js"], status="ready")
    status, body, _ = call(env, "POST", "/api/features/%d/deploy" % feat["n"],
                           bearer(env.admin))
    assert status == 202
    done = wait_op(env, body["op"])
    local = done["local"]
    assert local["status"] == "ok"
    assert any(i["path"] == "owui/code-toggle.js" for i in local["installed"])
    # The page script goes next to the loader target with its version; loader.js is the fixed stub that loads it.
    d = os.path.dirname(env.loader)
    with open(os.path.join(d, "code-toggle.js")) as fh:
        script = fh.read()
    assert script == env.mgr.files["owui/code-toggle.js"]
    with open(os.path.join(d, "code-toggle.version")) as fh:
        assert fh.read().strip() == hashlib.sha256(script.encode("utf-8")).hexdigest()[:12]
    with open(env.loader) as fh:
        assert fh.read() == code_deploy.LOADER_STUB
    assert local["reload_page"] is True
    # Drift reads the page script, not the stub: nothing has drifted right after the install.
    assert not [x for x in code_deploy.compute_local_drift(env.cfg, code_deploy.load_installed(env.cfg))
                if x.get("path") == "owui/code-toggle.js"]


def test_page_script_node_check_fail_restores_and_rolls_back(env):
    env.mgr.files["owui/code-toggle.js"] = "function((){{ not valid js\n"
    feat = add_feature(env, remote_touched=["owui/code-toggle.js"], status="ready")
    status, body, _ = call(env, "POST", "/api/features/%d/deploy" % feat["n"],
                           bearer(env.admin))
    assert status == 202
    done = wait_op(env, body["op"])
    local = done["local"]
    assert local["status"] == "failed"
    assert "node" in (local["error"] or "").lower() or "check" in (local["error"] or "").lower()
    # The loader file was NOT written (check failed before install), and no page script appeared next to it.
    with open(env.loader) as fh:
        assert fh.read() == "(function(){})();\n"
    assert not os.path.exists(os.path.join(os.path.dirname(env.loader), "code-toggle.js"))
    # A deploy failure rolls the feature back on the manager.
    assert local["auto_rollback"] is not None
    assert env.mgr.calls.__len__() > 0


def test_owui_400_restores_installed_and_rolls_back(env):
    # code_mode installs fine, then desktop_code is rejected (400).
    env.mgr.raise_on_load_paths = {"owui/desktop_code_tool.py"}
    feat = add_feature(env,
                       remote_touched=["owui/code_mode.py", "owui/desktop_code_tool.py"],
                       status="ready")
    status, body, _ = call(env, "POST", "/api/features/%d/deploy" % feat["n"],
                           bearer(env.admin))
    assert status == 202
    done = wait_op(env, body["op"])
    local = done["local"]
    assert local["status"] == "failed"
    # code_mode was installed, then restored on failure.
    restored_paths = [r["path"] for r in local["restored"]]
    assert "owui/code_mode.py" in restored_paths
    # The manager rollback was called for the deploy failure.
    assert local["auto_rollback"] is not None
    assert any(p.endswith("/rollback") for (_m, p) in env.mgr.calls)
    # The function update was called once with new content (install) and once
    # restoring the old content.
    func_updates = [u for u in env.ow.update_calls
                    if u["path"] == "/api/v1/functions/id/code_mode/update"]
    assert len(func_updates) == 2
    new = hashlib.sha256(env.mgr.files["owui/code_mode.py"].encode()).hexdigest()
    old = hashlib.sha256(env.ow.func_content.encode()).hexdigest()
    assert func_updates[0]["body"]["content"] == env.mgr.files["owui/code_mode.py"]
    assert func_updates[1]["body"]["content"] == env.ow.func_content


# --------------------------------------------------------------------------
# Manager pass-throughs
# --------------------------------------------------------------------------
def test_manager_400_passthrough(env):
    env.mgr.reject_feature = 4
    status, body, _ = call(env, "POST", "/api/features/4/rollback", bearer(env.admin))
    assert status == 400
    assert "error" in body
    # No local op was started.
    done = wait_op(env, "whatever", timeout=1)
    assert done is None


def test_manager_409_passthrough(env):
    env.mgr.busy = "some-running-op"
    status, body, _ = call(env, "POST", "/api/features/4/rollback", bearer(env.admin))
    assert status == 409
    assert body["error"] == "busy"
    assert body["op"] == "some-running-op"


def test_manager_op_failed(env):
    feat = add_feature(env, remote_touched=["owui/code_mode.py"], status="ready")
    env.mgr.fail_feature = feat["n"]
    status, body, _ = call(env, "POST", "/api/features/%d/deploy" % feat["n"],
                           bearer(env.admin))
    done = wait_op(env, body["op"])
    local = done["local"]
    assert local["status"] == "failed"
    # Manager-level failure: no install, no auto-rollback.
    assert local["installed"] == []
    assert local["auto_rollback"] is None
    assert any("/rollback" in p for (_m, p) in env.mgr.calls) is False


# --------------------------------------------------------------------------
# rollback / discard / sync
# --------------------------------------------------------------------------
def test_rollback(env):
    status, body, _ = call(env, "POST", "/api/features/4/rollback", bearer(env.admin))
    assert status == 202
    done = wait_op(env, body["op"])
    assert done["local"]["status"] == "ok"


def test_discard(env):
    status, body, _ = call(env, "POST", "/api/features/1/discard", bearer(env.admin))
    assert status == 202
    done = wait_op(env, body["op"])
    assert done["local"]["status"] == "ok"


def test_sync(env):
    status, body, _ = call(env, "POST", "/api/sync", bearer(env.admin))
    assert status == 202
    done = wait_op(env, body["op"])
    local = done["local"]
    assert local["status"] == "ok"
    assert len(local["installed"]) >= 1


# --------------------------------------------------------------------------
# op detail
# --------------------------------------------------------------------------
def test_op_get(env):
    feat = add_feature(env, remote_touched=["owui/code_mode.py"], status="ready")
    status, body, _ = call(env, "POST", "/api/features/%d/deploy" % feat["n"],
                           bearer(env.admin))
    op_id = body["op"]
    done = wait_op(env, op_id)
    local_id = done["local"]["id"]
    # /api/ops/<manager_id> returns the manager op + log + local correlation.
    # It is an /api/* route and requires an admin (the manager op endpoint is
    # itself Bearer-gated, so it is not public here).
    status, body, _ = call(env, "GET", "/api/ops/%s" % op_id, bearer(env.admin))
    assert status == 200
    assert body["op"]["id"] == op_id
    assert body["log"]
    assert body["local"]["id"] == local_id
    assert body["local_log"]


def test_op_get_requires_admin(env):
    # No bearer -> 401. The single-op read is not a public endpoint.
    status, body, _ = call(env, "GET", "/api/ops/20261006-dead-beef")
    assert status == 401
    assert body == {"error": "sign in"}


# --------------------------------------------------------------------------
# drift
# --------------------------------------------------------------------------
def test_drift(env):
    status, body, _ = call(env, "GET", "/api/state", bearer(env.admin))
    drift = body["drift"]
    assert len(drift) >= 1
    for d in drift:
        assert {"component", "to", "expected", "actual"}.issubset(d.keys())


# --------------------------------------------------------------------------
# CLI: restore and sync, with JWT verification
# --------------------------------------------------------------------------
def test_restore_command(env):
    op_dir = env.state / "ops" / "op-restore" / "before"
    (op_dir / "owui").mkdir(parents=True)
    with open(os.path.join(op_dir, "owui/code_mode.py"), "w") as fh:
        fh.write("// restored old version\n")
    # Point the config's secret key at a known one so we can verify the JWT.
    key_path = env.state / "secret_key"
    with open(key_path, "w") as fh:
        fh.write("cli-test-key\n")
    env.cfg.secret_key_file = str(key_path)
    env.ow.key_file = str(key_path)

    rc = code_deploy.cmd_restore(env.cfg, "op-restore")
    assert rc == 0
    updates = [u for u in env.ow.update_calls
               if u["path"] == "/api/v1/functions/id/code_mode/update"]
    assert len(updates) == 1
    u = updates[0]
    assert u["body"]["content"] == "// restored old version\n"
    # The Bearer is a JWT signed with the key and verifies with that key.
    auth = u["auth"]
    assert auth.startswith("Bearer ")
    token = auth[len("Bearer "):]
    payload = fakes.verify_jwt(token, b"cli-test-key")
    assert payload is not None
    assert payload["id"] == env.ow.admin_id
    assert payload["exp"] > fakes._now()


def test_restore_command_missing_dir(env):
    rc = code_deploy.cmd_restore(env.cfg, "no-such-op")
    assert rc == 1


def test_sync_command(env):
    key_path = env.state / "secret_key"
    with open(key_path, "w") as fh:
        fh.write("cli-sync-key\n")
    env.cfg.secret_key_file = str(key_path)
    env.ow.key_file = str(key_path)

    rc = code_deploy.cmd_sync(env.cfg)
    assert rc == 0
    func_updates = [u for u in env.ow.update_calls
                    if u["path"] == "/api/v1/functions/id/code_mode/update"]
    assert len(func_updates) == 1
    u = func_updates[0]
    assert u["body"]["content"] == env.mgr.files["owui/code_mode.py"]
    token = u["auth"][len("Bearer "):]
    payload = fakes.verify_jwt(token, b"cli-sync-key")
    assert payload is not None


# --------------------------------------------------------------------------
# no secrets in the service log
# --------------------------------------------------------------------------
def test_no_secrets_in_log(env):
    feat = add_feature(env, remote_touched=["owui/code_mode.py"], status="ready")
    key_content = open(env.key_file).read()
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        status, body, _ = call(env, "POST", "/api/features/%d/deploy" % feat["n"],
                               bearer(env.admin))
        wait_op(env, body["op"], timeout=15)
    log = buf.getvalue()
    assert env.admin not in log
    assert env.user not in log
    assert env.mgr_token not in log
    assert key_content.strip() not in log


def test_local_drift_reads_installed_json_as_written(env):
    # installed.json holds {sha256, rev, op, at} per file (what _finalize_installed writes); the drift check must look
    # up where each file lives in TARGETS. It used to read installed["kind"] and /api/state answered 500 after the
    # first real install.
    info = code_deploy.read_function(env.cfg, "code_mode")
    cur = code_deploy._sha256_bytes(info["content"].encode("utf-8")) if info else None
    same = {"owui/code_mode.py": {"sha256": cur, "rev": "r", "op": "o", "at": 1}}
    assert code_deploy.compute_local_drift(env.cfg, same) == []
    other = {"owui/code_mode.py": {"sha256": "0" * 64, "rev": "r", "op": "o", "at": 1}}
    drift = code_deploy.compute_local_drift(env.cfg, other)
    assert len(drift) == 1 and drift[0]["to"] == "function code_mode" and drift[0]["actual"] == cur


def test_checklist_proxy(env):
    status, body, _ = call(env, "GET", "/api/checklist/chat-check", bearer(env.admin))
    assert status == 200 and body["exists"] and (body["done"], body["total"], body["now"]) == (1, 2, 1)
    status, body, _ = call(env, "GET", "/api/checklist/chat-none", bearer(env.admin))
    assert status == 200 and body["exists"] is False
    status, _, _ = call(env, "GET", "/api/checklist/chat-down", bearer(env.admin))
    assert status == 502
    status, _, _ = call(env, "GET", "/api/checklist/a.b", bearer(env.admin))
    assert status == 404
    status, _, _ = call(env, "GET", "/api/checklist/chat-check")
    assert status == 401


def test_loader_stub_loads_the_current_version_and_falls_back(tmp_path):
    """The stub, run in node with a fake page: it asks for the version without the cache and loads that version;
    when the version cannot be read it still loads the page script (with a time-based tag)."""
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    harness = """
const stub = require("fs").readFileSync(process.argv[2], "utf8");
async function run(fetchImpl) {
  const added = [];
  const doc = { head: { appendChild: (s) => added.push(s.src) }, createElement: () => ({}) };
  const calls = [];
  const fetch = (url, init) => { calls.push([url, init && init.cache]); return fetchImpl(url); };
  new Function("document", "fetch", stub)(doc, fetch);
  await new Promise((r) => setTimeout(r, 50));
  return { added, calls };
}
(async () => {
  const ok = await run(async () => ({ ok: true, text: async () => "abc123def456\\n" }));
  const down = await run(async () => { throw new Error("down"); });
  const missing = await run(async () => ({ ok: false, text: async () => "" }));
  console.log(JSON.stringify({ ok, down, missing }));
})();
"""
    stub = tmp_path / "loader.js"
    stub.write_text(code_deploy.LOADER_STUB)
    h = tmp_path / "h.js"
    h.write_text(harness)
    out = json.loads(subprocess.run([node, str(h), str(stub)], capture_output=True, text=True, check=True).stdout)
    assert out["ok"]["calls"] == [["/static/code-toggle.version", "no-store"]]
    assert out["ok"]["added"] == ["/static/code-toggle.js?v=abc123def456"]
    for case in ("down", "missing"):
        assert len(out[case]["added"]) == 1 and out[case]["added"][0].startswith("/static/code-toggle.js?v="), case


def test_drift_follows_the_installed_record_written_after_start(env):
    """The page script installed and recorded after the server started (a deploy, a sync, a hand install) is not
    reported as drifted: the state reads the record on disk each time (2026-10-19: a stale copy flagged loader.js)."""
    content = "(function(){ /* v2 */ })();\n"
    ok, err = code_deploy._install_loader(env.cfg, content, lambda m: None)
    assert ok, err
    installed = code_deploy.load_installed(env.cfg)
    installed["owui/code-toggle.js"] = {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                                        "rev": "x", "op": "hand", "at": 1}
    code_deploy.save_installed(env.cfg, installed)
    status, body, _ = call(env, "GET", "/api/state", bearer(env.admin))
    assert status == 200
    assert body["installed"]["owui/code-toggle.js"]["op"] == "hand", "the state still shows the record from start-up"
    assert not [d for d in body["drift"] if d.get("to") == "loader.js" and d.get("component") != "code-features"], body["drift"]
