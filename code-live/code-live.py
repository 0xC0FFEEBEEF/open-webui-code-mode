#!/usr/bin/env python3
"""code-live.py - a read-only live window onto a Code chat's opencode turn, embedded in the Open WebUI reply.

The Code pipe embeds /w/<chat id>/<sig>/?s=<session>&m=<user message> as soon as a turn starts. The page shows
what the agent is doing while it does it: thinking, commentary, tool calls, and the code of every write/edit as
the model types it. Once the turn is over the window folds into a one-line bar that replays the turn on click.

    GET /w/<chat>/<sig>/?s=&m=          the window (live.html next to this file)
    GET /w/<chat>/<sig>/events?s=&m=    server-sent events: the turn so far ("snap"), then live updates
                                        (&full=1 replays a finished turn; without it a finished turn is "past")
    GET /w/<chat>/<sig>/engine          the model server's state for the Code page's Watch mode (from the slerm
                                        panel's /api/ai/live, which the browser may not reach): JSON, cached 1.5 s
    GET /w/<chat>/<sig>/tools?s=&m=     the session's tool calls by name over its whole life, the MCP servers opencode has
                                        (and how often each was called), and the review send-backs it got: Watch mode's
                                        Tools tab. Counted once, then only new messages (STATS_TTL)
    GET /health

While opencode compacts a session its window also gets "compact" events (start, the history it reads as short "tape"
lines, the summary as it is written, end, done) and "cprog" events: the engine's read/write progress, polled once a
second from the slerm panel's /api/ai/live (STRATA_LIVE_URL) until the model has read the new context. live.html
hands these to the Code page, which draws the compaction overlay.

A review window (one of the reviewer's sessions) also gets its brief in the snapshot ("brief": the prompt Code mode
gave the reviewer) and "cprog" while the reviewer reads it; a builder window whose prompt is the review's send-back
gets "handback": the findings it was given. live.html turns those into the Code page's review overlay.

sig = first 32 hex chars of HMAC-SHA256(PREVIEW_SECRET, "live:" + chat id); the session must belong to that chat
— it carries the chat id in its metadata (owui_chat_id), or, for one of the review sessions Code mode's reviewer
runs for the chat, owui_review_of (plus owui_review_round, which the window shows). A session of no chat, or of
another one, is refused. It only reads from opencode: GET
/api/session/..., one shared GET /api/event. Python 3.12 standard library only.

It writes in one place only, the Upload button's files (Feature #31):

    PUT /u/<chat>/<usig>/file?path=<relative path>   the request body is the file; it lands in the chat's project
                                                     folder under uploads/<relative path> (folders kept)
    OPTIONS /u/<chat>/<usig>/file                    the browser's CORS preflight (the Open WebUI page sends it)

usig = first 32 hex chars of HMAC-SHA256(PREVIEW_SECRET, "upload:" + chat id): another key than the live window's, so
a live link cannot upload. The project folder is PROJECTS_ROOT/features/<chat> for a feature chat, else
PROJECTS_ROOT/chats/<chat> (made when the chat has none yet). A path may not climb out (no "..", no absolute path, no
.git part); a file may be at most UPLOAD_MAX_FILE bytes and is refused when it would leave less than UPLOAD_MIN_FREE
bytes on the disk. uploads/.gitignore ("*") keeps the uploads out of git, so a commit or a push never carries them.
"""
import base64
import difflib
import hashlib
import hmac
import json
import os
import queue
import re
import shlex
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SID_RE = re.compile(r"^ses_[A-Za-z0-9]{1,64}$")
MSG_RE = re.compile(r"^msg_[A-Za-z0-9]{1,64}$")
GONE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>opencode · cleared</title><style>
html, body { margin: 0; background: transparent; font: 13px/1.5 Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
#win { display: flex; align-items: center; gap: 9px; height: 38px; padding: 0 14px; box-sizing: border-box; overflow: hidden;
  background: #0a0a0b; border: 1px solid #1f1f23; border-radius: 16px; color: #85858f; }
.dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: #4f4f58; }
.brand { flex: none; font: 600 10.5px/1 ui-monospace, "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
  letter-spacing: .14em; color: #b4b4bb; text-transform: uppercase; }
.now { flex: 1; min-width: 0; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style></head><body>
<div id="win" title="Code mode clears a turn's review sessions when the next turn starts, so this one has nothing to replay.">
<i class="dot"></i><span class="brand">opencode</span><span class="now">Cleared &middot; no longer on the coding machine</span></div>
<script>parent.postMessage({ type: "iframe:height", height: Math.ceil(document.getElementById("win").getBoundingClientRect().height), live: false }, "*");</script>
</body></html>
"""
ROUTE_RE = re.compile(r"^/w/(?P<chat>[^/]+)/(?P<sig>[0-9a-f]{32})/(?P<rest>events|engine|tools)?$")
UPLOAD_RE = re.compile(r"^/u/(?P<chat>[^/]+)/(?P<sig>[0-9a-f]{32})/file$")
PROJECTS_ROOT = os.environ.get("PROJECTS_ROOT", "/home/coder/projects")
UPLOAD_DIR = "uploads"
UPLOAD_MAX_FILE = int(os.environ.get("UPLOAD_MAX_FILE", str(1 << 30)))          # 1 GiB a file
UPLOAD_MIN_FREE = int(os.environ.get("UPLOAD_MIN_FREE", str(4 << 30)))          # keep 4 GiB free on the disk
UPLOAD_MAX_PATH, UPLOAD_MAX_PART, UPLOAD_MAX_DEPTH = 1024, 255, 32

OPENCODE = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096").rstrip("/")
OPENCODE_CONFIG = os.environ.get("OPENCODE_CONFIG", "/home/coder/.config/opencode/service.json")
FRAME_ANCESTORS = os.environ.get("LIVE_FRAME_ANCESTORS", "http://cryoai:8080 http://cryoai:8080")
PORT = int(os.environ.get("LIVE_PORT", "4099"))
# The engine's progress while a compaction shows: the slerm panel's live view of its model servers (Strata's prompt
# read position and generated tokens). The engine itself is reachable only from the cluster head.
STRATA_LIVE = os.environ.get("STRATA_LIVE_URL", "http://slerm-head:8080/api/ai/live")

INPUT_CAP = 150_000   # chars kept of one tool-input string (a whole file being written)
OUTPUT_CAP = 4_000    # chars kept of one tool result (head + tail)
OUTPUT_CAPS = {"read": 24_000, "webfetch": 12_000, "websearch": 8_000, "grep": 8_000}  # shown in the follow view
TEXT_CAP = 60_000     # chars kept of one thinking / text part
PAGE = 50             # messages per history page
MAX_PAGES = 40
PING = 15             # seconds between keep-alive comments
RECHECK = 30          # seconds of silence before asking opencode whether the run is still going

DIFF_CONTEXT = 3      # unchanged lines kept around each change (hunks closer than 2×this are joined)
DIFF_MAX_LINES = 1200  # diff rows sent for one tool call before the rest is noted away
DIFF_CHUNK = 400      # rows per hunk before a long hunk is split
DIFF_MAX_FILE = 1_000_000  # bytes: a file bigger than this is not read for a diff
DIFF_MAX_ROWS = 12_000     # lines: a file with more lines than this is not diffed
DIFF_RATIO = 0.5      # line similarity needed to mark the changed characters of a -/+ pair
GIT_ROW_CAP = 2_000   # diff rows parsed for one shell `git diff` / `git show` call; past it the rest is noted away
BEFORE_MAX = 64       # "before" file contents cached per tool call (oldest dropped first)
BEFORE_BYTES = 8 << 20   # total size of that cache
DIFF_EVERY = 0.3      # seconds between recomputing a streaming tool call's diff

VIEW_MAX_LINES = 2000  # a file view carries at most this many lines of the file
VIEW_PAD = 200         # lines kept around the asked-for lines when the file is bigger than that
VIEWS_PER_CALL = 4     # file views sent for one tool call
READS_MAX = 64         # file contents the agent read, cached per tool call (oldest dropped first)
READS_BYTES = 8 << 20  # total size of that cache

COMPACT_POLL = 1.0     # seconds between engine progress polls while a compaction shows
COMPACT_MAX = 3600     # a compaction overlay stops polling after this long, whatever happened
TAPE_PAGES = 20        # history pages read back for the compaction tape (to the last compaction)
TAPE_MAX = 400         # tape lines sent (the newest)
TAPE_LINE = 140        # chars of one tape line
BRIEF_CAP = 20_000     # chars of a review brief sent to its window
HANDBACK_HEAD = "[Independent review by Code mode"   # how review_fix_prompt (the Code pipe) starts


def log(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- signing

def live_sig(chat_id, secret=None):
    key = (secret if secret is not None else os.environ.get("PREVIEW_SECRET", "")).encode()
    return hmac.new(key, ("live:" + chat_id).encode(), hashlib.sha256).hexdigest()[:32]


def valid_sig(chat_id, sig):
    secret = os.environ.get("PREVIEW_SECRET", "")
    return bool(secret) and hmac.compare_digest(live_sig(chat_id, secret), sig)


def upload_sig(chat_id, secret=None):
    key = (secret if secret is not None else os.environ.get("PREVIEW_SECRET", "")).encode()
    return hmac.new(key, ("upload:" + chat_id).encode(), hashlib.sha256).hexdigest()[:32]


def valid_upload_sig(chat_id, sig):
    secret = os.environ.get("PREVIEW_SECRET", "")
    return bool(secret) and hmac.compare_digest(upload_sig(chat_id, secret), sig)


def mask(path):
    return re.sub(r"^(/[wu]/[^/]+/)[0-9a-f]{32}", r"\1***", path.split("?", 1)[0])


# ---------------------------------------------------------------- uploads (the one place this writes)

def project_dir(chat_id):
    """The chat's project folder: its feature workspace, else its own chats/ folder (which may not exist yet)."""
    feature = os.path.join(PROJECTS_ROOT, "features", chat_id)
    return feature if os.path.isdir(feature) else os.path.join(PROJECTS_ROOT, "chats", chat_id)


def upload_rel(path):
    """A browser's relative path ("report.pdf", "data/2024/q1.csv") as a safe path under uploads/, or None."""
    if not isinstance(path, str) or not path or len(path) > UPLOAD_MAX_PATH:
        return None
    if "\x00" in path or "\\" in path or path.startswith("/") or any(ord(ch) < 32 for ch in path):
        return None
    parts = path.split("/")
    if len(parts) > UPLOAD_MAX_DEPTH:
        return None
    for part in parts:
        if part in ("", ".", "..") or part == ".git" or len(part.encode()) > UPLOAD_MAX_PART:
            return None
    return "/".join(parts)


def save_upload(chat_id, rel, length, stream):
    """Write `length` bytes from `stream` to <project>/uploads/<rel> (a temp file beside it, then a rename).
    Returns (status, info): 201 and {"path", "bytes"}, or an error status and {"error"}."""
    folder = project_dir(chat_id)
    root = os.path.join(folder, UPLOAD_DIR)
    target = os.path.join(root, rel)
    # the deepest part of the path that exists already must be inside uploads/: a link the agent made in there
    # must not carry a write somewhere else (checked before any folder is made)
    root_real = os.path.realpath(root)
    probe = os.path.dirname(target)
    while probe != root and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    if os.path.exists(probe):
        real = os.path.realpath(probe)
        if real != root_real and not real.startswith(root_real + os.sep):
            return 400, {"error": "That path leaves the uploads folder."}
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    except OSError as e:   # (the unit must let code-live write there: ReadWritePaths in code-live.service)
        return 500, {"error": "Could not make the folder on the coding machine: %s" % e.strerror}
    if os.path.isdir(target):
        return 409, {"error": "A folder of that name is already there."}
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = 0
    if free - length < UPLOAD_MIN_FREE:
        return 507, {"error": "The coding machine's disk is too full for this file (%d MB free, %d MB kept free)."
                              % (free >> 20, UPLOAD_MIN_FREE >> 20)}
    ignore = os.path.join(root, ".gitignore")
    try:
        if not os.path.exists(ignore):
            with open(ignore, "w") as fh:
                fh.write("# Files uploaded from the chat (Code mode's Upload button): kept out of git\n*\n")
    except OSError as e:
        return 500, {"error": "Could not write on the coding machine: %s" % e.strerror}
    tmp = os.path.join(os.path.dirname(target), ".%s.part-%d" % (os.path.basename(target)[:100], threading.get_ident()))
    got = 0
    try:
        with open(tmp, "wb") as fh:
            while got < length:
                chunk = stream.read(min(1 << 16, length - got))
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
        if got != length:
            os.unlink(tmp)
            return 400, {"error": "The upload ended early (%d of %d bytes)." % (got, length)}
        os.replace(tmp, target)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 500, {"error": "Could not write the file: %s" % e.strerror}
    return 201, {"path": "%s/%s" % (UPLOAD_DIR, rel), "bytes": got}


# ---------------------------------------------------------------- opencode

_auth = None


def auth_header():
    global _auth
    if _auth is None:
        with open(OPENCODE_CONFIG) as fh:
            pw = json.load(fh)["password"]
        _auth = "Basic " + base64.b64encode(("opencode:" + pw).encode()).decode()
    return _auth


def oc_get(path, timeout=20):
    req = urllib.request.Request(OPENCODE + path, headers={"Authorization": auth_header()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def asc_cursor(message_id):
    """opencode 2.0.18's message cursor is base64url JSON {id, order, direction}; this one pages forward from a
    message (exclusive), oldest first."""
    raw = json.dumps({"id": message_id, "order": "asc", "direction": "next"}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


_owner = {}  # session id -> (chat id, checked at)
_dirs = {}   # session id -> (project directory, checked at) — filled by the same lookup as _owner
_review = {}  # session id -> ({"round": N} | None, checked at) — the same lookup


def review_info(meta):
    """How a review session's window names itself: {"round": N} for a session the reviewer ran for a chat
    (metadata owui_review_of / owui_review_round), None for the chat's own builder session."""
    if not (meta or {}).get("owui_review_of"):
        return None
    try:
        n = int(meta.get("owui_review_round"))
    except (TypeError, ValueError):
        n = 0  # a round this server cannot read is no reason to hide that this is a review
    return {"round": max(0, n)}


def session_chat(sid):
    hit = _owner.get(sid)
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    data = oc_get("/api/session/%s" % sid).get("data") or {}
    meta = data.get("metadata") or {}
    # the chat's own session, or a review session the reviewer ran for that chat: either belongs to the chat
    chat = meta.get("owui_chat_id") or meta.get("owui_review_of")
    _owner[sid] = (chat, time.time())
    # opencode 2.0.18 says where a session works as location.directory (older captures: directory). Without it no
    # file is read for a diff, a file view or the compaction tape's short paths.
    _dirs[sid] = (data.get("directory") or (data.get("location") or {}).get("directory"), time.time())
    _review[sid] = (review_info(meta), time.time())
    return chat


def session_dir(sid):
    """The session's own project directory: the only place diffs may read files from."""
    hit = _dirs.get(sid)
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    session_chat(sid)  # one lookup fills both caches
    return (_dirs.get(sid) or (None,))[0]


def session_review(sid):
    """{"round": N} when this session is one of the chat's review sessions, None when it is the chat's own."""
    hit = _review.get(sid)
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    session_chat(sid)  # one lookup fills all three caches
    return (_review.get(sid) or (None,))[0]


def session_active(sid):
    try:
        return sid in (oc_get("/api/session/active", timeout=10).get("data") or {})
    except Exception:
        return True  # unknown: keep listening rather than end the window early


# ---------------------------------------------------------------- shaping

def cap(s, n):
    return s if len(s) <= n else s[:n] + "\n… (%d more characters)" % (len(s) - n)


OUTPUT_GAP = "… (%d characters) …"                          # what a cut result says where the middle was dropped
OUTPUT_GAP_RE = re.compile(r"^… \((\d+) characters\) …$")   # the same line, read back by grep views


def cap_output(s, limit=OUTPUT_CAP):
    if len(s) <= limit:
        return s
    head = limit * 3 // 10
    return s[:head] + "\n" + (OUTPUT_GAP % (len(s) - limit)) + "\n" + s[-(limit - head):]


def slim_input(inp):
    if isinstance(inp, str):
        return cap(inp, INPUT_CAP)
    if isinstance(inp, dict):
        return {k: slim_input(v) for k, v in inp.items()}
    if isinstance(inp, list):
        return [slim_input(v) for v in inp[:50]]
    return inp


def content_text(content, tool=None):
    """A tool result's text parts; images and other files become a short marker (no data: URIs)."""
    limit = OUTPUT_CAPS.get(tool, OUTPUT_CAP)
    if isinstance(content, str):
        return cap_output(content, limit)
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            out.append(p.get("text") or "")
        elif p.get("type") == "file":
            out.append("[%s]" % ("image" if str(p.get("uri", "")).startswith("data:image") else "file"))
    return cap_output("\n".join(out), limit)


def err_text(e):
    if isinstance(e, dict):
        return str(e.get("message") or e.get("type") or "failed")[:500]
    return str(e or "failed")[:500]


def progress_output(meta):
    if isinstance(meta, dict):
        for k in ("output", "stdout", "preview", "text"):
            if isinstance(meta.get(k), str) and meta[k]:
                return cap_output(meta[k])
    return None


# ---------------------------------------------------------------- diffs
# The server computes every edit/write diff; the page only draws it, so this logic is testable in Python.

def split_lines(text):
    """File text as lines: LF or CRLF, and a missing final newline never shifts a line number."""
    if not text:
        return []
    if text.endswith("\n"):
        text = text[:-1]
    return [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]


def jstr(raw, key):
    """A string value out of tool-input JSON that may still be half-typed (the Python port of live.html's
    jstr). Returns (value, closed) — closed=False while the value is incomplete — or None while the key
    hasn't arrived yet."""
    if not isinstance(raw, str):
        return None
    m = re.search('"%s"\\s*:\\s*"' % re.escape(key), raw)
    if not m:
        return None
    esc = re.compile(r'["\\]')
    simple = {"n": "\n", "t": "\t", "r": "", "b": "", "f": ""}  # same table as live.html
    out, i, closed = [], m.end(), False
    while True:
        hit = esc.search(raw, i)
        if not hit:
            out.append(raw[i:])
            break
        out.append(raw[i:hit.start()])
        if hit.group(0) == '"':
            closed = True
            break
        nxt = raw[hit.end():hit.end() + 1]
        if not nxt:
            break  # the escape itself is still being typed: keep what came before it
        if nxt == "u":
            hexs = raw[hit.end() + 1:hit.end() + 5]
            if len(hexs) < 4:
                break
            out.append(chr(int(hexs, 16)))
            i = hit.end() + 5
        else:
            out.append(simple.get(nxt, nxt))
            i = hit.end() + 1
    return "".join(out), closed


def input_val(inp, key, raw=None):
    """One input string of a tool call: from its finished dict, or from the half-typed raw JSON."""
    if isinstance(inp, dict):
        v = inp.get(key)
        return (str(v), True) if v is not None else None
    return jstr(raw if raw is not None else inp, key)


def apply_edit(before, old, new, replace_all=False):
    """What the file becomes after an edit — before with oldString replaced by newString (all occurrences
    if replaceAll, else the first) — or None when oldString is not in the file."""
    if not old or old not in before:
        return None
    if replace_all:
        return before.replace(old, new)
    i = before.index(old)
    return before[:i] + new + before[i + len(old):]


def _merge_ranges(rs):
    out = []
    for s, e in rs:
        if out and s == out[-1][1]:
            out[-1][1] = e
        else:
            out.append([s, e])
    return out


TOKEN_RE = re.compile(r"[A-Za-z]+|[0-9]+|[^A-Za-z0-9\s]+|\s+")


def line_tokens(line):
    """A line cut into the pieces a word mark can fall on: a run of letters, a run of digits, a run of anything
    else that is not a space, or a run of spaces. Matching whole pieces is what keeps a mark on a word: comparing
    "create_session_token" with "issue_session_token" character by character leaves the shared "e_session_token"
    unmarked and marks the leftover letters "creat" / "issu" instead."""
    return [(m.group(0), m.start(), m.end()) for m in TOKEN_RE.finditer(line)]


def pair_marks(la, lb):
    """Character ranges where a removed line and its replacement differ ([old side], [new side]), or None
    when the two lines are less than DIFF_RATIO alike (whole-line red/green only). How alike they are is
    measured on characters; where they differ is worked out on the line's tokens."""
    like = difflib.SequenceMatcher(None, la, lb, autojunk=False)
    if like.quick_ratio() < DIFF_RATIO or like.ratio() < DIFF_RATIO:
        return None
    ta, tb = line_tokens(la), line_tokens(lb)
    sm = difflib.SequenceMatcher(None, [t[0] for t in ta], [t[0] for t in tb], autojunk=False)
    ra, rb = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            ra.append([ta[i1][1], ta[i2 - 1][2]])
        if j2 > j1:
            rb.append([tb[j1][1], tb[j2 - 1][2]])
    return _merge_ranges(ra), _merge_ranges(rb)


def _group_opcodes(ops, n):
    """difflib's get_grouped_opcodes over an explicit opcode list (we may have edited it: a streaming write
    folds its not-yet-touched old tail out first). An equal run longer than 2n splits the group; shorter
    runs join hunks."""
    codes = list(ops)
    if not codes:
        return []
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes = [(tag, max(i1, i2 - n), i2, max(j1, j2 - n), j2)] + codes[1:]
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes = codes[:-1] + [(tag, i1, min(i1 + n, i2), j1, min(j1 + n, j2))]
    groups, group = [], []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > 2 * n:
            group.append((tag, i1, i1 + n, j1, j1 + n))
            groups.append(group)
            group = [(tag, i2 - n, i2, j2 - n, j2)]
        else:
            group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        groups.append(group)
    return groups


def _group_rows(group, a, b):
    """Diff rows for one opcode group: [kind, old no, new no, text, marks]; kinds " ", "-", "+". A replace
    pairs its lines up so a removed line is followed by its replacement, with changed characters marked."""
    rows = []
    for tag, i1, i2, j1, j2 in group:
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append([" ", i1 + k + 1, j1 + k + 1, a[i1 + k], []])
        elif tag == "delete":
            for k in range(i1, i2):
                rows.append(["-", k + 1, None, a[k], []])
        elif tag == "insert":
            for k in range(j1, j2):
                rows.append(["+", None, k + 1, b[k], []])
            continue
        else:  # replace
            ol, nl = i2 - i1, j2 - j1
            mo, mn = [None] * ol, [None] * nl
            for k in range(min(ol, nl)):
                pm = pair_marks(a[i1 + k], b[j1 + k])
                if pm:
                    mo[k], mn[k] = pm
            for k in range(max(ol, nl)):
                if k < ol:
                    rows.append(["-", i1 + k + 1, None, a[i1 + k], mo[k] or []])
                if k < nl:
                    rows.append(["+", None, j1 + k + 1, b[j1 + k], mn[k] or []])
    return rows


def _mk_hunk(rows):
    old_start = next((r[1] for r in rows if r[1] is not None), None)
    new_start = next((r[2] for r in rows if r[2] is not None), None)
    return {"old_start": old_start, "new_start": new_start, "lines": rows}


def build_diff(a_text, b_text, fold_tail=False):
    """The diff of two file contents as the window draws it: {"hunks": [...], "more_old": N, "note": str|None}.
    fold_tail=True (a write still being typed): old lines at the end of the file that the typed content has
    not reached yet are not deletions — they are folded out and counted in more_old."""
    a, b = split_lines(a_text), split_lines(b_text)
    if len(a) > DIFF_MAX_ROWS or len(b) > DIFF_MAX_ROWS:
        return {"hunks": [], "more_old": 0, "note": "file too large for a live diff"}
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = list(sm.get_opcodes())
    more_old = 0
    if fold_tail:
        while ops:
            tag, i1, i2, j1, j2 = ops[-1]
            if i2 != len(a) or j2 != len(b):
                break
            if tag == "delete":
                more_old += i2 - i1
                ops.pop()
                continue
            if tag == "replace" and i2 - i1 > j2 - j1:
                extra = (i2 - i1) - (j2 - j1)
                ops[-1] = (tag, i1, i2 - extra, j1, j2)
                more_old += extra
            break
    hunks, total, truncated = [], 0, False
    for group in _group_opcodes(ops, DIFF_CONTEXT):
        rows = _group_rows(group, a, b)
        n = min(len(rows), DIFF_MAX_LINES - total)
        if n < len(rows):
            truncated = True
        for start in range(0, n, DIFF_CHUNK):
            hunks.append(_mk_hunk(rows[start:min(start + DIFF_CHUNK, n)]))
        total += n
        if truncated:
            break
    note = "diff too large to show in full" if truncated else None
    return {"hunks": hunks, "more_old": more_old, "note": note}


def capped_input(inp):
    """True when a tool input was cut by INPUT_CAP (the raw JSON or one of its strings carries the cap marker),
    so there is no full content to diff."""
    mark = re.compile(r"\n… \(\d+ more characters\)$")
    if isinstance(inp, str):
        return bool(mark.search(inp))
    if isinstance(inp, dict):
        return any(isinstance(v, str) and mark.search(v) for v in inp.values())
    return False


def diff_of_call(before, name, inp, streaming=False):
    """The diff for one edit/write tool call given its file's cached "before" text (None: unknown, no diff).
    inp is the finished input dict or the half-typed raw JSON string; streaming writes fold their old tail."""
    if before is None or name not in ("edit", "write"):
        return None
    path = ""
    for key in ("path", "filePath"):
        got = input_val(inp, key)
        if got and got[0] and got[1]:  # a path whose quote is still open names no file yet
            path = got[0]
            break
    if not path and not isinstance(inp, dict):
        return None  # the path has not arrived in the half-typed input: nothing to show yet
    if capped_input(inp):
        return {"path": path, "hunks": [], "more_old": 0, "note": "file too large for a live diff"}
    if name == "write":
        got = input_val(inp, "content") or ("", False)
        d = build_diff(before, got[0], fold_tail=bool(streaming) and not got[1])
    else:
        old = input_val(inp, "oldString")
        new = input_val(inp, "newString")
        if not old:
            return None  # oldString has not arrived yet
        replace_all = bool(inp.get("replaceAll")) if isinstance(inp, dict) else False
        after = apply_edit(before, old[0], (new or ("", False))[0], replace_all)
        if after is None:
            if old[1] and (not new or new[1]):
                return {"path": path, "hunks": [], "more_old": 0, "note": "could not place this edit in the file"}
            return None  # still typing: nothing to show until the placement is known
        d = build_diff(before, after)
    d["path"] = path
    return d


def read_within(dir_path, rel):
    """File text for a diff, only from inside the session's own directory: everything is resolved with
    realpath and refused if it lands outside (symlinks included). "" for a file that is not there yet (a
    new write), None when the read is refused or the file is too big or not text."""
    if not dir_path or not isinstance(rel, str) or not rel:
        return None
    root = os.path.realpath(dir_path)
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        return None
    try:
        if not os.path.exists(p):
            return ""
        if not os.path.isfile(p) or os.path.getsize(p) > DIFF_MAX_FILE:
            return None
        with open(p, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", "replace")


class BeforeReader:
    """What each edit/write found in its file, read once when the call starts and kept per tool call id (a
    reconnected window never re-reads a file that the agent has since changed). Bounded: at most max_entries
    entries and max_bytes in total, oldest dropped first."""

    def __init__(self, max_entries=BEFORE_MAX, max_bytes=BEFORE_BYTES):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.cache = OrderedDict()  # call id -> text (None: the read was refused, so that call gets no diff)
        self.sizes, self.total = {}, 0

    def clear(self):
        self.cache.clear()
        self.sizes, self.total = {}, 0

    def has(self, cid):
        return cid in self.cache

    def get(self, cid):
        return self.cache.get(cid)

    def put(self, cid, text):
        size = len((text or "").encode("utf-8", "ignore"))
        self.total += size - self.sizes.get(cid, 0)
        self.sizes[cid] = size
        self.cache[cid] = text
        while (len(self.cache) > self.max_entries or self.total > self.max_bytes) and len(self.cache) > 1:
            oldest = next(iter(self.cache))
            self.total -= self.sizes.pop(oldest)
            self.cache.popitem(last=False)

    def ensure(self, cid, dir_path, rel):
        if cid not in self.cache:
            self.put(cid, read_within(dir_path, rel))
        return self.cache[cid]


BEFORE = BeforeReader()


class CallTracker:
    """One edit/write whose input is being typed: keeps its raw input JSON, parses the half-typed strings
    (jstr), and emits a growing diff at most every DIFF_EVERY seconds and once when the call completes.
    The first read of the file wins, whenever it happens."""

    def __init__(self, cid, name, dir_path, emit, reader=BEFORE):
        self.cid, self.name = cid, name
        self.dir_path, self.emit, self.reader = dir_path, emit, reader
        self.raw, self.inp, self.done = "", None, False
        self.last = 0.0

    def seed(self, input_):
        if isinstance(input_, dict):
            self.inp = input_
        elif isinstance(input_, str):
            self.raw = input_

    def delta(self, d):
        room = INPUT_CAP - len(self.raw)
        if room > 0:
            self.raw += d[:room]
        self._maybe(False)

    def got_input(self, input_):
        if isinstance(input_, dict):
            self.inp = input_
        elif isinstance(input_, str):
            self.raw = input_
        self._maybe(False)

    def complete(self, applied=True):
        """The call is over. applied=False (it failed): the file never reached the state the typed input
        describes, so keep the streaming view (a partial write has not deleted its old tail)."""
        self._maybe(True, applied)

    def _maybe(self, force, final=True):
        if self.done:
            return
        now = time.monotonic()
        if not force and now - self.last < DIFF_EVERY:
            return
        inp = self.inp if self.inp is not None else self.raw
        path = ""
        for key in ("path", "filePath"):
            got = input_val(inp, key)  # a path that is still being typed names no file yet
            if got and got[0] and got[1]:
                path = got[0]
                break
        if not path:
            return
        before = self.reader.ensure(self.cid, self.dir_path, path)
        if self.inp is None and len(self.raw) >= INPUT_CAP:
            diff = {"path": path, "hunks": [], "more_old": 0, "note": "file too large for a live diff"}
        else:
            diff = diff_of_call(before, self.name, inp, streaming=not (force and final))
        if diff:
            self.last = now
            self.emit({"k": "diff", "id": self.cid, "path": diff["path"], "hunks": diff["hunks"],
                       "more_old": diff["more_old"], "note": diff["note"]})
        if force:
            self.done = True


# ---------------------------------------------------------------- reads as file views
# Which files a shell command reads, and which lines of them the agent asked for. The parser is pure: it only
# tokenises the command (shlex), never runs, expands or evals any of it, and whatever it is not sure about - a
# glob or a $() or a heredoc or an option it does not know - simply gets no view.

READ_CMDS = ("cat", "nl", "head", "tail", "sed", "grep", "awk")
BOUNDARIES = ("&&", "||", ";", "|", "&")
REDIRECTS = (">", ">>", "<", ">&", "<&", "<>", ">|")
EXPAND_RE = re.compile(r"[$*?[\]~`]")           # a path built by the shell names no file we can read
SED_CMD_RE = re.compile(r"^(\d+)(?:,(\d+|\$))?p$")  # "60,120p", "1,40p", "40,$p", "60p"
AWK_RANGE_RES = (re.compile(r"^NR\s*>=\s*(\d+)\s*&&\s*NR\s*<=\s*(\d+)$"),
                 re.compile(r"^NR\s*<=\s*(\d+)\s*&&\s*NR\s*>=\s*(\d+)$"),
                 re.compile(r"^NR\s*==\s*(\d+)\s*,\s*NR\s*==\s*(\d+)$"))


def _tokens(command):
    """Command text as shell words, with && || ; | & kept as their own tokens (shlex splits quoted text on
    punctuation only outside the quotes, so a grep pattern's "\\|" stays inside its word)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _bad_path(tok):
    """A word that cannot be a file the agent read: empty, stdin, a directory (., .., a trailing slash), or a name
    the shell only makes up when it expands $ ` * [ ~."""
    return (not tok or tok == "-" or tok in (".", "..") or tok.endswith("/")
            or bool(EXPAND_RE.search(tok)))


def _bad_cd(tok):
    """cd may name a directory, so . and .. are all right here; only an unknown place is not."""
    return not tok or tok == "-" or bool(EXPAND_RE.search(tok))


def _target(path, cd, how, ranges=None, tail=None, grep=False, limit=None, many=False):
    return {"path": path, "cd": cd, "how": how, "ranges": ranges, "tail": tail,
            "grep": grep, "limit": limit, "many": many}


def _merge_view_ranges(rs):
    """1-based inclusive ranges sorted and joined when they overlap or touch ([1,3] and [4,6] become one).
    A range whose end is None runs to the end of the file."""
    out = []
    for s, e in sorted((s, e) for s, e in rs):
        if s < 1:
            continue
        if out and (out[-1][1] is None or s <= out[-1][1] + 1):
            if e is None or (out[-1][1] is not None and e > out[-1][1]):
                out[-1][1] = e
        else:
            out.append([s, e])
    return out


def _reads_cat_or_nl(args, cd, how):
    """cat [-n] F [G], nl -ba F: the whole file."""
    files, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files += args[i + 1:]
            break
        if a.startswith("-") and a != "-":
            i += 1
            continue
        files.append(a)
        i += 1
    if not files or any(_bad_path(f) for f in files):
        return []
    return [_target(f, cd, how) for f in files]


def _head_tail(args, cd, how):
    """head F (10 lines), head -n N F, head -N F; tail F, tail -n N F, tail -N F (the last N lines),
    head/tail -n +K F (line K to the end). Bytes (-c), NUL (-z) and -f are not line ranges."""
    n, plus, files, i = None, False, [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files += args[i + 1:]
            break
        if a == "-n":
            if i + 1 >= len(args):
                return []
            v = args[i + 1]
            i += 2
            if v.startswith("+"):
                plus, v = True, v[1:]
            if not v.isdigit():
                return []
            n = int(v)
        elif len(a) > 1 and a[0] == "-" and a[1:].isdigit():
            n, i = int(a[1:]), i + 1
        elif a.startswith("-"):
            return []
        else:
            files.append(a)
            i += 1
    if not files or any(_bad_path(f) for f in files):
        return []
    n = 10 if n is None else n
    if plus:
        return [_target(f, cd, how, [[n, None]]) for f in files]
    if how == "tail":
        return [_target(f, cd, how, None, n) for f in files]
    return [_target(f, cd, how, [[1, n]]) for f in files]


def _sed_ranges(script):
    """One sed script as the lines it prints: "60,120p", "5p", "40,$p", and several of them joined with ";"
    or given with repeat -e. Anything else (a substitution, a delete, -i) prints nothing we can name."""
    out = []
    for piece in script.split(";"):
        piece = piece.strip()
        if not piece:
            continue
        m = SED_CMD_RE.match(piece)
        if not m:
            return None
        start = int(m.group(1))
        if m.group(2) is None:
            end = start
        elif m.group(2) == "$":
            end = None
        else:
            end = int(m.group(2))
            if end < start:
                return None
        out.append([start, end])
    return out or None


def _reads_sed(args, cd):
    scripts, files, typed, printed, i = [], [], False, False, 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files += args[i + 1:]
            break
        if a.startswith("--"):
            return []                       # --include and friends: not a plain read of a named file
        if a.startswith("-") and len(a) > 1:
            flags = a[1:]
            if "i" in flags or "z" in flags:
                return []                   # -i rewrites the file; -z splits on NULs: no line ranges here
            if any(c not in "nEruexLs" for c in flags):
                return []                   # an option we do not know: we cannot say which lines printed
            if "n" in flags:
                printed = True
            if "e" in flags:
                if i + 1 >= len(args):
                    return []
                scripts.append(args[i + 1])
                typed, i = True, i + 2
                continue
            i += 1
            continue                        # regex / buffering flags do not change which lines print
        files.append(a)
        i += 1
    if not typed:
        if not files:
            return []
        scripts.append(files.pop(0))        # the first word that is not an option is the script
    ranges = []
    for s in scripts:
        r = _sed_ranges(s)
        if not r:
            return []
        ranges += r
    if not files or any(_bad_path(f) for f in files):
        return []
    if not printed:
        return [_target(f, cd, "sed") for f in files]   # without -n, sed prints the whole file whatever the script says
    return [_target(f, cd, "sed", _merge_view_ranges(ranges)) for f in files]


def _reads_grep(args, cd):
    """grep -n PATTERN FILE [FILE...]: the matching lines are the highlighted ones, and their numbers come from
    the output later. Without -n, or with -r, or with a directory instead of files: no view."""
    numbered, patterns, files, i = False, [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files += args[i + 1:]
            break
        if a.startswith("--"):
            if a == "--line-number":
                numbered, i = True, i + 1
                continue
            if a.startswith("--color"):
                i += 1
                continue
            return []                       # --include / --exclude / --recursive: it searches more than these files
        if a.startswith("-") and len(a) > 1:
            flags = a[1:]
            if any(c in "rRcfZd" for c in flags):
                return []                   # recursive, count-only, pattern file: no line numbers to show
            if "n" in flags:
                numbered = True
            if "e" in flags:
                if i + 1 >= len(args):
                    return []
                patterns.append(args[i + 1])
                i += 2
                continue
            if any(c in "ABC" for c in flags):
                if i + 1 >= len(args) or not args[i + 1].isdigit():
                    return []
                i += 2                      # context lines: their numbers arrive with the output
                continue
            i += 1                          # -i -E -F -w -v -H -h -s: the line numbers still line up
            continue
        files.append(a)
        i += 1
    if not numbered or not files:
        return []
    if not patterns:
        patterns.append(files.pop(0))       # the first word that is not an option is the pattern
    if not files or any(_bad_path(f) for f in files):
        return []
    return [_target(f, cd, "grep", None, None, True, many=len(files) > 1) for f in files]


def _awk_ranges(prog):
    """awk 'NR>=60 && NR<=120' F and awk 'NR==60,NR==120' F name the lines they print; any other program prints
    whatever it likes."""
    p = prog.strip().rstrip(";")
    for rx in AWK_RANGE_RES:
        m = rx.match(p)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            return [[a, b]]
    return None


def _reads_awk(args, cd):
    operands, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            operands += args[i + 1:]
            break
        if a.startswith("-"):
            i += 1
            continue                        # -F, --field-separator: they do not say which lines print
        operands.append(a)
        i += 1
    if len(operands) < 2:
        return []                           # a program with no file reads the pipe, not a file
    prog, files = operands[0], operands[1:]
    ranges = _awk_ranges(prog)
    if not ranges or any(_bad_path(f) for f in files):
        return []
    return [_target(f, cd, "awk", ranges) for f in files]


def _seg_targets(words, cd):
    cmd = words[0]
    if cmd == "cat":
        return _reads_cat_or_nl(words[1:], cd, "cat")
    if cmd == "nl":
        return _reads_cat_or_nl(words[1:], cd, "nl")
    if cmd in ("head", "tail"):
        return _head_tail(words[1:], cd, cmd)
    if cmd == "sed":
        return _reads_sed(words[1:], cd)
    if cmd == "grep":
        return _reads_grep(words[1:], cd)
    if cmd == "awk":
        return _reads_awk(words[1:], cd)
    return []


def _strip_redirects(words):
    """A segment without its redirections: 2>/dev/null, > log 2>&1 and < /dev/null name no file the agent read.
    (shlex keeps the descriptor digit on its own: "2", ">", "/dev/null".)"""
    out, i = [], 0
    while i < len(words):
        t = words[i]
        if t in REDIRECTS:
            i += 2 if i + 1 < len(words) and words[i + 1] not in REDIRECTS else 1
            continue
        if t.isdigit() and i + 1 < len(words) and words[i + 1] in REDIRECTS:
            i += 1
            continue
        out.append(t)
        i += 1
    return out


def _segments(toks):
    """(words, boundary after them) for every && || ; | & separated segment, in order."""
    out, cur = [], []
    for t in toks:
        if t in BOUNDARIES:
            out.append((cur, t))
            cur = []
        else:
            cur.append(t)
    out.append((cur, ""))
    return out


def _head_only_limit(words):
    """A pipe consumer that is a plain `head -n N` / `head -N` of the text it is given: the file's first N lines
    are still what the agent saw. Any other consumer changes the text, so the producer gets no view."""
    if not words or words[0] != "head":
        return None
    n, i = None, 1
    while i < len(words):
        a = words[i]
        if a == "-n":
            if i + 1 >= len(words) or not words[i + 1].isdigit():
                return None
            n, i = int(words[i + 1]), i + 2
        elif len(a) > 1 and a[0] == "-" and a[1:].isdigit():
            n, i = int(a[1:]), i + 1
        elif a.startswith("-"):
            return None
        else:
            return None                     # it names files of its own: not a consumer of this pipe
    return n


def read_targets(command):
    """Every file a shell command reads through cat / nl / head / tail / sed -n / grep -n / awk, and the lines of
    it the command asked for: [{"path", "cd", "how", "ranges", "tail", "grep", "limit", "many"}, ...].
    "ranges" are 1-based inclusive line ranges (None end = to the end of the file), "tail" N = the last N lines,
    "grep" True = the ranges are the matching lines (their numbers come from the output), "limit" N = the output
    was cut to its first N lines by a trailing `| head -N`, "cd" = the directory the path is relative to as the
    command chained its cd's. Never raises: a command this cannot read precisely returns fewer targets or none."""
    if not isinstance(command, str) or not command.strip():
        return []
    try:
        toks = _tokens(command)
    except ValueError:
        return []                                   # unbalanced quotes: nothing in it can be trusted
    if not toks or any(t in ("<<", "<<-", "(", ")") for t in toks):
        return []                                   # a heredoc or a (...) substitution: the text is not here
    out, cd, lost = [], "", False
    segs = _segments(toks)
    for idx, (raw, boundary) in enumerate(segs):
        words = _strip_redirects(raw)
        if not words:
            continue
        if words[0] == "cd":
            args = [a for a in words[1:] if a != "--"]
            if not args or len(args) > 1 or _bad_cd(args[0]):
                lost = True                          # where the shell landed is unknown: reads after it name nothing
            elif args[0].startswith("/"):
                cd, lost = os.path.normpath(args[0]), False   # an absolute cd starts the chain again
            elif lost:
                lost = True                          # a relative cd from an unknown place is still unknown
            else:
                cd = os.path.normpath(os.path.join(cd, args[0])) if cd else args[0]
            continue
        if lost or words[0] not in READ_CMDS:
            continue
        targets = _seg_targets(words, cd)
        if not targets:
            continue
        if boundary == "|":                          # its output is piped on
            limit, ok = None, True
            j = idx + 1
            while j < len(segs) and segs[j - 1][1] == "|":
                n = _head_only_limit(_strip_redirects(segs[j][0]))
                if n is None:
                    ok = False
                    break
                limit = n if limit is None else min(limit, n)
                j += 1
            if not ok:
                continue
            for t in targets:
                t["limit"] = limit
        out.extend(targets)
    return out


def _pos(value):
    """offset / limit as a positive number. opencode sends numbers; a captured input can hold them as text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def read_tool_target(inp):
    """The read tool's own target: path plus its 1-based offset and limit as a line range (whole file without them).
    Input with no path, or one the shell only expanded into being, names no file."""
    if not isinstance(inp, dict):
        return None
    path = inp.get("path") or inp.get("filePath")
    if not isinstance(path, str) or _bad_path(path):
        return None
    off, lim = _pos(inp.get("offset")), _pos(inp.get("limit"))
    ranges = None
    if off or lim:
        start = off or 1
        end = start + lim - 1 if lim else None
        ranges = _merge_view_ranges([[start, end]])
    return _target(path, "", "read", ranges)


# A view is built from the file's own text, so it only ever comes from a file the guard above will read:
# inside the session's own directory, a real file, text, not enormous. A refused, missing, binary or huge
# file gets no view at all: its call stays a plain terminal entry. Fixture mode never touches the disk -
# there, only files whose contents the capture recorded get a view.

FIXTURE_NO_DISK = False

READS = BeforeReader(READS_MAX, READS_BYTES)   # what each call read, so one call reads each file once


def view_rel(dir_path, path, cd):
    """The file a read names, as a path inside the session, or None when it is refused: outside the session's
    own directory (symlinks included), a bare directory (or "." or ".."), or a path the shell would have had
    to expand to name at all. Resolving it does not touch the file."""
    if not dir_path or not path or _bad_path(path):
        return None
    root = os.path.realpath(dir_path)
    p = os.path.realpath(os.path.join(root, os.path.join(cd, path) if cd else path))
    if p == root or not p.startswith(root + os.sep):
        return None
    return p[len(root) + 1:]


def grep_ranges(out, target):
    """Which lines a grep read, and how many characters of its result did not reach the window. grep prints the
    line numbers itself, so they come from the call's own output and not from the command text, which has none
    in it. With several files grep names the file on every line, and only the lines belonging to this target
    count; a cut in the middle names no line, but it is counted, so the view can say it is incomplete."""
    want = os.path.normpath(target.get("path") or "")
    res, cut = [], 0
    for line in split_lines(out or ""):
        gap = OUTPUT_GAP_RE.match(line)
        if gap:
            cut += int(gap.group(1))
            continue
        num = ""
        if target.get("many"):
            parts = line.split(":", 2)
            if len(parts) >= 3 and parts[1].isdigit() and os.path.normpath(parts[0]) == want:
                num = parts[1]
        else:
            head = line.split(":", 1)[0]
            if not head.isdigit():
                head = line.split("-", 1)[0]        # grep -C prints a context line as "42-text"
            if head.isdigit():
                num = head
        if num:
            res.append([int(num), int(num)])
    return res, cut


def view_of(text, target, out=None):
    """One read as a file view: the file's text, the lines the agent asked for marked, and where the marks
    sit in it. A file of up to VIEW_MAX_LINES lines is shown whole; a bigger one shows a window around the
    asked-for lines. grep views take their marks from the output, because grep chose those lines. A target
    with no range asked for the whole file. None means there is nothing to show.

    ``target`` may also be a list: several reads of the one file by one call, which share a view. Each
    keeps its own range and its own ``| head`` cut, the marks are the lines of all of them, and a read
    that asked for the whole file makes the whole file the range."""
    lines = split_lines(text or "")
    total = len(lines)
    if not total:
        return None
    targets = target if isinstance(target, list) else [target]
    whole, wanted, cut, note = False, [], 0, ""
    for tg in targets:
        if tg.get("grep"):
            w, c = grep_ranges(out, tg)
            cut += c
        elif _pos(tg.get("tail")):
            w = [[total - _pos(tg.get("tail")) + 1, total]]   # tail -N: the file's last N lines
        else:
            w = [list(r) for r in tg.get("ranges") or []]
            if not w and not (isinstance(tg.get("limit"), int) and tg.get("limit") > 0):
                whole = True                                  # a read with no range asked for the whole file
                continue
            if not w:
                w = [[1, None]]                               # the whole file, cut by a trailing head
        clipped = []
        for s, e in w:
            s = max(1, int(s))
            e = total if e is None else min(int(e), total)
            if s <= e:
                clipped.append([s, e])
        limit = tg.get("limit")
        if isinstance(limit, int) and limit > 0:              # "… | head -n N": only the first N lines
            kept, seen, before = [], 0, sum(e - s + 1 for s, e in clipped)   # ever reached the agent
            for s, e in clipped:
                if seen >= limit:
                    break
                e = min(e, s + limit - seen - 1)
                kept.append([s, e])
                seen += e - s + 1
            clipped = kept
            if seen < before:
                note = note or "the command only printed the first %d lines" % limit
        wanted.extend(clipped)
    if whole:
        clipped = [[1, total]]          # a read asked for the whole file: that is the range
        cut, note = 0, ""               # and as every line is marked, no read's own cuts matter any more
    else:
        clipped = _merge_view_ranges(wanted)
    if not clipped:
        return None                     # e.g. a grep that matched nothing: nothing to mark
    if cut:
        note = note or "the result was cut short, so some matched lines are not marked"
    if total > VIEW_MAX_LINES:
        start = max(1, clipped[0][0] - VIEW_PAD)
        end = min(total, clipped[-1][1] + VIEW_PAD)
        if end - start + 1 > VIEW_MAX_LINES:
            end = start + VIEW_MAX_LINES - 1
        note = note or "showing lines %d-%d of %d" % (start, end, total)
    else:
        start, end = 1, total
    marks = []
    for s, e in clipped:
        s, e = max(s, start), min(e, end)
        if s <= e:
            marks.append([s, e])
    if not marks:
        return None
    hows = []
    for tg in targets:                              # one view, the verbs that read the file: "sed + grep"
        h = tg.get("how") or ""
        if h and h not in hows:
            hows.append(h)
    return {"total": total, "from": start, "lines": lines[start - 1:end], "ranges": marks,
            "how": " + ".join(hows), "note": note}


def file_view(cid, target, dir_path, out, live, reads=None, rel=None):
    """Read the file one call's targets name and build its view. Several targets of one call naming the
    same file give one view with all their lines marked. Each call reads each file at most once, and the
    same guard the diffs use decides whether the file may be read at all. None when it may not, or when
    the read turned out to name no lines (a grep that matched nothing). now=True marks a view whose text
    was read after the call, so it is the file as it is now rather than as the agent found it then."""
    reads = READS if reads is None else reads
    targets = target if isinstance(target, list) else [target]
    if rel is None:
        rel = view_rel(dir_path, targets[0].get("path") or "", targets[0].get("cd") or "")
    if not rel:
        return None
    key = "%s\x00%s" % (cid, rel)
    seen = reads.has(key)
    if not seen:
        if FIXTURE_NO_DISK:
            return None                      # fixture mode: only files the capture recorded get a view
        body = read_within(dir_path, rel)
        if not body:
            return None                      # refused, too big, binary, or not there (yet): nothing to show,
        reads.put(key, body)                 # and nothing kept, so a later look can still find the file
    view = view_of(reads.get(key), targets, out)
    if not view:
        return None
    view["path"] = rel
    view["now"] = (not live) and not seen   # live text is what the agent read; a cached one was read live too
    return view


def call_targets(name, inp):
    """The files one tool call reads: the read tool's own path and range, or what a shell command reads.
    Every other tool - and any command we are not sure about - names nothing."""
    if not isinstance(inp, dict):
        return None
    if name == "read":
        target = read_tool_target(inp)
        return [target] if target else None
    if name == "shell":
        cmd = inp.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return read_targets(cmd) or None
    return None


GIT_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _git_path(text: str) -> str | None:
    """The file path in a ``--- a/`` / ``+++ b/`` header (the part after the three dashes), or None
    for /dev/null. Strips the a/ b/ prefix, an optional tab-separated timestamp, and quotes."""
    path = text.split("\t", 1)[0].strip()
    if path.startswith('"') and path.endswith('"') and len(path) > 1:
        path = path[1:-1]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return None if not path or path == "/dev/null" else path


def parse_git_diff(text: str) -> tuple:
    """Parse the full text output of a git diff/show command into per-file entries.

    Returns ``(files, note)``. Each file is ``{"path", "hunks", "added", "removed"}`` with hunks and
    rows in the same shape build_diff makes (see there for the row format), so the page renders both
    the same way; ``added``/``removed`` count the rows actually stored, and word marks pair a removed
    line with the similar added line below it like stage 1 does. Paths come from the ``+++ b/`` header
    (or ``--- a/`` for a deleted file). Git's commit header, mode/index/binary lines and
    "\\ No newline" notes are skipped; a file with no ``@@`` body is not reported. When the whole call
    passes GIT_ROW_CAP rows parsing stops and note says so. Returns ([], None) for text that is not
    a diff.
    """
    files: list = []
    capped = False
    rows = 0
    path: str | None = None       # path from the file header being read
    header_path: str | None = None  # fallback path from the diff --git line
    hunk: list | None = None      # rows of the @@ body being read
    hunk_old = 0                  # old-file line number the next body row gets
    hunk_new = 0                  # new-file line number the next body row gets
    cur: dict | None = None       # the file the open hunk belongs to

    def flush_hunk() -> None:
        # Pair each - run with the + run right after it before closing the hunk.
        nonlocal hunk
        if hunk and cur is not None:
            i = 0
            while i < len(hunk):
                if hunk[i][0] != "-":
                    i += 1
                    continue
                j = i
                while j < len(hunk) and hunk[j][0] == "-":
                    j += 1
                if j >= len(hunk) or hunk[j][0] != "+":
                    i = j
                    continue
                k = j
                while k < len(hunk) and hunk[k][0] == "+":
                    k += 1
                for z in range(min(j - i, k - j)):  # pair the k-th removed line with the k-th added one
                    pm = pair_marks(hunk[i + z][3], hunk[j + z][3])
                    if pm:
                        hunk[i + z][4] = pm[0]
                        hunk[j + z][4] = pm[1]
                i = k
            cur["removed"] += sum(1 for row in hunk if row[0] == "-")
            cur["added"] += sum(1 for row in hunk if row[0] == "+")
            cur["hunks"].append(_mk_hunk(hunk))
        hunk = None

    def flush_file() -> None:
        nonlocal cur, path, header_path
        flush_hunk()
        if cur is not None and cur["hunks"]:
            files.append(cur)
        cur = None
        path = None
        header_path = None

    def body_row(kind: str, text: str) -> None:
        nonlocal rows, hunk_old, hunk_new, capped
        if hunk is None:
            return
        if rows >= GIT_ROW_CAP:
            capped = True
            return
        rows += 1
        if kind == " ":
            hunk.append([kind, hunk_old, hunk_new, text, []])
            hunk_old += 1
            hunk_new += 1
        elif kind == "-":
            hunk.append([kind, hunk_old, None, text, []])
            hunk_old += 1
        else:
            hunk.append([kind, None, hunk_new, text, []])
            hunk_new += 1

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush_file()
            parts = line.split(" b/", 1)
            header_path = _git_path("b/" + parts[1]) if len(parts) > 1 else None
        elif line.startswith("+++") and len(line) > 3:
            p = _git_path(line[3:].strip())
            if p is not None:  # a deleted file's +++ side is /dev/null; its path stays the --- one
                path = p
            flush_hunk()
        elif line.startswith("---") and len(line) > 3:
            if path is None and hunk is None:
                # A deleted file has no +++ b/ side; its path comes from the --- header.
                path = _git_path(line[3:].strip())
            flush_hunk()
        elif line.startswith("@@"):
            m = GIT_HUNK_RE.match(line)
            if not m:
                flush_file()
                continue
            flush_hunk()
            if cur is None:
                cur = {"path": path or header_path or "", "hunks": [], "added": 0, "removed": 0}
            hunk = []
            hunk_old = int(m.group(1))
            hunk_new = int(m.group(3))
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            body_row(line[0], line[1:])
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
    if rows >= GIT_ROW_CAP:
        capped = True
    flush_file()
    note = "diff too long to show in full" if capped else None
    return files, note


def _git_diff_segment(segment: list) -> bool:
    """True when one command segment (a list of shell words) runs ``git diff`` or ``git show``."""
    i = 0
    while i < len(segment):
        w = segment[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1  # VAR=value prefix
            continue
        break
    if i >= len(segment):
        return False
    if segment[i].split("/")[-1] != "git":
        return False
    i += 1
    while i < len(segment):
        w = segment[i]
        if w in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"):
            i += 2  # a global option and its value
            continue
        if w.startswith("-"):
            i += 1  # any other flag: --no-pager, --cached, …
            continue
        return w in ("diff", "show")
    return False


def is_git_diff_command(command) -> bool:
    """True when a shell command's text runs git diff / git show (``git -C dir diff``,
    ``git --no-pager show``, ``cd x && git diff``, ``git diff --cached``, …).

    Decided from the command text alone — nothing here runs git. ``cat changes.diff``, ``git status``
    and a quoted "git diff" argument are not git diff commands.
    """
    if not isinstance(command, str) or ("diff" not in command and "show" not in command):
        return False
    try:
        toks = _tokens(command)
    except ValueError:
        return False                                # unbalanced quotes: cannot tell, so not a diff
    for raw, _ in _segments(toks):
        if _git_diff_segment(_strip_redirects(raw)):
            return True
    return False


def full_content_text(content) -> str:
    """Join a tool result's text parts with no cap (content_text caps shell output)."""
    if not content:
        return ""
    parts = []
    for c in content:
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text", "")))
    return "\n".join(parts)


def gitdiff_of_call(inp, content) -> tuple:
    """(files, note) when a shell call runs git diff / git show and its full output parses as one or
    more file diffs; (None, None) for any other call or output (``cat changes.diff`` gets nothing).
    """
    command = inp.get("command") if isinstance(inp, dict) else None
    if not is_git_diff_command(command):
        return None, None
    files, note = parse_git_diff(full_content_text(content))
    if not files:
        return None, None
    return files, note


def call_view_groups(name, inp, dir_path):
    """The files a call reads, grouped by the file each target resolves to: every range the call asked of
    one file shares one view of it. Targets are kept in the order the command named them; a whole-file
    read of a file makes that file's view the whole file. Targets that resolve to nothing safe or known
    (outside the session's directory, a path we are not sure about) are left out; at most
    VIEWS_PER_CALL groups are ever shown."""
    groups = []
    for tg in call_targets(name, inp) or []:
        rel = view_rel(dir_path, tg.get("path") or "", tg.get("cd") or "")
        if not rel:
            continue
        for i, (r, ts) in enumerate(groups):
            if r == rel:
                groups[i] = (rel, ts + [tg])
                break
        else:
            groups.append((rel, [tg]))
    return groups[:VIEWS_PER_CALL]


def step_from_message(msg, reader=None, reads=None, dir_path=None):
    """One assistant message (= one model step) as the window draws it. reader: a BeforeReader with cached
    "before" contents; edit/write items whose file was read get the same diff the live view shows. reads +
    dir_path: the same for read/shell items - the file they read, as a view with the read lines marked."""
    t = msg.get("time") or {}
    parts, counts = [], {}
    for p in msg.get("content") or []:
        kind = p.get("type")
        if kind in ("reasoning", "text"):
            o = counts.get(kind, 0)
            counts[kind] = o + 1
            pt = p.get("time") or {}
            parts.append({"p": "think" if kind == "reasoning" else "text", "o": o,
                          "text": cap(p.get("text") or "", TEXT_CAP),
                          "done": bool(pt.get("completed")) or bool(t.get("completed")) or kind == "text",
                          "t0": pt.get("created"), "t1": pt.get("completed")})
        elif kind == "tool":
            st = p.get("state") or {}
            status = st.get("status") or "running"
            item = {"p": "tool", "id": p.get("id"), "name": p.get("name") or "tool", "status": status,
                    "input": slim_input(st.get("input"))}
            if status == "completed":
                item["out"] = content_text(st.get("content"), item["name"])
            elif status == "error":
                item["err"] = err_text(st.get("error")) if st.get("error") else content_text(st.get("content"))
            else:
                live = progress_output(st.get("metadata"))
                if live:
                    item["out"] = live
            pt = p.get("time") or {}
            item["t0"], item["t1"] = pt.get("created"), pt.get("completed") or pt.get("ended")
            if status == "completed" and item["name"] == "shell":
                # a git diff/show call: the same per-file diff the live window streams
                files, note = gitdiff_of_call(st.get("input"), st.get("content"))
                if files:
                    item["gitdiff"] = files
                    if note:
                        item["gitnote"] = note
            if reader is not None and item["name"] in ("edit", "write") and reader.has(item["id"]):
                d = diff_of_call(reader.get(item["id"]), item["name"], item["input"],
                                 streaming=status == "streaming")
                if d:
                    item["diff"] = d
            if reads is not None and dir_path and item["name"] in ("read", "shell"):
                got = []
                out = item.get("out") if status == "completed" else None
                for rel, tgts in call_view_groups(item["name"], st.get("input"), dir_path):
                    v = file_view(item["id"], tgts, dir_path, out, False, reads, rel=rel)
                    if v:
                        got.append(v)
                if got:
                    item["views"] = got
            parts.append(item)
    return {"id": msg.get("id"), "model": (msg.get("model") or {}).get("id"), "t0": t.get("created"),
            "t1": t.get("completed"), "tokens": msg.get("tokens"), "finish": msg.get("finish"), "parts": parts}


def turn_messages(sid, m):
    """Messages after user message m, oldest first, up to the turn's end. Returns (messages, ended)."""
    out, cursor = [], asc_cursor(m)
    for _ in range(MAX_PAGES):
        d = oc_get("/api/session/%s/message?limit=%d&cursor=%s" % (sid, PAGE, cursor), timeout=30)
        page = d.get("data") or []
        for msg in page:
            if msg.get("type") in ("user", "idle"):
                return out, True
            out.append(msg)
        cursor = (d.get("cursor") or {}).get("next")
        if not page or not cursor or len(page) < PAGE:
            break
    return out, False


def snapshot(sid, m, settle=False):
    """The turn so far. settle=True: the run is known to be over, whatever the history looks like."""
    msgs, ended = turn_messages(sid, m)
    steps, notes = [], []
    try:
        dir_path = session_dir(sid)   # file views read from the session's own directory, and nowhere else
    except Exception:
        dir_path = None
    compacting = None
    for msg in msgs:
        if msg.get("type") == "assistant":
            steps.append(step_from_message(msg, BEFORE, READS, dir_path))
            if compacting and compacting["ph"] == "end" and msg.get("content"):
                compacting = None   # the step after it has started answering: the new context is read
        elif msg.get("type") == "compaction":
            notes.append({"after": steps[-1]["id"] if steps else None, "text": "Compacted the conversation to fit the context"})
            compacting = compaction_state(msg)
    # No messages yet and not ended = the prompt is still being filed: that is a run about to start.
    running = not settle and not ended and (not msgs or session_active(sid))
    if not running:
        compacting = None
    try:
        prompt = oc_get("/api/session/%s/message/%s" % (sid, m), timeout=10).get("data") or {}
    except Exception:
        prompt = {}
    since = (prompt.get("time") or {}).get("created")
    review = session_review(sid)
    text = prompt.get("text") or ""
    snap = {"k": "snap", "steps": steps, "notes": notes, "running": running, "since": since,
            "review": review,               # {"round": N} for a reviewer's session, None for the chat's own
            "compacting": compacting,       # a compaction under way, or just ended with the new context being read
            "now": int(time.time() * 1000)}
    if review and text:
        snap["brief"] = cap(text, BRIEF_CAP)     # what Code mode gave the reviewer
    elif text.startswith(HANDBACK_HEAD):
        snap["handback"] = {"findings": handback_findings(text)}   # this run is the builder fixing a review's findings
    return snap


def handback_findings(text):
    """The findings a review sent back, from the builder's prompt (review_fix_prompt): its "- [kind] ..." lines."""
    out = []
    for line in (text or "").split("Go through them one by one")[0].splitlines():
        if line.startswith("- ["):
            out.append(line[2:].strip()[:600])
    return out[:10]


def has_output(steps):
    """Whether a turn's steps hold anything the model produced yet (thinking, text or a tool call)."""
    return any(p.get("p") == "tool" or (p.get("text") or "").strip() for st in steps for p in st.get("parts") or [])


def compaction_state(msg):
    """A compaction message as the window's overlay starts from: still running (it reads the history), or done with
    the step after it still reading the new context."""
    tm = msg.get("time") or {}
    if msg.get("status") in ("running", "pending", None) and not msg.get("summary"):
        return {"ph": "start", "t0": tm.get("created")}
    if msg.get("status") == "completed":
        return {"ph": "end", "t0": tm.get("created"), "t1": tm.get("completed"), "text": cap(msg.get("summary") or "", TEXT_CAP),
                "tokens": msg.get("tokens"), "recent": len(msg.get("recent") or "")}
    return None


def _first_line(s, n=TAPE_LINE):
    s = str(s or "").strip()
    s = re.sub(r"^cd \S+ && ", "", s)  # the agent's habitual "cd <project> && ..."
    return s.split("\n", 1)[0][:n]


def tape_lines(msg, dir_path=None):
    """One history message as the compaction tape's short lines: [{"k", "s", "w"}], w = its size in characters (what
    the model reads of it, roughly), so the page can place its read position by the engine's token count."""
    kind = msg.get("type")
    if kind == "user":
        text = msg.get("text") or ""
        return [{"k": "you", "s": _first_line(text), "w": len(text)}] if text.strip() else []
    if kind == "compaction":
        sm = msg.get("summary") or ""
        return [{"k": "sum", "s": "The summary from the last compaction", "w": len(sm) + len(msg.get("recent") or "")}]
    if kind != "assistant":
        return []
    out = []
    for p in msg.get("content") or []:
        pk = p.get("type")
        if pk == "text" and (p.get("text") or "").strip():
            out.append({"k": "says", "s": _first_line(p["text"]), "w": len(p["text"])})
        elif pk == "reasoning":
            if out and out[-1]["k"] != "think" or not out:
                out.append({"k": "think", "s": _first_line(p.get("text")) or "thinking", "w": len(p.get("text") or "")})
            else:
                out[-1]["w"] += len(p.get("text") or "")
        elif pk == "tool":
            st = p.get("state") or {}
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            name = p.get("name") or "tool"
            size = len(json.dumps(st.get("input") or "", ensure_ascii=False)) + len(full_content_text(st.get("content")))
            path = inp.get("filePath") or inp.get("path") or ""
            if dir_path and isinstance(path, str) and path.startswith(dir_path.rstrip("/") + "/"):
                path = path[len(dir_path.rstrip("/")) + 1:]
            if name == "shell":
                line = {"k": "sh", "s": _first_line(inp.get("command"))}
            elif name in ("edit", "write", "read"):
                line = {"k": name, "s": path}
            elif name in ("grep", "glob"):
                line = {"k": "sh", "s": "%s %s" % (name, _first_line(inp.get("pattern"), 80))}
            else:
                line = {"k": "tool", "s": name}
            line["w"] = size
            out.append(line)
    return out


def compaction_tape(sid):
    """What a compaction reads, as tape lines: the session's messages back to its last finished compaction (that one's
    summary first), oldest first, capped at the newest TAPE_MAX. Also returns the model of the newest step."""
    msgs, cursor, model = [], None, None
    for _ in range(TAPE_PAGES):
        q = "/api/session/%s/message?limit=%d" % (sid, PAGE) + ("&cursor=%s" % cursor if cursor else "&order=desc")
        d = oc_get(q, timeout=30)
        page = d.get("data") or []
        stop = False
        for msg in page:
            if model is None and msg.get("type") == "assistant":
                model = (msg.get("model") or {}).get("id")
            if msg.get("type") == "compaction":
                if msg.get("status") == "completed":
                    msgs.append(msg)
                    stop = True
                    break
                continue   # the compaction that is running now
            msgs.append(msg)
        cursor = (d.get("cursor") or {}).get("next")
        if stop or not page or not cursor:
            break
    try:
        dir_path = session_dir(sid)
    except Exception:
        dir_path = None
    lines = []
    for msg in reversed(msgs):
        lines.extend(tape_lines(msg, dir_path))
    return lines[-TAPE_MAX:], model


def strata_live(model=None, url=None):
    """The engine's progress for this model from the slerm panel: {state, read, total, prompt, cached, gen, rate,
    pps}, or None when no server for it is up (or the panel cannot be reached)."""
    url = url or STRATA_LIVE
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            doc = json.load(r)
    except Exception:
        return None
    for s in doc.get("servers") or []:
        if model and s.get("loaded") != model:
            continue
        if not model and s.get("engine") != "strata":
            continue
        live = (s.get("monitor") or {}).get("live") or {}
        slot = (s.get("slots") or [{}])[0] or {}
        return {"state": live.get("state"), "read": live.get("prompt_read"), "total": live.get("prompt_total"),
                "prompt": live.get("prompt_tokens"), "cached": slot.get("prompt_cached"), "gen": live.get("generated"),
                "rate": live.get("tok_s"), "pps": live.get("prefill_tok_s_mean")}
    return None


ENGINE_TTL = 1.5     # seconds one /api/ai/live answer serves every Watch page that asks
_engine_cache = {"at": 0.0, "doc": None}
_engine_lock = threading.Lock()


def engine_view(doc, model=None):
    """The slerm panel's live view of its model servers, cut to what Watch mode shows: what the engine is doing, its
    speed, the context of the request in hand, both GPUs, the node, and the last requests. None without a server."""
    servers = (doc or {}).get("servers") or []
    pick = next((s for s in servers if model and s.get("loaded") == model), None) or \
        next((s for s in servers if s.get("engine") == "strata"), None) or (servers[0] if servers else None)
    if not pick:
        return None
    mon = pick.get("monitor") or {}
    live, feed, eng = mon.get("live") or {}, mon.get("feed") or {}, mon.get("engine") or {}
    host = feed.get("host_stats") or {}
    num = lambda x: x if isinstance(x, (int, float)) else None
    return {
        "model": pick.get("loaded"), "node": pick.get("node"), "engine": pick.get("engine"),
        "state": live.get("state"), "phase": live.get("phase"), "tok_s": num(live.get("tok_s")),
        "tok_s_mean": num(live.get("tok_s_mean")), "generated": num(live.get("generated")),
        "prompt_tokens": num(live.get("prompt_tokens")), "prompt_read": num(live.get("prompt_read")),
        "prompt_total": num(live.get("prompt_total")), "pps": num(live.get("prefill_tok_s_mean")),
        "max_context": num(eng.get("max_context")),
        "gpus": [{"name": g.get("name"), "util": num(g.get("util_pct")), "mem_used": num(g.get("mem_used_mb")),
                  "mem_total": num(g.get("mem_total_mb")), "temp": num(g.get("temp_c")), "power": num(g.get("power_w")),
                  "pcie_rx": num(g.get("pcie_rx_mb_s"))} for g in (feed.get("gpus") or [])[:4]],
        "host": {"mem_used": num(host.get("mem_used_mb")), "mem_total": num(host.get("mem_total_mb")),
                 "cpu": num(host.get("cpu_pct")), "cpus": num(host.get("cpus")), "disk_read": num(host.get("disk_read_mb_s"))},
        "requests": [{"prompt": num(r.get("prompt_tokens")), "reused": num(r.get("reused")), "read": num(r.get("prompt_read")),
                      "out": num(r.get("output_tokens")), "tok_s": num(r.get("decode_tok_s")), "secs": num(r.get("duration_s")),
                      "t": num(r.get("time"))} for r in (mon.get("requests") or [])[:12]],
    }


def engine_now(url=None):
    """engine_view of the panel's answer, shared for ENGINE_TTL seconds; None when the panel cannot be reached."""
    url = url or STRATA_LIVE
    if not url:
        return None
    with _engine_lock:
        if _engine_cache["doc"] is not None and time.monotonic() - _engine_cache["at"] < ENGINE_TTL:
            return _engine_cache["doc"]
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                doc = engine_view(json.load(r))
        except Exception:
            doc = None
        _engine_cache.update(at=time.monotonic(), doc=doc)
        return doc


STATS_TTL = 20.0     # seconds one count of a session's tool calls serves before its new messages are read
STATS_PAGES = 400    # pages of 200 messages read in one go at most
_stats = {}          # session id -> {"last": message id, "tools": {...}, "steps", "sendbacks", "kinds": {...}, "at"}
_stats_lock = threading.Lock()


def tally_messages(msgs, st):
    """Add messages to a session's counts: an assistant message is a step and its tool parts are calls; a prompt that
    is a review send-back (HANDBACK_HEAD) counts with the kinds of its findings."""
    for msg in msgs:
        kind = msg.get("type")
        if kind == "assistant":
            st["steps"] += 1
            for p in msg.get("content") or []:
                if p.get("type") == "tool":
                    name = p.get("name") or "tool"
                    st["tools"][name] = st["tools"].get(name, 0) + 1
        elif kind == "user" and (msg.get("text") or "").startswith(HANDBACK_HEAD):
            st["sendbacks"] += 1
            for f in handback_findings(msg.get("text")):
                k = f[1:].split("]", 1)[0] if f.startswith("[") else "finding"
                st["kinds"][k] = st["kinds"].get(k, 0) + 1


def session_stats(sid):
    """A session's tool calls by name, steps and review send-backs over its whole life: read once (a long session is a
    few seconds), then only the messages after the last one counted."""
    with _stats_lock:
        st = _stats.get(sid)
        if st is None:
            st = _stats[sid] = {"last": None, "tools": {}, "steps": 0, "sendbacks": 0, "kinds": {}, "at": 0.0}
        if time.monotonic() - st["at"] >= STATS_TTL:
            q = "order=asc" if not st["last"] else "cursor=" + asc_cursor(st["last"])
            for _ in range(STATS_PAGES):
                d = oc_get("/api/session/%s/message?limit=200&%s" % (sid, q), timeout=60)
                page = d.get("data") or []
                tally_messages(page, st)
                if page:
                    st["last"] = page[-1].get("id") or st["last"]
                nxt = (d.get("cursor") or {}).get("next")
                if not page or not nxt or len(page) < 200:
                    break
                q = "cursor=" + nxt
            st["at"] = time.monotonic()
        return {"tools": dict(st["tools"]), "steps": st["steps"], "sendbacks": st["sendbacks"], "kinds": dict(st["kinds"])}


def tools_view(sid):
    """Watch mode's Tools tab: the session's counts, and opencode's MCP servers with the calls their tools got
    (opencode names an MCP tool <server>_<tool>)."""
    st = session_stats(sid)
    try:
        servers = oc_get("/api/mcp", timeout=10).get("data") or []
    except Exception:
        servers = []
    mcp = []
    for s in servers:
        name = s.get("name") or ""
        calls = sum(n for t, n in st["tools"].items() if name and (t.startswith(name + "_") or t.startswith(name + ":")))
        mcp.append({"name": name, "status": (s.get("status") or {}).get("status"), "calls": calls})
    tools = sorted(st["tools"].items(), key=lambda kv: (-kv[1], kv[0]))
    return {"tools": tools, "calls": sum(st["tools"].values()), "steps": st["steps"], "mcp": mcp,
            "sendbacks": st["sendbacks"], "kinds": sorted(st["kinds"].items(), key=lambda kv: (-kv[1], kv[0]))}


class CompactionWatch:
    """While a compaction shows in one window: the history it reads (once) and the engine's progress (every
    COMPACT_POLL seconds), put on that window's queue as ready-made events. It stops when told (done, failed, the turn
    ended, the window closed) or after COMPACT_MAX seconds."""

    def __init__(self, sid, q, tape=None, live=None):
        self.sid, self.q, self.tape, self.live = sid, q, tape, live   # None: this module's own, looked up when used
        self.halt = threading.Event()
        self.thread = None
        self.model = None

    def running(self):
        return bool(self.thread and self.thread.is_alive() and not self.halt.is_set())

    def start(self, with_tape=True):
        if self.running():
            return
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(with_tape, self.halt), name="compact", daemon=True)
        self.thread.start()

    def stop(self):
        self.halt.set()

    def _put(self, item):
        try:
            self.q.put_nowait(item)
        except queue.Full:
            pass

    def _run(self, with_tape, halt):
        began = time.monotonic()
        if with_tape:
            try:
                lines, self.model = (self.tape or compaction_tape)(self.sid)
                if not halt.is_set():
                    self._put({"k": "compact", "ph": "tape", "lines": lines})
            except Exception as ex:
                log("compaction tape %s: %s" % (self.sid, type(ex).__name__))
        while not halt.is_set() and time.monotonic() - began < COMPACT_MAX:
            p = (self.live or strata_live)(self.model)
            if halt.is_set():
                break
            self._put(dict(p or {"state": None}, k="cprog"))
            halt.wait(COMPACT_POLL)


def turn_is_live(sid, m):
    """Cheap check before replaying anything: is m the session's newest prompt, and is the session running?"""
    d = oc_get("/api/session/%s/message?type=user&order=desc&limit=1" % sid, timeout=15)
    newest = (d.get("data") or [{}])[0].get("id")
    if newest and newest != m:
        return m > newest  # ids are time-ordered: a newer m is a prompt opencode hasn't filed as a message yet
    return session_active(sid)


def shape_event(e, names=None):
    """An opencode event as the window's small vocabulary (None = not shown). names: tool call id -> tool name,
    kept by the caller (only input.started names the tool; results are sized by it)."""
    t, d = e.get("type", ""), e.get("data") or {}
    names = names if names is not None else {}
    mid = d.get("assistantMessageID")
    if t == "session.step.started":
        return {"k": "step", "id": mid, "model": (d.get("model") or {}).get("id"), "t0": d.get("started") or e.get("created")}
    if t == "session.step.ended":
        return {"k": "stepend", "id": mid, "tokens": d.get("tokens"), "finish": d.get("finish"), "t1": e.get("created")}
    if t == "session.step.failed":
        err = d.get("error") or {}
        if isinstance(err, dict) and err.get("type") == "aborted":
            return None
        return {"k": "note", "level": "err", "text": "Step failed: " + err_text(err)}
    for kind, name in (("reasoning", "think"), ("text", "text")):
        if t == "session.%s.started" % kind:
            return {"k": name + "0", "id": mid, "o": d.get("ordinal", 0), "t0": e.get("created")}
        if t == "session.%s.delta" % kind:
            return {"k": name + "+", "id": mid, "o": d.get("ordinal", 0), "d": d.get("delta") or ""}
        if t == "session.%s.ended" % kind:
            return {"k": name + ".", "id": mid, "o": d.get("ordinal", 0), "text": cap(d.get("text") or "", TEXT_CAP),
                    "t1": e.get("created")}
    if t == "session.tool.input.started":
        names[d.get("id")] = d.get("name")
        return {"k": "tool", "id": d.get("id"), "msg": mid, "name": d.get("name") or "tool", "status": "streaming",
                "t0": e.get("created")}
    if t == "session.tool.input.delta":
        return {"k": "tool+", "id": d.get("id"), "msg": mid, "d": d.get("delta") or ""}
    if t == "session.tool.input.ended":
        return {"k": "tool", "id": d.get("id"), "msg": mid, "raw": cap(d.get("text") or "", INPUT_CAP)}
    if t == "session.tool.called":
        return {"k": "tool", "id": d.get("id"), "msg": mid, "status": "running", "input": slim_input(d.get("input"))}
    if t == "session.tool.progress":
        out = progress_output(d.get("metadata"))
        return {"k": "tool", "id": d.get("id"), "msg": mid, "out": out} if out else None
    if t == "session.tool.success":
        return {"k": "tool", "id": d.get("id"), "msg": mid, "status": "completed",
                "out": content_text(d.get("content"), names.get(d.get("id"))),
                "t1": e.get("created")}
    if t in ("session.tool.failed", "session.tool.error"):
        return {"k": "tool", "id": d.get("id"), "msg": mid, "status": "error", "err": err_text(d.get("error")),
                "t1": e.get("created")}
    if t == "permission.asked":
        what = "%s: %s" % (d.get("action", "action"), "; ".join(d.get("resources") or []))
        return {"k": "note", "level": "ask", "text": "Asking for approval in the chat — " + what[:300]}
    if t == "permission.replied":
        return {"k": "note", "text": "Approved" if d.get("reply") in ("once", "always") else "Refused"}
    if t == "form.created":
        fields = (d.get("form") or {}).get("fields") or []
        q = (fields[0].get("description") or fields[0].get("title")) if fields else "a question"
        return {"k": "note", "level": "ask", "text": "Asking you in the chat — " + str(q)[:300]}
    if t == "session.retry.scheduled":
        return {"k": "note", "level": "err", "text": "Model call failed (%s); retry %s" % (err_text(d.get("error")), d.get("attempt", ""))}
    if t == "session.compaction.started":
        return {"k": "compact", "ph": "start", "reason": d.get("reason"), "t0": e.get("created")}
    if t == "session.compaction.delta":
        return {"k": "compact", "ph": "delta", "d": d.get("text") or ""}
    if t == "session.compaction.ended":
        return {"k": "compact", "ph": "end", "text": cap(d.get("text") or "", TEXT_CAP), "tokens": d.get("tokens"),
                "recent": len(d.get("recent") or ""), "t1": e.get("created")}
    if t == "session.compaction.failed":
        return {"k": "compact", "ph": "fail", "err": err_text(d.get("error"))}
    if t.startswith("session.execution.") and t.rsplit(".", 1)[1] in ("succeeded", "failed", "interrupted"):
        return {"k": "end", "how": t.rsplit(".", 1)[1]}
    return None


def session_of(e):
    d = e.get("data") or {}
    return d.get("sessionID") or (d.get("form") or {}).get("sessionID")


def views_of_event(shaped: dict, names: dict, dir_path: str, call_reads: dict) -> list:
    """File views for one tool event of a shell or read call: the file(s) that call reads, with the
    lines it asked for marked, as ``view`` events to send after the tool event they belong to.

    ``call_reads`` remembers, per call id, which files the call's command named and which of them
    have already been viewed, so a file is read from disk once and shown once per call: reads of the
    same file by one call share a single view that marks all their lines. A call is looked at as soon
    as its input is known (a file its command creates part-way through is caught by the last look,
    when the call ends), which is also why a view recorded while the turn was live is marked
    ``now`` = False: it shows the text the agent actually read.

    A ``grep`` target is the exception: its marks are the lines it matched, which only exist once the
    call is over, and a failed call has none.
    """
    cid = shaped.get("id") or ""
    if not cid or not dir_path or shaped.get("k") != "tool":
        return []
    rec = call_reads.get(cid)
    if rec is None:
        if "input" not in shaped:
            return []
        groups = call_view_groups(shaped.get("name") or names.get(cid) or "", shaped["input"], dir_path)
        if not groups:
            return []
        rec = call_reads[cid] = {"groups": groups, "sent": set(), "tried": set()}
    status = shaped.get("status") or ""
    out = shaped.get("out") if status == "completed" else None
    res = []
    for rel, tgts in rec["groups"]:
        if len(rec["sent"]) >= VIEWS_PER_CALL:
            break
        if any(t.get("grep") for t in tgts):
            if status != "completed":
                continue                      # their marks come from the command's own output
        elif rel in rec["tried"] and status != "completed":
            continue                          # looked once already; one last look when the call ends
        if rel in rec["sent"]:
            continue
        rec["tried"].add(rel)
        view = file_view(cid, tgts, dir_path, out, True, rel=rel)
        if view:
            rec["sent"].add(rel)
            view["k"], view["id"] = "view", cid
            res.append(view)
    return res


# ---------------------------------------------------------------- event hub

class Hub:
    """One shared subscription to opencode's /api/event, fanned out to open windows by session id. A dropped
    stream tells every window to re-read the turn ("gap"), so nothing goes missing silently."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = {}
        self.connected = threading.Event()
        self.thread = None

    def subscribe(self, sid):
        q = queue.Queue(maxsize=20000)
        with self.lock:
            self.subs.setdefault(sid, set()).add(q)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="hub", daemon=True)
                self.thread.start()
        return q

    def unsubscribe(self, sid, q):
        with self.lock:
            s = self.subs.get(sid)
            if s:
                s.discard(q)
                if not s:
                    del self.subs[sid]

    def _deliver(self, sid, item):
        with self.lock:
            targets = list(self.subs.get(sid, ())) if sid else [q for s in self.subs.values() for q in s]
        for q in targets:
            try:
                q.put_nowait(item)
            except queue.Full:  # a stalled window: make it start over instead of growing without bound
                with q.mutex:
                    q.queue.clear()
                q.put_nowait({"k": "gap"})

    def _run(self):
        while True:
            try:
                req = urllib.request.Request(OPENCODE + "/api/event",
                                             headers={"Authorization": auth_header(), "Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=600) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            e = json.loads(line[5:])
                        except ValueError:
                            continue
                        if e.get("type") == "server.connected":
                            self.connected.set()
                            continue
                        sid = session_of(e)
                        if sid:
                            self._deliver(sid, e)
            except Exception as ex:
                log("hub: event stream dropped (%s)" % type(ex).__name__)
            self.connected.clear()
            self._deliver(None, {"k": "gap"})
            time.sleep(2)


HUB = Hub()


# ---------------------------------------------------------------- fixture mode

FIXTURE_SECRET = "code-live-fixture"  # a fixture server answers only 127.0.0.1, so its links sign themselves


def fixture_chat(fx):
    """The chat id a fixture link is signed for: the capture's own, or the one its session directory names."""
    chat = (fx["session"].get("metadata") or {}).get("owui_chat_id") or ""
    if not CHAT_RE.match(chat):
        chat = os.path.basename((fx["session"].get("directory") or "").rstrip("/"))
    return chat if CHAT_RE.match(chat) else "fixture"


def fixture_turn(fx):
    """The prompt the captured turn starts from (the one before its first edit/write), and how many edit/write
    calls that turn made."""
    user, start, calls = None, None, 0
    for msg in fx["messages"]:
        if msg.get("type") == "user":
            user = msg.get("id")
            continue
        for p in msg.get("content") or []:
            if p.get("type") == "tool" and p.get("name") in ("edit", "write"):
                calls += 1
                if start is None:
                    start = user
    return start or user, calls


def fixture_seed(fx):
    """Fill the caches the way the live reads would have: each file starts at the content the capture found it
    at, and every edit/write advances it for the calls after it. Each read/shell call is given the content its
    files had at that moment, so a captured turn shows the same file views the live window would have shown -
    without reading this machine's disk. A call whose input the capture dropped gets nothing, exactly as a
    refused read would. Returns how many edit/write calls got a before, and how each file ended up."""
    content = {p: (r.get("before") or "") for p, r in (fx.get("files") or {}).items()}
    dir_path = (fx.get("session") or {}).get("directory") or ""
    BEFORE.clear()
    READS.clear()
    seeded = 0
    for msg in fx["messages"]:
        if msg.get("type") != "assistant":
            continue
        for p in msg.get("content") or []:
            if p.get("type") != "tool" or not p.get("id"):
                continue
            inp = (p.get("state") or {}).get("input")
            if p.get("name") not in ("edit", "write"):
                for tg in (call_targets(p.get("name") or "", inp) or []):
                    rel = view_rel(dir_path, tg.get("path") or "", tg.get("cd") or "")
                    if rel in content:
                        READS.put("%s\x00%s" % (p["id"], rel), content[rel])
                continue
            path = ""
            for key in ("path", "filePath"):
                got = input_val(inp, key)
                if got and got[0]:
                    path = got[0]
                    break
            if not path:
                continue
            before = content.get(path, "")
            BEFORE.put(p["id"], before)
            seeded += 1
            state = p.get("state") or {}
            if p["name"] == "edit":
                old, new = input_val(inp, "oldString"), input_val(inp, "newString")
                after = apply_edit(before, (old or ("",))[0], (new or ("",))[0], bool(state.get("replaceAll")))
            else:
                body = input_val(inp, "content")
                after = body[0] if body else ""
            if after is not None:
                content[path] = after
    return seeded, content



REPLAY_DIV, REPLAY_MAX, REPLAY_GAP = 10, 2.5, 0.12  # a live fixture replays at 1/10 real speed, no beat longer than 2.5 s


def fixture_events(fx):
    """The captured turn rebuilt as the event stream the live window would have seen: the same steps, tool calls
    and text, in the same order, each event carrying the timestamp the capture recorded for it so fixture_replay
    can pace them. Reconstructed from the history alone: nothing is run, and no file is read. Fixture live mode
    only, and it is deliberately built from the captured input/output — not from this machine's disk."""
    sid = fx["session"]["id"]
    events = []

    def ev(kind, data, ts=None):
        e = {"type": "session." + kind, "data": dict(data, sessionID=sid)}
        if ts:
            e["created"] = ts
        events.append(e)

    # the captured turn is the first prompt's turn: what a window opened on it would have streamed, from that
    # prompt to the next one (a capture can hold a follow-up turn too, and that is not this window's business)
    start = next((m.get("id") for m in fx["messages"] if m.get("type") == "user"), None)
    started = False
    for msg in fx["messages"]:
        if not started:
            if msg.get("id") == start:
                started = True
            continue
        if msg.get("type") in ("user", "idle"):
            break
        if msg.get("type") == "compaction":
            ev("compaction.started", {})
            ev("compaction.ended", {})
            continue
        if msg.get("type") != "assistant":
            continue
        mid, mt = msg.get("id"), msg.get("time") or {}
        ev("step.started", {"assistantMessageID": mid, "model": msg.get("model"), "started": mt.get("created")},
           mt.get("created"))
        counts = {}
        for p in msg.get("content") or []:
            pt = p.get("time") or {}
            if p.get("type") in ("reasoning", "text"):
                kind = p["type"]
                o = counts.get(kind, 0)
                counts[kind] = o + 1
                text = p.get("text") or ""
                ev("%s.started" % kind, {"assistantMessageID": mid, "ordinal": o}, pt.get("created"))
                for i in range(0, len(text), 400):
                    ev("%s.delta" % kind, {"assistantMessageID": mid, "ordinal": o, "delta": text[i:i + 400]})
                ev("%s.ended" % kind, {"assistantMessageID": mid, "ordinal": o, "text": text}, pt.get("completed"))
            elif p.get("type") == "tool" and p.get("id"):
                cid, name = p["id"], p.get("name") or "tool"
                st = p.get("state") or {}
                ev("tool.input.started", {"assistantMessageID": mid, "id": cid, "name": name}, pt.get("created"))
                if st.get("input") is not None:
                    body = json.dumps(st["input"], ensure_ascii=False)
                    ev("tool.input.delta", {"assistantMessageID": mid, "id": cid, "delta": body}, pt.get("created"))
                    ev("tool.input.ended", {"assistantMessageID": mid, "id": cid, "text": body},
                       pt.get("ran") or pt.get("completed"))
                    ev("tool.called", {"assistantMessageID": mid, "id": cid, "input": st["input"]},
                       pt.get("ran") or pt.get("created"))
                if st.get("status") == "completed":
                    content = st.get("content")
                    if isinstance(content, str):
                        content = [{"type": "text", "text": content}]
                    ev("tool.success", {"assistantMessageID": mid, "id": cid,
                                        "content": content if content is not None else []}, pt.get("completed"))
                elif st.get("status") == "error":
                    ev("tool.error", {"assistantMessageID": mid, "id": cid,
                                      "error": st.get("error") or st.get("content") or "failed"}, pt.get("completed"))
        ev("step.ended", {"assistantMessageID": mid, "tokens": msg.get("tokens"), "finish": msg.get("finish")},
           mt.get("completed"))
    ev("execution.succeeded", {})
    return events


def fixture_replay(fx, q, pace=None):
    """Push the capture's events into a live window's queue at replay speed: the gap between two events is the
    capture's own gap divided by REPLAY_DIV (or by pace), never longer than REPLAY_MAX, or REPLAY_GAP when the
    capture has no timestamps to pace by. pace=0 sends everything at once, for tests."""
    prev = None
    for e in fixture_events(fx):
        ts = e.get("created")
        if pace != 0 and prev is not None:
            if ts and prev:
                time.sleep(min(REPLAY_MAX, max(0.0, (ts - prev) / 1000.0 / (pace or REPLAY_DIV))))
            else:
                time.sleep(REPLAY_GAP)
        q.put(e)
        prev = ts or prev


def fixture_oc_get(fx):
    """oc_get replaced by the fixture file: the same answers, no opencode and no event stream. Fixture mode only."""
    sid = fx["session"]["id"]
    msgs = sorted(fx["messages"], key=lambda m: m.get("id") or "")

    def page(q):
        picked = [m for m in msgs if not q.get("type") or m.get("type") in q["type"]]
        cur = (q.get("cursor") or [""])[0]
        if cur:
            try:
                cid = json.loads(base64.urlsafe_b64decode(cur + "===")).get("id")
            except Exception:
                cid = None
            if cid:
                picked = [m for m in picked if (m.get("id") or "") > cid]
        limit = int((q.get("limit") or [str(PAGE)])[0])
        if (q.get("order") or ["asc"])[0] == "desc":
            return {"data": picked[::-1][:limit]}
        data = picked[:limit]
        out = {"data": data}
        if data and len(picked) > len(data):
            out["cursor"] = {"next": asc_cursor(data[-1]["id"])}
        return out

    def get(path, timeout=20):
        path, _, qs = path.partition("?")
        parts = [p for p in path.split("/") if p]
        if parts[:2] != ["api", "session"]:
            raise LookupError("the fixture does not answer " + path)
        rest = parts[2:]
        if rest == ["active"]:
            return {"data": []}  # a captured turn is over
        if len(rest) >= 2 and rest[0] == sid and rest[1] == "message":
            if len(rest) > 2:
                for m in msgs:
                    if m.get("id") == rest[2]:
                        return {"data": m}
                raise LookupError("the fixture has no message " + rest[2])
            return page(urllib.parse.parse_qs(qs))
        if len(rest) == 1 and rest[0] == sid:
            return {"data": fx["session"]}
        raise LookupError("the fixture does not answer " + path)

    return get


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "code-live/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _head(self, status, ctype, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Open WebUI sandboxes the frame without allow-same-origin, so name this host as well as 'self'.
        # The window frames the chat's live desktop (sandbox-agent on this VM, another port).
        host = self.headers.get("Host", "")
        ok = re.match(r"^([A-Za-z0-9.-]+)(:\d+)?$", host)
        own = (" http://" + host) if ok else ""
        frames = ("http://%s:*" % ok.group(1)) if ok else "'none'"
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'%s; img-src data:; frame-src %s; frame-ancestors 'self' %s"
                         % (own, frames, FRAME_ANCESTORS))
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _gone(self):
        """The window of a session opencode no longer has: one line in the look of a finished window's header, sized
        to itself (the chat's box would otherwise stay a tall empty frame around a sentence of plain text)."""
        body = GONE_HTML.encode()
        self._head(404, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)
        log("GET %s 404 (gone)" % mask(self.path))

    def _plain(self, status, text):
        body = text.encode()
        self._head(status, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)
        log("%s %s %d" % (self.command, mask(self.path), status))

    def _upload_head(self, status, length):
        origin = self.headers.get("Origin", "")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", origin if re.match(r"^https?://[A-Za-z0-9.:-]+$", origin) else "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _upload_json(self, status, obj):
        body = json.dumps(obj).encode()
        self._upload_head(status, len(body))
        self.wfile.write(body)
        log("%s %s %d%s" % (self.command, mask(self.path), status,
                            " (%s bytes)" % obj.get("bytes") if "bytes" in obj else " " + str(obj.get("error", ""))[:80]))

    def do_OPTIONS(self):
        m = UPLOAD_RE.match(self.path.partition("?")[0])
        if not m:
            return self._plain(404, "Not found.")
        self._upload_head(204, 0)
        return None

    def do_PUT(self):
        path, _, qs = self.path.partition("?")
        m = UPLOAD_RE.match(path)
        if not m or not CHAT_RE.match(m.group("chat")):
            self.close_connection = True
            return self._upload_json(404, {"error": "Not found."})
        if not valid_upload_sig(m.group("chat"), m.group("sig")):
            self.close_connection = True
            return self._upload_json(403, {"error": "This upload link is not valid."})
        rel = upload_rel((urllib.parse.parse_qs(qs).get("path") or [""])[0])
        if rel is None:
            self.close_connection = True
            return self._upload_json(400, {"error": "That file name or folder path can't be used."})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            return self._upload_json(411, {"error": "The upload has no length."})
        if length > UPLOAD_MAX_FILE:
            self.close_connection = True
            return self._upload_json(413, {"error": "That file is larger than %d MB." % (UPLOAD_MAX_FILE >> 20)})
        try:
            status, info = save_upload(m.group("chat"), rel, length, self.rfile)
        except Exception as e:  # noqa: BLE001 - an answer, never a dropped connection
            status, info = 500, {"error": "The upload failed on the coding machine (%s)." % type(e).__name__}
        if status >= 400:
            self.close_connection = True
        return self._upload_json(status, {"ok": status < 400, **info})

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/health":
            return self._plain(200, "ok")
        m = ROUTE_RE.match(path)
        if not m or not CHAT_RE.match(m.group("chat")):
            return self._plain(404, "Not found.")
        if not valid_sig(m.group("chat"), m.group("sig")):
            return self._plain(403, "This live link is not valid.")
        if m.group("rest") == "engine":   # Watch mode's engine strip: the same for every session of the chat
            doc = engine_now()
            body = json.dumps({"engine": doc, "now": int(time.time() * 1000)}).encode()
            self._head(200, "application/json", len(body), extra={"Access-Control-Allow-Origin": "*"})
            self.wfile.write(body)
            return None
        q = urllib.parse.parse_qs(qs)
        sid, msg = (q.get("s") or [""])[0], (q.get("m") or [""])[0]
        if not SID_RE.match(sid) or not MSG_RE.match(msg):
            return self._plain(404, "This live link is incomplete.")
        try:
            owner = session_chat(sid)
        except urllib.error.HTTPError as ex:
            if ex.code == 404:  # gone: Code mode clears a turn's review sessions when the next turn starts
                if not m.group("rest"):
                    return self._gone()
                return self._plain(404, "This session is no longer on the coding machine, so there is nothing "
                                        "to replay. (Code mode clears a turn's review sessions when the next "
                                        "turn starts.)")
            return self._plain(502, "opencode answered with an error (HTTP %d)." % ex.code)
        except Exception as ex:
            return self._plain(502, "opencode is not answering (%s)." % type(ex).__name__)
        if owner != m.group("chat"):
            return self._plain(404, "That session is not part of this chat.")
        if m.group("rest") == "tools":   # Watch mode's Tools tab
            try:
                doc = tools_view(sid)
            except Exception as ex:
                return self._plain(502, "opencode is not answering (%s)." % type(ex).__name__)
            body = json.dumps(doc).encode()
            self._head(200, "application/json", len(body), extra={"Access-Control-Allow-Origin": "*"})
            self.wfile.write(body)
            return None
        if not m.group("rest"):
            with open(os.path.join(HERE, "live.html"), "rb") as fh:
                body = fh.read()
            self._head(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return log("GET %s 200" % mask(self.path))
        self._events(sid, msg, (q.get("full") or [""])[0] == "1")

    def _send(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n")
        self.wfile.flush()

    def _events(self, sid, m, full):
        self.close_connection = True
        self._head(200, "text/event-stream; charset=utf-8",
                   extra={"Access-Control-Allow-Origin": "*", "X-Accel-Buffering": "no"})
        log("GET %s events (%s)" % (mask(self.path), "full" if full else "auto"))
        q = None
        try:
            if not full and not turn_is_live(sid, m):
                self._send({"k": "past"})
                return
            q = HUB.subscribe(sid)
            HUB.connected.wait(10)
            try:
                dir_path = session_dir(sid)  # diffs only read files inside the session's own directory
            except Exception:
                dir_path = None
            trackers = {}
            call_reads = {}   # call id -> the files its command read, so each one is read once and viewed once
            call_git = {}     # call id -> True/False once seen enough to decide it is a git diff/show call

            def views_for(shaped, names):
                return views_of_event(shaped, names, dir_path, call_reads)

            def seed_trackers(steps):
                """Tool calls that were already mid-flight when this window opened keep streaming their diff too."""
                for st in steps:
                    for p in st["parts"]:
                        if p["p"] == "tool" and p["name"] in ("edit", "write") and p["status"] == "streaming" \
                                and p["id"] not in trackers:
                            tr = CallTracker(p["id"], p["name"], dir_path, self._send)
                            tr.seed(p.get("input"))
                            trackers[p["id"]] = tr
                        elif p["p"] == "tool" and p["name"] == "shell" and p.get("gitdiff"):
                            call_git[p["id"]] = True  # already in the snapshot: do not stream it again

            def track(shaped, names, e):
                cid = shaped.get("id")
                if not cid:
                    return
                for v in views_for(shaped, names):
                    self._send(v)
                if shaped["k"] == "tool":
                    if "input" in shaped and isinstance(shaped["input"], dict):
                        call_git[cid] = is_git_diff_command(shaped["input"].get("command"))
                    elif "raw" in shaped and call_git.get(cid) is None:
                        got = input_val(shaped["raw"], "command")
                        if got and got[1]:  # only a fully typed command decides
                            call_git[cid] = is_git_diff_command(got[0])
                    if shaped.get("status") == "completed" and call_git.get(cid):
                        # full output from the raw event (shaped["out"] is capped), parsed into file diffs
                        files, note = parse_git_diff(full_content_text((e.get("data") or {}).get("content")))
                        if files:
                            ev = {"k": "gitdiff", "id": cid, "files": files}
                            if note:
                                ev["note"] = note
                            self._send(ev)
                if shaped["k"] == "tool+":
                    tr = trackers.get(cid)
                    if tr:
                        tr.delta(shaped["d"])
                    return
                if shaped["k"] != "tool":
                    return
                if cid not in trackers:
                    name = shaped.get("name") or names.get(cid)
                    if name in ("edit", "write") and (shaped.get("status") == "streaming" or "input" in shaped or "raw" in shaped):
                        trackers[cid] = CallTracker(cid, name, dir_path, self._send)
                tr = trackers.get(cid)
                if not tr:
                    return
                if "input" in shaped:
                    tr.got_input(shaped["input"])
                elif "raw" in shaped:
                    tr.got_input(shaped["raw"])
                if shaped.get("status") in ("completed", "error"):
                    tr.complete(applied=shaped.get("status") == "completed")

            watch = CompactionWatch(sid, q)
            compact = {"ph": None,   # where the compaction overlay is: start/delta = reading or writing, end = reading it in
                       "brief": False}   # a review window whose reviewer is still reading its brief

            def follow_compaction(snap):
                c = snap.get("compacting")
                compact["ph"] = c["ph"] if c else None
                compact["brief"] = bool(snap.get("review")) and not c and not has_output(snap["steps"])
                if c:
                    watch.start(with_tape=c["ph"] == "start")
                elif compact["brief"]:
                    watch.start(with_tape=False)   # the engine's progress while the reviewer reads its brief
                else:
                    watch.stop()

            snap = snapshot(sid, m)
            self._send(snap)
            seed_trackers(snap["steps"])
            if not snap["running"]:
                self._send({"k": "end", "how": "done"})
                return
            follow_compaction(snap)
            quiet = time.monotonic()
            last_ping = time.monotonic()
            names = {p["id"]: p["name"] for st in snap["steps"] for p in st["parts"] if p["p"] == "tool"}
            while True:
                try:
                    e = q.get(timeout=PING)
                except queue.Empty:
                    e = None
                now = time.monotonic()
                if e is None or now - last_ping >= PING:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = now
                if e is None:
                    if now - quiet >= RECHECK:
                        quiet = now
                        if not session_active(sid):  # missed the end event: settle from the history
                            snap = snapshot(sid, m, settle=True)
                            self._send(snap)
                            self._send({"k": "end", "how": "done"})
                            return
                    continue
                quiet = now
                if e.get("k") == "gap":
                    HUB.connected.wait(10)
                    snap = snapshot(sid, m)
                    self._send(snap)
                    seed_trackers(snap["steps"])
                    if not snap["running"]:
                        self._send({"k": "end", "how": "done"})
                        return
                    follow_compaction(snap)
                    continue
                if e.get("k") in ("compact", "cprog"):   # the compaction watch's own events
                    if compact["ph"] or (compact["brief"] and e["k"] == "cprog"):
                        self._send(e)
                    continue
                shaped = shape_event(e, names)
                if shaped:
                    if compact["brief"] and shaped["k"] in ("think0", "text0", "think+", "text+", "tool", "tool+", "end"):
                        compact["brief"] = False    # the reviewer has read its brief and started
                        if not compact["ph"]:
                            watch.stop()
                    if compact["ph"] == "end" and shaped["k"] in ("think0", "text0", "think+", "text+", "tool", "tool+"):
                        compact["ph"] = None    # the step after the compaction answers: the new context is read
                        watch.stop()
                        self._send({"k": "compact", "ph": "done"})
                    if shaped["k"] == "compact":
                        compact["ph"] = None if shaped["ph"] == "fail" else shaped["ph"]
                        if shaped["ph"] == "start":
                            watch.start(with_tape=True)
                        elif shaped["ph"] == "fail":
                            watch.stop()
                    self._send(shaped)
                    track(shaped, names, e)
                    if shaped["k"] == "end":
                        return
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as ex:
            log("events %s: %s: %s" % (sid, type(ex).__name__, ex))
            try:
                self._send({"k": "note", "level": "err", "text": "Live view lost opencode (%s); retrying" % type(ex).__name__})
            except Exception:
                pass
        finally:
            try:
                watch.stop()
            except NameError:
                pass
            if q is not None:
                HUB.unsubscribe(sid, q)


def fixture_prep(fx_path):
    """Read a captured turn and make it answer where opencode would: returns (fx, chat, prompt id, calls, seeded).
    Raises ValueError for a file that is not a usable capture."""
    with open(fx_path, encoding="utf-8") as fh:
        fx = json.load(fh)
    sid = (fx.get("session") or {}).get("id") or ""
    if not SID_RE.match(sid) or not isinstance(fx.get("messages"), list):
        raise ValueError("needs a session id and a list of messages")
    chat = fixture_chat(fx)
    fx["session"]["metadata"] = dict(fx["session"].get("metadata") or {}, owui_chat_id=chat)
    start = next((m.get("id") for m in fx["messages"] if m.get("type") == "user"), "")
    if not MSG_RE.match(start or ""):
        raise ValueError("no user prompt to start the turn from")
    seeded, _ = fixture_seed(fx)
    calls = fixture_turn(fx)[1]
    return fx, chat, start, calls, seeded


def fixture_server(fx, port, live=False, pace=None):
    """A server that answers a prepared fixture instead of opencode. Bound to 127.0.0.1, and signed with a fixture
    secret of its own: it is for looking at one captured turn on this machine, nothing else.

    live=True replays the capture through the event stream instead of answering it as history: every window
    gets its own replay, started the moment it connects, so it watches the whole turn event by event from its
    start at 1/REPLAY_DIV real speed - a window opened a minute after the server also watches the whole turn,
    live, and opens on an empty Follow stage rather than on history."""
    global oc_get, FIXTURE_NO_DISK, turn_is_live, snapshot, STRATA_LIVE
    os.environ["PREVIEW_SECRET"] = FIXTURE_SECRET
    FIXTURE_NO_DISK = True          # a captured turn is shown from the capture, never from this machine's disk
    STRATA_LIVE = ""                # nor with the engine's progress of today
    oc_get = fixture_oc_get(fx)
    if live:
        def subscribe(sid):         # a window that connects starts its own replay of the whole turn
            q = queue.Queue()       # unbounded: the replay is finite, and no event may be dropped
            threading.Thread(target=fixture_replay, args=(fx, q, pace),
                             name="fixture-replay", daemon=True).start()
            return q

        HUB.subscribe = subscribe
        HUB.unsubscribe = lambda sid, qq: None
        since = next(((m.get("time") or {}).get("created") for m in fx["messages"] if m.get("type") == "user"), None)

        def live_snapshot(sid, m, settle=False):  # each window opens on an empty Follow stage; the turn
            return {"k": "snap", "steps": [], "notes": [], "running": True, "since": since,  # arrives as events
                    "review": session_review(sid),  # a review capture names its window like the real service does
                    "now": int(time.time() * 1000)}

        snapshot = live_snapshot
        turn_is_live = lambda sid, m: True
    else:
        HUB.subscribe = lambda sid: queue.Queue(maxsize=1)  # a fixture has no live events
        HUB.unsubscribe = lambda sid, q: None
    HUB.connected.set()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def fixture_mode(fx_path, port, live=False):
    """Serve a captured turn from a fixture file instead of opencode: the window's own page, its own snapshot
    path, no opencode and no real event stream. Nothing is written anywhere. With live=True the turn is replayed
    as a live run (see fixture_server); without it the window opens on the finished turn."""
    try:
        fx, chat, start, calls, seeded = fixture_prep(fx_path)
    except (OSError, ValueError) as ex:
        log("code-live: fixture %s: %s" % (fx_path, ex))
        sys.exit(2)
    server = fixture_server(fx, port, live=live)
    port = server.server_address[1]
    print("code-live fixture: %s (%s), %d messages, %d edit/write calls, %d with their file's before%s" %
          (fx_path, fx["session"]["id"], len(fx["messages"]), calls, seeded,
           ", replaying live at 1/%d speed" % REPLAY_DIV if live else ""), flush=True)
    print("open http://127.0.0.1:%d/w/%s/%s/?s=%s&m=%s%s" %
          (port, chat, live_sig(chat), fx["session"]["id"], start, "" if live else "&full=1"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    args, fx_path, port, live = sys.argv[1:], None, PORT, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--fixture" and i + 1 < len(args):
            fx_path, i = args[i + 1], i + 2
        elif a == "--live":
            live, i = True, i + 1
        elif a == "--port" and i + 1 < len(args) and args[i + 1].isdigit():
            port, i = int(args[i + 1]), i + 2
        elif a in ("-h", "--help"):
            print("usage: code-live.py [--fixture FILE [--live] --port N]   "
                  "(fixture: serve a captured turn locally, live: replay it as a live run)")
            return
        else:
            log("code-live: unknown argument: %s" % a)
            sys.exit(2)
    if live and not fx_path:
        log("code-live: --live replays a captured turn; it needs --fixture FILE")
        sys.exit(2)
    if fx_path:
        return fixture_mode(fx_path, port, live=live)
    if not os.environ.get("PREVIEW_SECRET"):
        log("code-live: PREVIEW_SECRET is not set; refusing to start")
        sys.exit(2)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print("code-live: serving on 0.0.0.0:%d (opencode %s)" % (port, OPENCODE), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
