#!/usr/bin/env python3
"""Start a chat in Open WebUI from outside, the way the page would (run on cryoai): create the chat with the user's
message and an empty assistant message, then ask Open WebUI's own backend to fill it. The reply is saved into the chat
and streamed to any browser page of that user that has it open. The admin token is signed in-process (compose.py) and
never printed.    python3 owui-chat.py MODEL TITLE < prompt.txt   -> prints the chat id
                  python3 owui-chat.py --poll CHAT_ID             -> prints reply length / title
Code mode: CODE=1 [CODE_EFFORT=high] python3 owui-chat.py MODEL TITLE < prompt.txt  sends what the page's Code pill sends
(model code_mode.ornith-1.5-35b-a3b + code_model = MODEL), so the turn runs in opencode on VM 129 with MODEL."""
import json, os, sys, time, uuid, urllib.request
sys.path.insert(0, "/tmp")
import compose
BASE = "http://127.0.0.1:8080"
tok = compose.token()

def call(method, path, body=None, timeout=120):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

if sys.argv[1] == "--poll":
    c = call("GET", "/api/v1/chats/" + sys.argv[2])
    h = c["chat"]["history"]
    m = h["messages"][h["currentId"]]
    st = [str(x.get("description"))[:110] for x in (m.get("statusHistory") or [])]
    print(json.dumps({"title": c.get("title"), "role": m.get("role"), "chars": len(m.get("content") or ""),
                      "done": m.get("done"), "error": m.get("error"), "followUps": len(m.get("followUps") or []),
                      "steps": len(st), "working_with": next((x for x in st if x.startswith("Working with")), None),
                      "last": st[-3:]}))
    sys.exit(0)

model, title, prompt = sys.argv[1], sys.argv[2], sys.stdin.read().strip()
now = int(time.time())
uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
umsg = {"id": uid, "parentId": None, "childrenIds": [aid], "role": "user", "content": prompt, "timestamp": now, "models": [model]}
# The reply is labelled with the picked model, as the page does it: the Code pipe's own id ("code_mode.ornith-...")
# only goes in the request. Labelled with the pipe id, the chat read "ornith" for a Qwen run (2026-10-13).
amodel = model
amsg = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant", "content": "", "model": amodel,
        "modelName": amodel, "modelIdx": 0, "timestamp": now}
chat = {"title": title, "models": [model], "params": {}, "tags": [], "timestamp": now * 1000,
        "history": {"messages": {uid: umsg, aid: amsg}, "currentId": aid}, "messages": [umsg, amsg]}
cid = call("POST", "/api/v1/chats/new", {"chat": chat})["id"]
body = {
    "stream": True, "model": model, "messages": [{"role": "user", "content": prompt}], "params": {}, "features": {},
    "chat_id": cid, "id": aid, "parent_id": uid, "session_id": "outside-" + uuid.uuid4().hex[:12],
    "background_tasks": {"title_generation": True, "tags_generation": True, "follow_up_generation": True}}
if os.environ.get("CODE") == "1":           # exactly what owui/code-toggle.js does for a Code-mode chat
    body["code_model"], body["model"] = model, "code_mode.ornith-1.5-35b-a3b"
    del body["features"]
    if os.environ.get("CODE_EFFORT"):
        body["code_effort"] = os.environ["CODE_EFFORT"]
r = call("POST", "/api/chat/completions", body)
print(cid)
print(json.dumps(r)[:200], file=sys.stderr)
