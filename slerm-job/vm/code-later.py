#!/usr/bin/env python3
"""code-later - lets a Code mode agent wait and come back (2026-10-20).

The agent (opencode on VM 129, user coder) sets a wake-up with the `later` CLI and may end its turn:
    run    a command keeps running here; the chat is woken when it exits (exit code + output tail)
    in     a timer
    until  a check command is re-run every N seconds; the chat is woken when it succeeds (or the time runs out)
A fired item becomes a wake. code-wake on cryoai polls GET /api/wakes, posts the wake into the Open WebUI chat as
the next Code mode turn (on the chat's own model) once that chat has no turn running and no other Code turn is
active, then reports back with POST /api/wakes/<id>.

Runs as coder (code-later.service, same /tmp as opencode via JoinsNamespaceOf), so commands run exactly as the
agent's own would, inside the coder firewall. HTTP on :4101 (firewall: cryoai + loopback), Bearer LATER_TOKEN from
/etc/code-mode/later.env for every call. State: ~/.local/state/code-mode/later/items.json + logs/<id>.log.
Source: ~/slerm-codebuild/vm/code-later.py on Ghost.
"""
import base64, json, os, re, secrets, signal, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = os.path.expanduser("~/.local/state/code-mode/later")
ITEMS = os.path.join(STATE, "items.json")
LOGS = os.path.join(STATE, "logs")
PROJECTS = "/home/coder/projects"
PORT = int(os.environ.get("LATER_PORT", "4101"))
TOKEN = os.environ.get("LATER_TOKEN", "")
OPENCODE_CFG = os.path.expanduser("~/.config/opencode/service.json")

MAX_PENDING_PER_CHAT = 5        # set but not yet delivered
MAX_PER_DAY_PER_CHAT = 30       # created in the last 24 h
MAX_WAIT_S = 24 * 3600
MIN_EVERY_S = 15
TAIL_LINES, TAIL_CHARS = 40, 4000
KEEP_DONE_S = 3 * 86400
CHAT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

lock = threading.RLock()
items = {}          # id -> item dict (persisted)
procs = {}          # id -> Popen for "run" items and running "until" checks


def now():
    return time.time()


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, flush=True)


def save():
    tmp = ITEMS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f)
    os.replace(tmp, ITEMS)


def fmt_s(sec):
    sec = int(sec)
    if sec >= 3600:
        return "%dh%02dm" % (sec // 3600, sec % 3600 // 60)
    return "%dm%02ds" % (sec // 60, sec % 60) if sec >= 60 else "%ds" % sec


def tail(path, lines=TAIL_LINES, chars=TAIL_CHARS):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 256 * 1024))
            txt = f.read().decode(errors="replace")
    except OSError:
        return ""
    out = "\n".join(txt.rstrip("\n").splitlines()[-lines:])
    return out[-chars:]


def chat_of(cwd):
    m = re.match(r"^%s/(?:chats|features)/([^/]+)(?:/|$)" % re.escape(PROJECTS), os.path.realpath(cwd or ""))
    return m.group(1) if m else None


def session_model(chat):
    """The model this chat's opencode session runs on, as Open WebUI's id (slerm/X -> slerm-turing.X)."""
    try:
        cfg = json.load(open(OPENCODE_CFG))
        pw = next(v for k, v in cfg.items() if k.startswith("pass"))
        auth = "Basic " + base64.b64encode(("opencode:" + pw).encode()).decode()
        port = cfg.get("port", 4096)
        for d in ("%s/chats/%s" % (PROJECTS, chat), PROJECTS):
            req = urllib.request.Request("http://127.0.0.1:%d/api/session?directory=%s" % (port, d),
                                         headers={"Authorization": auth})
            for s in json.load(urllib.request.urlopen(req, timeout=10)).get("data", []):
                if (s.get("metadata") or {}).get("owui_chat_id") == chat:
                    m = s.get("model") or {}
                    if m.get("providerID") == "slerm" and m.get("id"):
                        return "slerm-turing." + m["id"]
    except Exception as e:                                  # noqa: BLE001
        log("session model of", chat, "unknown:", e)
    return None


def opencode_active():
    try:
        cfg = json.load(open(OPENCODE_CFG))
        pw = next(v for k, v in cfg.items() if k.startswith("pass"))
        auth = "Basic " + base64.b64encode(("opencode:" + pw).encode()).decode()
        req = urllib.request.Request("http://127.0.0.1:%d/api/session/active" % cfg.get("port", 4096),
                                     headers={"Authorization": auth})
        data = json.load(urllib.request.urlopen(req, timeout=10)).get("data", {})
        return list(data) if isinstance(data, (dict, list)) else []
    except Exception as e:                                  # noqa: BLE001
        log("cannot read opencode's active sessions:", e)
        return None


# ---- items ------------------------------------------------------------------------------------------------

def new_item(req):
    kind = req.get("kind")
    cwd = req.get("cwd") or ""
    chat = chat_of(cwd)
    if not chat or not CHAT_RE.match(chat):
        raise ValueError("`later` works only from a Code mode chat folder (%s/chats/<chat id>)" % PROJECTS)
    note = str(req.get("note") or "")[:1500]
    tag = str(req.get("tag") or "")[:80]
    t = now()
    it = {"id": "L" + secrets.token_hex(4), "kind": kind, "chat": chat, "cwd": os.path.realpath(cwd), "note": note,
          "tag": tag, "created": t, "state": "waiting", "label": str(req.get("label") or "")[:200]}
    if kind == "run":
        cmd = req.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 4000:
            raise ValueError("the command must be 1-4000 characters")
        it.update(cmd=cmd, deadline=t + MAX_WAIT_S)
    elif kind == "in":
        sec = int(req.get("seconds") or 0)
        if not 10 <= sec <= MAX_WAIT_S:
            raise ValueError("a timer runs 10 s to 24 h")
        it.update(due=t + sec, seconds=sec)
    elif kind == "until":
        cmd, every, limit = req.get("cmd"), int(req.get("every") or 60), int(req.get("for") or 3600)
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 4000:
            raise ValueError("the check must be 1-4000 characters")
        if every < MIN_EVERY_S:
            raise ValueError("--every is at least %d s" % MIN_EVERY_S)
        if not 30 <= limit <= MAX_WAIT_S:
            raise ValueError("--for runs 30 s to 24 h")
        it.update(cmd=cmd, every=every, deadline=t + limit, next_check=t, checks=0)
    else:
        raise ValueError("kind must be run, in or until")
    with lock:
        mine = [x for x in items.values() if x["chat"] == chat]
        if sum(1 for x in mine if x["state"] in ("waiting", "ready")) >= MAX_PENDING_PER_CHAT:
            raise ValueError("this chat already has %d wake-ups set (later list / later cancel)" %
                             MAX_PENDING_PER_CHAT)
        if sum(1 for x in mine if x["created"] > t - 86400) >= MAX_PER_DAY_PER_CHAT:
            raise ValueError("this chat has set %d wake-ups in the last 24 h, the limit" % MAX_PER_DAY_PER_CHAT)
        items[it["id"]] = it
        if kind == "run":
            start(it)
        save()
    log("set", it["id"], kind, chat[:8], (it.get("label") or it.get("cmd") or "%ss" % it.get("seconds"))[:80])
    return it


def env_for(it):
    e = dict(os.environ)
    e.pop("LATER_TOKEN", None)
    e["LATER_ID"] = it["id"]
    return e


def start(it):
    os.makedirs(LOGS, exist_ok=True)
    out = open(os.path.join(LOGS, it["id"] + ".log"), "ab")
    try:
        p = subprocess.Popen(["bash", "-c", it["cmd"]], cwd=it["cwd"], stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, env=env_for(it), start_new_session=True)
    except OSError as e:
        fire(it, "could not start: %s" % e)
        return
    finally:
        out.close()
    procs[it["id"]] = p
    it["pid"] = p.pid
    it["started"] = now()


def kill(it):
    p = procs.pop(it["id"], None)
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def fire(it, headline, body=""):
    it["state"] = "ready"
    it["fired"] = now()
    it["headline"] = headline
    it["body"] = body
    log("ready", it["id"], it["chat"][:8], headline[:100])


def text_of(group):
    """The message posted into the chat for one or more ready wakes."""
    parts = []
    for it in group:
        when = time.strftime("%H:%M", time.localtime(it["created"]))
        what = {"run": "`later run`", "in": "`later in`", "until": "`later until`"}[it["kind"]]
        s = "⏰ **Wake-up** (%s, set at %s): %s" % (what, when, it["headline"])
        if it.get("body"):
            s += "\n\n" + it["body"]
        if it.get("note"):
            s += "\n\nYour note to yourself: " + it["note"]
        parts.append(s)
    parts.append("_Sent by `later` on the agent's behalf, not typed by the user. Carry on with the task from here._")
    return "\n\n---\n\n".join(parts[:-1]) + "\n\n" + parts[-1]


def tick():
    t = now()
    with lock:
        changed = False
        for it in list(items.values()):
            if it["state"] != "waiting":
                continue
            if it["kind"] == "run":
                p = procs.get(it["id"])
                if p is None:
                    fire(it, "`%s` was stopped: code-later restarted while it ran" % (it.get("label") or it["cmd"])[:200])
                    changed = True
                    continue
                rc = p.poll()
                if rc is None:
                    if t > it["deadline"]:
                        kill(it)
                        fire(it, "`%s` was still running after 24 h, so it was stopped" % (it.get("label") or
                                                                                           it["cmd"])[:200])
                        changed = True
                    continue
                procs.pop(it["id"], None)
                logp = os.path.join(LOGS, it["id"] + ".log")
                name = it.get("label") or it["cmd"]
                verdict = "finished OK (exit 0)" if rc == 0 else "failed (exit %d)" % rc if rc > 0 else \
                    "was killed (signal %d)" % -rc
                fire(it, "`%s` %s after %s." % (name[:200], verdict, fmt_s(t - it.get("started", it["created"]))),
                     "Last lines of its output:\n```\n%s\n```\nFull output: %s" % (tail(logp) or "(none)", logp))
                it["exit"] = rc
                changed = True
            elif it["kind"] == "in":
                if t >= it["due"]:
                    fire(it, "the %s timer you set is up." % fmt_s(it["seconds"]))
                    changed = True
            elif it["kind"] == "until":
                p = procs.get(it["id"])
                if p is not None:
                    rc = p.poll()
                    if rc is None:
                        if t - it.get("check_started", t) > min(it["every"], 300):
                            kill(it)
                        continue
                    procs.pop(it["id"], None)
                    it["checks"] += 1
                    it["last_exit"] = rc
                    logp = os.path.join(LOGS, it["id"] + ".log")
                    if rc == 0:
                        fire(it, "the check `%s` succeeded after %s (%d tries)." % (
                            it["cmd"][:200], fmt_s(t - it["created"]), it["checks"]),
                             "Its output:\n```\n%s\n```" % (tail(logp, 20, 2000) or "(none)"))
                        changed = True
                        continue
                    it["next_check"] = t + it["every"]
                if t > it["deadline"]:
                    fire(it, "gave up: the check `%s` never succeeded in %s (%d tries, last exit %s)." % (
                        it["cmd"][:200], fmt_s(it["deadline"] - it["created"]), it["checks"], it.get("last_exit")),
                         "Its last output:\n```\n%s\n```" % (tail(os.path.join(LOGS, it["id"] + ".log"), 20, 2000)
                                                             or "(none)"))
                    changed = True
                elif t >= it["next_check"] and it["id"] not in procs:
                    os.makedirs(LOGS, exist_ok=True)
                    with open(os.path.join(LOGS, it["id"] + ".log"), "wb") as out:   # keep only the latest check
                        procs[it["id"]] = subprocess.Popen(
                            ["bash", "-c", it["cmd"]], cwd=it["cwd"], stdin=subprocess.DEVNULL, stdout=out,
                            stderr=subprocess.STDOUT, env=env_for(it), start_new_session=True)
                    it["check_started"] = t
                    it["next_check"] = t + it["every"]
        for k, it in list(items.items()):
            if it["state"] in ("delivered", "cancelled", "failed") and t - it.get("closed", t) > KEEP_DONE_S:
                items.pop(k)
                changed = True
        if changed:
            save()


def worker():
    while True:
        try:
            tick()
        except Exception as e:                              # noqa: BLE001
            log("tick failed:", repr(e))
        time.sleep(1)


def public(it):
    keys = ("id", "kind", "chat", "state", "label", "cmd", "note", "tag", "created", "due", "deadline", "every",
            "checks", "headline", "fired", "closed", "status_note", "exit", "held")
    return {k: it[k] for k in keys if k in it}


# ---- delivery: exactly once (2026-10-24) -------------------------------------------------------------------
# Two deliverers: code-wake (a new turn, once the chat is idle) and the Code pipe itself (into the chat's running
# turn, at the end of a run). Whoever claims a ready wake first delivers it; the claim is one step under the lock.

def claim(chat, ids=None, note=""):
    """Marks the chat's ready, unheld wakes (only `ids`, when given) delivered and returns (ids, message text)."""
    with lock:
        group = [x for x in items.values() if x["chat"] == chat and x["state"] == "ready" and not x.get("held")
                 and (ids is None or x["id"] in ids)]
        group.sort(key=lambda x: x["fired"])
        for x in group:
            x["state"], x["closed"], x["status_note"] = "delivered", now(), str(note or "claimed")[:300]
        if group:
            save()
    if group:
        log("claimed", [x["id"] for x in group], chat[:8], note[:60])
    return [x["id"] for x in group], text_of(group) if group else ""


def unclaim(ids):
    """A claimed delivery that failed: the wakes are ready again (only ones a claim closed, not cancelled ones)."""
    n = 0
    with lock:
        for i in ids:
            it = items.get(i)
            if it and it["state"] == "delivered":
                it["state"], it["status_note"] = "ready", "delivery failed; ready again"
                it.pop("closed", None)
                n += 1
        if n:
            save()
    return n


def hold(chat, on):
    """Stop pressed in the chat: its waiting and ready wakes are held (never delivered by themselves) until the
    user's next message releases them. Returns how many changed."""
    n = 0
    with lock:
        for it in items.values():
            if it["chat"] == chat and it["state"] in ("waiting", "ready") and bool(it.get("held")) != bool(on):
                if on:
                    it["held"] = True
                else:
                    it.pop("held", None)
                n += 1
        if n:
            save()
    if n:
        log("held" if on else "released", n, "for", chat[:8])
    return n


# ---- HTTP -------------------------------------------------------------------------------------------------

class H(BaseHTTPRequestHandler):
    server_version = "code-later/1"

    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def authed(self):
        a = self.headers.get("Authorization", "")
        if not TOKEN or not secrets.compare_digest(a, "Bearer " + TOKEN):
            self.send(401, {"error": "unauthorized"})
            return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self.authed():
            return
        path, _, q = self.path.partition("?")
        args = dict(p.split("=", 1) for p in q.split("&") if "=" in p)
        if path == "/api/health":
            return self.send(200, {"ok": True, "items": len(items)})
        if path == "/api/items":
            with lock:
                rows = [public(x) for x in items.values() if not args.get("chat") or x["chat"] == args["chat"]]
            return self.send(200, {"items": sorted(rows, key=lambda x: x["created"])})
        if path == "/api/wakes":
            with lock:
                ready = [x for x in items.values() if x["state"] == "ready" and not x.get("held")]
                groups = {}
                for x in sorted(ready, key=lambda x: x["fired"]):
                    groups.setdefault(x["chat"], []).append(x)
            out = []
            for chat, group in groups.items():
                out.append({"chat": chat, "ids": [x["id"] for x in group], "text": text_of(group),
                            "code_model": session_model(chat)})
            # ?active=1: always look (code-wake also sends the user's queued messages, which wait the same way)
            want = bool(out) or args.get("active") == "1"
            return self.send(200, {"wakes": out, "opencode_active": opencode_active() if want else [],
                                   "active_checked": want})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.authed():
            return
        path = self.path.partition("?")[0]
        try:
            req = self.body()
            if path == "/api/items":
                return self.send(200, {"item": public(new_item(req))})
            if path == "/api/claim":
                chat = req.get("chat")
                ids = req.get("ids")
                if not isinstance(chat, str) or not CHAT_RE.match(chat):
                    raise ValueError("chat must be a chat id")
                if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                    raise ValueError("ids must be a list of item ids")
                got, text = claim(chat, set(ids) if ids is not None else None, str(req.get("note") or ""))
                return self.send(200, {"ids": got, "text": text})
            if path == "/api/unclaim":
                ids = req.get("ids")
                if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                    raise ValueError("ids must be a list of item ids")
                return self.send(200, {"ready_again": unclaim(ids)})
            if path == "/api/hold":
                chat = req.get("chat")
                if not isinstance(chat, str) or not CHAT_RE.match(chat):
                    raise ValueError("chat must be a chat id")
                return self.send(200, {"changed": hold(chat, bool(req.get("hold")))})
            m = re.fullmatch(r"/api/items/(L[0-9a-f]{8})/cancel", path)
            if path == "/api/cancel" or m:
                n = 0
                with lock:
                    for it in items.values():
                        if it["state"] not in ("waiting", "ready"):
                            continue
                        if m and it["id"] != m.group(1):
                            continue
                        if not m and (it["chat"] != req.get("chat") or
                                      req.get("tag") and it.get("tag") != req["tag"]):
                            continue
                        kill(it)
                        it["state"], it["closed"] = "cancelled", now()
                        n += 1
                    save()
                return self.send(200, {"cancelled": n})
            m = re.fullmatch(r"/api/wakes/(L[0-9a-f]{8})", path)
            if m:
                st = req.get("status")
                with lock:
                    it = items.get(m.group(1))
                    if not it:
                        return self.send(404, {"error": "no such item"})
                    if st in ("delivered", "failed"):
                        it["state"], it["closed"] = st, now()
                    it["status_note"] = str(req.get("note") or "")[:300]
                    save()
                log("wake", it["id"], st or "note", it["status_note"][:100])
                return self.send(200, {"item": public(it)})
        except (ValueError, TypeError) as e:
            return self.send(400, {"error": str(e)})
        self.send(404, {"error": "not found"})


def main():
    if not TOKEN:
        sys.exit("LATER_TOKEN is not set")
    os.makedirs(LOGS, exist_ok=True)
    try:
        with open(ITEMS) as f:
            items.update(json.load(f))
    except (OSError, ValueError):
        pass
    threading.Thread(target=worker, daemon=True).start()
    log("code-later listening on :%d with %d items" % (PORT, len(items)))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
