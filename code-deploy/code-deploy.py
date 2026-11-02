#!/usr/bin/env python3
"""code-deploy: the deployer for the Features panel, running on cryoai.

The VM is a sandbox where models run code. Only a click by the signed-in Open
WebUI admin may install code into Open WebUI, and this service holds no Open
WebUI credential of its own: it uses the clicking user's own token for the
Open WebUI calls. It sits between the Features page and two services:

  * the manager (code-features, `code-deploy`'s sibling): feature registry and
    the deploy / rollback / discard / sync operations;
  * Open WebUI itself: verify the clicking user is an admin, then install the
    Open WebUI-side files an operation changed (the code pipe, the chat tool,
    and the page script).

The page is served at /features/. Every /api/* route requires an Open WebUI
admin (Bearer token or the cd_token cookie, with X-Code-Deploy: 1 on POSTs).
The service never acts on its own and never writes a user's token to disk.

Standard library only.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# --------------------------------------------------------------------------
# Paths / defaults
# --------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV_FILE = os.path.expanduser("~/.config/code-deploy/env")

# Remote file (in code-mode) -> what it becomes on Open WebUI.
TARGETS = {
    "owui/code_mode.py": {
        "target": "function code_mode",
        "kind": "function",
        "id": "code_mode",
    },
    "owui/desktop_code_tool.py": {
        "target": "tool desktop_code",
        "kind": "tool",
        "id": "desktop_code",
    },
    "owui/code-toggle.js": {
        "target": "loader.js",
        "kind": "loader",
    },
}

JWT_TTL = 300           # seconds; the CLI-made admin token lives this long
AUTH_TTL = 60           # seconds; how long a verify() answer is cached
OP_TIMEOUT = 30 * 60    # seconds; give up following a manager op after 30 min
SESSION_MAX_AGE = 43200  # 12 h; the cd_token cookie

CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The Open WebUI endpoints that install a function/tool file's content.
FUNCTION_UPDATE = "/api/v1/functions/id/%s/update"
TOOL_UPDATE = "/api/v1/tools/id/%s/update"


def _now():
    return int(time.time())


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _atomic_write(path, data, mode=None):
    if isinstance(data, str):
        data = data.encode("utf-8")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _sha256_file(path):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _read_env_file(path):
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _split_origins(raw):
    return [o.strip() for o in (raw or "").split(",") if o.strip()]


def _cookie(headers, name):
    m = re.search("(?:^|; )%s=([^;]*)" % re.escape(name), headers.get("Cookie", ""))
    return m.group(1) if m else None


class Config:
    """All configuration, from the env file then environment variables."""

    def __init__(self):
        env = _read_env_file(DEFAULT_ENV_FILE)

        def get(key, default=None):
            return env.get(key, os.environ.get(key, default))

        def opt(key, default=None):  # the environment overrides the file
            v = os.environ.get(key)
            if v is None:
                v = env.get(key)
            return v if v is not None else default

        self.features_url = opt("FEATURES_URL", "http://ai-sandbox:4100").rstrip("/")
        self.features_token = opt("FEATURES_TOKEN", "")
        self.owui_url = opt("OWUI_URL", "http://127.0.0.1:8080").rstrip("/")
        owui_db = opt("OWUI_DB", os.path.expanduser("~/.open-webui/webui.db"))
        self.owui_db = owui_db
        secret_file = opt("WEBUI_SECRET_KEY_FILE", os.path.expanduser("~/.webui_secret_key"))
        self.secret_key_file = secret_file
        self.port = int(opt("DEPLOY_PORT", "8095"))
        state = opt("DEPLOY_STATE", os.path.expanduser("~/.local/state/code-deploy"))
        self.state_dir = os.path.expanduser(state)
        self.allowed_origins = _split_origins(
            opt("ALLOWED_ORIGINS", "http://cryoai:8080,http://cryoai:8080"))
        targets = opt("LOADER_TARGETS", ",".join([
            os.path.expanduser("~/owui-code-toggle/loader.js"),
            os.path.expanduser("~/.venvs/openwebui/lib/python3.12/site-packages/open_webui/static/loader.js"),
            os.path.expanduser("~/.venvs/openwebui/lib/python3.12/site-packages/open_webui/frontend/static/loader.js")]))
        self.loader_targets = [t.strip() for t in targets.split(",") if t.strip()]
        self.node = opt("NODE", "/usr/bin/node")
        # Command-line only; never required for `serve`.
        self.db_for_cli = opt("OWUI_DB_CLI") or owui_db


def load_config():
    return Config()


# --------------------------------------------------------------------------
# Small HTTP clients (used both by the service and by the CLI commands)
# --------------------------------------------------------------------------
def http_json(method, url, headers=None, body=None, timeout=30):
    """One HTTP call. Returns (status, json_or_None, body_text, ms). Logs
    nothing about secrets; the caller records method/path/status/ms where it
    matters. Every return path yields a 4-tuple so callers can unpack it safely
    even on a connection error."""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
    except (urllib.error.URLError, OSError):
        # Connection refused/reset/timeout, DNS failure, etc. Return a
        # consistent 4-tuple (ms = 0.0) so callers can treat this as
        # "no response" and retry rather than crashing a background
        # polling thread, and so "%.0f" formatting never sees None.
        return None, None, None, 0.0
    ms = (time.time() - t0) * 1000.0
    text = raw.decode("utf-8", "replace")
    obj = None
    if text:
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
    return status, obj, text, ms


# --------------------------------------------------------------------------
# Open WebUI database access (read-only) and JWT admin token signing
# --------------------------------------------------------------------------
def _owui_db_read(config, sql, args=()):
    """Open a read-only connection and return the first row, or None."""
    db = config.owui_db
    if not db or not os.path.exists(db):
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        cur = conn.execute(sql, args)
        row = cur.fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    return row


def read_function(config, fid="code_mode"):
    row = _owui_db_read(config, "select name, content from function where id=?", (fid,))
    return {"name": row[0], "content": row[1]} if row else None


def read_tool(config, fid="desktop_code"):
    row = _owui_db_read(config, "select name, content from tool where id=?", (fid,))
    return {"name": row[0], "content": row[1]} if row else None


def read_frontmatter(content):
    """Pull `key: value` lines out of the leading triple-quote docstring."""
    if content is None:
        return {}
    m = re.match(r'\s*"""\s*\n?(.*?)(?<=\n)"""', content, re.S)
    body = m.group(1) if m else ""
    fields = {}
    for line in body.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def sign_admin_token(config, admin_id, ttl=JWT_TTL):
    """An HS256 JWT signed in-process with the secret key file's content."""
    try:
        with open(config.secret_key_file) as fh:
            key = fh.read().strip().encode("utf-8")
    except OSError:
        key = b""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"id": str(admin_id), "exp": _now() + ttl}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")
    sig = hmac.new(key, signing_input, hashlib.sha256).digest()
    return signing_input.decode("ascii") + "." + _b64url(sig)


def find_admin_id(config):
    row = _owui_db_read(
        config, "select id from user where role='admin' order by created_at limit 1")
    return row[0] if row else None


# --------------------------------------------------------------------------
# Manager client (proxy GET/POST to code-features)
# --------------------------------------------------------------------------
class Manager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.features_url

    def headers(self):
        return {"Authorization": "Bearer " + self.cfg.features_token,
                "Content-Type": "application/json"}

    def get(self, path):
        return http_json("GET", self.base + path, headers=self.headers())

    def post(self, path, body=None):
        return http_json("POST", self.base + path, headers=self.headers(), body=body)


# --------------------------------------------------------------------------
# Open WebUI installers (use the clicking user's admin token)
# --------------------------------------------------------------------------
def _install_function(config, token, fid, content, logcb):
    info = read_function(config, fid)
    if info is None:
        return False, "Open WebUI has no %s function to update" % fid
    desc = read_frontmatter(content).get("description", "")
    url = config.owui_url + (FUNCTION_UPDATE % fid)
    body = {"id": fid, "name": info["name"], "content": content,
            "meta": {"description": desc, "manifest": {}}}
    status, obj, text, ms = http_json("POST", url,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        body=body, timeout=60)
    logcb("HTTP POST %s -> %d (%.0fms)" % (url, status or 0, ms))
    if status == 200:
        return True, None
    detail = (obj or {}).get("detail") if isinstance(obj, dict) else None
    return False, (detail or ("Open WebUI update failed (%s)" % status))


def _install_tool(config, token, fid, content, logcb):
    info = read_tool(config, fid)
    if info is None:
        return False, "Open WebUI has no %s tool to update" % fid
    desc = read_frontmatter(content).get("description", "")
    url = config.owui_url + (TOOL_UPDATE % fid)
    body = {"id": fid, "name": info["name"], "content": content,
            "meta": {"description": desc, "manifest": {}}}
    status, obj, text, ms = http_json("POST", url,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        body=body, timeout=60)
    logcb("HTTP POST %s -> %d (%.0fms)" % (url, status or 0, ms))
    if status == 200:
        return True, None
    detail = (obj or {}).get("detail") if isinstance(obj, dict) else None
    return False, (detail or ("Open WebUI update failed (%s)" % status))


# Open WebUI loads /static/loader.js with no cache headers, so a browser kept running an old page script after a
# deploy, for hours (2026-10-18: cleared cookies + reload still ran the old one). loader.js is now this fixed stub,
# which never changes and so is harmless to cache: it asks for the current version (never cached) and loads the
# page script under it, code-toggle.js?v=<version>, which is cached per version. The page script sits next to each
# loader target; Open WebUI's start copies frontend/static into static/, and the unit's ExecStartPre puts our files
# into frontend/static first.
PAGE_SCRIPT_NAME = "code-toggle.js"
PAGE_VERSION_NAME = "code-toggle.version"
LOADER_STUB = """/* Code mode: loads the current page script by its version, so a browser never runs a cached old one.
 * This file never changes (code-deploy writes code-toggle.js and code-toggle.version next to it). */
(function () {
  function load(v) {
    var s = document.createElement("script");
    s.src = "/static/code-toggle.js?v=" + encodeURIComponent(v);
    (document.head || document.documentElement).appendChild(s);
  }
  fetch("/static/code-toggle.version", { cache: "no-store" })
    .then(function (r) { return r.ok ? r.text() : ""; })
    .then(function (v) { load((v || "").trim() || String(Math.floor(Date.now() / 60000))); },
          function () { load(String(Math.floor(Date.now() / 60000))); });
})();
"""


def _page_script_path(target):
    return os.path.join(os.path.dirname(target) or ".", PAGE_SCRIPT_NAME)


def _installed_page_script(config):
    """The page script as installed: code-toggle.js beside the first loader target (loader.js itself on a box
    installed before the stub)."""
    first = config.loader_targets[0] if config.loader_targets else None
    if not first:
        return None
    real = _page_script_path(first)
    return real if os.path.exists(real) else (first if os.path.exists(first) else None)


def _install_loader(config, content, logcb):
    """Write the page script, its version and the fixed loader stub next to every loader target atomically, after
    `NODE --check` passes on the temp file. Returns (ok, message)."""
    node = config.node
    targets = config.loader_targets
    if not targets:
        return False, "no loader targets configured"
    # Validate against NODE once, on a temp file in the first target's dir.
    d = os.path.dirname(targets[0]) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".loader-check-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content.encode("utf-8") if isinstance(content, str) else content)
        if node:
            p = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            if p.returncode != 0:
                logcb("NODE --check failed on the page script")
                return False, "page script failed %s --check" % node
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    data = content.encode("utf-8") if isinstance(content, str) else content
    version = _sha256_bytes(data)[:12]
    for t in targets:
        _atomic_write(_page_script_path(t), content, mode=0o644)
        _atomic_write(os.path.join(os.path.dirname(t) or ".", PAGE_VERSION_NAME), version + "\n", mode=0o644)
        _atomic_write(t, LOADER_STUB, mode=0o644)   # last: the stub only ever points at files already in place
        logcb("installed page script %s -> %s" % (version, _page_script_path(t)))
    return True, None


# --------------------------------------------------------------------------
# Installed-files record + drift
# --------------------------------------------------------------------------
def _installed_path(cfg):
    return os.path.join(cfg.state_dir, "installed.json")


def load_installed(cfg):
    p = _installed_path(cfg)
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_installed(cfg, installed):
    _atomic_write(_installed_path(cfg), json.dumps(installed, indent=2) + "\n")


def current_sha(config, remote_path, installed_meta):
    """The current sha256 of an installed remote file: database content for the
    function/tool, the first loader target for the page script."""
    target = TARGETS.get(remote_path)  # installed.json records shas; where a file goes is in TARGETS
    if target is None:
        return None
    kind = target["kind"]
    if kind == "function":
        info = read_function(config, target["id"])
        return _sha256_bytes(info["content"].encode("utf-8")) if info else None
    if kind == "tool":
        info = read_tool(config, target["id"])
        return _sha256_bytes(info["content"].encode("utf-8")) if info else None
    if kind == "loader":
        path = _installed_page_script(config)
        return _sha256_file(path) if path else None
    return None


def compute_local_drift(config, installed):
    drift = []
    for path, meta in installed.items():
        expected = meta.get("sha256")
        cur = current_sha(config, path, meta)
        if cur != expected:
            drift.append({
                "component": "code-mode",
                "to": TARGETS.get(path, {}).get("target", path),
                "expected": expected,
                "actual": cur,
            })
    return drift


# --------------------------------------------------------------------------
# Local operation runner (background worker, one at a time)
# --------------------------------------------------------------------------
class LocalOp:
    """The local side of a manager operation: poll the manager, then install the
    changed Open WebUI-side files with the clicking user's token."""

    def __init__(self, cfg, manager):
        self.cfg = cfg
        self.manager = manager
        self.lock = threading.Lock()
        self.ops = {}          # local_id -> op record (in memory)
        self.running = False
        self.queue = __import__("queue").Queue()
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def local_dir(self, op_id):
        return os.path.join(self.cfg.state_dir, "ops", op_id)

    def op_json_path(self, op_id):
        return os.path.join(self.local_dir(op_id), "op.json")

    def log_path(self, op_id):
        return os.path.join(self.local_dir(op_id), "log.txt")

    def write_json(self, op):
        _atomic_write(self.op_json_path(op["id"]), json.dumps(op, indent=2) + "\n")

    def log(self, op_id, msg):
        d = self.local_dir(op_id)
        os.makedirs(d, exist_ok=True)
        with open(self.log_path(op_id), "a") as fh:
            fh.write("[%s] %s\n" % (_ts(), msg))

    def _loop(self):
        while True:
            task = self.queue.get()
            if task is None:
                return
            self._follow(task)

    def _begin(self, op_id, manager_op_id, feature, kind, user):
        rec = {
            "id": op_id,
            "kind": kind,
            "feature": feature,
            "user": {"name": user.get("name"), "email": user.get("email")} if user else None,
            "status": "running",
            "error": None,
            "started": _now(),
            "ended": None,
            "manager": manager_op_id,
            "manager_status": "running",
            "installed": [],
            "restored": [],
            "auto_rollback": None,
            "reload_page": False,
        }
        self.ops[op_id] = rec
        self.write_json(rec)
        self.log(op_id, "local op started: %s feature #%s (manager %s)"
                 % (kind, feature, manager_op_id))
        return rec

    def start(self, manager_op_id, feature, kind, user, token):
        op_id = "cd-%d-%04x" % (_now(), int.from_bytes(os.urandom(2), "little") & 0xffff)
        self._begin(op_id, manager_op_id, feature, kind, user)
        self.queue.put({
            "op_id": op_id,
            "manager_op_id": manager_op_id,
            "feature": feature,
            "kind": kind,
            "user": user,
            "token": token,
        })
        return op_id

    def _follow(self, task):
        op_id, manager_op_id = task["op_id"], task["manager_op_id"]
        op = self.ops[op_id]
        # 1. Poll the manager op until it stops running.
        last = {"status": "running", "remote": {"changed": []}, "error": None}
        deadline = time.time() + OP_TIMEOUT
        while time.time() < deadline:
            status, obj, text, ms = self.manager.get("/api/ops/%s" % manager_op_id)
            self.log(op_id, "HTTP GET /api/ops/%s -> %d (%.0fms)"
                     % (manager_op_id, status or 0, ms))
            if status == 200 and isinstance(obj, dict) and isinstance(obj.get("op"), dict):
                m = obj["op"]
                last["status"] = m.get("status", "running")
                last["remote"] = m.get("remote") or {"changed": []}
                last["error"] = m.get("error")
                op["manager_status"] = m.get("status")
                self.write_json(op)
                if m.get("status") != "running":
                    break
            time.sleep(1)
        else:
            self._fail(op, "gave up after 30 min waiting for the manager op")
            return

        if last["status"] != "ok":
            self._fail(op, "the manager op %s failed: %s"
                       % (manager_op_id, last.get("error") or "status %s" % last["status"]))
            return

        changed = last["remote"].get("changed") or []
        rev = last["remote"].get("rev")

        # 2. Nothing on the Open WebUI side changed: the op is done.
        if not changed:
            op["ended"] = _now()
            op["status"] = "ok"
            self.write_json(op)
            self.log(op_id, "no Open WebUI-side files changed; done")
            return

        # 3. Fetch + install each changed file, keeping before/ copies.
        installed = []
        before_dir = os.path.join(self.local_dir(op_id), "before")
        after_dir = os.path.join(self.local_dir(op_id), "after")
        os.makedirs(before_dir, exist_ok=True)
        os.makedirs(after_dir, exist_ok=True)
        ok = True
        err = None
        for path in changed:
            meta = TARGETS.get(path)
            if meta is None:
                self.log(op_id, "skipped %s: no install target" % path)
                continue
            content, ferr = self._fetch_and_save(op_id, manager_op_id, rev, path,
                                                 before_dir, after_dir)
            if ferr:
                ok = False
                err = "could not fetch %s from the manager (%s)" % (path, ferr)
                break
            # Install using the user's token.
            ins_ok, ierr = self._do_install(op_id, meta, content, task["token"])
            if not ins_ok:
                ok = False
                err = "Open WebUI rejected %s: %s" % (path, ierr)
                break
            installed.append({
                "path": path,
                "target": meta["target"],
                "sha_before": self._sha_in(before_dir, path),
                "sha_after": _sha256_bytes(
                    content.encode("utf-8") if isinstance(content, str) else content),
            })
            self.log(op_id, "installed %s -> %s" % (path, meta["target"]))
        if ok:
            self._finalize_installed(op, op_id, rev, installed, before_dir)
        else:
            self._handle_failure(op, op_id, installed, before_dir, task, err)

    def _sha_in(self, d, path):
        p = os.path.join(d, path)
        return _sha256_file(p) if os.path.exists(p) else None

    def _fetch_and_save(self, op_id, manager_op_id, rev, path, before_dir, after_dir):
        """Fetch the new content, write it to after/<path>, and write the
        currently-installed version to before/<path>."""
        url = "/api/files/code-mode/%s/%s" % (rev, path.lstrip("/"))
        status, obj, text, ms = self.manager.get(url)
        self.log(op_id, "HTTP GET %s -> %d (%.0fms)" % (url, status or 0, ms))
        if status != 200 or text is None:
            return None, str(status)
        # The currently-installed version, read from Open WebUI's own state.
        meta = TARGETS[path]
        prev = self._current_content(meta)
        if prev is not None:
            _atomic_write(os.path.join(before_dir, path), prev)
        _atomic_write(os.path.join(after_dir, path), text)
        return text, None

    def _current_content(self, meta):
        """The currently-installed content of a remote file: database content
        for the function/tool, the first loader target for the page script."""
        if meta["kind"] == "function":
            info = read_function(self.cfg, meta["id"])
            return info["content"] if info else None
        if meta["kind"] == "tool":
            info = read_tool(self.cfg, meta["id"])
            return info["content"] if info else None
        if meta["kind"] == "loader":
            path = _installed_page_script(self.cfg)
            if path:
                with open(path, "rb") as fh:
                    return fh.read().decode("utf-8", "replace")
        return None

    def _do_install(self, op_id, meta, content, token):
        if meta["kind"] == "function":
            return _install_function(self.cfg, token, meta["id"], content,
                                     lambda m: self.log(op_id, m))
        if meta["kind"] == "tool":
            return _install_tool(self.cfg, token, meta["id"], content,
                                 lambda m: self.log(op_id, m))
        if meta["kind"] == "loader":
            return _install_loader(self.cfg, content, lambda m: self.log(op_id, m))
        return False, "unknown install target"

    def _finalize_installed(self, op, op_id, rev, installed, before_dir):
        # 5. Record installed files in installed.json.
        installed_json = load_installed(self.cfg)
        for item in installed:
            installed_json[item["path"]] = {
                "sha256": item["sha_after"],
                "rev": rev,
                "op": op_id,
                "at": _now(),
            }
        save_installed(self.cfg, installed_json)
        # 6. The page script changed -> ask the user to reload.
        for item in installed:
            if item["path"] == "owui/code-toggle.js":
                op["reload_page"] = True
        op["installed"] = installed
        op["ended"] = _now()
        op["status"] = "ok"
        self.write_json(op)
        self.log(op_id, "installed %d file(s); done" % len(installed))

    def _handle_failure(self, op, op_id, installed, before_dir, task, err):
        # 4. Put back every file this op already installed (reverse order).
        restored = []
        for item in reversed(installed):
            path = item["path"]
            bp = os.path.join(before_dir, path)
            if not os.path.exists(bp):
                continue
            with open(bp, "rb") as fh:
                prev = fh.read().decode("utf-8")
            ins_ok, ierr = self._do_install(op_id, TARGETS[path], prev, task["token"])
            if ins_ok:
                restored.append({"path": path, "target": TARGETS[path]["target"]})
                self.log(op_id, "restored %s" % path)
            else:
                self.log(op_id, "restore of %s failed: %s" % (path, ierr))
        op["restored"] = restored
        op["ended"] = _now()
        op["status"] = "failed"

        if task["kind"] == "deploy":
            # For a deploy, also roll the feature back on the manager.
            status, obj, text, ms = self.manager.post(
                "/api/features/%s/rollback" % task["feature"])
            self.log(op_id, "HTTP POST /api/features/%s/rollback -> %d (%.0fms)"
                     % (task["feature"], status or 0, ms))
            if status in (200, 202) and isinstance(obj, dict):
                rb = obj.get("op") or obj.get("op_id")
                op["auto_rollback"] = rb
                op["error"] = ("%s. Put the previous version back and rolled "
                               "feature #%s back." % (err, task["feature"]))
            else:
                op["error"] = ("%s. Put the previous version back, but the manager "
                               "rollback (%s) also needs attention."
                               % (err, status))
        else:
            op["error"] = ("%s. The installed files were restored, but the "
                           "operation needs attention." % err)
        self.write_json(op)

    def _fail(self, op, msg):
        op["error"] = msg
        op["ended"] = _now()
        op["status"] = "failed"
        self.write_json(op)
        self.log(op["id"], "failed: %s" % msg)


# --------------------------------------------------------------------------
# Compose: type a message into a chat from outside (2026-10-09)
# --------------------------------------------------------------------------
class ComposeQueue:
    """Messages to type into a chat's message box in the user's own browser. The user asked for this so they can
    watch a message land in their "Send a message" box and go out, the way start_coding types "Let's build it.".
    An admin queues one (POST /api/compose); the page script open on that chat long-polls GET /api/compose, claims
    it, types it, sends it and reports back (POST /api/compose/<id>). Memory only: a restart drops the queue."""

    EXPIRE_S = 15 * 60      # an unclaimed message is dropped after this
    KEEP = 50               # finished messages kept for status checks

    def __init__(self):
        self.cv = threading.Condition()
        self.items = []

    def _expire(self):
        now = time.time()
        for it in self.items:
            if it["status"] == "queued" and now - it["at"] > self.EXPIRE_S:
                it["status"], it["note"] = "expired", "no page claimed it"
        done = [it for it in self.items if it["status"] not in ("queued", "claimed")]
        for it in done[:-self.KEEP]:
            self.items.remove(it)

    @staticmethod
    def public(it):
        return {k: it.get(k) for k in ("id", "chat_id", "text", "send", "mode", "status", "note", "at",
                                       "claimed_at", "finished_at")}

    def add(self, chat_id, text, send=True, mode=None):
        it = {"id": _b64url(os.urandom(9)), "chat_id": chat_id, "text": text, "send": bool(send), "mode": mode,
              "status": "queued", "note": "", "at": time.time(), "claimed_at": None, "finished_at": None, "tab": None}
        with self.cv:
            self._expire()
            self.items.append(it)
            self.cv.notify_all()
        return it

    def claim(self, chat_id, tab, wait):
        deadline = time.time() + wait
        with self.cv:
            while True:
                self._expire()
                for it in self.items:
                    if it["status"] == "queued" and it["chat_id"] == chat_id:
                        it["status"], it["claimed_at"], it["tab"] = "claimed", time.time(), tab
                        return it
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.cv.wait(min(left, 5))

    def get(self, item_id):
        with self.cv:
            self._expire()
            return next((it for it in self.items if it["id"] == item_id), None)

    def finish(self, item_id, status, note=""):
        with self.cv:
            it = next((x for x in self.items if x["id"] == item_id), None)
            if it is None:
                return None
            if status == "cancelled" and it["status"] != "queued":
                return it                     # too late: a page already has it
            if status == "requeue":            # the page could not type it yet (the user was typing): offer it again
                it["status"], it["claimed_at"], it["tab"] = "queued", None, None
                self.cv.notify_all()
                return it
            it["status"], it["note"], it["finished_at"] = status, (note or "")[:300], time.time()
            return it


# --------------------------------------------------------------------------
# Queue: messages typed while a reply runs, kept here until they go out (2026-10-30)
# --------------------------------------------------------------------------
QUEUE_EXTRAS = {"code_model": 200, "code_effort": 20, "code_repo": 300}   # string extras and their max length


class MessageQueue:
    """Messages the user queued in a chat while its reply was still running. Open WebUI keeps its own queue only in
    the tab's memory and sends it when THAT tab sees the reply finish, so a tab that slept, crashed or reloaded
    first lost them for good (chat 0c682232, 2026-10-29). The page script moves each one here the moment Open WebUI
    queues it. It goes out once the chat's reply has finished: a tab open on the chat types and sends it the normal
    way, or, for a Code chat with no tab to do it, code-wake on this host sends it. Kept on disk (queue.json).

    An item is queued -> claimed -> sent. Stop holds a chat's queued items (held, like Open WebUI's own queue, which
    Stop does not send) until Send now; Edit / Delete cancel one. A claim takes every queued item of a chat at once,
    joined by blank lines, the way Open WebUI sends its queue. A claim nobody finishes runs out: if the chat has the
    text by then it counts as sent, else it is queued again."""

    CLAIM_TTL = {"tab": 120, "code-wake": 600}
    KEEP_DONE_S = 3 * 24 * 3600      # sent / cancelled / failed items are kept this long for status checks
    HELD_EXPIRE_S = 14 * 24 * 3600   # a held item nobody acts on goes after two weeks
    SHOW_DONE_S = 30 * 60            # a chat's list includes items finished this recently (tabs reload on them)
    MAX_OPEN_PER_CHAT = 50
    MAX_TEXT = 100000
    OPEN = ("queued", "held", "claimed")

    def __init__(self, path, delivered=None):
        """delivered(chat_id, text, since) -> True when the chat has a user message with this text from that
        time on (used when a claim runs out); None = never assume so."""
        self.path = path
        self.delivered = delivered
        self.lock = threading.Lock()
        self.items, self.claims = [], {}
        self.seen = {}               # chat -> {"visible": ts, "any": ts}: the last poll from a tab (memory only)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.items = [it for it in data.get("items") or [] if isinstance(it, dict) and it.get("id")]
            self.claims = {k: v for k, v in (data.get("claims") or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    # ---- storage --------------------------------------------------------
    def _save(self):
        _atomic_write(self.path, json.dumps({"items": self.items, "claims": self.claims}), mode=0o600)

    def _sweep(self):
        """Claims that ran out, old finished items, held items nobody acted on. True when something changed."""
        now, changed = time.time(), False
        for cid, c in list(self.claims.items()):
            if now - c["at"] <= self.CLAIM_TTL.get(c["kind"], 120):
                continue
            mine = [it for it in self.items if it.get("claim") == cid]
            sent = False
            if self.delivered and mine:
                try:
                    sent = bool(self.delivered(c["chat_id"], c["text"], c["at"]))
                except Exception:  # noqa: BLE001 - a failed check only means "not known to be sent"
                    sent = False
            for it in mine:
                if sent:
                    it.update(status="sent", sent_by=c["by"], finished_at=now, note="found in the chat")
                else:
                    it.update(status="queued", note="")
                it["claim"] = None
            del self.claims[cid]
            changed = True
        keep = []
        for it in self.items:
            if it["status"] == "held" and now - it["at"] > self.HELD_EXPIRE_S:
                it.update(status="expired", finished_at=now, note="held for two weeks")
                changed = True
            if it["status"] not in self.OPEN and now - (it.get("finished_at") or it["at"]) > self.KEEP_DONE_S:
                changed = True
                continue
            keep.append(it)
        self.items = keep
        return changed

    @staticmethod
    def public(it):
        return {k: it.get(k) for k in ("id", "chat_id", "text", "mode", "extras", "status", "note", "at",
                                       "finished_at", "sent_by", "message_id", "send_now")}

    @staticmethod
    def clean_extras(raw):
        raw = raw if isinstance(raw, dict) else {}
        out = {k: str(raw[k])[:n] for k, n in QUEUE_EXTRAS.items() if isinstance(raw.get(k), str) and raw[k]}
        if raw.get("code_feature") is True:
            out["code_feature"] = True
        return out

    def _find(self, item_id):
        return next((it for it in self.items if it["id"] == item_id), None)

    # ---- what the page and code-wake call -------------------------------
    def chat_of(self, item_or_claim_id):
        with self.lock:
            it = self._find(item_or_claim_id)
            if it:
                return it["chat_id"]
            c = self.claims.get(item_or_claim_id)
            return c["chat_id"] if c else None

    def add(self, user_id, chat_id, text, mode, extras, lid="", held=False):
        """Returns (item, error). lid is the page's own id for the message, so a retried POST adds it once."""
        text = str(text or "")
        if not text.strip():
            return None, "the message is empty"
        if len(text) > self.MAX_TEXT:
            return None, "the message is too long (%d characters, at most %d)" % (len(text), self.MAX_TEXT)
        with self.lock:
            changed = self._sweep()
            lid = str(lid or "")[:64]
            if lid:
                same = next((it for it in self.items if it["chat_id"] == chat_id and it.get("lid") == lid), None)
                if same:
                    if changed:
                        self._save()
                    return same, None
            if sum(1 for it in self.items if it["chat_id"] == chat_id and it["status"] in self.OPEN) \
                    >= self.MAX_OPEN_PER_CHAT:
                return None, "this chat already has %d messages waiting" % self.MAX_OPEN_PER_CHAT
            it = {"id": "q" + _b64url(os.urandom(9)), "lid": lid, "chat_id": chat_id, "user_id": user_id,
                  "text": text, "mode": mode if mode in ("code", "chat") else "chat",
                  "extras": self.clean_extras(extras), "status": "held" if held else "queued",
                  "note": "", "at": time.time(), "claim": None, "finished_at": None, "sent_by": None,
                  "message_id": None, "send_now": False}
            self.items.append(it)
            self._save()
            return it, None

    def list_chat(self, chat_id, visible=False):
        now = time.time()
        with self.lock:
            seen = self.seen.setdefault(chat_id, {})
            seen["any"] = now
            if visible:
                seen["visible"] = now
            if self._sweep():
                self._save()
            rows = [self.public(it) for it in self.items if it["chat_id"] == chat_id and (
                it["status"] in self.OPEN or now - (it.get("finished_at") or 0) < self.SHOW_DONE_S)]
        return sorted(rows, key=lambda r: r["at"])

    def act(self, item_id, op):
        """cancel (Edit / Delete), send_now, hold, release. Returns (item, error)."""
        with self.lock:
            self._sweep()
            it = self._find(item_id)
            if not it:
                return None, "no such message"
            if op == "cancel":
                if it["status"] == "claimed":
                    return None, "it is being sent right now"
                if it["status"] in ("queued", "held", "failed"):   # a failed one: Delete clears it
                    it.update(status="cancelled", finished_at=time.time())
            elif op in ("send_now", "release"):
                if it["status"] not in ("queued", "held"):
                    return None, "it is not waiting any more"
                it.update(status="queued", note="")
                if op == "send_now":
                    it["send_now"] = True
            elif op == "hold":
                if it["status"] == "queued":
                    it["status"] = "held"
            else:
                return None, "unknown op"
            self._save()
            return it, None

    def hold_chat(self, chat_id):
        """Stop was pressed in the chat: its queued messages wait for Send now (not those just sent with Send now)."""
        with self.lock:
            n = 0
            for it in self.items:
                if it["chat_id"] == chat_id and it["status"] == "queued" and not it.get("send_now"):
                    it["status"], n = "held", n + 1
            if n:
                self._save()
            return n

    def note_chat(self, chat_id, note):
        """code-wake's word on why a chat's queue waits ("" clears it)."""
        with self.lock:
            n = 0
            for it in self.items:
                if it["chat_id"] == chat_id and it["status"] == "queued" and it.get("note") != note:
                    it["note"], n = (note or "")[:200], n + 1
            if n:
                self._save()
            return n

    def claim(self, chat_id, by):
        """Take every queued message of the chat for one send. None when there is none, or someone else is
        sending this chat's queue right now."""
        kind = "code-wake" if by == "code-wake" else "tab"
        with self.lock:
            changed = self._sweep()
            if any(c["chat_id"] == chat_id for c in self.claims.values()):
                if changed:
                    self._save()
                return None
            mine = sorted((it for it in self.items if it["chat_id"] == chat_id and it["status"] == "queued"),
                          key=lambda it: it["at"])
            if not mine:
                if changed:
                    self._save()
                return None
            cid = "c" + _b64url(os.urandom(9))
            text = "\n\n".join(it["text"] for it in mine)
            self.claims[cid] = {"id": cid, "chat_id": chat_id, "by": str(by)[:80], "kind": kind, "at": time.time(),
                                "text": text}
            for it in mine:
                it.update(status="claimed", claim=cid)
            self._save()
            last = mine[-1]
            return {"claim": cid, "text": text, "mode": last["mode"], "extras": last["extras"],
                    "user_id": last["user_id"], "items": [self.public(it) for it in mine]}

    def finish(self, claim_id, status, note="", message_id=None):
        """sent | requeue | failed. Returns (n items, error)."""
        if status not in ("sent", "requeue", "failed"):
            return 0, "unknown status"
        with self.lock:
            c = self.claims.pop(claim_id, None)
            if not c:
                return 0, "no such claim (it ran out, or was finished already)"
            now, n = time.time(), 0
            for it in self.items:
                if it.get("claim") != claim_id:
                    continue
                n += 1
                it["claim"] = None
                if status == "sent":
                    it.update(status="sent", sent_by=c["by"], finished_at=now, note=(note or "")[:200],
                              message_id=str(message_id)[:80] if message_id else None)
                elif status == "requeue":
                    it.update(status="queued", note=(note or "")[:200])
                else:
                    it.update(status="failed", finished_at=now, note=(note or "")[:300])
            self._save()
            return n, None

    def pending(self):
        """Chats with messages ready to go: what code-wake looks at."""
        with self.lock:
            if self._sweep():
                self._save()
            claimed = {c["chat_id"] for c in self.claims.values()}
            out = {}
            for it in sorted(self.items, key=lambda it: it["at"]):
                if it["status"] != "queued" or it["chat_id"] in claimed:
                    continue
                p = out.setdefault(it["chat_id"], {"chat_id": it["chat_id"], "user_id": it["user_id"], "n": 0,
                                                   "oldest_at": it["at"]})
                p["n"] += 1
                p["mode"], p["extras"] = it["mode"], it["extras"]
                seen = self.seen.get(it["chat_id"]) or {}
                p["tab_visible_at"], p["tab_any_at"] = seen.get("visible"), seen.get("any")
            return list(out.values())


def chat_has_user_text(db, chat_id, text, since):
    """True when the Open WebUI chat has a user message with this text, sent at or after `since` (read-only)."""
    if not db or not os.path.exists(db):
        return False
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        row = conn.execute("select chat from chat where id = ?", (chat_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    try:
        msgs = ((json.loads(row[0]) or {}).get("history") or {}).get("messages") or {}
    except ValueError:
        return False
    want = text.strip()
    return any(isinstance(m, dict) and m.get("role") == "user" and (m.get("content") or "").strip() == want
               and float(m.get("timestamp") or 0) >= since - 5 for m in msgs.values())


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------
class Deployer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.manager = Manager(cfg)
        self.installed = load_installed(cfg)
        self.auth_cache = {}   # sha256(token) -> {"user": ..., "exp": <ts>}
        self.cache_lock = threading.Lock()
        self.ops = LocalOp(cfg, self.manager)
        self.compose = ComposeQueue()
        self.queue = MessageQueue(os.path.join(cfg.state_dir, "queue.json"),
                                  delivered=lambda chat, text, since: chat_has_user_text(cfg.owui_db, chat, text, since))

    # ---- auth ------------------------------------------------------------
    def verify_token(self, token):
        """GET /api/v1/auths/ and cache the answer for AUTH_TTL. Returns
        (status_code, user). 200 admin, 403 not admin, 401 bad."""
        if not token:
            return 401, None
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.cache_lock:
            entry = self.auth_cache.get(key)
            if entry and entry["exp"] > time.time():
                # A cache hit is only an admin hit. Returning 200 without
                # checking role would let a non-admin's cached token in as an
                # admin for the rest of the 60 s window.
                if entry["user"].get("role") == "admin":
                    return 200, entry["user"]
                return 403, entry["user"]
        status, obj, text, ms = http_json(
            "GET", self.cfg.owui_url + "/api/v1/auths/",
            headers={"Authorization": "Bearer " + token})
        if status == 200 and isinstance(obj, dict):
            user = {"id": obj.get("id"), "email": obj.get("email"),
                    "name": obj.get("name"), "role": obj.get("role")}
            with self.cache_lock:
                self.auth_cache[key] = {"user": user, "exp": time.time() + AUTH_TTL}
            if user.get("role") == "admin":
                return 200, user
            return 403, user
        return 401, None

    def resolve_admin(self, headers, method):
        """Given request headers and the HTTP method, return (status_code, user)."""
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return self.verify_token(auth[7:])
        cookie_token = _cookie(headers, "cd_token")
        if cookie_token:
            if method == "POST" and headers.get("X-Code-Deploy") != "1":
                return 401, None
            return self.verify_token(cookie_token)
        return 401, None

    def resolve_user(self, headers):
        """Any signed-in Open WebUI user, admin or not (Bearer only): (200, user) or (401, None). The queue routes
        use it; each of them then checks that the chat is the user's own."""
        auth = headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return 401, None
        code, user = self.verify_token(auth[7:])
        if code in (200, 403) and user and user.get("id"):
            return 200, user
        return 401, None

    def chat_owner(self, chat_id):
        row = _owui_db_read(self.cfg, "select user_id from chat where id = ?", (chat_id,))
        return row[0] if row else None

    # ---- data proxies ----------------------------------------------------
    def get_state(self):
        status, obj, text, ms = self.manager.get("/api/features")
        features = (obj or {}).get("features", []) if status == 200 else []
        non_discarded = [f for f in features if f.get("status") != "discarded"]

        ops_status, ops_obj, _, _ = self.manager.get("/api/ops?limit=30")
        manager_ops = (ops_obj or {}).get("ops", []) if ops_status == 200 else []
        now = time.time()
        for m in manager_ops:
            m["local"] = None
        for local in self.ops.ops.values():
            mid = local.get("manager")
            for m in manager_ops:
                if m.get("id") == mid:
                    m["local"] = local
        manager_ops.sort(key=lambda o: o.get("started", 0), reverse=True)

        # busy: a running manager op, else a running local op.
        busy = None
        for m in manager_ops:
            if m.get("status") == "running":
                busy = m.get("id")
                break
        if not busy:
            for local in self.ops.ops.values():
                if local.get("status") == "running":
                    busy = local.get("id")
                    break

        drift = []
        d_status, d_obj, _, _ = self.manager.get("/api/drift")
        if d_status == 200:
            drift.extend((d_obj or {}).get("drift", []))
        # The record on disk, not a copy from start-up: a deploy, a sync or a restore writes it (and so may a hand
        # install), and a stale copy reported the page script as drifted after one (2026-10-19).
        self.installed = load_installed(self.cfg)
        drift.extend(compute_local_drift(self.cfg, self.installed))

        # Until Ornith names a feature, show its chat's title (Open WebUI's database, read-only).
        try:
            ids = [f.get("chat_id") for f in features if not f.get("title")]
            if ids:
                conn = sqlite3.connect("file:%s?mode=ro" % self.cfg.owui_db, uri=True)
                try:
                    rows = conn.execute("select id, title from chat where id in (%s)" % ",".join("?" * len(ids)),
                                        ids).fetchall()
                finally:
                    conn.close()
                titles = {r[0]: r[1] for r in rows}
                for f in features:
                    if not f.get("title") and titles.get(f.get("chat_id")):
                        f["chat_title"] = titles[f["chat_id"]]
        except Exception:  # noqa - a title is nice to have, never worth failing the page
            pass

        user = {"name": None, "email": None}
        return {
            "user": user,
            "features": features,  # discarded too: the page shows them behind a toggle
            "ops": manager_ops,
            "busy": busy,
            "drift": drift,
            "installed": self.installed,
        }

    def get_chats(self):
        status, obj, _, _ = self.manager.get("/api/features")
        features = (obj or {}).get("features", []) if status == 200 else []
        chats = {}
        for f in features:
            if f.get("status") == "discarded":
                continue
            chats[f.get("chat_id")] = {
                "n": f.get("n"),
                "title": f.get("title") or "",
                "status": f.get("status"),
                "update_available": f.get("update_available", False),
            }
        return chats

    def get_op(self, op_id):
        status, obj, text, ms = self.manager.get("/api/ops/%s" % op_id)
        if status != 200 or not isinstance(obj, dict):
            return 404, {"error": "No such op"}
        result = {"op": obj.get("op", {}), "log": obj.get("log", ""), "local": None,
                  "local_log": ""}
        for local in self.ops.ops.values():
            if local.get("manager") == op_id:
                result["local"] = local
                lp = self.ops.log_path(op_id)
                if not os.path.exists(lp):
                    lp = self.ops.log_path(local["id"])
                if os.path.exists(lp):
                    with open(lp) as fh:
                        result["local_log"] = fh.read()
                break
        return 200, result

    def proxy_feature(self, n, suffix="", what=None):
        path = "/api/features/%s" % n
        if suffix:
            path += suffix
            if what:
                path += "?what=" + what
        status, obj, text, ms = self.manager.get(path)
        return status, obj

    def propose(self, n, kind, token, user):
        path = "/api/features/%s/%s" % (n, kind)
        status, obj, text, ms = self.manager.post(path)
        if status not in (200, 202):
            return status, obj
        op = (obj or {}).get("op") or (obj or {}).get("op_id")
        if not op:
            return status, obj
        if token and user:
            self.ops.start(op, n, kind, user, token)
        return status, obj

    def propose_sync(self, token, user):
        """POST /api/sync: install every file the manager's sync op lists."""
        status, obj, _, _ = self.manager.post("/api/sync")
        if status not in (200, 202):
            return status, obj
        op = (obj or {}).get("op")
        if op and token and user:
            self.ops.start(op, None, "sync", user, token)
        return status, obj


# --------------------------------------------------------------------------
# HTTP server + routing
# --------------------------------------------------------------------------
PAGE_PATH = os.path.join(SCRIPT_DIR, "page.html")
HARNESS_PATH = os.path.join(SCRIPT_DIR, "harness.html")


def _render_page(path, origins):
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    return (html
            .replace("/*ALLOWED_ORIGINS*/", json.dumps(origins))
            .replace("/*CSP_ORIGINS*/", " ".join(origins) or "none"))


def _page_html(origins):
    return _render_page(PAGE_PATH, origins)


def make_handler(deployer):
    CFG = deployer.cfg
    handler_deployer = deployer

    class Handler(BaseHTTPRequestHandler):
        server_version = "code-deploy/1.0"
        protocol_version = "HTTP/1.1"
        deployer = handler_deployer

        def log_message(self, *a):
            pass

        # ---- helpers -----------------------------------------------------
        def _headers_out(self, csp=False):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if csp:
                origins = " ".join(CFG.allowed_origins) or "none"
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self' 'unsafe-inline'; connect-src 'self'; "
                    "frame-ancestors %s" % origins)

        def _apply_cors(self):
            origin = self.headers.get("Origin")
            if origin and origin in CFG.allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers",
                                 "Authorization, Content-Type, X-Code-Deploy")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")

        def _send(self, code, obj=None, raw=None, ctype="application/json", csp=False):
            if raw is not None:
                body = raw if isinstance(raw, (bytes, bytearray)) else raw.encode()
            elif isinstance(obj, (bytes, bytearray)):
                body = bytes(obj)
            else:
                body = (json.dumps(obj) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self._headers_out(csp=csp)
            self._apply_cors()
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        def _log(self, code, path, ms):
            try:
                sys.stderr.write("[%s] %s %s %d %.1fms\n"
                                 % (time.strftime("%H:%M:%S"), self.command, path, code, ms))
                sys.stderr.flush()
            except OSError:
                pass

        def _body(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}

        def _read_page(self):
            html = _page_html(CFG.allowed_origins)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._headers_out(csp=True)
            self._apply_cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _read_harness(self):
            if os.environ.get("DEPLOY_DEMO") != "1" or not os.path.exists(HARNESS_PATH):
                return self._send(404, {"error": "not found"})
            with open(HARNESS_PATH, encoding="utf-8") as fh:
                body = fh.read().replace("/*ALLOWED_ORIGINS*/",
                                         " ".join(CFG.allowed_origins)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._headers_out(csp=True)
            self._apply_cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        # ---- verbs -------------------------------------------------------
        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def do_HEAD(self):
            self._dispatch()

        def do_OPTIONS(self):
            t0 = time.time()
            origin = self.headers.get("Origin")
            if origin and origin in CFG.allowed_origins:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers",
                                 "Authorization, Content-Type, X-Code-Deploy")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
            else:
                self.send_response(204)
                self._headers_out()
                self.end_headers()
            self._log(204, urlparse(self.path).path, (time.time() - t0) * 1000.0)

        def _dispatch(self):
            t0 = time.time()
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                code, obj, raw, ctype, csp = self._route(parsed)
            except Exception as e:  # noqa
                code, obj, raw, ctype, csp = 500, {"error": str(e)}, None, \
                    "application/json", False
            if not (code == 200 and self.command == "GET" and path in ("/api/queue", "/api/queue/pending")):
                # every tab polls the first, code-wake the second
                self._log(code, path, (time.time() - t0) * 1000.0)
            self._send(code, obj if obj is not None else {}, raw=raw,
                       ctype=ctype, csp=csp)

        def _api_authed(self):
            code, user = self.deployer.resolve_admin(self.headers, self.command)
            if code != 200:
                self._send(code, {"error": "sign in"})
                return None
            return user

        def _route(self, parsed):
            query = parse_qs(parsed.query)
            path = parsed.path
            method = self.command
            parts = [p for p in path.strip("/").split("/") if p != ""]

            if path == "/features/" or path == "/features":
                return 200, None, _page_html(CFG.allowed_origins), "text/html; charset=utf-8", True
            if path == "/harness.html":
                raw = self._harness_raw()
                if raw is None:
                    return 404, {"error": "Not found"}, None, \
                        "application/json", False
                return 200, None, raw, "text/html; charset=utf-8", True
            if path == "/auth/signin" and method == "POST":
                return self._signin()
            if path == "/auth/signout" and method == "POST":
                return self._signout()

            # Queued messages: any signed-in user, for their own chats (the route checks the owner).
            if path == "/api/queue" or path.startswith("/api/queue/"):
                code, quser = self.deployer.resolve_user(self.headers)
                if code != 200:
                    return 401, {"error": "sign in"}, None, "application/json", False
                return self._route_queue(path, query, quser)

            # Everything below is an /api/* route needing an admin.
            if not path.startswith("/api/") or path == "/api":
                return 404, {"error": "Not found"}, None, "application/json", False
            user = self._api_authed()
            if user is None:
                return 401, {"error": "sign in"}, None, "application/json", False

            if path == "/api/compose" or path.startswith("/api/compose/"):
                return self._route_compose(path, query, user)
            if path == "/api/chats":
                return 200, {"chats": self.deployer.get_chats()}, None, \
                    "application/json", False
            if path.startswith("/api/checklist/") and method == "GET":
                # the builder's CHECKLIST.md for one chat, parsed by code-features (the page's Checklist tab)
                chat_id = path[len("/api/checklist/"):]
                if not CHAT_ID_RE.match(chat_id):
                    return 404, {"error": "Not found"}, None, "application/json", False
                status, obj, _, _ = self.deployer.manager.get("/api/checklist/" + chat_id)
                if status != 200 or not isinstance(obj, dict):
                    return 502, {"error": "the feature manager did not answer"}, None, "application/json", False
                return 200, obj, None, "application/json", False
            if path == "/api/state":
                state = self.deployer.get_state()
                state["user"] = {"name": user.get("name"), "email": user.get("email")}
                return 200, state, None, "application/json", False

            if path == "/api/sync" and method == "POST":
                token = self._op_token()
                status, obj = self.deployer.propose_sync(token, self._current_user())
                return (status or 400), (obj or {}), None, \
                    "application/json", False

            if path == "/api/ops" and method == "GET":
                # Proxied but local correlation is done via /api/state; still
                # answer direct queries for convenience.
                status, obj, _, _ = self.deployer.manager.get("/api/ops?limit=30")
                return (status or 404), (obj or {}), None, "application/json", False

            parts = [p for p in path.strip("/").split("/") if p != ""]
            # strip leading "api"
            if parts[:1] == ["api"]:
                parts = parts[1:]
            if parts[:1] == ["features"]:
                return self._route_feature(parts[1:], query)
            if len(parts) == 2 and parts[:1] == ["ops"]:
                return self.deployer.get_op(parts[1]) + (None, "application/json", False)
            return 404, {"error": "Not found"}, None, "application/json", False

        def _route_queue(self, path, query, user):
            q = self.deployer.queue
            method = self.command
            admin = user.get("role") == "admin"

            def out(code, obj):
                return code, obj, None, "application/json", False

            def owner_of(chat_id):
                """(owner id, None) when this user may use the chat, else (None, an error answer)."""
                if not chat_id or not CHAT_ID_RE.match(chat_id):
                    return None, out(400, {"error": "a chat id is needed"})
                owner = self.deployer.chat_owner(chat_id)
                if owner is None:
                    return None, out(404, {"error": "no such chat"})
                if owner != user.get("id") and not admin:
                    return None, out(403, {"error": "not your chat"})
                return owner, None

            body = self._body() if method == "POST" else {}
            if path == "/api/queue" and method == "GET":
                chat_id = (query.get("chat") or [""])[0]
                _, bad = owner_of(chat_id)
                if bad:
                    return bad
                visible = (query.get("visible") or ["0"])[0] == "1"
                return out(200, {"items": q.list_chat(chat_id, visible)})
            if path == "/api/queue" and method == "POST":
                chat_id = str(body.get("chat") or "")
                owner, bad = owner_of(chat_id)
                if bad:
                    return bad
                it, err = q.add(owner, chat_id, body.get("text"), body.get("mode"), body.get("extras"),
                                lid=body.get("lid") or "", held=body.get("held") is True)
                if err:
                    return out(400, {"error": err})
                sys.stderr.write("queue: %s for chat %s (%d chars, %s)\n"
                                 % (it["id"], chat_id, len(it["text"]), it["mode"]))
                return out(200, {"item": q.public(it)})
            if path == "/api/queue/pending" and method == "GET":
                if not admin:
                    return out(403, {"error": "admins only"})
                return out(200, {"chats": q.pending()})
            if method != "POST":
                return out(404, {"error": "Not found"})
            if path in ("/api/queue/hold", "/api/queue/note", "/api/queue/claim"):
                chat_id = str(body.get("chat") or "")
                _, bad = owner_of(chat_id)
                if bad:
                    return bad
                if path == "/api/queue/hold":
                    return out(200, {"held": q.hold_chat(chat_id)})
                if path == "/api/queue/note":
                    if not admin:
                        return out(403, {"error": "admins only"})
                    return out(200, {"changed": q.note_chat(chat_id, str(body.get("note") or ""))})
                by = str(body.get("by") or "")
                if by == "code-wake" and not admin:
                    return out(403, {"error": "admins only"})
                if by != "code-wake" and not by.startswith("tab:"):
                    return out(400, {"error": "by: tab:<id> or code-wake"})
                c = q.claim(chat_id, by)
                if c:
                    sys.stderr.write("queue: chat %s claimed by %s (%d messages)\n" % (chat_id, by, len(c["items"])))
                return out(200, c or {"claim": None})
            rest = path[len("/api/queue/"):]
            is_claim = rest.startswith("claim/")
            ref = rest[len("claim/"):] if is_claim else rest
            chat_id = q.chat_of(ref) if CHAT_ID_RE.match(ref or "") else None
            if not chat_id:
                return out(404, {"error": "no such message" if not is_claim else "no such claim"})
            _, bad = owner_of(chat_id)
            if bad:
                return bad
            if is_claim:
                n, err = q.finish(ref, str(body.get("status") or ""), str(body.get("note") or ""),
                                  body.get("message_id"))
                if err:
                    return out(409, {"error": err})
                sys.stderr.write("queue: claim %s in chat %s -> %s (%d) %s\n"
                                 % (ref, chat_id, body.get("status"), n, body.get("note") or ""))
                return out(200, {"finished": n})
            it, err = q.act(ref, str(body.get("op") or ""))
            if err:
                return out(409, {"error": err})
            return out(200, {"item": q.public(it)})

        def _route_compose(self, path, query, user):
            q = self.deployer.compose
            method = self.command
            ok = lambda obj: (200, obj, None, "application/json", False)       # noqa: E731
            bad = lambda code, msg: (code, {"error": msg}, None, "application/json", False)  # noqa: E731
            if path == "/api/compose" and method == "POST":
                body = self._body()
                chat_id, text, mode = body.get("chat_id") or "", body.get("text"), body.get("mode")
                if not CHAT_ID_RE.match(chat_id):
                    return bad(400, "chat_id")
                if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                    return bad(400, "text (1-20000 characters)")
                if mode not in (None, "code", "chat"):
                    return bad(400, "mode (code, chat or null)")
                it = q.add(chat_id, text, body.get("send", True), mode)
                sys.stderr.write("compose: %s queued for chat %s (%d chars, send=%s) by %s\n"
                                 % (it["id"], chat_id, len(text), it["send"], user.get("email")))
                return ok({"item": q.public(it)})
            if path == "/api/compose" and method == "GET":
                chat_id = (query.get("chat") or [""])[0]
                if not CHAT_ID_RE.match(chat_id):
                    return bad(400, "chat")
                try:
                    wait = max(0.0, min(25.0, float((query.get("wait") or ["0"])[0])))
                except ValueError:
                    wait = 0.0
                it = q.claim(chat_id, (query.get("tab") or [""])[0][:40], wait)
                if it:
                    sys.stderr.write("compose: %s claimed in chat %s\n" % (it["id"], chat_id))
                return ok({"item": q.public(it) if it else None})
            item_id = path[len("/api/compose/"):]
            it = q.get(item_id)
            if it is None:
                return bad(404, "no such message")
            if method == "GET":
                return ok({"item": q.public(it)})
            if method == "POST":
                body = self._body()
                status = body.get("status")
                if status not in ("typed", "sent", "skipped", "failed", "cancelled", "requeue"):
                    return bad(400, "status")
                it = q.finish(item_id, status, body.get("note") or "")
                sys.stderr.write("compose: %s -> %s %s\n" % (item_id, it["status"], it.get("note") or ""))
                return ok({"item": q.public(it)})
            return bad(405, "Method not allowed")

        def _harness_raw(self):
            if os.environ.get("DEPLOY_DEMO") != "1" or not os.path.exists(HARNESS_PATH):
                return None
            return _render_page(HARNESS_PATH, CFG.allowed_origins)

        def _route_feature(self, parts, query):
            method = self.command
            try:
                n = int(parts[0])
            except ValueError:
                return 404, {"error": "No such feature"}, None, "application/json", False
            if len(parts) == 1:
                if method == "GET":
                    status, obj = self.deployer.proxy_feature(n)
                    if status != 200 or obj is None:
                        return 404, {"error": "No such feature"}, None, \
                            "application/json", False
                    return 200, {"feature": obj.get("feature", obj)}, None, \
                        "application/json", False
                return 405, {"error": "Method not allowed"}, None, \
                    "application/json", False
            if parts[1] == "diff":
                status, obj = self.deployer.proxy_feature(n, "/diff", query.get("what", [None])[0])
                if status != 200 or obj is None:
                    return 404, {"error": "No such feature"}, None, "application/json", False
                return 200, obj, None, "application/json", False
            if len(parts) == 2 and parts[1] in ("deploy", "rollback", "discard"):
                if method != "POST":
                    return 405, {"error": "Method not allowed"}, None, "application/json", False
                kind = parts[1]
                token = self._op_token()
                status, obj = self.deployer.propose(n, kind, token, self._current_user())
                return (status or 400), (obj or {}), None, "application/json", False
            return 404, {"error": "Not found"}, None, "application/json", False

        def _op_token(self):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:]
            cookie_token = _cookie(self.headers, "cd_token")
            if cookie_token and self.headers.get("X-Code-Deploy") == "1":
                return cookie_token
            return None

        def _current_user(self):
            _, user = self.deployer.resolve_admin(self.headers, self.command)
            return user

        def _signin(self):
            data = self._body()
            email = data.get("email")
            password = data.get("password")
            if not email or not password:
                return 400, {"error": "email and password required"}, None, \
                    "application/json", False
            url = CFG.owui_url + "/api/v1/auths/signin"
            status, obj, text, ms = http_json("POST", url,
                headers={"Content-Type": "application/json"},
                body={"email": email, "password": password})
            if status == 200 and isinstance(obj, dict) and obj.get("token"):
                code, user = self.deployer.verify_token(obj["token"])
                if code != 200 or (user or {}).get("role") != "admin":
                    return 403, {"error": "not an admin"}, None, \
                        "application/json", False
                name = (user or {}).get("name") or obj.get("name") or email
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie",
                    "cd_token=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
                    % (obj["token"], SESSION_MAX_AGE))
                self._headers_out()
                body = json.dumps({"ok": True, "name": name}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return 200, None, None, None, False
            if status in (400, 401):
                return 401, {"error": "invalid email or password"}, None, \
                    "application/json", False
            return 403, {"error": "could not sign in"}, None, "application/json", False

        def _signout(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie",
                "cd_token=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            self._headers_out()
            body = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return 200, None, None, None, False

    return Handler


# --------------------------------------------------------------------------
# CLI commands
# --------------------------------------------------------------------------
def cmd_status(cfg):
    mgr = Manager(cfg)
    st, obj, _, _ = mgr.get("/api/features")
    features = (obj or {}).get("features", []) if st == 200 else []
    print("Features:")
    if not features:
        print("  (none)")
    for f in sorted(features, key=lambda x: x.get("n", 0), reverse=True):
        print("  #%s  %-12s  %s" % (f.get("n"), f.get("status"),
                                     f.get("title") or "Untitled feature"))
        for c in f.get("components", []):
            print("       - %s (%s ahead=%s)" % (c.get("label"), c.get("name"),
                                                  c.get("ahead")))

    st2, opsobj, _, _ = mgr.get("/api/ops?limit=30")
    ops = (opsobj or {}).get("ops", []) if st2 == 200 else []
    running = [o for o in ops if o.get("status") == "running"]
    print("\nRunning op:", running[0]["id"] if running else "none")

    installed = load_installed(cfg)
    print("\nInstalled files:")
    if not installed:
        print("  (none)")
    for path, meta in installed.items():
        print("  %-24s %s rev=%s" % (path, TARGETS.get(path, {}).get("target", ""),
                                      (meta.get("rev") or "")[:8]))

    drift = compute_local_drift(cfg, installed)
    st3, dobj, _, _ = mgr.get("/api/drift")
    if st3 == 200:
        drift.extend((dobj or {}).get("drift", []))
    print("\nDrift:", len(drift))
    for d in drift:
        print("  %s -> %s expected=%s actual=%s"
              % (d.get("to"), d.get("component"), (d.get("expected") or "")[:8],
                 (d.get("actual") or "")[:8]))


def _restore_file(cfg, token, meta, content, logcb):
    if meta["kind"] == "function":
        return _install_function(cfg, token, meta["id"], content, logcb)
    if meta["kind"] == "tool":
        return _install_tool(cfg, token, meta["id"], content, logcb)
    if meta["kind"] == "loader":
        return _install_loader(cfg, content, logcb)
    return False, "no install target"


def cmd_restore(cfg, op_id):
    d = os.path.join(cfg.state_dir, "ops", op_id, "before")
    if not os.path.isdir(d):
        print("no before/ files for op %s" % op_id)
        return 1
    admin_id = find_admin_id(cfg)
    token = sign_admin_token(cfg, admin_id) if admin_id else None
    def log(m):
        print("  " + m)
    if not token:
        print("could not make an admin token (need secret key + admin user in DB)")
        return 1
    count = 0
    for root, _, files in os.walk(d):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, d)
            meta = TARGETS.get(rel)
            if meta is None:
                print("  skipped %s: no install target" % rel)
                continue
            with open(full, "rb") as fh:
                content = fh.read().decode("utf-8")
            ok, err = _restore_file(cfg, token, meta, content, log)
            count += 1
            print("  %s %s" % ("ok" if ok else "FAIL", rel) + ("" if ok else ": " + err))
    print("restored %d file(s)" % count)
    return 0


def cmd_sync(cfg):
    mgr = Manager(cfg)
    st, obj, _, _ = mgr.post("/api/sync")
    if st not in (200, 202):
        print("sync: manager returned %s" % st)
        return 1
    op = (obj or {}).get("op")
    # The manager returns the op id as a bare string ({"op": <id>}); a dict is
    # tolerated for compatibility.
    op_id = op if isinstance(op, str) else (op or {}).get("id")
    print("sync op:", op_id)
    admin_id = find_admin_id(cfg)
    token = sign_admin_token(cfg, admin_id) if admin_id else None
    if not token:
        print("could not make an admin token")
        return 1

    # Follow the manager op and install the changed remote files.
    if not op_id:
        print("no op id")
        return 1
    runner = LocalOp(cfg, mgr)
    local_id = "cli-sync-%d" % _now()
    runner._begin(local_id, op_id, None, "sync", {"name": "cli", "email": ""})
    runner._follow({
        "op_id": local_id,
        "manager_op_id": op_id,
        "feature": None,
        "kind": "sync",
        "user": {"name": "cli", "email": ""},
        "token": token,
    })
    rec = runner.ops[local_id]
    for item in rec.get("installed") or []:
        print("installed %s -> %s" % (item.get("path"), item.get("target")))
    print("status: %s%s" % (rec.get("status"), (" - " + rec["error"]) if rec.get("error") else ""))
    print("log: %s" % runner.log_path(local_id))
    return 0 if rec.get("status") == "ok" else 1


def main(argv):
    cfg = load_config()
    os.makedirs(cfg.state_dir, exist_ok=True)
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        deployer = Deployer(cfg)
        server = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(deployer))
        print("code-deploy listening on 0.0.0.0:%d (manager %s, owui %s)"
              % (cfg.port, cfg.features_url, cfg.owui_url), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        server.shutdown()
    elif cmd == "status":
        cmd_status(cfg)
    elif cmd == "restore":
        if len(argv) < 3:
            print("usage: code-deploy.py restore <op id>")
            return 2
        return cmd_restore(cfg, argv[2])
    elif cmd == "sync":
        return cmd_sync(cfg)
    else:
        print("usage: code-deploy.py [serve|status|restore <id>|sync]")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
