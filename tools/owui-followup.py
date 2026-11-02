#!/usr/bin/env python3
"""Send the next message in an existing Open WebUI chat from outside (run on cryoai), the way owui-chat.py starts one:
the user message and an empty assistant message are added to the chat, then Open WebUI's backend fills the reply,
which is saved and streamed to any page that has the chat open. Needs no browser (compose.py types into the user's
own page instead, and found nothing to type into on 2026-10-13 while the browser window was in the background).
    CODE=1 python3 owui-followup.py CHAT_ID MODEL < message.txt        MODEL = Open WebUI id, e.g. slerm-turing.X
The admin token is signed in-process (compose.py) and never printed."""
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


cid, model, text = sys.argv[1], sys.argv[2], sys.stdin.read().strip()
code = os.environ.get("CODE") == "1"
chat = call("GET", "/api/v1/chats/" + cid)["chat"]
h = chat["history"]
parent = h["currentId"]
if h["messages"][parent].get("role") != "assistant" or not h["messages"][parent].get("done"):
    sys.exit("the chat's last message is not a finished reply; not sending")
now = int(time.time())
uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
amodel = model  # the label on the reply = the picked model, as the page does it; the pipe id goes in the request only
h["messages"][parent].setdefault("childrenIds", []).append(uid)
h["messages"][uid] = {"id": uid, "parentId": parent, "childrenIds": [aid], "role": "user", "content": text,
                      "timestamp": now, "models": [model]}
h["messages"][aid] = {"id": aid, "parentId": uid, "childrenIds": [], "role": "assistant", "content": "",
                      "model": amodel, "modelName": amodel, "modelIdx": 0, "timestamp": now}
h["currentId"] = aid
thread, at = [], uid
while at:
    m = h["messages"][at]
    thread.append({"role": m["role"], "content": m.get("content") or ""})
    at = m.get("parentId")
thread.reverse()
chat["messages"] = list(h["messages"].values())
call("POST", "/api/v1/chats/" + cid, {"chat": chat})
body = {"stream": True, "model": "code_mode.ornith-1.5-35b-a3b" if code else model, "messages": thread, "params": {}, "chat_id": cid, "id": aid, "parent_id": uid,
        "session_id": "outside-" + uuid.uuid4().hex[:12],
        "background_tasks": {"title_generation": False, "tags_generation": False, "follow_up_generation": True}}
if code:
    body["code_model"] = model
else:
    body["features"] = {}
r = call("POST", "/api/chat/completions", body)
print(json.dumps(r)[:200])
