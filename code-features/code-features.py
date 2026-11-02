#!/usr/bin/env python3
"""code-features: manage feature chats with deploy and rollback.

Each feature chat owns one *feature*: a set of git branches (`feature/<n>`)
checked out of the four "component" repos, plus the state that tracks where the
feature is (draft / ready / deployed / rolled_back / discarded) and the
operations that have run on it. Deploying stages the feature's changes in
temporary worktrees, runs the component tests, and only then promotes the main
branches, installs the live files and restarts services. Every deployed feature
can be rolled back on its own.

Standard library only.
"""

import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COMPONENTS = os.path.join(SCRIPT_DIR, "components.json")
DEFAULT_WS_AGENTS = os.path.join(SCRIPT_DIR, "workspace-AGENTS.md")
DEFAULT_ENV_FILE = "/etc/code-mode/features.env"

GIT_USER_ARGS = ["-c", "user.name=code-features",
                 "-c", "user.email=code-features@localhost"]
CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REV_RE = re.compile(r"^[0-9a-f]{7,40}$")
OP_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{4}-[0-9a-f]{4}$")
BACKUP_PREFIX = "backup"          # ops/<id>/backup/<abs target path, no leading />

FIXER_BUSY_MSG = ("The bug fixer is working on an incident; deploy when it "
                  "finishes.")


def _now():
    return int(time.time())


# --------------------------------------------------------------------------
# Checklists: the builder's CHECKLIST.md, read for the Code page's Checklist tab
# --------------------------------------------------------------------------
CHECKLIST_NAME = "CHECKLIST.md"
CHECKLIST_MAX = 1024 * 1024       # bytes read of one checklist; the rest is ignored
CHECK_ITEM_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*\S)\s*$")
CHECK_ID_RE = re.compile(r"^\**([A-Z]{1,3}\.?\d+(?:\.\d+)*[a-z]?)\**[\s:.)]+(.+)$")
CHECK_SPLIT_RE = re.compile(r"\s+(?:—|–|--)\s+")
CHECK_SHA_RE = re.compile(r"\b[0-9a-f]{7,12}\b")
CHECK_NONE_RE = re.compile(r"^\(?\s*(none|nothing|n/a)\b", re.I)


NOW_LINE_RE = re.compile(r"^\s*(?:[-*+]\s+)?\**\s*(task|doing)\s*\**\s*:\s*\**\s*(.+?)\s*$", re.I)


def parse_checklist(text):
    """CHECKLIST.md as the page draws it. The builder writes it by hand, so the parse is forgiving: every `- [ ]` /
    `- [x]` line is an item (with the `## ` heading above it as its section), an item's own id ("A.1", "B.10") is
    split off its text, and the text is cut at its first " - " dash into the requirement and the evidence the builder
    wrote after it. Bullets under `## Open` are the open issues. "now" is the first item not ticked: the builder's own
    "Next" line is not used, because it goes stale between commits. "current" is the builder's own "## Now" section
    (a "Task:" and a "Doing:" line it rewrites when it moves to another item), or None when it has none."""
    title, section, items, open_issues, current = "", "", [], [], {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()[:200]
            continue
        if line.startswith("## "):
            section = line[3:].strip()[:80]
            continue
        if section.lower().rstrip(":").strip() == "now":
            n = NOW_LINE_RE.match(line)
            if n and n.group(1).lower() not in current:
                current[n.group(1).lower()] = n.group(2).strip("*").strip()[:240]
            continue
        m = CHECK_ITEM_RE.match(line)
        if m:
            body = m.group(2)
            mid = CHECK_ID_RE.match(body)
            ident, body = (mid.group(1), mid.group(2)) if mid else ("", body)
            parts = CHECK_SPLIT_RE.split(body, maxsplit=1)
            detail = parts[1] if len(parts) > 1 else ""
            sha = CHECK_SHA_RE.search(detail)
            items.append({"id": ident[:12], "label": parts[0][:240], "detail": detail[:400],
                          "sha": sha.group(0)[:7] if sha else "", "done": m.group(1) != " ",
                          "section": section})
            continue
        if section.lower().startswith("open") and re.match(r"^\s*[-*+]\s+\S", line):
            issue = re.sub(r"^\s*[-*+]\s+", "", line)
            if not CHECK_NONE_RE.match(issue):
                open_issues.append(issue[:300])
    now = next((i for i, it in enumerate(items) if not it["done"]), None)
    return {"title": title, "items": items, "done": sum(1 for it in items if it["done"]),
            "total": len(items), "now": now, "open": open_issues[:20], "current": current or None}


def _atomic_write(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _sha256_file(path):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_show_bytes(repo, rev, path):
    p = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (rev, path)],
                       capture_output=True)
    if p.returncode != 0:
        return None
    return p.stdout


def _split_diff(patch):
    files, chunks, cur = [], [], None
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur is not None:
                chunks.append(cur)
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        chunks.append(cur)
    for chunk in chunks:
        m = re.search(r"a/(.+)\tb/(.+)", chunk[0])
        files.append((m.group(2) if m else "", "".join(chunk)))
    return files


def _count_adds_deletes(patch):
    adds = deletes = 0
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            adds += 1
        elif line.startswith("-"):
            deletes += 1
    return adds, deletes


def _porcelain_paths(out):
    paths = []
    for line in out.splitlines():
        if not line:
            continue
        rest = line[2:]                        # skip the 2-char status column
        if rest[:1] == " ":
            rest = rest[1:]                    # skip the separator space
        if " -> " in rest:
            rest = rest.split(" -> ")[1]
        paths.append(rest)
    return paths


def _path_names(out):
    """Parse 'git diff --name-only' / 'git ls-files --others': each line is a
    path with no status prefix."""
    return [line for line in out.splitlines() if line]


def load_config_from_env():
    data = {}
    if os.path.exists(DEFAULT_ENV_FILE):
        with open(DEFAULT_ENV_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip()
    for k in os.environ:
        if k.startswith("FEATURES_"):
            data[k] = os.environ[k]
    return Config(data)


class Config:
    """Resolved configuration from the env file, then the environment."""

    def __init__(self, data):
        missing = []
        def req(k):
            v = data.get(k)
            if v is None or v == "":
                missing.append(k)
            return v
        self.token = req("FEATURES_TOKEN")
        self.port = int(data.get("FEATURES_PORT", "4100") or 4100)
        self.dir = os.path.abspath(
            data.get("FEATURES_DIR", "/home/coder/features") or "/home/coder/features")
        self.projects = os.path.abspath(
            data.get("FEATURES_PROJECTS", "/home/coder/projects")
            or "/home/coder/projects")
        self.home = data.get("FEATURES_HOME", "/home/coder") or "/home/coder"
        self.components_path = data.get("FEATURES_COMPONENTS", DEFAULT_COMPONENTS)
        self.restart_cmd = data.get("FEATURES_RESTART_CMD",
                                    "sudo -n /usr/bin/systemctl restart")
        self.fixer_index = data.get("FEATURES_FIXER_INDEX",
                                    "/home/coder/fixes/index.jsonl")
        if missing:
            raise SystemExit("code-features: missing required config: %s"
                             % ", ".join(missing))
        self.components = self._load_components()

    def _load_components(self):
        with open(self.components_path) as fh:
            return json.load(fh)

    def main_checkout(self, name):
        return os.path.join(self.projects, name)

    def worktree_dir(self, feat, name):
        return os.path.join(feat["dir"], name)

    def stage_dir(self, op_id, name):
        return os.path.join(self.dir, "stage", op_id, name)

    def venv(self, name):
        return os.path.join(self.projects, name, ".venv")

    def home_expand(self, path):
        return os.path.join(self.home, path[2:]) if path.startswith("~/") else path


# --------------------------------------------------------------------------
# Operation record + logger
# --------------------------------------------------------------------------
class Op:
    def __init__(self, op_id, kind, feature_n, title):
        self.id = op_id
        self.kind = kind
        self.feature = feature_n
        self.title = title or ""
        self.status = "running"
        self.error = None
        self.started = _now()
        self.ended = None
        self.steps = []
        self.commits = {}            # component -> [{"sha","kind"}...] creation order
        self.installed = []
        self.restarted = []
        self.remote = None
        self._log_path = None

    def path(self):
        return os.path.join(CFG.dir, "ops", self.id)

    def json_path(self):
        return os.path.join(self.path(), "op.json")

    def log_path(self):
        if self._log_path is None:
            self._log_path = os.path.join(self.path(), "log.txt")
        return self._log_path

    def write_json(self):
        obj = {
            "id": self.id, "kind": self.kind, "feature": self.feature,
            "title": self.title, "status": self.status, "error": self.error,
            "started": self.started, "ended": self.ended,
            "commits": self.commits, "installed": self.installed,
            "restarted": self.restarted, "remote": self.remote,
        }
        _atomic_write(self.json_path(), json.dumps(obj, indent=2) + "\n")

    def _ts(self):
        return time.strftime("%H:%M:%S", time.localtime())

    def log(self, text=""):
        d = os.path.dirname(self.log_path())
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.log_path(), "a") as fh:
            fh.write(text + "\n")

    def step(self, name):
        self.steps.append({"name": name, "status": "running",
                           "started": _now(), "ended": None, "detail": None})
        self.log("[%s] step: %s" % (self._ts(), name))

    def step_end(self, ok, detail=None):
        st = self.steps[-1]
        st["status"] = "ok" if ok else "failed"
        st["ended"] = _now()
        if detail is not None:
            st["detail"] = detail
        self.log("[%s] step %s: %s" % (self._ts(), st["name"], st["status"]))
        self.write_json()

    def run(self, argv, cwd=None, commit=False, timeout=None, label=None):
        argv = list(argv)
        if commit and argv:
            # GIT_USER_ARGS (e.g. -c user.name=...) must follow the command
            # executable, not precede it, otherwise subprocess tries to run
            # "-c" as the program.
            argv = [argv[0]] + GIT_USER_ARGS + argv[1:]
        desc = label or " ".join(argv)
        self.log("[%s] $ %s (cwd=%s)"
                 % (self._ts(), desc, cwd or os.getcwd()))
        start = time.time()
        try:
            p = subprocess.run(argv, cwd=cwd, timeout=timeout,
                               capture_output=True, text=True)
            out = (p.stdout or "") + (p.stderr or "")
            elapsed = time.time() - start
            shown = out[:20000]
            if len(out) > 20000:
                shown += "\n...[cut at 20000 chars]..."
            if shown.strip():
                for sline in shown.splitlines():
                    self.log("  " + sline)
            self.log("-> exit %d (%.1fs)" % (p.returncode, elapsed))
            return p.returncode, p.stdout or "", p.stderr or "", elapsed
        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - start
            self.log("  " + ((e.stdout or "") + (e.stderr or ""))[:20000])
            self.log("-> timeout after %.1fs" % elapsed)
            return 124, e.stdout or "", e.stderr or "", elapsed

    def finish(self, ok, error=None):
        self.status = "ok" if ok else "failed"
        self.error = error
        self.ended = _now()
        self.log("[%s] %s %s%s" % (self._ts(), self.status, self.kind,
                                   (" - " + error) if error else ""))


# --------------------------------------------------------------------------
# The manager
# --------------------------------------------------------------------------
class Features:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.RLock()       # guards self.features + on-disk state
        self._op_lock = threading.Lock()     # one op at a time
        self._active_op = None
        self._ops_order = []                 # op ids, creation order
        self.features = {}
        self._load_features()

    # ---- persistence ----------------------------------------------------
    def _features_root(self):
        return os.path.join(self.cfg.dir, "features")

    def _feature_path(self, n):
        return os.path.join(self._features_root(), "%d.json" % n)

    def _load_features(self):
        root = self._features_root()
        if not os.path.isdir(root):
            return
        for fn in os.listdir(root):
            if not re.match(r"^[0-9]+\.json$", fn):  # features are saved as <n>.json
                continue
            try:
                with open(os.path.join(root, fn)) as fh:
                    feat = json.load(fh)
                self.features[feat["n"]] = feat
            except (OSError, ValueError, KeyError):
                continue

    def _save_feature(self, feat):
        feat["updated"] = _now()
        _atomic_write(self._feature_path(feat["n"]),
                      json.dumps(feat, indent=2) + "\n")

    # ---- git helpers ----------------------------------------------------
    def _git(self, args, cwd=None, commit=False, timeout=None):
        argv = ["git"] + (GIT_USER_ARGS if commit else []) + list(args)
        p = subprocess.run(argv, cwd=cwd, timeout=timeout, capture_output=True,
                           text=True)
        return p.returncode, p.stdout or "", p.stderr or ""

    def _op_git(self, op, args, cwd=None, commit=False, timeout=None, label=None):
        """Run a git command via op.run (which logs it), prefixed with 'git'."""
        run_label = label
        if run_label is None:
            run_label = " ".join(args)
        return op.run(["git"] + list(args), cwd=cwd, commit=commit,
                      timeout=timeout, label="git " + run_label)

    def _rev_parse(self, repo, ref):
        rc, out, _ = self._git(["rev-parse", ref], cwd=repo)
        return out.strip() if rc == 0 else None

    def _count_ahead(self, repo, main_branch, branch):
        rc, out, _ = self._git(["rev-list", "--count",
                                "%s..%s" % (main_branch, branch)], cwd=repo)
        return int(out.strip() or 0) if rc == 0 else 0

    def _list_commits(self, repo, main_branch, branch):
        commits = []
        rc, out, _ = self._git(["log", "--format=%H%x09%h%x09%ct%x09%s",
                                "%s..%s" % (main_branch, branch)], cwd=repo)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                commits.append({"sha": parts[0], "short": parts[1],
                                "time": int(parts[2]), "subject": "\t".join(parts[3:])})
        return commits

    def _dirty_paths(self, worktree):
        if not os.path.isdir(worktree):
            return []
        rc, out, _ = self._git(["status", "--porcelain"], cwd=worktree)
        return _porcelain_paths(out) if rc == 0 else []

    def _untracked(self, worktree):
        if not os.path.isdir(worktree):
            return set()
        rc, out, _ = self._git(["ls-files", "--others", "--exclude-standard"],
                               cwd=worktree)
        return set(_path_names(out) if rc == 0 else [])

    def _merge_base(self, repo, main_branch, branch):
        rc, out, _ = self._git(["merge-base", main_branch, branch], cwd=repo)
        return out.strip() if rc == 0 else None

    # ---- fixer busy -----------------------------------------------------
    def fixer_busy(self):
        path = self.cfg.fixer_index
        if not os.path.exists(path):
            return False
        last = {}
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("id") is not None:
                        last[rec["id"]] = rec
        except OSError:
            return False
        now = _now()
        for rec in last.values():
            st = rec.get("status")
            if st not in (None, "done") and 0 <= now - rec.get("created", 0) < 7200:
                return True
        return False

    # ---- feature creation ----------------------------------------------
    def create(self, chat_id):
        if not isinstance(chat_id, str) or not CHAT_ID_RE.match(chat_id):
            return 400, {"error": "Invalid chat_id (must match "
                          "^[A-Za-z0-9_-]{1,64}$)"}
        with self._lock:
            existing = next((f for f in self.features.values()
                             if f["chat_id"] == chat_id and f["status"] != "discarded"),
                            None)
            if existing is not None:
                return 200, {"feature": self.view(existing)}
            # Reserve n under the lock so two concurrent creates can't collide.
            n = 1 + max(self.features, default=0)
            base_dir = os.path.join(self.cfg.projects, "features", chat_id)
            branch = "feature/%d" % n
            wts_created, dirs_created = [], []
            try:
                os.makedirs(base_dir, exist_ok=True)
                dirs_created.append(base_dir)
                base = {}
                for comp in self.cfg.components:
                    name = comp["name"]
                    repo = self.cfg.main_checkout(name)
                    sha = self._rev_parse(repo, comp["branch"])
                    if sha is None:
                        raise RuntimeError("cannot resolve %s in %s"
                                           % (comp["branch"], repo))
                    base[name] = sha
                    wt = os.path.join(base_dir, name)
                    rc, out, err = self._git(["worktree", "add", "-b", branch, wt,
                                              comp["branch"]], cwd=repo)
                    if rc != 0:
                        blob = (out + err).lower()
                        if "exists" in blob or "already" in blob:
                            raise RuntimeError("branch %s already exists; refusing to "
                                               "reuse it (start a fresh feature chat for "
                                               "this chat id)" % branch)
                        raise RuntimeError("worktree add failed for %s: %s%s"
                                           % (name, out.strip(), err.strip()))
                    wts_created.append(wt)
                with open(DEFAULT_WS_AGENTS) as fh:
                    agents = fh.read().replace("{n}", str(n)).replace("{chat_id}",
                                                                       chat_id)
                _atomic_write(os.path.join(base_dir, "AGENTS.md"), agents)
                _atomic_write(os.path.join(base_dir, ".feature.json"),
                              json.dumps({"n": n, "chat_id": chat_id}))
                now = _now()
                feat = {"n": n, "chat_id": chat_id, "dir": base_dir, "branch": branch,
                        "title": "", "summary": "", "status": "draft", "created": now,
                        "updated": now, "ready_at": None, "deployed_at": None,
                        "rolled_back_at": None, "base": base, "applied": [],
                        "rollback": [], "tests": {}, "ops": []}
                self.features[n] = feat
                self._save_feature(feat)
            except RuntimeError as e:
                for wt in wts_created:
                    self._git(["worktree", "remove", "--force", wt],
                              cwd=self.cfg.main_checkout(os.path.basename(wt)))
                for d in dirs_created:
                    try:
                        if os.path.isdir(d) and not os.listdir(d):
                            os.rmdir(d)
                    except OSError:
                        pass
                return 400, {"error": str(e)}
            return 201, {"feature": self.view(feat)}

    # ---- view -----------------------------------------------------------
    def view(self, feat):
        comp_views, touched, any_ahead = [], [], False
        for comp in self.cfg.components:
            name = comp["name"]
            wt = self.cfg.worktree_dir(feat, name)
            repo = self.cfg.main_checkout(name)
            ahead = self._count_ahead(repo, comp["branch"], feat["branch"])
            commits = self._list_commits(repo, comp["branch"], feat["branch"])
            dirty = self._dirty_paths(wt)
            applied = [{"sha": c["sha"], "short": c["sha"][:7],
                        "subject": self._subject(c["sha"], repo), "kind": c["kind"]}
                       for c in feat.get("applied", []) if c["component"] == name]
            if ahead > 0 or dirty or applied:
                comp_views.append({"name": name, "label": comp.get("label", name),
                                   "ahead": ahead, "commits": commits, "dirty": dirty,
                                   "applied": applied,
                                   "tests": feat.get("tests", {}).get(name)})
                touched.append(name)
                if ahead > 0:
                    any_ahead = True
        remote_touched = self._remote_touched(feat)
        status = feat["status"]
        update_available = status == "deployed" and any_ahead
        blocked = self._deploy_blocked(comp_views, status, any_ahead,
                                       update_available)
        can_deploy = blocked is None
        return {
            "n": feat["n"], "chat_id": feat["chat_id"], "dir": feat["dir"],
            "branch": feat["branch"], "title": feat["title"],
            "summary": feat["summary"], "status": status, "created": feat["created"],
            "updated": feat["updated"], "ready_at": feat["ready_at"],
            "deployed_at": feat["deployed_at"],
            "rolled_back_at": feat["rolled_back_at"],
            "components": comp_views, "touched": touched,
            "remote_touched": remote_touched, "update_available": update_available,
            "can_deploy": can_deploy, "can_rollback": status == "deployed",
            "can_discard": status in ("draft", "ready", "rolled_back"),
            "blocked": blocked,
            "last_op": self._last_op(feat),
        }

    def _subject(self, sha, repo):
        rc, out, _ = self._git(["log", "-1", "--format=%s", sha], cwd=repo)
        return out.strip() if rc == 0 else ""

    def _remote_touched(self, feat):
        for comp in self.cfg.components:
            if not comp.get("remote"):
                continue
            rel = comp["remote"].rstrip("/")
            wt = self.cfg.worktree_dir(feat, comp["name"])
            if not os.path.isdir(wt):
                continue
            paths = set()
            rc, out, _ = self._git(["diff", "--name-only", comp["branch"], "--", rel],
                                   cwd=wt)
            if rc == 0:
                paths.update(_path_names(out))
            rc2, out2, _ = self._git(["ls-files", "--others", "--exclude-standard",
                                      "--", rel], cwd=wt)
            if rc2 == 0:
                paths.update(_path_names(out2))
            return sorted(paths)
        return []

    def _deploy_blocked(self, comp_views, status, any_ahead, update_available):
        if status == "discarded":
            return "This feature has been discarded"
        for cv in comp_views:
            if cv["dirty"]:
                return "Uncommitted changes in %s: %s" % (
                    cv["label"], ", ".join(cv["dirty"]))
        if not any_ahead and status != "rolled_back":
            return ("Nothing to deploy: no commits ahead of main and the feature "
                    "is not rolled back")
        if status == "deployed" and not update_available:
            return "Already deployed; deploy again only when an update is available"
        return None

    def _last_op(self, feat):
        ops = feat.get("ops", [])
        if not ops:
            return None
        try:
            with open(os.path.join(self.cfg.dir, "ops", ops[-1], "op.json")) as fh:
                return json.load(fh)
        except OSError:
            return None

    # ---- listing --------------------------------------------------------
    def list_features(self, chat=None):
        feats = [f for f in self.features.values()
                 if chat is None or f["chat_id"] == chat]
        feats.sort(key=lambda f: f["n"], reverse=True)
        return [self.view(f) for f in feats]

    def get_feature(self, n):
        feat = self.features.get(n)
        if feat is None:
            return None
        return feat, self.view(feat)

    # ---- checklist ------------------------------------------------------
    def checklist(self, chat_id):
        """The chat's CHECKLIST.md, parsed: a feature chat's in its feature folder (outside every component), a
        project chat's in <project>/.code-mode/. Only those two places, only under the projects root."""
        if not isinstance(chat_id, str) or not CHAT_ID_RE.match(chat_id):
            return 404, {"error": "No such chat"}
        feats = sorted((f for f in self.features.values()
                        if f["chat_id"] == chat_id and f.get("status") != "discarded"),
                       key=lambda f: f["n"], reverse=True)
        if feats:
            kind, n, path = "feature", feats[0]["n"], os.path.join(feats[0]["dir"], CHECKLIST_NAME)
        else:
            kind, n = "project", None
            path = os.path.join(self.cfg.projects, "chats", chat_id, ".code-mode", CHECKLIST_NAME)
        root = os.path.realpath(self.cfg.projects)
        real = os.path.realpath(path)
        if not real.startswith(root + os.sep) or not os.path.isfile(real):
            return 200, {"exists": False, "kind": kind, "feature": n, "server_time": _now()}
        try:
            with open(real, "rb") as fh:
                text = fh.read(CHECKLIST_MAX).decode("utf-8", "replace")
            updated = int(os.path.getmtime(real))
        except OSError:
            return 200, {"exists": False, "kind": kind, "feature": n, "server_time": _now()}
        out = parse_checklist(text)
        out.update(exists=True, kind=kind, feature=n, updated=updated, server_time=_now())
        return 200, out

    # ---- ready / tests --------------------------------------------------
    def set_ready(self, n, title, summary):
        if not isinstance(title, str) or not (1 <= len(title) <= 120):
            return 400, {"error": "title must be 1-120 characters"}
        if not isinstance(summary, str) or len(summary) > 4000:
            return 400, {"error": "summary must be at most 4000 characters"}
        feat = self.features.get(n)
        if feat is None:
            return 404, {"error": "No such feature"}
        feat["title"] = title
        feat["summary"] = summary
        # A feature that is live stays "deployed" (and roll-backable); a newer ready_at than deployed_at plus
        # update_available tells the panel an update is waiting.
        if not feat.get("applied"):
            feat["status"] = "ready"
        feat["ready_at"] = _now()
        with self._lock:
            self._save_feature(feat)
        return 200, {"feature": self.view(feat)}

    def set_tests(self, n, tests):
        feat = self.features.get(n)
        if feat is None:
            return 404, {"error": "No such feature"}
        names = {c["name"] for c in self.cfg.components}
        if not isinstance(tests, dict) or not all(
                k in names and isinstance(v, dict) for k, v in tests.items()):
            return 400, {"error": "tests must be {component: {ok, summary}} for known components"}
        kept = {}
        for comp, res in tests.items():
            kept[comp] = {"ok": bool(res.get("ok")), "summary": res.get("summary", ""),
                          "at": _now()}
        feat["tests"].update(kept)
        with self._lock:
            self._save_feature(feat)
        return 200, {"feature": self.view(feat)}

    # ---- operations: dispatch ------------------------------------------
    def _new_op(self, kind, feat):
        op = Op(self._next_op_id(), kind, feat["n"], feat["title"])
        with self._lock:
            feat["ops"].append(op.id)
            self._ops_order.append(op.id)
            self._save_feature(feat)
        op.write_json()
        return op

    @staticmethod
    def _next_op_id():
        rr = hashlib.md5(str(time.time()).encode()).hexdigest()[:4]
        return time.strftime("%Y%m%d-%H%M-", time.localtime()) + rr

    def health(self):
        with self._op_lock:
            return {"ok": True, "busy": self._active_op}

    def _start(self, kind, feat):
        with self._op_lock:
            if self._active_op is not None:
                return 409, {"error": "busy", "op": self._active_op}
            # Don't create an op record for a request we're about to refuse,
            # otherwise every refused deploy/rollback leaves an orphan in /api/ops.
            if kind != "discard" and self.fixer_busy():
                return 400, {"error": FIXER_BUSY_MSG}
            op = self._new_op(kind, feat)
            self._active_op = op.id
        threading.Thread(target=self._execute, args=(op, kind, feat),
                         daemon=True).start()
        return 202, {"op": op.id}

    def _execute(self, op, kind, feat):
        try:
            if kind == "deploy":
                self._op_deploy(op, feat)
            elif kind == "rollback":
                self._op_rollback(op, feat)
            elif kind == "discard":
                self._op_discard(op, feat)
            elif kind == "sync":
                self._run_sync(op)
        except Exception as e:  # noqa
            op.finish(False, error=str(e))
            op.write_json()
        finally:
            with self._lock:
                if feat is not None:
                    self._save_feature(feat)
            with self._op_lock:
                self._active_op = None

    # -- preconditions ---------------------------------------------------
    def propose_deploy(self, n):
        feat = self.features.get(n)
        if feat is None:
            return 404, {"error": "No such feature"}
        cv = [c for c in self._comp_views(feat)]
        blocked = self._deploy_blocked(cv, feat["status"],
                                       self._any_ahead(feat),
                                       feat["status"] == "deployed"
                                       and self._any_ahead(feat))
        if blocked:
            return 400, {"error": blocked}
        # every affected main checkout must be clean
        for name in self._affected(feat):
            repo = self.cfg.main_checkout(name)
            rc, out, _ = self._git(["status", "--porcelain",
                                    "--untracked-files=no"], cwd=repo)
            if rc == 0 and out.strip():
                return 400, {"error": "Feature #%d cannot deploy: %s has "
                                      "uncommitted changes; commit or stash "
                                      "them first" % (feat["n"], name)}
        return self._start("deploy", feat)

    def propose_rollback(self, n):
        feat = self.features.get(n)
        if feat is None:
            return 404, {"error": "No such feature"}
        if feat["status"] != "deployed":
            return 400, {"error": "Can only roll back a deployed feature"}
        for name in {a["component"] for a in feat["applied"]}:
            repo = self.cfg.main_checkout(name)
            rc, out, _ = self._git(["status", "--porcelain",
                                    "--untracked-files=no"], cwd=repo)
            if rc == 0 and out.strip():
                return 400, {"error": "Feature #%d cannot roll back: %s has "
                                      "uncommitted changes" % (feat["n"], name)}
        return self._start("rollback", feat)

    def propose_discard(self, n):
        feat = self.features.get(n)
        if feat is None:
            return 404, {"error": "No such feature"}
        if feat["status"] not in ("draft", "ready", "rolled_back"):
            return 400, {"error": "Cannot discard a %s feature" % feat["status"]}
        return self._start("discard", feat)

    def propose_sync(self):
        with self._op_lock:
            if self._active_op is not None:
                return 409, {"error": "busy", "op": self._active_op}
            if self.fixer_busy():
                return 400, {"error": FIXER_BUSY_MSG}
            op = Op(self._next_op_id(), "sync", None, "sync")
            self._active_op = op.id
        threading.Thread(target=self._execute, args=(op, "sync", None),
                         daemon=True).start()
        return 202, {"op": op.id}

    # helpers used by preconditions --------------------------------------
    def _comp_views(self, feat):
        views = []
        for comp in self.cfg.components:
            name = comp["name"]
            repo = self.cfg.main_checkout(name)
            ahead = self._count_ahead(repo, comp["branch"], feat["branch"])
            dirty = self._dirty_paths(self.cfg.worktree_dir(feat, name))
            applied = [c for c in feat.get("applied", []) if c["component"] == name]
            if ahead > 0 or dirty or applied:
                views.append({"name": name, "label": comp.get("label", name),
                              "ahead": ahead, "dirty": dirty})
        return views

    def _any_ahead(self, feat):
        for comp in self.cfg.components:
            repo = self.cfg.main_checkout(comp["name"])
            if self._count_ahead(repo, comp["branch"], feat["branch"]) > 0:
                return True
        return False

    def _affected(self, feat):
        affected = set()
        for comp in self.cfg.components:
            name = comp["name"]
            repo = self.cfg.main_checkout(name)
            if self._count_ahead(repo, comp["branch"], feat["branch"]) > 0:
                affected.add(name)
        if feat["status"] == "rolled_back":
            for entry in feat.get("rollback", []):
                affected.add(entry["component"])
        return sorted(affected)

    # ---- staging / promote / install / restart / remote ----------------
    def _stage(self, op, affected):
        stage_base = {}
        for name in affected:
            repo = self.cfg.main_checkout(name)
            mb = next(c["branch"] for c in self.cfg.components if c["name"] == name)
            stage_base[name] = self._rev_parse(repo, mb)
            sdir = self.cfg.stage_dir(op.id, name)
            rc, out, err, _ = self._op_git(op, ["worktree", "add", "--detach", sdir, mb],
                                  cwd=repo, label="worktree add --detach %s" % name)
            if rc != 0:
                raise RuntimeError("failed to stage %s: %s%s" % (name, out, err))
        return stage_base

    def _cleanup_stages(self, op, affected):
        for name in affected:
            repo = self.cfg.main_checkout(name)
            sdir = self.cfg.stage_dir(op.id, name)
            self._op_git(op, ["worktree", "remove", "--force", sdir], cwd=repo,
                   label="worktree remove --force %s" % name)
            self._op_git(op, ["worktree", "prune"], cwd=repo, label="worktree prune %s" % name)

    def _promote(self, op, affected, stage_head, stage_base):
        for name in affected:
            repo = self.cfg.main_checkout(name)
            mb = next(c["branch"] for c in self.cfg.components if c["name"] == name)
            now = self._rev_parse(repo, mb)
            if now != stage_base[name]:
                raise RuntimeError("main checkout %s moved during staging; abort"
                                   % name)
            rc, out, _ = self._git(["status", "--porcelain",
                                    "--untracked-files=no"], cwd=repo)
            if rc == 0 and out.strip():
                raise RuntimeError("main checkout %s has uncommitted changes; abort"
                                   % name)
        for name in affected:
            repo = self.cfg.main_checkout(name)
            rc, out, err, _ = self._op_git(op, ["merge", "--ff-only", stage_head[name]],
                                  cwd=repo, label="merge --ff-only %s" % name)
            if rc != 0:
                raise RuntimeError("promote failed for %s: %s%s" % (name, out, err))

    def _install(self, op, affected):
        for name in affected:
            comp = next(c for c in self.cfg.components if c["name"] == name)
            repo = self.cfg.main_checkout(name)
            for entry in comp.get("install", []):
                src, target = entry["from"], self.cfg.home_expand(entry["to"])
                rev = self._rev_parse(repo, comp["branch"])
                data = _git_show_bytes(repo, rev, src)
                if data is None:
                    raise RuntimeError("cannot install %s:%s (missing)"
                                       % (name, src))
                sha_before = _sha256_file(target)
                backup_rel = None
                old_data = None
                if sha_before is not None:
                    with open(target, "rb") as fh:
                        old_data = fh.read()
                    backup_rel = os.path.join(BACKUP_PREFIX,
                                              target.lstrip("/"))
                    backup_path = os.path.join(op.path(), backup_rel)
                    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                    with open(backup_path, "wb") as fh:
                        fh.write(old_data)
                tmp = target + ".tmp-" + op.id
                os.makedirs(os.path.dirname(tmp), exist_ok=True)
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.chmod(tmp, int(entry["mode"], 8))
                os.replace(tmp, target)
                op.installed.append({"component": name, "to": target,
                                     "sha_before": sha_before,
                                     "sha_after": _sha256_file(target),
                                     "backup": backup_rel})

    def _restart_units(self, op, affected, only_fixer_free=False):
        for name in affected:
            comp = next(c for c in self.cfg.components if c["name"] == name)
            for unit in comp.get("restart", []):
                if unit == "code-fixer" and self.fixer_busy():
                    if not self._wait_fixer_free(op):
                        op.restarted.append({"unit": unit, "ok": False,
                                             "output": "fixer still busy after 15 min"})
                        raise RuntimeError("bug fixer busy; could not restart %s"
                                           % unit)
                argv = shlex.split(self.cfg.restart_cmd) + [unit]
                rc, out, err, _ = op.run(argv, label="restart %s" % unit)
                op.restarted.append({"unit": unit, "ok": rc == 0,
                                     "output": (out + err).strip()})
                if rc != 0:
                    raise RuntimeError("restart of %s failed (exit %d): %s"
                                       % (unit, rc, (out + err).strip()))

    def _wait_fixer_free(self, op, timeout=15 * 60):
        start = time.time()
        while self.fixer_busy():
            if time.time() - start > timeout:
                return False
            time.sleep(5)
        return True

    def _remote_info(self, affected, stage_base, new_sha):
        for comp in self.cfg.components:
            if not comp.get("remote"):
                continue
            name = comp["name"]
            repo = self.cfg.main_checkout(name)
            if name in affected:
                base, new = stage_base[name], new_sha[name]
            else:
                cur = self._rev_parse(repo, comp["branch"])
                base = new = cur
            rc, out, _ = self._git(["diff", "--name-only", base, new, "--",
                                    comp["remote"].rstrip("/")], cwd=repo)
            return {"rev": self._rev_parse(repo, comp["branch"]),
                    "changed": sorted(_path_names(out) if rc == 0 else [])}
        return {"rev": None, "changed": []}

    # -- deploy op -------------------------------------------------------
    def _op_deploy(self, op, feat):
        affected = self._affected(feat)
        stage_base = self._stage(op, affected)
        try:
            stage_head, new_commits = {}, {}
            for name in affected:
                comp = next(c for c in self.cfg.components if c["name"] == name)
                sdir = self.cfg.stage_dir(op.id, name)
                op.step("Merge into %s" % name)
                rc, err, commits = self._deploy_component(op, feat, comp, sdir)
                if rc != 0:
                    op.step_end(False, detail=err)
                    raise RuntimeError(err.rstrip() or "merge into %s failed" % name)
                op.step_end(True)
                op.step("Test %s" % name)
                test_cmd = [c.replace("{venv}", self.cfg.venv(name))
                            for c in comp["test"]]
                rc, out, err, _ = op.run(test_cmd, cwd=sdir, timeout=600,
                                         label="tests for %s" % name)
                combined = out + err
                if rc != 0:
                    op.step_end(False, detail=combined[-3000:])
                    raise RuntimeError((combined[-3000:] or
                                        "tests failed for %s" % name).strip())
                op.step_end(True)
                stage_head[name] = self._rev_parse(sdir, "HEAD")
                new_commits[name] = commits
            self._promote(op, affected, stage_head, stage_base)
            # The feature is now on main: record it before install/restart so a
            # failure there still leaves the feature deployed and roll-backable.
            with self._lock:
                feat = self.features[feat["n"]]
                for name, commits in new_commits.items():
                    for c in commits:              # oldest first
                        feat["applied"].append({"component": name, "sha": c["sha"],
                                                "kind": c["kind"]})
                feat["rollback"] = []
                feat["status"] = "deployed"
                feat["deployed_at"] = _now()
                self._save_feature(feat)
            op.commits = new_commits
            self._install(op, affected)
            self._restart_units(op, affected)
            new_sha = {name: self._rev_parse(self.cfg.main_checkout(name),
                        next(c["branch"] for c in self.cfg.components
                             if c["name"] == name)) for name in affected}
            op.remote = self._remote_info(affected, stage_base, new_sha)
        finally:
            self._cleanup_stages(op, affected)
        op.finish(True)
        op.write_json()

    def _deploy_component(self, op, feat, comp, sdir):
        """Returns (rc, error, [{"sha","kind"}] oldest-first)."""
        name = comp["name"]
        main_branch = comp["branch"]
        branch = feat["branch"]
        created = []
        if feat["status"] == "rolled_back":
            for entry in reversed([r for r in feat.get("rollback", [])
                                   if r["component"] == name]):
                rc, out, err, _ = self._op_git(op, ["revert", "--no-edit", entry["sha"]],
                                         cwd=sdir, commit=True,
                                         label="revert (restore)")
                if rc != 0:
                    return 1, (out + err).strip(), []
                created.append({"sha": self._head(sdir), "kind": "restore"})
        if self._count_ahead(self.cfg.main_checkout(name), main_branch, branch) > 0:
            msg1 = "Feature #%d: %s" % (feat["n"], feat["title"] or "untitled")
            msg2 = "Branch %s, chat %s" % (branch, feat["chat_id"])
            rc, out, err, _ = self._op_git(op, ["merge", "--no-ff", "-m", msg1, "-m", msg2,
                                      branch], cwd=sdir, commit=True,
                                     label="merge --no-ff feature branch")
            if rc != 0:
                rcu, uout, _ = self._git(["diff", "--name-only", "--diff-filter=U"],
                                         cwd=sdir)
                files = _path_names(uout) if rcu == 0 else []
                self._op_git(op, ["merge", "--abort"], cwd=sdir, label="merge --abort")
                if files:
                    return 1, ("Feature #%d conflicts with newer changes in %s: %s. "
                               "Ask in the feature chat to merge %s into the branch "
                               "and resolve it."
                               % (feat["n"], comp["label"], ", ".join(files),
                                  main_branch)), []
                return 1, (out + err).strip(), []
            created.append({"sha": self._head(sdir), "kind": "merge"})
        return 0, "", created

    @staticmethod
    def _head(repo):
        p = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else None

    # -- rollback op -----------------------------------------------------
    def _op_rollback(self, op, feat):
        affected = sorted({a["component"] for a in feat["applied"]})
        stage_base = self._stage(op, affected)
        try:
            stage_head, new_reverts = {}, {}
            for name in affected:
                comp = next(c for c in self.cfg.components if c["name"] == name)
                sdir = self.cfg.stage_dir(op.id, name)
                entries = [a for a in feat["applied"] if a["component"] == name]
                op.step("Roll back %s" % name)
                rc, err = self._rollback_component(op, feat, name, sdir, entries)
                if rc != 0:
                    op.step_end(False, detail=err)
                    raise RuntimeError(err.rstrip() or "rollback failed")
                op.step_end(True)
                op.step("Test %s" % name)
                test_cmd = [c.replace("{venv}", self.cfg.venv(name))
                            for c in comp["test"]]
                rc, out, err, _ = op.run(test_cmd, cwd=sdir, timeout=600,
                                         label="tests for %s" % name)
                if rc != 0:
                    op.step_end(False, detail=(out + err)[-3000:])
                    raise RuntimeError((out + err)[-3000:] or "tests failed")
                op.step_end(True)
                stage_head[name] = self._rev_parse(sdir, "HEAD")
                new_reverts[name] = self._created_shas(sdir, stage_base[name])
            self._promote(op, affected, stage_head, stage_base)
            # The feature is now off main: record it before install/restart so a
            # failure there still leaves it roll-backable / redeployable.
            with self._lock:
                feat = self.features[feat["n"]]
                feat["rollback"] = [{"component": n, "sha": s}
                                    for n, shas in new_reverts.items()
                                    for s in shas]
                feat["applied"] = []
                feat["status"] = "rolled_back"
                feat["rolled_back_at"] = _now()
                self._save_feature(feat)
            op.commits = new_reverts
            self._install(op, affected)
            self._restart_units(op, affected)
            new_sha = {name: self._rev_parse(self.cfg.main_checkout(name),
                        next(c["branch"] for c in self.cfg.components
                             if c["name"] == name)) for name in affected}
            op.remote = self._remote_info(affected, stage_base, new_sha)
        finally:
            self._cleanup_stages(op, affected)
        op.finish(True)
        op.write_json()

    def _rollback_component(self, op, feat, name, sdir, entries):
        for entry in reversed(entries):
            sha = entry["sha"]
            rc, _, _ = self._git(["rev-parse", "--verify", "--quiet", "%s^2" % sha],
                                 cwd=sdir)
            is_merge = rc == 0
            args = ["revert", "--no-edit"] + (["-m", "1", sha] if is_merge
                                              else [sha])
            rc, out, err, _ = self._op_git(op, args, cwd=sdir, commit=True,
                                     label="revert applied commit")
            if rc != 0:
                rcu, uout, _ = self._git(["diff", "--name-only", "--diff-filter=U"],
                                         cwd=sdir)
                files = _path_names(uout) if rcu == 0 else []
                self._op_git(op, ["revert", "--abort"], cwd=sdir, label="revert --abort")
                if files:
                    comp_branch = next(c["branch"] for c in self.cfg.components
                                       if c["name"] == name)
                    rc2, lout, _ = self._git(["log", "--format=%h %s",
                                              "%s..%s" % (sha, comp_branch), "--"] + files,
                                             cwd=sdir)
                    later = lout.strip() if rc2 == 0 else ""
                    return 1, ("Can't roll back #%d on its own: later changes "
                               "touch the same lines in %s (%s). Roll back the "
                               "later feature first."
                               % (feat["n"], ", ".join(files), later))
                return rc, (out + err).strip()
        return 0, ""

    def _created_shas(self, repo, base):
        rc, out, _ = self._git(["rev-list", base + "..HEAD"], cwd=repo)
        if rc != 0:
            return []
        # rev-list is newest-first; oldest first for rollback
        return list(reversed([l.strip() for l in out.splitlines() if l.strip()]))

    # -- discard op ------------------------------------------------------
    def _op_discard(self, op, feat):
        for comp in self.cfg.components:
            name = comp["name"]
            wt = self.cfg.worktree_dir(feat, name)
            if os.path.isdir(wt):
                self._op_git(op, ["worktree", "remove", "--force", wt],
                       cwd=self.cfg.main_checkout(name),
                       label="worktree remove --force %s" % name)
        base = feat["dir"]
        for fn in ("AGENTS.md", ".feature.json", "CHECKLIST.md"):
            p = os.path.join(base, fn)
            if os.path.exists(p):
                os.remove(p)
        try:
            if os.path.isdir(base) and not os.listdir(base):
                os.rmdir(base)
        except OSError as e:
            op.log("discard: could not rmdir %s: %s" % (base, e))
        with self._lock:
            feat = self.features[feat["n"]]
            feat["status"] = "discarded"
            self._save_feature(feat)
        op.remote = {"rev": None, "changed": []}
        op.finish(True)
        op.write_json()

    # -- sync op ---------------------------------------------------------
    def _run_sync(self, op):
        affected = [c["name"] for c in self.cfg.components]
        self._install(op, affected)
        self._restart_units(op, affected)
        comp = next((c for c in self.cfg.components if c.get("remote")), None)
        changed, rev = [], None
        if comp:
            repo = self.cfg.main_checkout(comp["name"])
            rev = self._rev_parse(repo, comp["branch"])
            rc, out, _ = self._git(["ls-files", "--", comp["remote"].rstrip("/")],
                                   cwd=repo)
            changed = sorted(_path_names(out) if rc == 0 else [])
        op.remote = {"rev": rev, "changed": changed}
        op.commits = {}
        op.finish(True)
        op.write_json()

    # ---- diffs ----------------------------------------------------------
    def _all_file_objs(self, per_component):
        out = []
        for comp in per_component:
            for f in comp["files"]:
                g = dict(f)
                g["component"] = comp["component"]
                out.append(g)
        return out

    def diff_pending(self, feat):
        files = []
        for comp in self.cfg.components:
            name = comp["name"]
            wt = self.cfg.worktree_dir(feat, name)
            repo = self.cfg.main_checkout(name)
            if not os.path.isdir(wt):
                continue
            mb = self._merge_base(repo, comp["branch"], feat["branch"])
            if mb is None:
                continue
            untracked = self._untracked(wt)
            rc, out, _ = self._git(["diff", "--name-only", mb], cwd=wt)
            tracked = set(_path_names(out) if rc == 0 else [])
            comp_files = []
            for f in sorted(tracked | untracked):
                if f in untracked:
                    rc2, pout, _ = self._git(["diff", "--no-index", "/dev/null", f],
                                             cwd=wt)
                    patch = pout if rc2 == 0 else ""
                    adds, dels = _count_adds_deletes(patch)
                else:
                    rc2, pout, _ = self._git(["diff", mb, "--", f], cwd=wt)
                    patch = pout if rc2 == 0 else ""
                    adds, dels = _count_adds_deletes(patch)
                comp_files.append({"file": f, "additions": adds,
                                   "deletions": dels, "patch": patch})
            if comp_files:
                files.append({"component": name, "files": comp_files})
        return self._all_file_objs(files)

    def diff_deployed(self, feat):
        files = []
        for comp in self.cfg.components:
            name = comp["name"]
            repo = self.cfg.main_checkout(name)
            comp_files = []
            for entry in feat.get("applied", []):
                if entry["component"] != name:
                    continue
                rc, out, _ = self._git(["diff", "%s^1" % entry["sha"], entry["sha"]],
                                       cwd=repo)
                if rc != 0:
                    continue
                for fname, fpatch in _split_diff(out):
                    adds, dels = _count_adds_deletes(fpatch)
                    comp_files.append({"file": fname, "additions": adds,
                                       "deletions": dels, "patch": fpatch})
            if comp_files:
                files.append({"component": name, "files": comp_files})
        return self._all_file_objs(files)

    # ---- drift / files --------------------------------------------------
    def drift(self):
        result = []
        for comp in self.cfg.components:
            name = comp["name"]
            repo = self.cfg.main_checkout(name)
            rev = self._rev_parse(repo, comp["branch"])
            for entry in comp.get("install", []):
                target = self.cfg.home_expand(entry["to"])
                actual = _sha256_file(target)
                data = _git_show_bytes(repo, rev, entry["from"])
                expected = hashlib.sha256(data).hexdigest() if data is not None else None
                if actual != expected:
                    result.append({"component": name, "to": target,
                                   "expected": expected, "actual": actual})
        return result

    def get_file(self, component, rev, path):
        names = {c["name"] for c in self.cfg.components}
        if component not in names:
            return None          # unknown component -> 404, don't touch the fs
        if not rev or not REV_RE.match(rev):
            return "BAD"
        if ".." in path or path.startswith("/") or "\x00" in path:
            return "BAD"
        repo = self.cfg.main_checkout(component)
        data = _git_show_bytes(repo, rev, path)
        return data  # None -> 404

    # ---- ops listing ----------------------------------------------------
    def list_ops(self, limit=50):
        # Every op on disk (sync ops too, and ops from before a restart), not just this process's.
        try:
            on_disk = [d for d in os.listdir(os.path.join(self.cfg.dir, "ops")) if OP_ID_RE.match(d)]
        except OSError:
            on_disk = []
        ids = list(set(self._ops_order) | set(on_disk))
        # newest first by started then id
        ids.sort(key=lambda i: (self._op_started(i), i), reverse=True)
        out = []
        for i in ids[:limit]:
            obj = self._read_op(i)
            if obj is not None:
                obj.pop("steps", None)
                out.append(obj)
        return out

    def _op_started(self, op_id):
        try:
            with open(os.path.join(self.cfg.dir, "ops", op_id, "op.json")) as fh:
                return json.load(fh).get("started", 0)
        except OSError:
            return 0

    def _read_op(self, op_id):
        try:
            with open(os.path.join(self.cfg.dir, "ops", op_id, "op.json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def get_op(self, op_id):
        if not OP_ID_RE.match(op_id or ""):
            return 404, {"error": "No such op"}
        obj = self._read_op(op_id)
        if obj is None:
            return 404, {"error": "No such op"}
        log = ""
        log_path = os.path.join(self.cfg.dir, "ops", op_id, "log.txt")
        if os.path.exists(log_path):
            with open(log_path) as fh:
                log = fh.read()
        return 200, {"op": obj, "log": log}


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
def make_handler(mgr):
    CFG = mgr.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "code-features/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, obj, raw=None, content_type="application/json"):
            body = raw if raw is not None else (json.dumps(obj) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _log_request(self, code, path, ms):
            try:
                sys.stderr.write("[%s] %s %s %d %.1fms\n"
                                 % (time.strftime("%H:%M:%S"), self.command,
                                    path, code, ms))
                sys.stderr.flush()
            except OSError:
                pass

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def do_HEAD(self):
            self._dispatch()

        def _dispatch(self):
            t0 = time.time()
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                code, obj, raw, ctype = self._route(parsed)
            except Exception as e:  # noqa
                code, obj, raw, ctype = 500, {"error": str(e)}, None, \
                    "application/json"
            self._log_request(code, path, (time.time() - t0) * 1000.0)
            self._send(code, obj if obj is not None else {}, raw=raw,
                       content_type=ctype)
            # (elapsed logged by the server; never send headers/secret)

        def _auth(self, path):
            if not path.startswith("/api/") or path == "/api/health":
                return True
            hdr = self.headers.get("Authorization", "")
            return hmac.compare_digest(
                hdr.encode(), ("Bearer " + CFG.token).encode())

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

        def _route(self, parsed):
            query = parse_qs(parsed.query)
            path = parsed.path
            method = self.command
            if not self._auth(path):
                return 401, {"error": "Unauthorized"}, None, "application/json"
            if path == "/api/health":
                return 200, mgr.health(), None, "application/json"
            if path == "/api/features" and method == "POST":
                return mgr.create(self._body().get("chat_id")) + (None, "application/json")
            if path == "/api/features" and method == "GET":
                return 200, {"features": mgr.list_features(query.get("chat", [None])[0])}, \
                    None, "application/json"
            if path == "/api/sync" and method == "POST":
                return mgr.propose_sync() + (None, "application/json")
            if path == "/api/drift":
                return 200, {"drift": mgr.drift()}, None, "application/json"
            if path == "/api/ops" and method == "GET":
                limit = int(query.get("limit", [50])[0]) if query.get("limit") else 50
                return 200, {"ops": mgr.list_ops(limit)}, None, "application/json"
            parts = [p for p in parsed.path.strip("/").split("/") if p != ""]
            # Drop a leading "api" mount segment, if present.
            if parts and parts[0] == "api" and len(parts) > 1:
                parts = parts[1:]
            if parts[:1] == ["features"]:
                return self._route_feature(parts[1:], query)
            if len(parts) == 2 and parts[0] == "checklist" and method == "GET":
                return mgr.checklist(parts[1]) + (None, "application/json")
            if len(parts) == 1 and parts[0] == "ops":
                return 404, {"error": "No such op"}, None, "application/json"
            if len(parts) == 2 and parts[0] == "ops":
                return mgr.get_op(parts[1]) + (None, "application/json")
            if len(parts) >= 2 and parts[:1] == ["files"]:
                data = mgr.get_file(parts[1], parts[2], "/".join(parts[3:]))
                if data is None:
                    return 404, {"error": "Not found"}, None, "application/octet-stream"
                if data == "BAD":
                    return 404, {"error": "Not found"}, None, "application/octet-stream"
                return 200, None, data, "application/octet-stream"
            return 404, {"error": "Not found"}, None, "application/json"

        def _route_feature(self, parts, query):
            try:
                n = int(parts[0])
            except ValueError:
                return 404, {"error": "No such feature"}, None, "application/json"
            if len(parts) == 1:
                if self.command == "GET":
                    res = mgr.get_feature(n)
                    if res is None:
                        return 404, {"error": "No such feature"}, None, "application/json"
                    return 200, {"feature": res[1]}, None, "application/json"
                return 405, {"error": "Method not allowed"}, None, "application/json"
            sub = parts[1]
            if sub == "diff":
                res = mgr.get_feature(n)
                if res is None:
                    return 404, {"error": "No such feature"}, None, "application/json"
                files = mgr.diff_deployed(res[0]) if query.get("what") == ["deployed"] \
                    else mgr.diff_pending(res[0])
                return 200, {"files": files}, None, "application/json"
            if len(parts) == 2 and sub == "ready":
                if self.command != "POST":
                    return 405, {"error": "Method not allowed"}, None, "application/json"
                b = self._body()
                return mgr.set_ready(n, b.get("title"), b.get("summary")) + \
                    (None, "application/json")
            if len(parts) == 2 and sub == "tests":
                if self.command != "POST":
                    return 405, {"error": "Method not allowed"}, None, "application/json"
                return mgr.set_tests(n, self._body()) + (None, "application/json")
            if len(parts) == 2 and sub == "deploy":
                return mgr.propose_deploy(n) + (None, "application/json")
            if len(parts) == 2 and sub == "rollback":
                return mgr.propose_rollback(n) + (None, "application/json")
            if len(parts) == 2 and sub == "discard":
                return mgr.propose_discard(n) + (None, "application/json")
            return 404, {"error": "Not found"}, None, "application/json"

    return Handler


def main(argv):
    global CFG
    cfg = load_config_from_env()
    CFG = cfg
    os.makedirs(os.path.join(cfg.dir, "features"), exist_ok=True)
    os.makedirs(os.path.join(cfg.dir, "ops"), exist_ok=True)
    os.makedirs(os.path.join(cfg.dir, "stage"), exist_ok=True)
    mgr = Features(cfg)
    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(mgr))
    print("code-features listening on 0.0.0.0:%d" % cfg.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.shutdown()


if __name__ == "__main__":
    main(sys.argv)
