"""A fake GitHub for tests: the parts of github.com and api.github.com the GitHub button uses, answering the way the
real ones do. One server plays both hosts, so point the pipe's GITHUB_API and GITHUB_WEB valves at `fake.url`.

    fake = FakeGitHub().start()
    fake.pending = 2          # sign-in polls answered "authorization_pending" before the token is issued
    ... run the pipe against fake.url ...
    fake.requests             # what it was asked: [{"method", "path", "query", "auth", "json"}, ...]
    fake.stop()

How the real GitHub behaves (the fake does the same, so code that passes here works there):
- The two sign-in endpoints answer form-encoded text unless the request has `Accept: application/json`.
- The token endpoint answers HTTP 200 for its errors too: {"error": "authorization_pending"}, "slow_down" (with the
  new "interval"), "expired_token", "access_denied". Read the body, not the status code.
- A dead or wrong API token gets 401 {"message": "Bad credentials"}.
- Lists are paged: a `Link: <url>; rel="next"` header while there is more.
- POST /user/repos makes a PUBLIC repo unless "private": true is sent; a taken name gets 422 with
  errors[0].message "name already exists on this account".
- GET /meta needs no token; its "ssh_keys" are github.com's SSH host keys ("<type> <base64>" each), for known_hosts.
- Deploy keys (an SSH key that works for ONE repo): POST /repos/{owner}/{repo}/keys {"title", "key": the public
  key line, "read_only": bool} -> 201 {"id": n, ...}; DELETE /repos/{owner}/{repo}/keys/{id} -> 204 with no body;
  GET .../keys lists them. A key that is not an OpenSSH public key, or one already in use anywhere: 422.
- GET /repos/{owner}/{repo}/branches -> [{"name", "commit": {"sha"}, "protected"}], [] for an empty repo.

Git itself is not played here: `fake.make_remotes(folder)` makes a real bare repository per fake repo and returns
the prefix to use as the pipe's GITHUB_GIT valve, so `<prefix>octo/alpha.git` clones and pushes for real (a local
path: no SSH involved).
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "gho_FAKE_TOKEN_DO_NOT_LEAK"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class FakeGitHub:
    def __init__(self):
        self.client_id = "Iv1.testclient"
        self.token = TOKEN
        self.login = "octo"
        self.device_flow_enabled = True
        self.interval = 5            # seconds between sign-in polls, as told to the client (tests use a fraction)
        self.slow_down_step = 5      # what a slow_down adds to the interval
        self.pending = 0             # polls answered authorization_pending first
        self.slow_down = 0           # polls answered slow_down first (before the pending ones)
        self.expire = False          # the next poll answers expired_token
        self.deny = False            # the next poll answers access_denied
        self.revoked = False         # every API call answers 401 Bad credentials
        self.max_per_page = 100      # lower it to make lists span pages
        self.repos = [self._repo("alpha", True, "First project", "2026-10-10T12:00:00Z"),
                      self._repo("beta", False, "A public one", "2026-09-01T08:30:00Z"),
                      self._repo("gamma", True, None, "2026-06-15T20:15:00Z")]
        self.branches = {"octo/alpha": ["main", "dev"], "octo/beta": ["main"], "octo/gamma": []}  # gamma is empty
        self.keys = {}               # full_name -> [{"id", "title", "key", "read_only"}]: deploy keys there now
        self.key_log = []            # every ("add" | "delete", full_name, read_only) in order
        self.deploy_keys_allowed = True  # False: adding one is refused, as when the user is not the repo's admin
        self.key_delete_fails = False    # True: deleting one answers 500, as when GitHub has a bad moment
        self._key_id = 100
        self._remotes = None         # set by make_remotes: the folder of real bare repositories
        self.requests = []
        self._srv = None
        self.url = ""

    def _repo(self, name, private, description=None, pushed_at="2026-10-11T00:00:00Z"):
        return {"name": name, "full_name": f"{self.login}/{name}", "private": bool(private), "description": description,
                "default_branch": "main", "pushed_at": pushed_at, "html_url": f"https://github.com/{self.login}/{name}",
                "owner": {"login": self.login}, "permissions": {"admin": True, "push": True, "pull": True}}

    def repo(self, full_name):
        return next((r for r in self.repos if r["full_name"] == full_name), None)

    def make_remotes(self, folder):
        """A real bare git repository for every fake repo, under `folder`: one commit (README.md) on each branch
        in self.branches, nothing in a repo with no branches. Returns the prefix for the pipe's GITHUB_GIT valve."""
        def git(*args, cwd):
            subprocess.run(["git", "-c", "user.name=octo", "-c", "user.email=octo@example.com", "-c",
                            "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True)
        self._remotes = str(folder)
        for r in self.repos:
            bare = os.path.join(str(folder), r["full_name"] + ".git")
            if os.path.isdir(bare):
                continue
            os.makedirs(bare)
            git("init", "--bare", "-q", ".", cwd=bare)
            names = self.branches.get(r["full_name"], ["main"])
            if not names:
                continue
            with tempfile.TemporaryDirectory() as work:
                git("init", "-q", ".", cwd=work)
                with open(os.path.join(work, "README.md"), "w") as f:
                    f.write("# %s\n\n%s\n" % (r["name"], r["description"] or ""))
                git("add", "-A", cwd=work)
                git("commit", "-q", "-m", "First commit", cwd=work)
                for b in names:
                    git("push", "-q", bare, "HEAD:refs/heads/" + b, cwd=work)
            git("symbolic-ref", "HEAD", "refs/heads/" + r["default_branch"], cwd=bare)
        return os.path.join(str(folder), "")

    def remote_branches(self, full_name):
        """The repo's branches: what its bare repository really has once make_remotes ran (so a push shows up),
        else the fixed list in self.branches."""
        bare = os.path.join(self._remotes or "", full_name + ".git")
        if not self._remotes or not os.path.isdir(bare):
            return self.branches.get(full_name, ["main"])
        out = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=bare,
                             capture_output=True, text=True).stdout.split()
        return sorted(out, key=lambda b: (b != "main", b))

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, headers=None, form=False):
                if form and isinstance(obj, dict):
                    body, ctype = urllib.parse.urlencode(obj).encode(), "application/x-www-form-urlencoded"
                else:
                    body, ctype = json.dumps(obj).encode(), "application/json"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if "json" in (self.headers.get("Content-Type") or ""):
                    try:
                        return json.loads(raw or "{}")
                    except ValueError:
                        return {}
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

            def _handle(self, method):
                u = urllib.parse.urlsplit(self.path)
                query = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                body = self._body() if method in ("POST", "PATCH", "PUT") else None
                auth = self.headers.get("Authorization") or ""
                fake.requests.append({"method": method, "path": u.path, "query": query, "auth": bool(auth), "json": body})
                as_form = "application/json" not in (self.headers.get("Accept") or "")

                # ---- github.com: device-flow sign-in
                if method == "POST" and u.path == "/login/device/code":
                    if body.get("client_id") != fake.client_id:
                        return self._send(404, {"error": "Not Found"})
                    if not fake.device_flow_enabled:
                        return self._send(400, {"error": "device_flow_disabled", "error_description":
                                                "Device Flow must be explicitly enabled for this App"}, form=as_form)
                    return self._send(200, {"device_code": "dev-123", "user_code": "WDJB-MJHT",
                                            "verification_uri": fake.url + "/login/device", "expires_in": 900,
                                            "interval": fake.interval}, form=as_form)
                if method == "POST" and u.path == "/login/oauth/access_token":
                    if body.get("client_id") != fake.client_id:
                        return self._send(200, {"error": "incorrect_client_credentials"}, form=as_form)
                    if body.get("grant_type") != "urn:ietf:params:oauth:grant-type:device_code":
                        return self._send(200, {"error": "unsupported_grant_type"}, form=as_form)
                    if body.get("device_code") != "dev-123":
                        return self._send(200, {"error": "incorrect_device_code"}, form=as_form)
                    if fake.deny:
                        fake.deny = False
                        return self._send(200, {"error": "access_denied"}, form=as_form)
                    if fake.expire:
                        fake.expire = False
                        return self._send(200, {"error": "expired_token"}, form=as_form)
                    if fake.slow_down > 0:
                        fake.slow_down -= 1
                        fake.interval += fake.slow_down_step
                        return self._send(200, {"error": "slow_down", "interval": fake.interval}, form=as_form)
                    if fake.pending > 0:
                        fake.pending -= 1
                        return self._send(200, {"error": "authorization_pending"}, form=as_form)
                    return self._send(200, {"access_token": fake.token, "token_type": "bearer", "scope": "repo"},
                                      form=as_form)

                # ---- api.github.com
                if method == "GET" and u.path == "/meta":  # public: no token needed
                    return self._send(200, {"ssh_keys": [
                        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKE",
                        "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBFAKEfakeFAKEfake=",
                        "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQFAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKEfake"]})
                if fake.revoked or auth not in (f"Bearer {fake.token}", f"token {fake.token}"):
                    return self._send(401, {"message": "Bad credentials"})
                if method == "GET" and u.path == "/user":
                    return self._send(200, {"login": fake.login, "id": 583231, "avatar_url": fake.url + "/avatar.png"})
                if method == "GET" and u.path == "/user/repos":
                    per = max(1, min(int(query.get("per_page") or 30), fake.max_per_page))
                    page = max(1, int(query.get("page") or 1))
                    rows = sorted(fake.repos, key=lambda r: r["pushed_at"], reverse=True)
                    part = rows[(page - 1) * per: page * per]
                    headers = {}
                    if page * per < len(rows):
                        q = dict(query, per_page=per, page=page + 1)
                        headers["Link"] = f'<{fake.url}/user/repos?{urllib.parse.urlencode(q)}>; rel="next"'
                    return self._send(200, part, headers)
                if method == "POST" and u.path == "/user/repos":
                    name = str(body.get("name") or "")
                    if not NAME_RE.match(name):
                        return self._send(422, {"message": "Repository creation failed.", "errors": [
                            {"resource": "Repository", "code": "custom", "field": "name",
                             "message": "name can only contain ASCII letters, digits, and the characters ., -, and _"}]})
                    if fake.repo(f"{fake.login}/{name}"):
                        return self._send(422, {"message": "Repository creation failed.", "errors": [
                            {"resource": "Repository", "code": "custom", "field": "name",
                             "message": "name already exists on this account"}]})
                    r = fake._repo(name, body.get("private", False), body.get("description") or None)
                    fake.repos.append(r)
                    if fake._remotes:  # git can reach the new, empty repo as well
                        bare = os.path.join(fake._remotes, r["full_name"] + ".git")
                        os.makedirs(bare, exist_ok=True)
                        subprocess.run(["git", "init", "--bare", "-q", "."], cwd=bare, check=True, capture_output=True)
                    return self._send(201, r)
                m = re.fullmatch(r"/repos/([^/]+/[^/]+)/branches", u.path)
                if m and method == "GET":
                    if not fake.repo(m.group(1)):
                        return self._send(404, {"message": "Not Found"})
                    return self._send(200, [{"name": b, "commit": {"sha": "%040x" % (i + 1)}, "protected": False}
                                            for i, b in enumerate(fake.remote_branches(m.group(1)))])
                m = re.fullmatch(r"/repos/([^/]+/[^/]+)/keys(?:/(\d+))?", u.path)
                if m:
                    full, key_id = m.group(1), m.group(2)
                    if not fake.repo(full):
                        return self._send(404, {"message": "Not Found"})
                    have = fake.keys.setdefault(full, [])
                    if method == "GET" and not key_id:
                        return self._send(200, have)
                    if method == "POST" and not key_id:
                        if not fake.deploy_keys_allowed:
                            return self._send(404, {"message": "Not Found"})  # what a non-admin gets
                        key = str(body.get("key") or "").strip()
                        if not re.fullmatch(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+( .*)?", key):
                            return self._send(422, {"message": "Validation Failed", "errors": [
                                {"resource": "PublicKey", "code": "custom", "field": "key", "message":
                                 "key is invalid. You must supply a key in OpenSSH public key format"}]})
                        if any(k["key"].split()[:2] == key.split()[:2] for ks in fake.keys.values() for k in ks):
                            return self._send(422, {"message": "Validation Failed", "errors": [
                                {"resource": "PublicKey", "code": "custom", "field": "key",
                                 "message": "key is already in use"}]})
                        fake._key_id += 1
                        row = {"id": fake._key_id, "title": str(body.get("title") or ""), "key": key,
                               "read_only": bool(body.get("read_only", False)), "verified": True}
                        have.append(row)
                        fake.key_log.append(("add", full, row["read_only"]))
                        return self._send(201, row)
                    if method == "DELETE" and key_id:
                        if fake.key_delete_fails:
                            return self._send(500, {"message": "Server Error"})
                        row = next((k for k in have if k["id"] == int(key_id)), None)
                        if not row:
                            return self._send(404, {"message": "Not Found"})
                        have.remove(row)
                        fake.key_log.append(("delete", full, row["read_only"]))
                        self.send_response(204)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return None
                    return self._send(404, {"message": "Not Found"})
                m = re.fullmatch(r"/repos/([^/]+/[^/]+)", u.path)
                if m:
                    r = fake.repo(urllib.parse.unquote(m.group(1)))
                    if not r:
                        return self._send(404, {"message": "Not Found"})
                    if method == "PATCH":
                        if "private" in body:
                            r["private"] = bool(body["private"])
                        return self._send(200, r)
                    if method == "GET":
                        return self._send(200, r)
                return self._send(404, {"message": "Not Found"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_DELETE(self):
                self._handle("DELETE")

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
