"""Open WebUI setup for the Desktop and Code tool (run on cryoai with Open WebUI's python).

1. installs/updates the tool `desktop_code` from desktop_code_tool.py
2. hides the Computer Agent entries from the model picker (the pipe stays; the tool calls it)
3. gives the tool to the main slerm chat models, with native function calling
Dry run by default (prints what it would do); --apply makes the changes after an online database backup.
Signs a short-lived admin token in memory with ~/.webui_secret_key (never printed or saved).
Usage: [secret |] python owui_setup_tools.py /tmp/desktop_code_tool.py [--apply] [--valve-stdin=KEY]
"""
import json, re, sqlite3, sys, time, urllib.error, urllib.request, warnings
from pathlib import Path

import jwt

warnings.filterwarnings("ignore")
APPLY = "--apply" in sys.argv
TOOL_ID, TOOL_NAME = "desktop_code", "Desktop and Code"
TARGETS = ["ornith-1.5-35b-a3b", "ornith-1.5-9b", "qwen3.6-35b-a3b-ud-q4kxl", "qwen3.6-35b-a3b-ud-q5kxl", "qwen3.6-35b-a3b-ud-q6kxl", "qwen3.8-27b", "qwen3.8-27b-turbo",
           "qwen3.8-flash-next-coder", "holo4-35b-a3b", "kat-coder-v2.5-dev", "gemma4-26b-a4b", "qwen3-30b-a3b-2507-q4km", "qwen3-coder-30b-a3b-q4km",
           "nemotron-3.5-lightning-30b-a3b", "gpt-oss-20b-mxfp4", "qwen3-14b", "qwen3-8b", "qwen3.5-9b", "bonsai-27b"]

db = Path.home() / ".open-webui" / "webui.db"
key = (Path.home() / ".webui_secret_key").read_text().strip()
uid = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
    "select id from user where role='admin' order by created_at limit 1").fetchone()[0]
tok = jwt.encode({"id": uid, "exp": int(time.time()) + 600}, key, algorithm="HS256")


def call(path, body=None, method=None):
    req = urllib.request.Request("http://127.0.0.1:8080" + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
                                 method=method or ("POST" if body is not None else "GET"))
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 404):
            return None
        raise


if APPLY:
    bak = db.with_name("webui.db.bak-desktoptool-" + time.strftime("%Y%m%d-%H%M%S"))
    with sqlite3.connect(db) as live, sqlite3.connect(bak) as b:
        live.backup(b)
    print("backup:", bak.name)

# 1. the tool
src = Path(sys.argv[1]).read_text()
front = re.match(r'\s*"""(.*?)"""', src, re.S).group(1)
manifest = {k.strip(): v.strip() for k, v in (l.split(":", 1) for l in front.strip().splitlines() if ":" in l)}
form = {"id": TOOL_ID, "name": TOOL_NAME, "content": src,
        "meta": {"description": manifest.get("description", ""), "manifest": manifest}}
existing = call(f"/api/v1/tools/id/{TOOL_ID}")
print(("update" if existing else "create"), "tool", TOOL_ID, "version", manifest.get("version"))
if APPLY:
    r = call(f"/api/v1/tools/id/{TOOL_ID}/update" if existing else "/api/v1/tools/create", form)
    print("  ->", (r or {}).get("id"))
    # --valve-stdin=KEY: one tool valve from stdin (a secret: never printed), merged into the saved valves
    key = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--valve-stdin=")), None)
    if key:
        value = sys.stdin.readline().strip()
        valves = call(f"/api/v1/tools/id/{TOOL_ID}/valves") or {}
        valves[key] = value
        call(f"/api/v1/tools/id/{TOOL_ID}/valves/update", valves)
        print("  -> tool valve set:", key)

models = {m["id"]: m for m in call("/api/models")["data"]}


def upsert(mid, change):
    """Merge `change(meta, params)` into the model's override (created if the model has none)."""
    cur = call(f"/api/v1/models/model?id={mid}")
    meta = dict((cur or {}).get("meta") or {})
    params = dict((cur or {}).get("params") or {})
    change(meta, params)
    form = {"id": mid, "base_model_id": (cur or {}).get("base_model_id"), "name": (cur or {}).get("name") or models[mid]["name"],
            "meta": meta, "params": params, "is_active": (cur or {}).get("is_active", True)}
    if APPLY:
        call("/api/v1/models/model/update" if cur else "/api/v1/models/create", form)
    return "update" if cur else "create"


# 2. hide the Computer Agent entries
for mid in sorted(m for m in models if m.startswith("computer_agent.")):
    print(upsert(mid, lambda meta, params: meta.update(hidden=True)), "hidden ", mid)

# 3. the tool on the main chat models, native function calling
def give(meta, params):
    meta["toolIds"] = sorted(set(meta.get("toolIds") or []) | {TOOL_ID})
    params["function_calling"] = "native"

for short in TARGETS:
    mid = "slerm-turing." + short
    if mid not in models:
        print("skip (not listed)", mid)
        continue
    print(upsert(mid, give), "tool+native", mid)

if APPLY:
    after = {m["id"]: m for m in call("/api/models")["data"]}
    hidden = [m for m in after if m.startswith("computer_agent.") and (after[m].get("info") or {}).get("meta", {}).get("hidden")]
    tooled = [m for m in after if TOOL_ID in ((after[m].get("info") or {}).get("meta", {}).get("toolIds") or [])]
    print(f"check: {len(hidden)} Computer Agent entries hidden, {len(tooled)} models have the tool")
