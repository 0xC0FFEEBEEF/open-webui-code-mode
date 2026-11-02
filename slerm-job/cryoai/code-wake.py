#!/usr/bin/env python3
"""code-wake - delivers `later` wake-ups into Open WebUI chats (cryoai, 2026-10-20).

A Code mode agent on VM 129 sets a wake-up with `later` (or `slerm-job run --later`) and ends its turn. When it
fires, code-later on the VM queues a wake. This service polls that queue (GET http://ai-sandbox:4101/api/wakes) and,
once the chat has no turn running and no other Code turn is active on the VM (one router slot), adds the wake text
to the chat as the next user message and asks Open WebUI for the reply, as a Code mode turn on the model the chat's
opencode session runs on (the way tools/owui-followup.py does it). Then it reports delivered/failed back.

It also sends the user's queued messages (2026-10-30): messages typed while a reply ran, which the page script keeps
on code-deploy (GET /api/queue/pending). A tab open on the chat sends them itself when the reply finishes; when no
tab does (asleep, closed, crashed), this sends a Code chat's queue the same way it sends a wake.

Config ~/.config/code-wake/env (0600): LATER_URL, LATER_TOKEN, optional OWUI_URL, DEPLOY_URL, PIPE_MODEL, POLL_S,
DRY_RUN=1.
The Open WebUI token is signed in-process for the chat's owner (~/.webui_secret_key) and never printed or stored.
Source: ~/slerm-codebuild/cryoai/code-wake.py on Ghost.
"""
import base64, hashlib, hmac, json, os, sqlite3, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path

ENV = Path.home() / ".config/code-wake/env"
DB = Path.home() / ".open-webui/webui.db"
SECRET = Path.home() / ".webui_secret_key"
GIVE_UP_S = 24 * 3600
# A browser tab that has the chat open saves the WHOLE chat when its own turn ends (POST /api/chat/completed, then
# a save, sometimes twice, seconds apart). A wake delivered in that window is wiped by the tab's older copy, and the
# reply then streams into a message the chat no longer has: a bare stub as the current message, and the page sits on
# "Loading..." (chat 00086b8e, 2026-10-20 17:48:08-12). So: wait until the chat has been quiet for SETTLE_S, and
# after a delivery keep checking that the wake's two messages are still there, putting them back if a tab dropped them.
STALE_REPLY_S = 600      # an unfinished reply with no chat save for this long is not streaming any more
# ...and sooner when Open WebUI itself says no turn runs in the chat (opencode is already idle by then): job 4684
# (2026-10-23 02:57) sat out the full 10 minutes on a reply whose done flag was lost.
QUIET_REPLY_S = 90
SETTLE_S = 30
WATCH_S = 30 * 60
WATCH_EVERY_S = 15
# Queued messages: a tab that is open and visible on the chat types them in and sends them itself, which the user
# sees happen. While such a tab keeps polling, leave it to that tab for this long after the chat went quiet (the
# user may be typing in the box); a tab that stopped polling (asleep, closed) gets only the usual SETTLE_S.
TAB_LIVE_S = 25
QUEUE_TAB_WAIT_S = 600


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, flush=True)


def load_env():
    cfg = {"OWUI_URL": "http://127.0.0.1:8080", "DEPLOY_URL": "http://127.0.0.1:8095",
           "PIPE_MODEL": "code_mode.ornith-1.5-35b-a3b", "POLL_S": "5", "DRY_RUN": "0"}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    cfg.update({k: v for k, v in os.environ.items() if k in cfg})
    return cfg


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def token_for(user_id):
    key = SECRET.read_text().strip().encode()
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "." + \
        b64(json.dumps({"id": user_id, "exp": int(time.time()) + 600}).encode())
    return head + "." + b64(hmac.new(key, head.encode(), hashlib.sha256).digest())


def chat_owner(chat_id):
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    try:
        row = con.execute("select user_id from chat where id = ?", (chat_id,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def first_admin():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    try:
        row = con.execute("select id from user where role = 'admin' order by created_at limit 1").fetchone()
        return row[0] if row else None
    finally:
        con.close()


def http(method, url, token, body=None, timeout=60):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class Waker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.notes = {}         # wake id -> last note sent (only send changes)
        self.first_seen = {}    # wake id -> when we first saw it
        self.tries = {}
        self.cooldown_until = 0.0   # after a delivery: give the pipe time to start its opencode turn before the next
        self.watching = []          # delivered wakes whose messages we keep an eye on (see SETTLE_S)
        self.queue_notes = {}       # chat -> the last note sent to code-deploy for its queued messages
        self.admin_id = None
        self.last_error = {}        # what -> the last error logged (log a failing service once, not every poll)

    def later(self, method, path, body=None):
        return http(method, self.cfg["LATER_URL"].rstrip("/") + path, self.cfg["LATER_TOKEN"], body, timeout=40)

    def report(self, ids, status=None, note=""):
        for i in ids:
            if status is None and self.notes.get(i) == note:
                continue
            self.notes[i] = note
            body = {"note": note}
            if status:
                body["status"] = status
            try:
                self.later("POST", "/api/wakes/" + i, body)
            except (OSError, ValueError) as e:
                log("could not report", i, status or "note", e)

    def deploy(self, method, path, body=None):
        if not self.admin_id:
            self.admin_id = first_admin()
            if not self.admin_id:
                raise ValueError("no Open WebUI admin to sign as")
        return http(method, self.cfg["DEPLOY_URL"].rstrip("/") + path, token_for(self.admin_id), body, timeout=30)

    def owui(self, method, path, token, body=None):
        return http(method, self.cfg["OWUI_URL"].rstrip("/") + path, token, body, timeout=120)

    def model_of(self, chat):
        """The model the page recorded as picked on the chat's latest user message (not the Code pipe's own id)."""
        h = chat.get("history") or {}
        msgs, at = h.get("messages") or {}, h.get("currentId")
        while at:
            m = msgs.get(at) or {}
            if m.get("role") == "user":
                picked = (m.get("models") or [""])[0]
                if picked and not picked.startswith("code_mode."):
                    return picked
            at = m.get("parentId")
        return None

    def busy_reason(self, chat_id, token, chat, updated_at=None):
        h = chat.get("history") or {}
        cur = (h.get("messages") or {}).get(h.get("currentId") or "")
        # A reply counts as unfinished only while it could still be streaming: not done, no error, and the chat saved
        # in the last few minutes. A reply that errored, or one whose done flag a tab lost, must not hold wakes forever;
        # a turn that is really running is caught by the task check below.
        if cur and (cur.get("role") != "assistant" or not (cur.get("done") or cur.get("error"))):
            age = time.time() - float(updated_at) if updated_at else 0.0
            if cur.get("role") != "assistant" or not updated_at or (
                    age < STALE_REPLY_S and not (age >= QUIET_REPLY_S and self.no_turn(chat_id, token))):
                return "waiting: the chat's last reply is not finished"
        if updated_at and time.time() - float(updated_at) < SETTLE_S:
            return "waiting: the chat was saved moments ago (an open tab may still be saving it)"
        try:
            t = self.owui("GET", "/api/tasks/chat/" + chat_id, token)
            if t.get("task_ids"):
                return "waiting: a turn is running in this chat"
        except (OSError, ValueError):
            pass
        return None

    def no_turn(self, chat_id, token):
        """True only when Open WebUI answered that no turn is running in this chat; an error is not that answer."""
        try:
            t = self.owui("GET", "/api/tasks/chat/" + chat_id, token)
        except (OSError, ValueError):
            return False
        return isinstance(t, dict) and not t.get("task_ids")

    def deliver(self, w):
        chat_id, ids, text = w["chat"], w["ids"], w["text"]
        owner = chat_owner(chat_id)
        if not owner:
            self.report(ids, "failed", "no Open WebUI chat with this id")
            log("no such chat", chat_id, ids)
            return
        token = token_for(owner)
        got = self.owui("GET", "/api/v1/chats/" + chat_id, token)
        chat = got["chat"]
        why = self.busy_reason(chat_id, token, chat, got.get("updated_at"))
        if why:
            self.report(ids, None, why)
            return
        model = w.get("code_model") or self.model_of(chat)   # else what the page recorded as picked
        if not model:
            self.report(ids, "failed", "cannot tell which model this chat uses")
            log("no model for", chat_id)
            return
        if self.cfg["DRY_RUN"] == "1":
            self.report(ids, None, "dry run: would send now on " + model)
            log("DRY RUN", chat_id, model, len(text), "chars")
            return
        # Claim first (2026-10-24): the Code pipe (0.27+) hands a wake to the chat's own running turn at the end of a
        # run. Whoever claims it first delivers it, so it arrives exactly once.
        claimed = self.claim(chat_id, ids)
        if claimed is None:
            return
        text = claimed or text
        try:
            self._post_wake(w, chat_id, owner, token, chat, model, text)
        except BaseException:
            if claimed:
                self.unclaim(ids)      # not delivered after all: ready again for the next poll
            raise

    def claim(self, chat_id, ids):
        """The wake's text once claimed for this delivery, '' when code-later predates claims (old behaviour), or
        None when someone else (a running turn) took one of them first."""
        try:
            got = self.later("POST", "/api/claim", {"chat": chat_id, "ids": ids, "note": "claimed by code-wake"})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
            raise
        mine = got.get("ids") or []
        if sorted(mine) != sorted(ids):
            if mine:
                self.unclaim(mine)
            log("not delivering", ids, "to", chat_id, "- already taken:", sorted(set(ids) - set(mine)))
            return None
        return got.get("text") or ""

    def unclaim(self, ids):
        try:
            self.later("POST", "/api/unclaim", {"ids": list(ids)})
        except (OSError, ValueError) as e:
            log("could not give back", ids, e)

    def _post_wake(self, w, chat_id, owner, token, chat, model, text):
        # code_wake: the pipe (>= 0.24.2) then leaves other chats' running servers alone (the user didn't start this turn)
        uid, r = self._post_turn(chat_id, owner, token, chat, model, text, {"code_wake": True})
        self.report(w["ids"], "delivered", "sent %s on %s" % (time.strftime("%H:%M"), model))
        log("delivered", w["ids"], "to", chat_id, "on", model, json.dumps(r)[:120])

    def _post_turn(self, chat_id, owner, token, chat, model, text, extra):
        """Add text as the chat's next user message and ask Open WebUI for the reply, as a Code turn on `model`
        (extra: more request fields for the pipe). Returns (the user message id, Open WebUI's answer)."""
        h = chat["history"]
        parent = h.get("currentId")
        msgs = h["messages"]
        now = int(time.time())
        uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
        if parent:
            msgs[parent].setdefault("childrenIds", []).append(uid)
        msgs[uid] = {"id": uid, "parentId": parent, "childrenIds": [aid], "role": "user", "content": text,
                     "timestamp": now, "models": [model]}
        msgs[aid] = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant", "content": "",
                     "model": model, "modelName": model, "modelIdx": 0, "timestamp": now}
        h["currentId"] = aid
        thread, at = [], uid
        while at:
            m = msgs[at]
            thread.append({"role": m["role"], "content": m.get("content") or ""})
            at = m.get("parentId")
        thread.reverse()
        chat["messages"] = list(msgs.values())
        self.owui("POST", "/api/v1/chats/" + chat_id, token, {"chat": chat})
        body = {"stream": True, "model": self.cfg["PIPE_MODEL"], "code_model": model, **extra,
                "messages": thread,
                "params": {}, "chat_id": chat_id, "id": aid, "parent_id": uid,
                "session_id": "code-wake-" + uuid.uuid4().hex[:12],
                "background_tasks": {"title_generation": False, "tags_generation": False,
                                     "follow_up_generation": False}}
        r = self.owui("POST", "/api/chat/completions", token, body)
        self.cooldown_until = time.time() + 90
        self.watching.append({"chat": chat_id, "owner": owner, "parent": parent, "user": msgs[uid], "reply": msgs[aid],
                              "until": time.time() + WATCH_S, "next": time.time() + WATCH_EVERY_S})
        return uid, r

    # ---- the user's queued messages (code-deploy /api/queue) -------------
    def queue_note(self, chat_id, note):
        """Tell code-deploy why a chat's queue waits (shown under the messages); only changes are sent."""
        if self.queue_notes.get(chat_id) == note:
            return
        try:
            self.deploy("POST", "/api/queue/note", {"chat": chat_id, "note": note})
            self.queue_notes[chat_id] = note
        except (OSError, ValueError) as e:
            log("could not note the queue of", chat_id, e)

    def log_once(self, what, err):
        msg = repr(err)[:300]
        if self.last_error.get(what) != msg:
            self.last_error[what] = msg
            log(what, "failed:", msg)

    def queue_pending(self):
        """Chats with Code-mode messages ready to go ([] when none), or None when code-deploy can't say."""
        try:
            chats = self.deploy("GET", "/api/queue/pending").get("chats") or []
        except (OSError, ValueError) as e:
            self.log_once("queue list", e)
            return None
        self.last_error.pop("queue list", None)
        return [p for p in chats if p.get("mode") == "code"]   # a Chat-mode message goes out from a tab

    def send_queue(self, pending, active):
        """Send one chat's queued messages when its reply is finished and no tab is there to do it. True when it
        sent something (the next delivery then waits for that turn)."""
        for p in pending:
            chat_id = p["chat_id"]
            owner = p.get("user_id") or chat_owner(chat_id)
            if not owner:
                continue
            token = token_for(owner)
            got = self.owui("GET", "/api/v1/chats/" + chat_id, token)
            chat, updated = got["chat"], got.get("updated_at")
            if self.busy_reason(chat_id, token, chat, updated):
                self.queue_note(chat_id, "")          # its own reply is still going: the page says so itself
                continue
            if active is None:
                self.queue_note(chat_id, "Waiting: cryoai can't see whether a Code turn is running")
                continue
            if active:
                self.queue_note(chat_id, "Waiting for the Code turn running in another chat")
                continue
            seen = p.get("tab_visible_at")
            quiet = time.time() - float(updated) if updated else float("inf")
            if seen and time.time() - float(seen) < TAB_LIVE_S and quiet < QUEUE_TAB_WAIT_S:
                self.queue_note(chat_id, "")          # a tab open on the chat sends it, in front of the user
                continue
            model = (p.get("extras") or {}).get("code_model") or self.model_of(chat)
            if not model:
                self.queue_note(chat_id, "Not sent: cryoai can't tell which model this chat uses. Open the chat.")
                continue
            if self.cfg["DRY_RUN"] == "1":
                self.queue_note(chat_id, "dry run: would send now on " + model)
                continue
            c = self.deploy("POST", "/api/queue/claim", {"chat": chat_id, "by": "code-wake"})
            if not c.get("claim"):
                continue
            extras = c.get("extras") or {}
            model = extras.get("code_model") or model
            extra = {"code_queued": True}
            for k in ("code_effort", "code_repo"):
                if extras.get(k):
                    extra[k] = extras[k]
            if extras.get("code_feature"):
                extra["code_feature"] = True
            try:
                uid, r = self._post_turn(chat_id, owner, token, chat, model, c["text"], extra)
            except BaseException as e:
                try:
                    self.deploy("POST", "/api/queue/claim/" + c["claim"],
                                {"status": "requeue", "note": "cryoai could not send it yet: %s" % repr(e)[:120]})
                except (OSError, ValueError) as e2:
                    log("could not give back claim", c["claim"], e2)   # it runs out and is queued again
                raise
            try:
                self.deploy("POST", "/api/queue/claim/" + c["claim"],
                            {"status": "sent", "message_id": uid, "note": "sent from cryoai"})
            except (OSError, ValueError) as e:
                log("sent, but could not tell code-deploy (the claim runs out and finds it in the chat):", e)
            self.queue_notes.pop(chat_id, None)
            log("queue: sent", len(c.get("items") or []), "message(s) to", chat_id, "on", model, json.dumps(r)[:120])
            return True
        return False

    def guard(self, w):
        """A delivered wake's messages are still in the chat; if a tab saved an older copy over them, put them back
        (keeping whatever Open WebUI has written into the reply since). Returns a short note when it changed something."""
        token = token_for(w["owner"])
        chat = self.owui("GET", "/api/v1/chats/" + w["chat"], token)["chat"]
        h = chat.get("history") or {}
        msgs = h.setdefault("messages", {})
        uid, aid, parent = w["user"]["id"], w["reply"]["id"], w["parent"]
        have_user = (msgs.get(uid) or {}).get("role") == "user"
        have_reply = (msgs.get(aid) or {}).get("role") == "assistant"
        if have_user and have_reply:
            return None
        cur = h.get("currentId")
        stub = cur == aid and not (msgs.get(cur) or {}).get("role")
        msgs[uid] = {**w["user"], **{k: v for k, v in (msgs.get(uid) or {}).items() if k != "role"}}
        msgs[uid]["childrenIds"] = sorted(set(msgs[uid].get("childrenIds") or []) | {aid})
        msgs[aid] = {**w["reply"], **(msgs.get(aid) or {})}
        for k in ("id", "parentId", "role", "model"):
            msgs[aid][k] = w["reply"][k]
        if parent in msgs:
            kids = msgs[parent].setdefault("childrenIds", [])
            if uid not in kids:
                kids.append(uid)
        elif parent:
            msgs[uid]["parentId"] = None   # the parent is gone too: keep the wake as its own branch
        # The wake is the current turn again only when the chat was left on it (the stub) or put back to before it;
        # if the user has carried on from an older copy since, their messages stay current and the wake is a branch.
        if stub or cur == parent or cur not in msgs:
            h["currentId"] = aid
            # the flat list follows the current branch
            line, at = [], aid
            while at and at in msgs and len(line) < 10000:
                line.append(msgs[at])
                at = msgs[at].get("parentId")
            chat["messages"] = line[::-1]
        self.owui("POST", "/api/v1/chats/" + w["chat"], token, {"chat": chat})
        return "put back the wake's messages (%s)" % ("as the current turn" if h["currentId"] == aid else "as a branch")

    def poll(self):
        for w in list(self.watching):
            if time.time() >= w["until"]:
                self.watching.remove(w)
                continue
            if time.time() < w["next"]:
                continue
            w["next"] = time.time() + WATCH_EVERY_S
            try:
                note = self.guard(w)
                if note:
                    log(w["chat"], note)
            except (OSError, ValueError, KeyError) as e:
                log("guard for", w["chat"], "failed:", repr(e)[:200])
        if time.time() < self.cooldown_until:
            return
        pending = self.queue_pending()
        try:
            # ?active=1: say whether a Code turn runs even with no wake ready (queued messages wait for it too)
            r = self.later("GET", "/api/wakes?active=1" if pending else "/api/wakes")
            self.last_error.pop("later", None)
        except (OSError, ValueError) as e:
            self.log_once("later", e)
            r = None
        # A code-later from before ?active=1 answers [] without looking when no wake is ready: then nobody knows.
        active = r.get("opencode_active") if r and (r.get("active_checked") or r.get("wakes")) else None
        if pending:
            try:
                if self.send_queue(pending, active):
                    return
            except (OSError, ValueError, KeyError) as e:
                log("sending a queue failed:", repr(e)[:300])
        wakes = (r or {}).get("wakes") or []
        if not wakes:
            return
        for w in wakes:
            for i in w["ids"]:
                self.first_seen.setdefault(i, time.time())
            if all(time.time() - self.first_seen[i] > GIVE_UP_S for i in w["ids"]):
                self.report(w["ids"], "failed", "gave up: not delivered within 24 h")
                continue
            if active is None:
                self.report(w["ids"], None, "waiting: cannot see whether a Code turn is running")
                continue
            if active:
                self.report(w["ids"], None, "waiting: a Code turn is running (one model slot)")
                continue
            key = w["ids"][0]
            try:
                self.deliver(w)
                self.tries.pop(key, None)
            except (OSError, ValueError, KeyError) as e:
                self.tries[key] = self.tries.get(key, 0) + 1
                log("delivery to", w["chat"], "failed (try %d):" % self.tries[key], repr(e)[:300])
                if self.tries[key] >= 5:
                    self.report(w["ids"], "failed", "delivery failed 5 times: %s" % repr(e)[:200])
                else:
                    self.report(w["ids"], None, "retrying: %s" % repr(e)[:200])
            break   # one delivery per poll: the next one waits until this turn has finished

    def loop(self):
        log("code-wake polling", self.cfg["LATER_URL"], "every", self.cfg["POLL_S"], "s",
            "(DRY RUN)" if self.cfg["DRY_RUN"] == "1" else "")
        while True:
            try:
                self.poll()
            except (OSError, ValueError) as e:
                log("poll failed:", repr(e)[:300])
            time.sleep(float(self.cfg["POLL_S"]))


if __name__ == "__main__":
    cfg = load_env()
    if not cfg.get("LATER_URL") or not cfg.get("LATER_TOKEN"):
        sys.exit("LATER_URL and LATER_TOKEN must be set in %s" % ENV)
    Waker(cfg).loop()
