"""Print a 10-minute Open WebUI admin token to stdout, for piping into a tool (run on cryoai; never echo it)."""
import base64, hashlib, hmac, json, sqlite3, time
from pathlib import Path
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
key = (Path.home() / ".webui_secret_key").read_text().strip().encode()
uid = sqlite3.connect(f"file:{Path.home()}/.open-webui/webui.db?mode=ro", uri=True).execute(
    "select id from user where role='admin' order by created_at limit 1").fetchone()[0]
head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "." + b64(json.dumps({"id": uid, "exp": int(time.time()) + 600}).encode())
print(head + "." + b64(hmac.new(key, head.encode(), hashlib.sha256).digest()))
