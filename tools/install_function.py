"""Install or update the Code Mode pipe in Open WebUI's database (run on cryoai, service may stay up).

Takes an online backup first (sqlite3 backup API, safe while Open WebUI runs), then upserts the
`function` row. Existing valves are kept; OPENCODE_PASSWORD is set from stdin when given (never printed).
Usage: [password |] python3 install_function.py /path/to/code_mode.py [--no-password] [--valve-stdin=KEY] [--copy-valve=FUNCTION:KEY]
"""
import json, re, sqlite3, sys, time
from pathlib import Path

DB = Path.home() / ".open-webui" / "webui.db"
FID, NAME = "code_mode", "Code Mode"

src = Path(sys.argv[1]).read_text()
stdin_key = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--valve-stdin=")), "OPENCODE_PASSWORD")
stdin_value = "" if "--no-password" in sys.argv else sys.stdin.readline().strip()
front = re.match(r'\s*"""(.*?)"""', src, re.S).group(1)
manifest = {k.strip(): v.strip() for k, v in (line.split(":", 1) for line in front.strip().splitlines() if ":" in line)}

stamp = time.strftime("%Y%m%d-%H%M%S")
backup = DB.with_name(f"webui.db.bak-codemode-{stamp}")
with sqlite3.connect(DB) as live, sqlite3.connect(backup) as bak:
    live.backup(bak)
print("backup:", backup.name)

c = sqlite3.connect(DB)
owner = c.execute("select user_id from function where id='computer_agent'").fetchone()
owner = owner[0] if owner else c.execute("select id from user where role='admin' order by created_at limit 1").fetchone()[0]
meta = json.dumps({"description": manifest.get("description", ""), "manifest": manifest})
now = int(time.time())
row = c.execute("select valves from function where id=?", (FID,)).fetchone()
valves = json.loads(row[0] or "{}") if row else {}
if stdin_value:  # OPENCODE_PASSWORD by default, or the key named by --valve-stdin=KEY (never printed)
    valves[stdin_key] = stdin_value
for arg in sys.argv:  # --copy-valve=computer_agent:VIEW_TOKEN copies a valve from another function
    if arg.startswith("--copy-valve="):
        src_fid, key = arg.split("=", 1)[1].split(":", 1)
        other = c.execute("select valves from function where id=?", (src_fid,)).fetchone()
        val = json.loads(other[0] or "{}").get(key) if other else None
        if val:
            valves[key] = val
            print("copied valve", key, "from", src_fid)
if row:
    c.execute("update function set content=?, meta=?, valves=?, updated_at=? where id=?",
              (src, meta, json.dumps(valves), now, FID))
    print("updated", FID, "version", manifest.get("version"))
else:
    c.execute("insert into function (id, user_id, name, type, content, meta, created_at, updated_at, valves, is_active, is_global) "
              "values (?, ?, ?, 'pipe', ?, ?, ?, ?, ?, 1, 0)", (FID, owner, NAME, src, meta, now, now, json.dumps(valves)))
    print("inserted", FID, "version", manifest.get("version"))
c.commit()
print("valves set:", sorted(valves))
