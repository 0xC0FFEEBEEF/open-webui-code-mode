"""code-wake against a fake Open WebUI: the 2026-10-20 collision (a tab's end-of-turn save wiping a just-delivered
wake, then the reply streaming into a message the chat no longer has) and the settle wait that prevents it."""
import copy, importlib.util, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("code_wake", os.path.join(HERE, "..", "code-wake.py"))
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


class FakeOWUI:
    def __init__(self, chat, updated_at):
        self.chat, self.updated_at, self.posts, self.completions = chat, updated_at, 0, []

    def __call__(self, method, path, token, body=None):
        if path.startswith("/api/v1/chats/") and method == "GET":
            return {"chat": copy.deepcopy(self.chat), "updated_at": self.updated_at}
        if path.startswith("/api/v1/chats/") and method == "POST":
            self.chat, self.updated_at, self.posts = copy.deepcopy(body["chat"]), time.time(), self.posts + 1
            return {}
        if path.startswith("/api/tasks/chat/"):
            return {"task_ids": []}
        if path == "/api/chat/completions":
            self.completions.append(body)
            return {"status": True}
        raise AssertionError(path)

    def upsert(self, mid, fields):  # what Open WebUI's backend does while a reply streams
        h = self.chat["history"]
        h["messages"][mid] = {**h["messages"].get(mid, {}), **fields}
        h["currentId"] = mid


def chat():
    m = {"u1": {"id": "u1", "parentId": None, "childrenIds": ["a1"], "role": "user", "content": "go", "models": ["slerm-turing.X"]},
         "a1": {"id": "a1", "parentId": "u1", "childrenIds": [], "role": "assistant", "content": "done", "done": True}}
    return {"history": {"messages": m, "currentId": "a1"}, "messages": list(m.values())}


def waker(fake, taken=(), claims=True):
    """taken: wake ids a running turn already claimed. claims=False: a code-later from before /api/claim (404)."""
    w = cw.Waker({"DRY_RUN": "0", "OWUI_URL": "x", "PIPE_MODEL": "code_mode.x", "LATER_URL": "y", "LATER_TOKEN": "t"})
    w.owui = fake
    w.report = lambda *a, **k: None
    w.calls = []

    def later(method, path, body=None):
        w.calls.append((path, body))
        if path == "/api/claim":
            if not claims:
                raise cw.urllib.error.HTTPError("y" + path, 404, "Not Found", {}, None)
            mine = [i for i in body["ids"] if i not in taken]
            return {"ids": mine, "text": "claimed: " + ",".join(mine) if mine else ""}
        return {}
    w.later = later
    return w


def run(monkeypatch, updated_ago):
    monkeypatch.setattr(cw, "chat_owner", lambda c: "owner-1")
    monkeypatch.setattr(cw, "token_for", lambda u: "tok")
    fake = FakeOWUI(chat(), time.time() - updated_ago)
    w = waker(fake)
    w.deliver({"chat": "c1", "ids": ["L1"], "text": "wake up", "code_model": "slerm-turing.X"})
    return fake, w


def test_a_chat_saved_moments_ago_waits(monkeypatch):
    fake, w = run(monkeypatch, 3)
    assert fake.completions == [] and fake.posts == 0 and w.watching == []


def test_the_tab_collision_is_put_back(monkeypatch):
    fake, w = run(monkeypatch, 120)
    [sent] = fake.completions
    aid, uid = sent["id"], sent["parent_id"]
    stale = chat()                                    # the tab's older copy, saved over the wake
    fake.chat = stale
    fake.upsert(aid, {"content": "[Preview](x)", "output": [{"type": "message"}]})
    cur = fake.chat["history"]["messages"][aid]
    assert cur.get("role") is None and cur.get("parentId") is None      # the broken stub of 10-20
    w.watching[0]["next"] = 0
    w.poll = w.poll  # guard runs from poll
    note = w.guard(w.watching[0])
    assert note and "current turn" in note
    h = fake.chat["history"]
    assert h["currentId"] == aid
    assert h["messages"][aid]["role"] == "assistant" and h["messages"][aid]["parentId"] == uid
    assert h["messages"][aid]["content"] == "[Preview](x)"               # what streamed in is kept
    assert h["messages"][uid]["parentId"] == "a1" and uid in h["messages"]["a1"]["childrenIds"]
    assert [m["id"] for m in fake.chat["messages"]] == ["u1", "a1", uid, aid]
    fake.upsert(aid, {"content": "more", "done": True})             # later stream writes merge into the real message
    assert fake.chat["history"]["messages"][aid]["role"] == "assistant"
    assert w.guard(w.watching[0]) is None                            # nothing to do now


def test_a_user_who_carried_on_keeps_their_turn(monkeypatch):
    fake, w = run(monkeypatch, 120)
    aid = fake.completions[0]["id"]
    c = chat()  # the user sent a new message from an older copy: u2/a2 under a1
    c["history"]["messages"]["a1"]["childrenIds"] = ["u2"]
    c["history"]["messages"]["u2"] = {"id": "u2", "parentId": "a1", "childrenIds": ["a2"], "role": "user", "content": "hi"}
    c["history"]["messages"]["a2"] = {"id": "a2", "parentId": "u2", "childrenIds": [], "role": "assistant", "content": "", "done": True}
    c["history"]["currentId"] = "a2"
    fake.chat = c
    note = w.guard(w.watching[0])
    assert "branch" in note and fake.chat["history"]["currentId"] == "a2"
    assert fake.chat["history"]["messages"][aid]["role"] == "assistant"


# ---- 2026-10-23: an unfinished reply with no turn running waits QUIET_REPLY_S, not STALE_REPLY_S (job 4684 sat 10 min)

def unfinished(monkeypatch, saved_ago, tasks):
    monkeypatch.setattr(cw, "chat_owner", lambda c: "owner-1")
    monkeypatch.setattr(cw, "token_for", lambda u: "tok")
    c = chat()
    c["history"]["messages"]["a1"]["done"] = False                    # the done flag a tab lost
    fake = FakeOWUI(c, time.time() - saved_ago)
    plain = fake.__call__

    def owui(method, path, token, body=None):
        if path.startswith("/api/tasks/chat/"):
            if isinstance(tasks, Exception):
                raise tasks
            return {"task_ids": tasks}
        return plain(method, path, token, body)
    w = waker(owui)
    w.deliver({"chat": "c1", "ids": ["L1"], "text": "wake up", "code_model": "slerm-turing.X"})
    return fake


def test_a_quiet_unfinished_reply_with_no_turn_running_gets_the_wake(monkeypatch):
    assert len(unfinished(monkeypatch, 120, []).completions) == 1


def test_an_unfinished_reply_saved_recently_still_waits(monkeypatch):
    assert unfinished(monkeypatch, 60, []).completions == []


def test_a_turn_that_is_running_is_never_bypassed(monkeypatch):
    assert unfinished(monkeypatch, 120, ["task-1"]).completions == []


def test_when_open_webui_cannot_say_it_waits_the_full_stale_time(monkeypatch):
    assert unfinished(monkeypatch, 120, OSError("down")).completions == []
    assert len(unfinished(monkeypatch, 700, OSError("down")).completions) == 1


# ---- 2026-10-24: claims. The Code pipe (0.27) can hand a wake to the chat's own running turn; each arrives once.

def test_a_wake_a_running_turn_already_took_is_not_sent_again(monkeypatch):
    monkeypatch.setattr(cw, "chat_owner", lambda c: "owner-1")
    monkeypatch.setattr(cw, "token_for", lambda u: "tok")
    fake = FakeOWUI(chat(), time.time() - 120)
    w = waker(fake, taken={"L1"})
    w.deliver({"chat": "c1", "ids": ["L1", "L2"], "text": "wake up", "code_model": "slerm-turing.X"})
    assert fake.completions == [] and fake.posts == 0
    assert ("/api/unclaim", {"ids": ["L2"]}) in w.calls        # the part it did claim goes back


def test_the_claimed_text_is_what_is_sent(monkeypatch):
    monkeypatch.setattr(cw, "chat_owner", lambda c: "owner-1")
    monkeypatch.setattr(cw, "token_for", lambda u: "tok")
    fake = FakeOWUI(chat(), time.time() - 120)
    w = waker(fake)
    w.deliver({"chat": "c1", "ids": ["L1"], "text": "stale text", "code_model": "slerm-turing.X"})
    [sent] = fake.completions
    assert sent["messages"][-1]["content"] == "claimed: L1"
    assert not any(p == "/api/unclaim" for p, _ in w.calls)


def test_a_failed_post_gives_the_claim_back(monkeypatch):
    monkeypatch.setattr(cw, "chat_owner", lambda c: "owner-1")
    monkeypatch.setattr(cw, "token_for", lambda u: "tok")
    fake = FakeOWUI(chat(), time.time() - 120)
    plain = fake.__call__

    def owui(method, path, token, body=None):
        if path == "/api/chat/completions":
            raise OSError("open webui went away")
        return plain(method, path, token, body)
    w = waker(owui)
    try:
        w.deliver({"chat": "c1", "ids": ["L1"], "text": "wake up", "code_model": "slerm-turing.X"})
    except OSError:
        pass
    else:
        raise AssertionError("the failure must reach poll(), which retries")
    assert ("/api/unclaim", {"ids": ["L1"]}) in w.calls


def test_a_code_later_without_claims_still_gets_its_wake_delivered(monkeypatch):
    monkeypatch.setattr(cw, "chat_owner", lambda c: "owner-1")
    monkeypatch.setattr(cw, "token_for", lambda u: "tok")
    fake = FakeOWUI(chat(), time.time() - 120)
    w = waker(fake, claims=False)
    w.deliver({"chat": "c1", "ids": ["L1"], "text": "wake up", "code_model": "slerm-turing.X"})
    [sent] = fake.completions
    assert sent["messages"][-1]["content"] == "wake up"


# ---- the user's queued messages (2026-10-30) --------------------------------------------------------------------
class FakeDeploy:
    """code-deploy's /api/queue, as code-wake sees it."""

    def __init__(self, pending, claim_text="do the next thing", extras=None):
        self.pending, self.calls = pending, []
        self.claim_text, self.extras = claim_text, extras or {}
        self.claimed = False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/api/queue/pending":
            return {"chats": self.pending}
        if path == "/api/queue/claim":
            if self.claimed:
                return {"claim": None}
            self.claimed = True
            return {"claim": "cl1", "text": self.claim_text, "mode": "code", "extras": self.extras,
                    "user_id": "owner-1", "items": [{"id": "q1"}]}
        if path.startswith("/api/queue/claim/") or path == "/api/queue/note":
            return {}
        raise AssertionError(path)

    def notes(self):
        return [b["note"] for m, p, b in self.calls if p == "/api/queue/note"]

    def finished(self):
        return [b for m, p, b in self.calls if p.startswith("/api/queue/claim/")]


def queued(monkeypatch, updated_ago=120, tab_visible_ago=None, extras=None, tasks=()):
    monkeypatch.setattr(cw, "chat_owner", lambda c: "owner-1")
    monkeypatch.setattr(cw, "token_for", lambda u: "tok")
    fake = FakeOWUI(chat(), time.time() - updated_ago)
    def owui(method, path, token, body=None):            # Open WebUI, with this chat's running tasks
        if path.startswith("/api/tasks/chat/"):
            return {"task_ids": list(tasks)}
        return fake(method, path, token, body)
    w = waker(owui)
    p = {"chat_id": "c1", "user_id": "owner-1", "n": 1, "mode": "code",
         "extras": extras if extras is not None else {"code_model": "slerm-turing.Q"},
         "tab_visible_at": time.time() - tab_visible_ago if tab_visible_ago is not None else None}
    d = FakeDeploy([p], extras=extras if extras is not None else {"code_model": "slerm-turing.Q"})
    w.deploy = d
    return fake, w, d, p


def test_a_queued_message_goes_out_as_a_code_turn_when_no_tab_is_there(monkeypatch):
    extras = {"code_model": "slerm-turing.Q", "code_effort": "low", "code_repo": "me/repo", "code_feature": True}
    fake, w, d, p = queued(monkeypatch, extras=extras)
    assert w.send_queue([p], []) is True
    [sent] = fake.completions
    assert sent["code_model"] == "slerm-turing.Q" and sent["code_effort"] == "low" and sent["code_repo"] == "me/repo"
    assert sent["code_feature"] is True and sent["code_queued"] is True and "code_wake" not in sent
    msgs = fake.chat["history"]["messages"]
    assert msgs[sent["parent_id"]]["content"] == "do the next thing" and msgs[sent["parent_id"]]["parentId"] == "a1"
    assert sent["messages"][-1] == {"role": "user", "content": "do the next thing"}
    [fin] = d.finished()
    assert fin["status"] == "sent" and fin["message_id"] == sent["parent_id"]
    assert w.watching and w.watching[0]["user"]["id"] == sent["parent_id"]   # guarded like a wake


def test_a_tab_open_on_the_chat_sends_it_itself_for_a_while(monkeypatch):
    fake, w, d, p = queued(monkeypatch, updated_ago=120, tab_visible_ago=3)
    assert w.send_queue([p], []) is False and fake.completions == []
    assert ("POST", "/api/queue/claim", {"chat": "c1", "by": "code-wake"}) not in d.calls
    fake, w, d, p = queued(monkeypatch, updated_ago=cw.QUEUE_TAB_WAIT_S + 5, tab_visible_ago=3)
    assert w.send_queue([p], []) is True                    # the tab never did (the user kept typing?)
    fake, w, d, p = queued(monkeypatch, updated_ago=120, tab_visible_ago=cw.TAB_LIVE_S + 5)
    assert w.send_queue([p], []) is True                    # the tab went quiet (asleep): no long wait


def test_a_code_turn_in_another_chat_holds_the_queue(monkeypatch):
    fake, w, d, p = queued(monkeypatch)
    assert w.send_queue([p], ["ses_other"]) is False and fake.completions == []
    assert d.notes() == ["Waiting for the Code turn running in another chat"]
    w.send_queue([p], ["ses_other"])
    assert len(d.notes()) == 1                              # notes go only when they change
    assert w.send_queue([p], None) is False
    assert d.notes()[-1].startswith("Waiting: cryoai can't see")


def test_the_chats_own_running_turn_is_never_bypassed(monkeypatch):
    fake, w, d, p = queued(monkeypatch, tasks=["t1"])
    assert w.send_queue([p], []) is False and fake.completions == []


def test_a_chat_saved_moments_ago_waits_like_a_wake(monkeypatch):
    fake, w, d, p = queued(monkeypatch, updated_ago=3)
    assert w.send_queue([p], []) is False and fake.completions == []


def test_a_failed_send_gives_the_queue_back(monkeypatch):
    fake, w, d, p = queued(monkeypatch)
    def boom(*a, **k):
        raise OSError("connection refused")
    real = w.owui
    w.owui = lambda method, path, token, body=None: boom() if path == "/api/chat/completions" else real(method, path, token, body)
    try:
        w.send_queue([p], [])
        raise AssertionError("expected the error")
    except OSError:
        pass
    [fin] = d.finished()
    assert fin["status"] == "requeue" and "could not send" in fin["note"]


def test_with_no_recorded_model_the_chats_last_pick_is_used(monkeypatch):
    fake, w, d, p = queued(monkeypatch, extras={})
    assert w.send_queue([p], []) is True
    assert fake.completions[0]["code_model"] == "slerm-turing.X"     # the last user message's models[0]


def test_poll_asks_whether_a_code_turn_runs_only_when_messages_wait(monkeypatch):
    fake, w, d, p = queued(monkeypatch)
    w.queue_pending = lambda: []
    w.poll()
    assert w.calls[-1][0] == "/api/wakes"
    w.queue_pending = lambda: [p]
    w.later = lambda method, path, body=None: (w.calls.append((path, body)) or {"wakes": [], "opencode_active": []})
    w.poll()                                        # an old code-later: [] without looking, so it must not send
    assert w.calls[-1][0] == "/api/wakes?active=1" and fake.completions == []
    assert d.notes()[-1].startswith("Waiting: cryoai can't see")
    w.later = lambda method, path, body=None: (w.calls.append((path, body)) or
                                              {"wakes": [], "opencode_active": [], "active_checked": True})
    w.poll()
    assert len(fake.completions) == 1
