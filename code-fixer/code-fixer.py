#!/usr/bin/env python3
"""code-fixer.py -- the bug fixer service for Code mode.

When a model in a chat hits a bug it files a report here. The service records
the incident, then -- one at a time, because the GPU router has a single slot --
spawns a fresh opencode session ("Ornith", the fixer) on this machine with the
chat context and service logs, fixes the bug if it is in scope, deploys the
fix, and publishes a live timeline to a signed viewer page plus a bug-log page.

Python 3.12 standard library only. See BRIEF.md for the full contract.

URLs served:

    POST /api/report                      file a report (Bearer FIXER_TOKEN)
    GET  /f/<id>/<sig>/                   the live viewer page
    GET  /f/<id>/<sig>/events             server-sent events (replay + live)
    GET  /f/<id>/<sig>/diff               diff.patch as text
    GET  /log/<sig>/                      the bug log page
    GET  /log/<sig>/incidents             bug log as JSON (same sig)
    GET  /api/incidents/<id>              incident record (Bearer)
    GET  /api/incidents/<id>/wait         block until done (Bearer)
    GET  /api/active?chat=<id>            newest unfinished incident (Bearer)
    GET  /api/pick                        the model a report filed now would run on (Bearer)
    POST /api/incidents/<id>/cancel       stop the fixer (or Claude after it): verdict "cancelled" (Bearer)
    POST /api/incidents/<id>/escalate     hand a finished report to Claude (the "Ask Claude" button) (Bearer)

  Escalation to Claude (2026-10-26). A fixer verdict of could_not_fix / needs_human does not close the
  report: it stays open as status "escalated" (the chat keeps waiting) until the code-claude runner on
  cryoai claims it, streams Claude's work into it and resolves it with Claude's verdict. Unclaimed after
  FIXER_ESCALATE_CLAIM_S, or silent after FIXER_ESCALATE_IDLE_S, it closes with the fixer's verdict.
    GET  /api/escalations                 reports waiting for Claude (Bearer)
    POST /api/incidents/<id>/claim        {"runner"}: the runner takes it (Bearer)
    GET  /api/incidents/<id>/file/<name>  an evidence file for the runner (Bearer)
    POST /api/incidents/<id>/events       {"events": [...]}: Claude's steps, as kind "claude" (Bearer)
    POST /api/incidents/<id>/resolve      Claude's result.json: closes the report (Bearer)
"""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Constants & configuration
# --------------------------------------------------------------------------- #

CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

DEFAULT_FRAME_ANCESTORS = "http://cryoai:8080 http://cryoai:8080"
CSP = ("default-src 'self' 'unsafe-inline'; connect-src 'self'; "
       "frame-ancestors " + DEFAULT_FRAME_ANCESTORS)

DEFAULT_DIR = "/home/coder/fixes"
DEFAULT_PROJECTS = "/home/coder/projects"
DEFAULT_PORT = 4098
DEFAULT_MODEL = "slerm/ornith-1.5-35b-a3b"
# The slerm router has one model slot and switching it costs minutes each way (and the chat's prompt cache), so
# when the router already has one of these coding models loaded the fixer runs on it instead of FIXER_MODEL.
DEFAULT_ROUTER_MODELS = "http://slerm-head:8090/v1/models"
DEFAULT_CAPABLE_MODELS = ("ornith-1.5-35b-a3b,qwen3.8-27b,qwen3.8-27b-turbo,qwen3.6-35b-a3b-ud-q4kxl,"
                          "kat-coder-v2.5-dev")
DEFAULT_OPENCODE = "http://127.0.0.1:4096"
DEFAULT_LOGS_CMD = "sudo -n /usr/local/sbin/code-mode-logs 30"
DEFAULT_RESTART_CMD = "sudo -n /usr/bin/systemctl restart code-preview"
OPENCODE_CREDENTIALS = os.environ.get("FIXER_OPENCODE_CREDENTIALS",
                                      "/home/coder/.config/opencode/service.json")
OPENCODE_USER = "opencode"

END_TYPES = ("session.execution.succeeded",
             "session.execution.failed",
             "session.execution.interrupted")

# Config: env vars override the file.
def _load_env_file(path):
    data = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                data[key.strip()] = val.strip().strip("'").strip('"')
    except OSError:
        pass
    return data


_env = _load_env_file("/etc/code-mode/fixer.env")
FIXER_TOKEN = os.environ.get("FIXER_TOKEN") or _env.get("FIXER_TOKEN")
FIXER_SECRET = os.environ.get("FIXER_SECRET") or _env.get("FIXER_SECRET")
FIXER_DIR = os.environ.get("FIXER_DIR", DEFAULT_DIR)
FIXER_PROJECTS = os.environ.get("FIXER_PROJECTS", DEFAULT_PROJECTS)
FIXER_PORT = int(os.environ.get("FIXER_PORT", DEFAULT_PORT))
FIXER_MODEL = os.environ.get("FIXER_MODEL", DEFAULT_MODEL)
FIXER_ROUTER_MODELS = os.environ.get("FIXER_ROUTER_MODELS", DEFAULT_ROUTER_MODELS)
FIXER_CAPABLE_MODELS = [m.strip() for m in
                        os.environ.get("FIXER_CAPABLE_MODELS", DEFAULT_CAPABLE_MODELS).split(",") if m.strip()]
OPENCODE_URL = os.environ.get("FIXER_OPENCODE_URL", DEFAULT_OPENCODE)
LOGS_CMD = os.environ.get("FIXER_LOGS_CMD", DEFAULT_LOGS_CMD)
RESTART_CMD = os.environ.get("FIXER_RESTART_CMD", DEFAULT_RESTART_CMD)
RESTART_LOG = os.environ.get("FIXER_RESTART_LOG", "")
FIXER_MAX_MINUTES = float(os.environ.get("FIXER_MAX_MINUTES", "45"))
ESCALATE = (os.environ.get("FIXER_ESCALATE") or _env.get("FIXER_ESCALATE") or "1").lower() not in ("0", "false", "no", "off")
ESCALATE_VERDICTS = ("could_not_fix", "needs_human")
ESCALATE_CLAIM_S = float(os.environ.get("FIXER_ESCALATE_CLAIM_S", "180"))
ESCALATE_IDLE_S = float(os.environ.get("FIXER_ESCALATE_IDLE_S", "900"))
ESCALATE_MAX_S = float(os.environ.get("FIXER_ESCALATE_MAX_S", "3600"))
WATCHDOG_S = float(os.environ.get("FIXER_WATCHDOG_S", "15"))
VALID_VERDICTS = ("fixed", "not_a_bug", "needs_human", "could_not_fix")
RESULT_FIELDS = ("verdict", "summary", "root_cause", "changed", "tests", "restart", "for_the_chat")
EVIDENCE_FILES = ("report.json", "context.md", "session.md", "logs.txt", "diff.patch", "result.json", "incident.json")


def _require_secret():
    if not FIXER_SECRET:
        sys.stderr.write("code-fixer: FIXER_SECRET is not set; refusing to start\n")
        sys.exit(2)
    if not FIXER_TOKEN:
        sys.stderr.write("code-fixer: FIXER_TOKEN is not set; refusing to start\n")
        sys.exit(2)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def now_sec():
    return int(time.time())


def split_model(spec):
    if "/" in spec:
        prov, mid = spec.split("/", 1)
        return prov or "slerm", mid
    return "slerm", spec


def pick_model_info():
    """(provider, model id, loaded) for a fixer run: the router's loaded (or loading) model when it is one of
    FIXER_CAPABLE_MODELS (loaded True), else FIXER_MODEL (loaded False: the run loads it; None when the router
    could not be asked). A plain request: opencode's password is not for the gateway."""
    provider, model_id = split_model(FIXER_MODEL)
    if not FIXER_ROUTER_MODELS:
        return provider, model_id, None
    try:
        with urllib.request.urlopen(FIXER_ROUTER_MODELS, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data") or []
        for m in data:
            if (m.get("slerm") or {}).get("status") in ("loaded", "loading") \
                    and m.get("id") in FIXER_CAPABLE_MODELS:
                return provider, m["id"], True
    except Exception:
        return provider, model_id, None
    return provider, model_id, False


def pick_model():
    provider, model_id, _ = pick_model_info()
    return provider, model_id


def viewer_sig(incident_id):
    return hmac.new(FIXER_SECRET.encode(), ("f:" + incident_id).encode(),
                    hashlib.sha256).hexdigest()[:32]


def log_sig():
    return hmac.new(FIXER_SECRET.encode(), b"log", hashlib.sha256).hexdigest()[:32]


def mask_sig(raw_path):
    """Replace the HMAC sig in a request path with *** for the access log."""
    p = raw_path.split("?", 1)[0]
    parts = p.split("/")
    if len(parts) >= 4 and parts[1] == "f" and parts[3]:
        parts[3] = "***"
        return "/".join(parts)
    if len(parts) >= 3 and parts[1] == "log" and parts[2]:
        parts[2] = "***"
        return "/".join(parts)
    return p


class CredentialsError(Exception):
    """Raised when opencode credentials cannot be loaded. Never falls back to
    a guessed password -- the incident is refused instead."""


def load_opencode_password():
    """The opencode Basic-auth password, read from the credentials file.
    Raises CredentialsError if the file can't be read or has no password."""
    try:
        with open(OPENCODE_CREDENTIALS) as fh:
            pw = json.load(fh).get("password")
    except OSError as exc:
        raise CredentialsError(
            "could not read opencode credentials file %s: %s"
            % (OPENCODE_CREDENTIALS, exc))
    if not pw:
        raise CredentialsError(
            "opencode credentials file %s has no password" % OPENCODE_CREDENTIALS)
    return pw


def opencode_auth_header():
    pw = load_opencode_password()
    token = base64.b64encode(("%s:%s" % (OPENCODE_USER, pw)).encode()).decode()
    return {"Authorization": "Basic " + token}


def opencode_headers():
    h = opencode_auth_header()
    h["Accept"] = "text/event-stream"
    return h


def ep(path):
    return OPENCODE_URL.rstrip("/") + path


def http_json(method, url, body=None, headers=None, timeout=None):
    data = None
    hdrs = dict(headers or {})
    # Every call to the opencode server carries its Basic-auth header. Callers
    # may still add headers (e.g. Content-Type is added below), but the auth
    # header must always be present so opencode doesn't 401 the request.
    if "Authorization" not in hdrs:
        hdrs.update(opencode_auth_header())
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def run_cmd(cmd):
    """Run a shell command; return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace"), \
            p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        return 124, out.decode("utf-8", "replace"), "[timed out]"
    except OSError as exc:
        return 127, "", str(exc)


def capture_logs():
    code, out, err = run_cmd(LOGS_CMD)
    if code != 0:
        tail = (err or "").strip()[:500]
        out = (out or "") + ("\n\n[logs command failed (exit %d): %s]\n"
                             % (code, tail)) if tail else \
             (out or "") + ("\n\n[logs command failed (exit %d)]\n" % code)
    return out or "[no logs]\n"


def tool_summary(name, inp):
    """A short, human-readable summary of a tool invocation for the timeline."""
    if not isinstance(inp, dict):
        return (name or "tool")
    # shell / command style tools
    for key in ("command", "cmd", "shell", "cmdline"):
        if key in inp:
            val = inp[key]
            if isinstance(val, list):
                val = " ".join(str(v) for v in val)
            return (str(val)[:120])
    # desktop / browser style tools: name + first argument
    for key in ("args", "query", "text", "url", "target"):
        if key in inp:
            return "%s %s" % (name, str(inp[key])[:120])
    # fall back to a compact representation
    try:
        return json.dumps(inp, ensure_ascii=False)[:120]
    except (TypeError, ValueError):
        return str(inp)[:120]


def tool_output(content):
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def permission_what(data):
    action = data.get("action")
    resources = data.get("resources")
    if isinstance(resources, list):
        r = ", ".join(str(x) for x in resources)[:120]
        return "%s: %s" % (action, r) if action else r
    return str(action) if action else "permission"


def render_context(context):
    """Render the chat context (list of {role, content}) as readable markdown."""
    if not context:
        return "(no chat context provided)\n"
    blocks = []
    for part in context:
        if not isinstance(part, dict):
            continue
        role = str(part.get("role", "message"))
        content = part.get("content", "")
        if isinstance(content, list):
            chunks = []
            for c in content:
                if isinstance(c, dict):
                    chunks.append(c.get("text", ""))
                else:
                    chunks.append(str(c))
            content = "\n".join(chunks)
        blocks.append("## %s\n\n%s\n" % (role, content if content else ""))
    return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #

class Incident:
    def __init__(self, incident_id, record):
        self.id = incident_id
        self.record = record
        self.dir = os.path.join(FIXER_DIR, incident_id)
        self.events = []
        self.subscribers = {}          # handler -> queue.Queue of bytes
        self.sub_lock = threading.Lock()
        self.cond = threading.Condition()
        self.session_id = None
        self.session_ran = False
        self.start_sec = None


incidents = {}
incidents_lock = threading.Lock()
work_queue = queue.Queue()


def index_path():
    return os.path.join(FIXER_DIR, "index.jsonl")


def index_entry_from_record(rec):
    return {
        "id": rec["id"],
        "created": rec.get("created"),
        "title": rec.get("title"),
        "reporter": rec.get("reporter_model"),
        "chat_id": rec.get("chat_id"),
        "source": rec.get("source"),
        "status": rec.get("status"),
        "verdict": rec.get("verdict"),
        "duration": rec.get("duration"),
        "view": rec.get("view"),
    }


def append_index(entry):
    os.makedirs(FIXER_DIR, exist_ok=True)
    with open(index_path(), "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def update_index(incident):
    path = index_path()
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    out = []
    replaced = False
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            out.append(ln)
            continue
        if isinstance(e, dict) and e.get("id") == incident.id:
            out.append(json.dumps(index_entry_from_record(incident.record)))
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(json.dumps(index_entry_from_record(incident.record)))
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(out) + ("\n" if out else ""))
    os.replace(tmp, path)


def record_event(incident, event):
    """Append to events.jsonl, memory, and push to live viewers + waiters."""
    event = dict(event)
    event["t"] = now_sec()
    # events.jsonl stays raw JSON (one compact object per line) for readers
    # that parse it directly.
    line = (json.dumps(event) + "\n").encode("utf-8")
    # Live viewers get SSE framing: a "data: " line followed by a blank line,
    # which is exactly what a browser's EventSource dispatches an event on.
    sse = b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
    with incident.cond:
        incident.events.append(event)
        try:
            with open(os.path.join(incident.dir, "events.jsonl"), "ab") as fh:
                fh.write(line)
        except OSError:
            pass
        for q in list(incident.subscribers.values()):
            try:
                q.put(sse)
            except Exception:
                pass
        incident.cond.notify_all()


def close_sse(incident):
    with incident.sub_lock:
        for q in list(incident.subscribers.values()):
            try:
                q.put(None)
            except Exception:
                pass


def new_incident(report):
    incident_id = time.strftime("%Y%m%d-%H%M-") + secrets.token_hex(2)
    sig = viewer_sig(incident_id)
    record = {
        "id": incident_id,
        "chat_id": report.get("chat_id"),
        "source": report.get("source"),
        "reporter_model": report.get("reporter_model"),
        "reporter": report.get("reporter_model"),
        "title": report.get("title"),
        "details": report.get("details"),
        "created": now_sec(),
        "started": None,
        "done": None,
        "status": "queued",
        "verdict": None,
        "summary": None,
        "root_cause": None,
        "changed": [],
        "tests": None,
        "restart": [],
        "restart_outcome": None,
        "for_the_chat": None,
        "view": "/f/%s/%s/" % (incident_id, sig),
        "sig": sig,
    }
    return Incident(incident_id, record)


# --------------------------------------------------------------------------- #
# Evidence files (step 1 of the worker)
# --------------------------------------------------------------------------- #

def write_evidence(incident):
    os.makedirs(incident.dir, exist_ok=True)
    report = incident.report
    with open(os.path.join(incident.dir, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    with open(os.path.join(incident.dir, "context.md"), "w") as fh:
        fh.write(render_context(report.get("context", [])))
    with open(os.path.join(incident.dir, "logs.txt"), "w") as fh:
        fh.write(capture_logs())
    with open(os.path.join(incident.dir, "session.md"), "w") as fh:
        fh.write(capture_session(incident))


def find_chat_session(chat_id):
    """GET /api/session?directory=... and return the session whose
    metadata.owui_chat_id matches, else None."""
    try:
        url = ep("/api/session?directory=%s" % urllib.parse.quote(
            os.path.join(FIXER_PROJECTS, "chats", chat_id)))
        data = http_json("GET", url, timeout=15).get("data", [])
    except (urllib.error.URLError, ValueError, KeyError):
        return None
    for sess in data:
        md = (sess.get("metadata") or {})
        if md.get("owui_chat_id") == chat_id:
            return sess.get("id")
    # Fall back: first session in the directory listing.
    if data:
        return data[0].get("id")
    return None


def get_messages(session_id):
    data = http_json("GET", ep("/api/session/%s/message" % session_id),
                     timeout=15).get("data", [])
    if not isinstance(data, list):
        return []
    data.sort(key=lambda m: (m.get("time", {}) or {}).get("created", 0))
    return data


def message_steps(messages):
    """Flatten messages into readable 'steps': tool calls with short output and
    what it said. Returns a list of markdown-ish step strings."""
    steps = []
    for msg in messages:
        mtype = msg.get("type")
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text = (part.get("text") or "").strip()
                if text:
                    steps.append(text[:500])
            elif ptype == "tool":
                name = part.get("name", "tool")
                state = part.get("state") or {}
                inp = state.get("input")
                out = state.get("content")
                out_txt = tool_output(out) if out else ""
                summary = tool_summary(name, inp)
                tail = ("\n    \"\"\"\n%s\n    \"\"\"" % out_txt[:600]
                        if out_txt else "")
                steps.append("`%s`: %s%s" % (name, summary, tail))
            elif ptype == "reasoning":
                text = (part.get("text") or "").strip()
                if text:
                    steps.append(">_ " + text[:200])
    return steps


def capture_session(incident):
    """The chat's coding session, whoever filed the report: a Code-mode report comes from that very session, and
    a Chat-mode or user report may come from a chat that also had Code turns. No chat folder = no session."""
    report = incident.report
    chat_id = report.get("chat_id")
    try:  # evidence must never stop an incident (unreadable opencode credentials are refused right after this)
        sess_id = find_chat_session(chat_id)
    except Exception as exc:
        return "(could not look up the coding session for chat %s: %s)\n" % (chat_id, exc)
    if not sess_id:
        return "(no coding session for chat %s: it has had no Code turns)\n" % chat_id
    try:
        messages = get_messages(sess_id)
    except Exception as exc:
        return "(could not read session %s: %s)\n" % (sess_id, exc)
    steps = message_steps(messages)
    last = steps[-40:]
    if not last:
        return "(session %s carried no tool calls or text)\n" % sess_id
    return "\n\n".join("### step %d\n%s" % (len(steps) - 40 + i + 1, s)
                       for i, s in enumerate(last))


# --------------------------------------------------------------------------- #
# Fixer prompt
# --------------------------------------------------------------------------- #

def build_prompt(incident):
    r = incident.report
    iid = incident.id
    if r.get("source") == "user":
        # The user pressed Fixer in the chat's pill: there is no reporting model, and they may not say what is wrong.
        opening = ("You are the bug fixer for Code mode on the AI-sandbox VM. The user opened you from one of "
                   "their chats to look into a problem there.\n"
                   "Title: %s\n"
                   "What the user wrote: %s\n"
                   "Chat %s.\n" % (
                       r.get("title", ""),
                       r.get("details") or "(nothing: find the problem yourself in context.md and session.md - a "
                       "tool that errored, a step that failed, something the model said it could not do)",
                       r.get("chat_id", "")))
        for_the_chat = "what the user should do now, in plain words: the user reads this, not a model"
    else:
        opening = ("You are the bug fixer for Code mode on the AI-sandbox VM. A model in a "
                   "chat reported a bug.\n"
                   "Title: %s\n"
                   "Details: %s\n"
                   "Reporter: %s (%s mode), chat %s.\n" % (
                       r.get("title", ""), r.get("details", ""), r.get("reporter_model", ""),
                       r.get("source", ""), r.get("chat_id", "")))
        for_the_chat = "what the reporting model should do now, e.g. retry the failed command"
    return opening + (
        "Evidence in %s/: report.json, context.md (the chat), "
        "session.md (the coding session's recent steps, if any), logs.txt "
        "(service logs, secrets masked).\n"
        "You may change only: the chat's project (/home/coder/projects/chats/%s, "
        "if it exists); the desktop tool "
        "(/home/coder/projects/desktop-cli - after its tests pass, deploy with "
        "`install -m 0755 desktop /home/coder/.local/bin/desktop`); the preview "
        "server (/home/coder/projects/code-preview - after its tests pass, copy "
        "code-preview.py to /home/coder/.local/lib/code-mode/code-preview.py and "
        'put "code-preview" in restart).\n'
        "Never change anything else, never use sudo, never use rm -rf, git reset "
        "or git clean.\n"
        "1. Reproduce the problem. 2. Find the root cause. 3. If it is a real "
        "bug in scope: fix it, add or update a test, run the test suite, "
        "commit, deploy. 4. If it is not a bug, or the cause is outside your "
        "scope (Open WebUI, the slerm router, other machines), change nothing "
        "and explain precisely what is wrong and where.\n"
        "5. Write %s/result.json:\n"
        '{"verdict": "fixed"|"not_a_bug"|"needs_human"|"could_not_fix", '
        '"summary": "...", "root_cause": "...", "changed": [{"path": "...", '
        '"why": "..."}], "tests": "...", "restart": [], '
        '"for_the_chat": "%s"}.\n'
        "6. Finish with a two-sentence summary." % (
            incident.dir, r.get("chat_id", ""), incident.dir, for_the_chat)
    )


# --------------------------------------------------------------------------- #
# The fixer worker: create session, send prompt, follow events
# --------------------------------------------------------------------------- #

def session_matches(incident, payload):
    data = payload.get("data", {}) or {}
    if data.get("sessionID") == incident.session_id:
        return True
    form = data.get("form")
    if isinstance(form, dict) and form.get("sessionID") == incident.session_id:
        return True
    return False


def _public_record(rec):
    """A copy of the incident record safe to publish over the API: keeps the
    fields the Open WebUI side reads (status, verdict, summary, ...) plus the
    `view` link, but drops the internal `sig`."""
    out = dict(rec)
    out.pop("sig", None)
    return out


def session_active(sid):
    """True if /api/session/active still lists this session, i.e. the run has
    not been ended server-side. If we cannot tell, assume it is still running."""
    try:
        data = http_json("GET", ep("/api/session/active"), timeout=10)
    except Exception:
        return True
    if not isinstance(data, dict):
        return True

    def ids_of(d):
        out = set()
        if isinstance(d, list):
            for s in d:
                if isinstance(s, dict):
                    out.add(s.get("id"))
        elif isinstance(d, dict):
            out.add(d.get("id"))
        elif isinstance(d, str):
            out.add(d)
        return out

    found = ids_of(data.get("data")) if "data" in data else set()
    return sid in (found | ids_of(data))


def _stop_after_max(incident):
    """The overall FIXER_MAX_MINUTES cap was hit: interrupt the running
    session and finalize the incident as could_not_fix."""
    sid = incident.session_id
    msg = "Stopped after %s minutes" % FIXER_MAX_MINUTES
    try:
        http_json("POST", ep("/api/session/%s/interrupt" % sid),
                  body={}, timeout=15)
    except Exception:
        pass
    record_event(incident, {"kind": "status", "text": msg})
    incident.record["status"] = "done"
    incident.record["verdict"] = "could_not_fix"
    incident.record["summary"] = msg
    finalize(incident, load_result=False)


def read_result(path, fallback_text):
    """Read result.json; if missing/invalid, return a could_not_fix record."""
    try:
        with open(path) as fh:
            result = json.load(fh)
        if not isinstance(result, dict):
            raise ValueError("result.json is not an object")
        valid = {"fixed", "not_a_bug", "needs_human", "could_not_fix"}
        if result.get("verdict") not in valid:
            raise ValueError("unknown verdict")
        return result
    except Exception:
        return {
            "verdict": "could_not_fix",
            "summary": (fallback_text or "The fixer produced no result.json."),
            "root_cause": "",
            "changed": [],
            "tests": "",
            "restart": [],
            "for_the_chat": "",
        }


def capture_diff(incident):
    """Record every git commit made since the incident started in the projects
    repos, and save their patches to diff.patch."""
    created = incident.record.get("created", 0)
    since = datetime.fromtimestamp(max(0, created - 10), tz=timezone.utc).isoformat()
    patches = []
    try:
        for name in sorted(os.listdir(FIXER_PROJECTS)):
            repo = os.path.join(FIXER_PROJECTS, name)
            if not os.path.isdir(repo) or name.startswith("."):
                continue
            if not os.path.isdir(os.path.join(repo, ".git")):
                continue
            cmd = [
                "git", "-C", repo, "log", '--since=%s' % since,
                '--pretty=format:commit %H%n authored-by %an%n    %s',
                "--stat", "--patch",
            ]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                patches.append("[git log failed: %s]\n" % exc)
                continue
            if res.returncode == 0 and res.stdout.strip():
                patches.append("# %s\n%s" % (name, res.stdout))
            elif res.stderr.strip():
                patches.append("# %s\n[git log failed: %s]" % (name, res.stderr.strip()[:200]))
    except OSError as exc:
        patches.append("[diff capture failed: %s]\n" % exc)
    with open(os.path.join(incident.dir, "diff.patch"), "w") as fh:
        fh.write("\n".join(patches) + ("\n" if patches else ""))


def process_incident(incident):
    """Run one incident to completion: evidence, fixer session, finalize."""
    incident.report = _report_cache.get(incident.id)
    if incident.record.get("status") != "queued":   # cancelled while it waited in the queue
        return
    incident.start_sec = now_sec()
    incident.record["status"] = "investigating"
    incident.record["started"] = incident.start_sec

    # Step 1: evidence files.
    write_evidence(incident)

    # Step 2: authenticate to opencode before we start the run. If the
    # credentials file can't be read, log it and refuse the incident rather
    # than falling back to a guessed password.
    try:
        load_opencode_password()
    except CredentialsError as exc:
        sys.stderr.write("code-fixer: %s; refusing incident %s\n"
                         % (exc, incident.id))
        sys.stderr.flush()
        record_event(incident, {"kind": "status", "text": "Failed to start fixer"})
        incident.record["status"] = "done"
        incident.record["verdict"] = "could_not_fix"
        incident.record["summary"] = "Could not start the fixer: %s" % exc
        finalize(incident)
        return

    # Step 3: create the fixer session and send the prompt.
    provider, model_id = pick_model()
    incident.record["fixer_model"] = model_id
    record_event(incident, {"kind": "status", "text": "Investigating with %s" % model_id})
    try:
        sid_resp = http_json("POST", ep("/api/session"), body={
            "model": {"id": model_id, "providerID": provider},
            "location": {"directory": FIXER_PROJECTS},
            "metadata": {"fixer_incident": incident.id},
        }, timeout=30)
        incident.session_id = sid_resp["data"]["id"]
        incident.session_ran = True
        if incident.record.get("cancelled"):   # Cancel came while the session was being made
            record_event(incident, {"kind": "status", "text": "Cancelled"})
            finalize(incident, load_result=False)
            return
    except Exception as exc:
        record_event(incident, {"kind": "status", "text": "Failed to start fixer"})
        incident.record["status"] = "done"
        incident.record["verdict"] = "could_not_fix"
        incident.record["summary"] = "Could not start the fixer: %s" % exc
        finalize(incident)
        return

    sid = incident.session_id
    stop = threading.Event()
    raw = queue.Queue()
    resp_holder = {}

    def read_stream_once():
        """Subscribe to /api/event and push payloads into `raw` until the
        stream ends. No per-read timeout: a real fixer can be silent for
        minutes (e.g. while running a test suite)."""
        req = urllib.request.Request(ep("/api/event"),
                                     headers=opencode_headers(), method="GET")
        with urllib.request.urlopen(req, timeout=None) as resp:
            resp_holder["resp"] = resp
            for line in resp:
                if stop.is_set():
                    return
                line = line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                try:
                    raw.put(json.loads(line[5:].strip()))
                except ValueError:
                    continue

    def reader():
        # Reconnect on drops (network blips, server restarts) until told to
        # stop; each drop is signalled to the main loop via a marker.
        while not stop.is_set():
            try:
                read_stream_once()
            except Exception as exc:
                if stop.is_set():
                    break
                raw.put({"__dropped__": str(exc)})
            time.sleep(0.5)

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    time.sleep(0.3)  # subscribe BEFORE sending the prompt

    # Send the prompt (now that we are subscribed).
    try:
        http_json("POST", ep("/api/session/%s/prompt" % sid),
                  body={"text": build_prompt(incident)}, timeout=30)
    except Exception as exc:
        record_event(incident, {"kind": "status", "text": "Prompt failed"})
        incident.record["summary"] = "Prompt failed: %s" % exc

    ended = False
    reasoning_start = None
    current = {}  # tool id -> {name, summary}
    deadline = time.time() + max(0.0, FIXER_MAX_MINUTES) * 60

    while not ended:
        if time.time() > deadline:
            stop.set()
            _stop_after_max(incident)
            return
        try:
            payload = raw.get(timeout=1)
        except queue.Empty:
            # Silent for a while: a fixer running a test suite is quiet for
            # minutes. Keep waiting until an event arrives or the cap hits.
            continue
        if isinstance(payload, dict) and payload.get("__dropped__"):
            # The stream dropped; the reader is reconnecting. If the session is
            # no longer active on opencode's side, treat the run as finished.
            if not session_active(sid):
                record_event(incident, {"kind": "status",
                                        "text": "Session ended"})
                ended = True
                break
            continue
        ev_type = payload.get("type")
        data = payload.get("data", {}) or {}
        if not session_matches(incident, payload):
            continue

        if ev_type == "session.reasoning.started":
            reasoning_start = time.time()
            record_event(incident, {"kind": "thinking", "state": "start"})
        elif ev_type == "session.reasoning.ended":
            secs = int(time.time() - reasoning_start) if reasoning_start else 0
            record_event(incident, {"kind": "thinking", "state": "end",
                                    "seconds": secs})
            reasoning_start = None
        elif ev_type == "session.text.ended":
            text = (data.get("text") or "").strip()
            if text:
                incident.record["_last_text"] = text
                record_event(incident, {"kind": "say", "text": text})
        elif ev_type == "session.tool.input.started":
            tid = data.get("id")
            if tid is not None:
                current[tid] = {"name": data.get("name"), "summary": ""}
        elif ev_type == "session.tool.called":
            tid = data.get("id")
            info = current.get(tid, {"name": None, "summary": ""})
            summ = tool_summary(info.get("name"), data.get("input"))
            info["summary"] = summ
            current[tid] = info
            record_event(incident, {"kind": "tool", "id": tid,
                                    "name": info.get("name"),
                                    "summary": summ, "state": "running"})
        elif ev_type == "session.tool.success":
            tid = data.get("id")
            info = current.get(tid, {"name": None, "summary": ""})
            out = tool_output(data.get("content"))
            record_event(incident, {"kind": "tool", "id": tid,
                                    "name": info.get("name"),
                                    "summary": info.get("summary", ""),
                                    "state": "ok",
                                    "output": out[:4000]})
        elif ev_type == "session.tool.failed":
            tid = data.get("id")
            info = current.get(tid, {"name": None, "summary": ""})
            err = data.get("error", {}) or {}
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            record_event(incident, {"kind": "tool", "id": tid,
                                    "name": info.get("name"),
                                    "summary": info.get("summary", ""),
                                    "state": "failed", "output": str(msg)[:4000]})
        elif ev_type == "permission.asked":
            tid = data.get("id")
            what = permission_what(data)
            try:
                http_json("POST", ep("/api/session/%s/permission/%s/reply"
                          % (sid, tid)), body={
                    "decision": "reject",
                    "message": "Not allowed in the fixer; find another way.",
                }, timeout=15)
            except Exception as exc:
                incident.record["_last_text"] = \
                    (incident.record.get("_last_text") or "") + \
                    "\n[permission reply failed: %s]" % exc
            record_event(incident, {"kind": "permission", "what": what,
                                    "decision": "refused"})
        elif ev_type == "form.created":
            form = data.get("form") or {}
            fid = form.get("id")
            fields = form.get("fields", []) or []
            answers = {f.get("key"): "Use your best judgement."
                       for f in fields if f.get("key")}
            try:
                http_json("POST", ep("/api/session/%s/form/%s/reply"
                          % (sid, fid)), body={"answer": answers}, timeout=15)
            except Exception:
                pass
        elif ev_type in END_TYPES:
            ended = True

    # Tear down the stream.
    stop.set()
    resp = resp_holder.get("resp")
    if resp:
        try:
            resp.close()
        except Exception:
            pass
    rt.join(timeout=5)

    # Step 4: finalize.
    finalize(incident)


_report_cache = {}


def finalize(incident, load_result=True):
    rec = incident.record
    # Only read result.json when the fixer session actually ran. A run that
    # failed before the session started (e.g. opencode 401) already has a
    # meaningful summary; don't let a missing result.json overwrite it.
    if load_result and getattr(incident, "session_ran", False):
        last_text = rec.get("_last_text") or ""
        result = read_result(os.path.join(incident.dir, "result.json"),
                             last_text)
        for key in ("verdict", "summary", "root_cause", "changed", "tests",
                    "restart", "for_the_chat"):
            if key in result:
                rec[key] = result[key]
    if rec.get("cancelled"):
        rec["verdict"] = "cancelled"
        rec["summary"] = "Cancelled by you while the fixer was working."
        rec["restart"] = []
    rec["status"] = "done"
    rec["done"] = now_sec()
    rec["duration"] = rec["done"] - (rec.get("started") or rec.get("created")
                                     or rec["done"])

    # Restart the preview server only if the fixer asked for it.
    if "code-preview" in (rec.get("restart") or []):
        code, out, err = run_cmd(RESTART_CMD)
        rec["restart_outcome"] = ("ok (exit %d)" % code if code == 0
                                  else "exit %d: %s%s" % (code, out, err))
        if RESTART_LOG:
            try:
                with open(RESTART_LOG, "a") as fh:
                    fh.write("restart called: code-preview -> %s\n"
                             % rec["restart_outcome"])
            except OSError:
                pass
    else:
        rec["restart_outcome"] = "not requested"

    capture_diff(incident)

    # Clean up internal-only bookkeeping keys from the published record.
    rec.pop("_last_text", None)

    if should_escalate(rec):
        hand_to_claude(incident, "auto")
        return
    close_incident(incident)


def save_record(incident):
    with incidents_lock:
        incidents[incident.id] = incident
    update_index(incident)
    try:
        with open(os.path.join(incident.dir, "incident.json"), "w") as fh:
            json.dump(incident.record, fh, indent=2)
    except OSError:
        pass


def close_incident(incident):
    """The report is over (status done already set): publish it, tell viewers and waiters."""
    save_record(incident)
    record_event(incident, {"kind": "done", "record": incident.record})
    close_sse(incident)


# --------------------------------------------------------------------------- #
# Escalation to Claude and cancelling
# --------------------------------------------------------------------------- #

escalation_lock = threading.RLock()


def should_escalate(rec):
    return (ESCALATE and rec.get("verdict") in ESCALATE_VERDICTS and not rec.get("cancelled")
            and not rec.get("escalation"))


def hand_to_claude(incident, by):
    """Keep the report open for the code-claude runner (status "escalated"): the chat keeps waiting."""
    with escalation_lock:
        rec = incident.record
        now = now_sec()
        fixer = {k: rec.get(k) for k in RESULT_FIELDS + ("restart_outcome", "duration", "fixer_model")}
        if rec.get("escalation"):
            rec.setdefault("escalations_before", []).append(rec["escalation"])
        rec["escalation"] = {"state": "waiting", "by": by, "asked": now, "claimed": None, "runner": None,
                             "last": now, "ended": None, "fixer": fixer}
        rec["status"] = "escalated"
        rec["done"] = None
        save_record(incident)
        record_event(incident, {"kind": "escalate", "by": by, "fixer_verdict": fixer.get("verdict"),
                                "fixer_summary": str(fixer.get("summary") or "")[:800]})


def end_escalation(incident, state, result=None, note=""):
    """Close an escalated report: with Claude's result, or with the fixer's verdict and a note."""
    with escalation_lock:
        rec = incident.record
        esc = rec.get("escalation") or {}
        if rec.get("status") != "escalated":
            return False
        now = now_sec()
        esc["state"] = state
        esc["ended"] = now
        fixer = esc.get("fixer") or {}
        if result is not None:
            for key in RESULT_FIELDS:
                if key in result:
                    rec[key] = result[key]
            for key in ("seconds", "turns", "model", "cost"):
                if key in result:
                    esc[key] = result[key]
            esc["verdict"] = result.get("verdict")
        else:
            for key in RESULT_FIELDS:
                rec[key] = fixer.get(key)
        if state == "cancelled":
            rec["verdict"] = "cancelled"
            rec["summary"] = "Cancelled by you while Claude was looking into it. The fixer had said: " + \
                str(fixer.get("summary") or "-")
            rec["cancelled"] = True
        if note:
            esc["note"] = note
            record_event(incident, {"kind": "status", "text": note})
        rec["status"] = "done"
        rec["done"] = now
        rec["duration"] = now - (rec.get("started") or rec.get("created") or now)
        close_incident(incident)
        return True


def cancel_incident(incident):
    """The user's Cancel: before the run, during the fixer's run, or while Claude looks into it.
    Returns (status, message)."""
    rec = incident.record
    status = rec.get("status")
    if status == "queued":
        rec["cancelled"] = True
        rec["verdict"] = "cancelled"
        rec["summary"] = "Cancelled by you before the fixer started."
        rec["status"] = "done"
        rec["done"] = now_sec()
        rec["duration"] = 0
        record_event(incident, {"kind": "status", "text": "Cancelled"})
        close_incident(incident)
        return 200, "cancelled"
    if status == "investigating":
        rec["cancelled"] = True
        record_event(incident, {"kind": "status", "text": "Cancelling…"})
        if incident.session_id:
            try:
                http_json("POST", ep("/api/session/%s/interrupt" % incident.session_id), body={}, timeout=15)
            except Exception:
                pass
        return 202, "cancelling"
    if status == "escalated":
        end_escalation(incident, "cancelled", note="Cancelled")
        return 200, "cancelled"
    return 409, "This report is already finished."


def load_incident(incident_id):
    """The live incident, or one read back from disk (then kept in memory, so it can be reopened)."""
    with incidents_lock:
        incident = incidents.get(incident_id)
    if incident is not None:
        return incident
    if not CHAT_ID_RE.match(incident_id or ""):
        return None
    path = os.path.join(FIXER_DIR, incident_id)
    try:
        with open(os.path.join(path, "incident.json")) as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    events = []
    try:
        with open(os.path.join(path, "events.jsonl"), "rb") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    inc = Incident(incident_id, record)
    inc.events = events
    return inc


def escalation_watchdog():
    """Nobody claimed it, or Claude went quiet: close with the fixer's verdict so no chat waits forever."""
    while True:
        time.sleep(WATCHDOG_S)
        try:
            with incidents_lock:
                items = list(incidents.values())
            now = now_sec()
            for inc in items:
                rec = inc.record
                if rec.get("status") != "escalated":
                    continue
                esc = rec.get("escalation") or {}
                if esc.get("state") == "waiting" and now - (esc.get("asked") or now) > ESCALATE_CLAIM_S:
                    end_escalation(inc, "unclaimed", note="Claude did not pick this up (is code-claude running on "
                                                          "cryoai?). The fixer's verdict stands.")
                elif esc.get("state") == "claimed" and (now - (esc.get("last") or now) > ESCALATE_IDLE_S or
                                                        now - (esc.get("claimed") or now) > ESCALATE_MAX_S):
                    end_escalation(inc, "stalled", note="Claude stopped answering. The fixer's verdict stands.")
        except Exception as exc:  # never let the watchdog die
            sys.stderr.write("code-fixer watchdog error: %s\n" % exc)


def sweep_unfinished():
    """At start: a report the index still has open was cut off by a restart (its run lived in memory). Close it,
    so a chat waiting on it (the Code pipe waits for reports the user filed) does not wait forever."""
    try:
        with open(index_path()) as fh:
            entries = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return 0
    n = 0
    for e in entries:
        if not isinstance(e, dict) or e.get("status") in ("done", None):
            continue
        inc = load_incident(e.get("id"))
        if inc is None:
            inc = Incident(e["id"], dict(e, view=e.get("view")))
            os.makedirs(inc.dir, exist_ok=True)
        rec = inc.record
        if rec.get("status") == "escalated":
            end_escalation(inc, "restarted", note="code-fixer restarted while Claude was looking into it. The "
                                                 "fixer's verdict stands.")
        elif rec.get("status") != "done":
            rec["status"] = "done"
            rec["verdict"] = rec.get("verdict") or "could_not_fix"
            rec["summary"] = rec.get("summary") or "code-fixer restarted before this report was finished."
            rec["done"] = now_sec()
            rec["duration"] = rec["done"] - (rec.get("started") or rec.get("created") or rec["done"])
            close_incident(inc)
        n += 1
    return n


# --------------------------------------------------------------------------- #
# Web viewer & log pages (loaded from disk alongside this file)
# --------------------------------------------------------------------------- #

def claude_event(ev):
    """One of Claude's steps from the runner, cut down to what the viewer shows. None = not one."""
    if not isinstance(ev, dict):
        return None
    kind = ev.get("type")
    if kind not in ("start", "status", "think", "say", "tool", "error"):
        return None
    out = {"kind": "claude", "type": kind}
    for key, limit in (("text", 8000), ("id", 80), ("name", 60), ("summary", 600), ("output", 4000), ("model", 80)):
        if ev.get(key) is not None:
            out[key] = str(ev.get(key))[:limit]
    if ev.get("state") in ("running", "ok", "failed", "start", "end"):
        out["state"] = ev["state"]
    if isinstance(ev.get("seconds"), (int, float)):
        out["seconds"] = ev["seconds"]
    return out


def _read_template(name):
    for candidate in (
        os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
        os.path.join(os.getcwd(), name),
    ):
        try:
            with open(candidate) as fh:
                return fh.read()
        except OSError:
            continue
    return ""


VIEWER_HTML = _read_template("viewer.html")
LOG_HTML = _read_template("log.html")


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "code-fixer/1.0"
    protocol_version = "HTTP/1.1"

    def _mask(self):
        return mask_sig(self.path)

    def log_message(self, format, *args):
        # The default BaseHTTPRequestHandler writes the raw request line (with
        # the signed viewer/log sig, e.g. /f/<id>/<32-hex>/) to stderr. _log()
        # already records only the masked path, so silence the default here.
        pass


    # -- response helpers --------------------------------------------------- #
    def _base_headers(self, extra=None):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)

    def _json(self, status, obj, extra=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._base_headers(extra)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._log(status)

    def _html(self, status, html_text):
        body = html_text.encode("utf-8")
        self.send_response(status)
        self._base_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._log(status)

    def _text(self, status, text, extra=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self._base_headers(extra)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._log(status)

    def _not_found(self):
        self._text(404, "Not found.\n")

    def _log(self, status):
        sys.stderr.write("%s %s\n" % (self._mask(), status))
        sys.stderr.flush()

    # -- auth / sig --------------------------------------------------------- #
    def _bearer_ok(self):
        hdr = self.headers.get("Authorization", "")
        return hdr.startswith("Bearer ") and hmac.compare_digest(
            hdr[len("Bearer "):].strip(), FIXER_TOKEN)

    def _log_sig_ok(self, sig):
        if not sig or len(sig) != 32:
            return False
        return hmac.compare_digest(sig, log_sig())

    # -- dispatch ----------------------------------------------------------- #
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = parsed.query

        m = re.match(r"^/f/(?P<id>[A-Za-z0-9_-]{1,64})/(?P<sig>[0-9a-f]{32})"
                     r"(?:/(?P<inner>[^/]*))?/?$", path)
        if m:
            return self._view(m.group("id"), m.group("sig"), m.group("inner"))

        m = re.match(r"^/log/(?P<sig>[0-9a-f]{32})(/(?P<inner>[^/]*))?/?$", path)
        if m:
            return self._log_page(m.group("sig"), m.group("inner"))

        m = re.match(r"^/api/incidents/(?P<id>[A-Za-z0-9_-]{1,64})/wait/?$", path)
        if m:
            return self._api_incident_wait(m.group("id"), q)

        m = re.match(r"^/api/incidents/(?P<id>[A-Za-z0-9_-]{1,64})/?$", path)
        if m:
            return self._api_incident(m.group("id"))

        if re.match(r"^/api/active$", path):
            return self._api_active(q)

        if path == "/api/escalations":
            return self._api_escalations()

        m = re.match(r"^/api/incidents/(?P<id>[A-Za-z0-9_-]{1,64})/file/(?P<name>[A-Za-z0-9_.-]{1,40})$", path)
        if m:
            return self._api_file(m.group("id"), m.group("name"))

        if path == "/api/pick":
            return self._api_pick()

        self._not_found()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/report":
            return self._api_report()
        m = re.match(r"^/api/incidents/(?P<id>[A-Za-z0-9_-]{1,64})/(?P<op>cancel|escalate|claim|events|resolve)$", path)
        if m:
            return self._api_incident_op(m.group("id"), m.group("op"))
        self._not_found()

    # -- cancel / escalation ------------------------------------------------ #
    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 2_000_000:
                return None
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            return body if isinstance(body, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None

    def _api_escalations(self):
        if not self._bearer_ok():
            return self._json(401, {"error": "unauthorized"})
        with incidents_lock:
            items = list(incidents.values())
        out = []
        for inc in items:
            rec = inc.record
            esc = rec.get("escalation") or {}
            if rec.get("status") == "escalated" and esc.get("state") == "waiting":
                out.append({"id": inc.id, "chat_id": rec.get("chat_id"), "title": rec.get("title"),
                            "source": rec.get("source"), "by": esc.get("by"), "asked": esc.get("asked"),
                            "fixer_verdict": (esc.get("fixer") or {}).get("verdict")})
        out.sort(key=lambda e: e.get("asked") or 0)
        return self._json(200, {"escalations": out})

    def _api_file(self, incident_id, name):
        if not self._bearer_ok():
            return self._json(401, {"error": "unauthorized"})
        if name not in EVIDENCE_FILES:
            return self._json(404, {"error": "not an evidence file"})
        try:
            with open(os.path.join(FIXER_DIR, incident_id, name), encoding="utf-8", errors="replace") as fh:
                text = fh.read(2_000_000)
        except OSError:
            return self._json(404, {"error": "no such file"})
        return self._text(200, text, {"X-Content-Type-Options": "nosniff"})

    def _api_incident_op(self, incident_id, op):
        if not self._bearer_ok():
            return self._json(401, {"error": "unauthorized"})
        body = self._body_json()
        if body is None:
            return self._json(400, {"error": "invalid JSON body"})
        incident = load_incident(incident_id)
        if incident is None:
            return self._json(404, {"error": "unknown incident"})
        rec = incident.record
        esc = rec.get("escalation") or {}
        if op == "cancel":
            code, msg = cancel_incident(incident)
            return self._json(code, {"status": msg} if code < 300 else {"error": msg})
        if op == "escalate":
            if rec.get("status") == "escalated":
                return self._json(200, {"status": "escalated", "already": True})
            if rec.get("status") != "done":
                return self._json(409, {"error": "The fixer is still working on it: wait for its verdict, or cancel it."})
            hand_to_claude(incident, str(body.get("by") or "user")[:20])
            return self._json(200, {"status": "escalated"})
        if rec.get("status") != "escalated":
            return self._json(409, {"error": "This report is not waiting for Claude.", "status": rec.get("status")})
        runner = str(body.get("runner") or "")[:80]
        if op == "claim":
            with escalation_lock:
                if esc.get("state") != "waiting":
                    return self._json(409, {"error": "Already claimed."})
                now = now_sec()
                esc.update(state="claimed", claimed=now, last=now, runner=runner or "runner")
                save_record(incident)
            record_event(incident, {"kind": "claude", "type": "status", "text": "Claude picked it up"})
            return self._json(200, _public_record(rec))
        if esc.get("state") != "claimed" or (runner and runner != esc.get("runner")):
            return self._json(409, {"error": "Not claimed by this runner."})
        if op == "events":
            evs = body.get("events")
            if not isinstance(evs, list):
                return self._json(400, {"error": "events must be a list"})
            for ev in evs[:200]:
                clean = claude_event(ev)
                if clean:
                    record_event(incident, clean)
            esc["last"] = now_sec()
            return self._json(200, {"ok": True, "status": rec.get("status")})
        if op == "resolve":
            result = {k: body.get(k) for k in RESULT_FIELDS + ("seconds", "turns", "model", "cost") if k in body}
            if result.get("verdict") not in VALID_VERDICTS:
                result["verdict"] = "needs_human"
            result["summary"] = str(result.get("summary") or "")[:8000]
            if not isinstance(result.get("changed"), list):
                result["changed"] = []
            if not isinstance(result.get("restart"), list):
                result["restart"] = []
            ok = end_escalation(incident, "done", result)
            return self._json(200 if ok else 409, {"ok": ok})
        return self._not_found()

    # -- /api/report -------------------------------------------------------- #
    def _api_report(self):
        if not self._bearer_ok():
            return self._json(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid JSON body"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "body must be an object"})

        chat_id = body.get("chat_id")
        if not isinstance(chat_id, str) or not CHAT_ID_RE.match(chat_id):
            return self._json(400, {"error": "invalid chat_id"})
        title = body.get("title", "")
        if not isinstance(title, str) or len(title) > 200:
            return self._json(400, {"error": "title must be <= 200 characters"})
        details = body.get("details", "")
        if not isinstance(details, str) or len(details) > 20000:
            return self._json(400, {"error": "details must be <= 20000 characters"})
        source = body.get("source", "code")
        if source not in ("chat", "code", "user"):  # user = the Fixer button in the chat's pill
            return self._json(400, {"error": "source must be 'chat', 'code' or 'user'"})
        context = body.get("context", []) or []
        if not isinstance(context, list):
            return self._json(400, {"error": "context must be a list"})

        report = {
            "chat_id": chat_id,
            "source": source,
            "reporter_model": body.get("reporter_model", "unknown"),
            "title": title,
            "details": details,
            "context": context,
            "received": now_sec(),
        }
        incident = new_incident(report)
        _report_cache[incident.id] = report
        os.makedirs(incident.dir, exist_ok=True)
        # A 'report' event first, so a live viewer can show the title and
        # reporter as soon as the incident exists, well before the stream
        # reaches 'done'.
        record_event(incident, {
            "kind": "report",
            "title": report.get("title", ""),
            "reporter_model": report.get("reporter_model", "unknown"),
            "source": report.get("source", "code"),
            "created": incident.record.get("created"),
        })
        # report.json + context.md immediately so the viewer works at once.
        with open(os.path.join(incident.dir, "report.json"), "w") as fh:
            json.dump(report, fh, indent=2)
        with open(os.path.join(incident.dir, "context.md"), "w") as fh:
            fh.write(render_context(context))
        with incidents_lock:
            incidents[incident.id] = incident
        append_index(index_entry_from_record(incident.record))
        work_queue.put(incident.id)

        self._json(201, {"id": incident.id,
                         "view": incident.record["view"],
                         "status": "queued"})

    # -- viewer routes ------------------------------------------------------ #
    def _load_incident(self, incident_id):
        """Return the incident object: live one from memory, or one loaded
        from disk for a finished incident that was evicted from memory.
        Returns None if nothing on disk."""
        with incidents_lock:
            incident = incidents.get(incident_id)
        if incident is not None:
            return incident
        path = os.path.join(FIXER_DIR, incident_id)
        try:
            events = []
            with open(os.path.join(path, "events.jsonl"), "rb") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
        except OSError:
            return None
        try:
            with open(os.path.join(path, "incident.json")) as fh:
                record = json.load(fh)
        except OSError:
            record = {"status": "unknown"}
        inc = Incident(incident_id, record)
        inc.events = events
        return inc

    def _view(self, incident_id, sig, inner):
        if not hmac.compare_digest(sig, viewer_sig(incident_id)):
            return self._text(403, "Invalid viewer link.\n")
        incident = self._load_incident(incident_id)
        if incident is None:
            return self._text(404, "Not found.\n")
        if inner in (None, ""):
            if not VIEWER_HTML:
                return self._text(404, "Viewer page not found.\n")
            return self._html(200, VIEWER_HTML)
        if inner == "events":
            return self._serve_sse(incident)
        if inner == "diff":
            diff = ""
            try:
                with open(os.path.join(incident.dir, "diff.patch")) as fh:
                    diff = fh.read()
            except OSError:
                diff = "(no diff produced)\n"
            return self._text(200, diff, {"X-Content-Type-Options": "nosniff"})
        return self._text(404, "Not found.\n")

    def _serve_sse(self, incident):
        self.send_response(200)
        self._base_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        # On a reconnect the client sends ?last=<n>; skip the first n events so
        # rows the viewer already rendered are not replayed (which would double
        # every row).
        last = 0
        try:
            params = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            last = min(int(params.get("last", ["0"])[0]), len(incident.events))
        except (ValueError, IndexError):
            last = 0

        conn_queue = queue.Queue()
        with incident.sub_lock:
            incident.subscribers[self] = conn_queue
        with incident.cond:
            snapshot = list(incident.events[last:])

        def push(ev):
            try:
                # SSE framing: a "data: " line followed by a blank line, which
                # is what a browser's EventSource dispatches an event on.
                conn_queue.put(b"data: " + json.dumps(ev).encode("utf-8")
                               + b"\n\n")
            except Exception:
                pass
        for ev in snapshot:
            push(ev)
        if getattr(incident, "record", {}).get("status") == "done":
            # A finished incident already recorded its 'done' event in the
            # snapshot; push the raw None sentinel to end the stream so the
            # reader does not hang waiting for events that will never come.
            conn_queue.put(None)
        self.wfile.flush()

        try:
            while True:
                item = conn_queue.get()
                if item is None:
                    break
                self.wfile.write(item)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            self._log(200)
            with incident.sub_lock:
                incident.subscribers.pop(self, None)

    # -- log routes --------------------------------------------------------- #
    def _log_page(self, sig, inner):
        if not self._log_sig_ok(sig):
            return self._text(403, "Invalid log link.\n")
        if inner == "incidents":
            entries = []
            try:
                with open(index_path()) as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                entries.append(json.loads(line))
                            except ValueError:
                                continue
            except OSError:
                pass
            entries.sort(key=lambda e: e.get("created", 0), reverse=True)
            return self._json(200, entries)
        if not LOG_HTML:
            return self._text(404, "Log page not found.\n")
        return self._html(200, LOG_HTML)

    # -- API incidents ------------------------------------------------------ #
    def _get_incident(self, incident_id):
        with incidents_lock:
            incident = incidents.get(incident_id)
        if incident is None:
            # A finished incident may have been pruned; fall back to the record
            # on disk if it exists.
            path = os.path.join(FIXER_DIR, incident_id, "incident.json")
            try:
                with open(path) as fh:
                    return json.load(fh), True
            except OSError:
                return None, False
        return incident.record, False

    def _api_incident(self, incident_id):
        if not self._bearer_ok():
            return self._json(401, {"error": "unauthorized"})
        record, _ = self._get_incident(incident_id)
        if record is None:
            return self._json(404, {"error": "unknown incident"})
        return self._json(200, _public_record(record))

    def _api_incident_wait(self, incident_id, query):
        if not self._bearer_ok():
            return self._json(401, {"error": "unauthorized"})
        params = urllib.parse.parse_qs(query)
        timeout = 30
        try:
            timeout = min(int(params.get("timeout", ["30"])[0]), 3600)
        except (ValueError, IndexError):
            timeout = 30
        with incidents_lock:
            incident = incidents.get(incident_id)
        if incident is None:
            record, _ = self._get_incident(incident_id)
            if record is None:
                return self._json(404, {"error": "unknown incident"})
            return self._json(200, _public_record(record))
        with incident.cond:
            deadline = time.time() + timeout
            while incident.record.get("status") != "done" and \
                    time.time() < deadline:
                incident.cond.wait(timeout=deadline - time.time())
        return self._json(200, _public_record(incident.record))

    def _api_pick(self):
        """Which model a report filed now would run on, so the Fixer button can say so before the user starts one:
        {"model", "loaded"} (loaded False = the run loads it, swapping out what is loaded; None = router unknown)."""
        if not self._bearer_ok():
            return self._json(401, {"error": "unauthorized"})
        _, model_id, loaded = pick_model_info()
        return self._json(200, {"model": model_id, "loaded": loaded})

    def _api_active(self, query):
        if not self._bearer_ok():
            return self._json(401, {"error": "unauthorized"})
        params = urllib.parse.parse_qs(query)
        chat = params.get("chat", [""])[0]
        best = None
        try:
            with open(index_path()) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("chat_id") != chat:
                        continue
                    if e.get("status") == "done":
                        continue
                    if best is None or e.get("created", 0) >= best.get("created", 0):
                        best = e
        except OSError:
            pass
        if not best:
            return self._json(404, {"error": "no active incident"})
        return self._json(200, {"id": best["id"], "view": best["view"]})


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def worker_loop():
    while True:
        incident_id = work_queue.get()
        try:
            with incidents_lock:
                incident = incidents.get(incident_id)
            if incident is None:
                continue
            process_incident(incident)
        except Exception as exc:  # never let the worker die
            sys.stderr.write("code-fixer worker error: %s\n" % exc)
            sys.stderr.flush()
        finally:
            work_queue.task_done()


def main():
    _require_secret()
    os.makedirs(FIXER_DIR, exist_ok=True)
    swept = sweep_unfinished()
    if swept:
        sys.stdout.write("code-fixer: closed %d report(s) a restart had cut off\n" % swept)
    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()
    threading.Thread(target=escalation_watchdog, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", FIXER_PORT), Handler)
    sys.stdout.write("code-fixer: serving on 0.0.0.0:%d (dir %s)\n"
                     % (FIXER_PORT, FIXER_DIR))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
