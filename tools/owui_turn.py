"""Run one turn through Open WebUI's own chat API (run on cryoai with Open WebUI's venv python).

Signs a short-lived admin token in memory with ~/.webui_secret_key (never printed or saved).
No browser is attached, so dialogs (approvals, questions) can't be answered - keep test prompts free of them.
Usage: python owui_turn.py MODEL CHAT_ID "message" [timeout_s]
CHAT_ID "new:<title>" makes a real chat first (it shows in the sidebar). CODE_FEATURE=1 sends code_feature, as the page
script does for a chat started with the Feature button.
"""
import os
import json, sqlite3, sys, time, urllib.request, warnings
from pathlib import Path

import jwt

warnings.filterwarnings("ignore")
model, chat_id, text = sys.argv[1], sys.argv[2], sys.argv[3]
timeout = int(sys.argv[4]) if len(sys.argv) > 4 else 1800
key = (Path.home() / ".webui_secret_key").read_text().strip()
uid = sqlite3.connect(f"file:{Path.home()}/.open-webui/webui.db?mode=ro", uri=True).execute(
    "select id from user where role='admin' order by created_at limit 1").fetchone()[0]
tok = jwt.encode({"id": uid, "exp": int(time.time()) + timeout + 60}, key, algorithm="HS256")
def api(path, payload):
    r = urllib.request.Request("http://127.0.0.1:8080" + path, data=json.dumps(payload).encode(),
                               headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=60))


if chat_id.startswith("new:"):
    chat_id = api("/api/v1/chats/new", {"chat": {"title": chat_id[4:], "models": [model], "messages": []}})["id"]
    print("chat:", chat_id, flush=True)
body = {"model": model, "stream": False, "chat_id": chat_id, "messages": [{"role": "user", "content": text}]}
if os.environ.get("CODE_FEATURE") == "1":
    body["code_feature"] = True
req = urllib.request.Request("http://127.0.0.1:8080/api/chat/completions", data=json.dumps(body).encode(),
                             headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
t0 = time.time()
d = json.load(urllib.request.urlopen(req, timeout=timeout))
print(f"[{time.time() - t0:.0f}s]")
print(d["choices"][0]["message"]["content"] if "choices" in d else json.dumps(d)[:3000])
