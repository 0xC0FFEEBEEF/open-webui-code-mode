"""Type a message into an Open WebUI chat from outside, in the user's own browser (run on cryoai).

The page script open on that chat types it into the message box, so the user watches it land, and sends it
(code-deploy /api/compose, 2026-10-09). The admin token is signed in-process and never printed.

    python3 compose.py CHAT_ID [--no-send] [--mode code|chat] [--wait SECONDS] < message.txt
    python3 compose.py --status ITEM_ID
    python3 compose.py --cancel ITEM_ID      # only while no page has claimed it

Prints the message id and each status change: queued -> claimed -> sent | typed | failed | skipped | expired.
"""
import argparse, base64, hashlib, hmac, json, sqlite3, sys, time, urllib.request
from pathlib import Path

DEPLOY = "http://127.0.0.1:8095"


def token():
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    key = (Path.home() / ".webui_secret_key").read_text().strip().encode()
    uid = sqlite3.connect(f"file:{Path.home()}/.open-webui/webui.db?mode=ro", uri=True).execute(
        "select id from user where role='admin' order by created_at limit 1").fetchone()[0]
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "." + \
        b64(json.dumps({"id": uid, "exp": int(time.time()) + 3600}).encode())
    return head + "." + b64(hmac.new(key, head.encode(), hashlib.sha256).digest())


def call(tok, method, path, body=None):
    req = urllib.request.Request(DEPLOY + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit("code-deploy %s: %s" % (e.code, e.read().decode()[:300]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("chat", nargs="?")
    ap.add_argument("--no-send", action="store_true", help="type it but leave it in the box")
    ap.add_argument("--mode", choices=("code", "chat"))
    ap.add_argument("--wait", type=int, default=1200, help="seconds to follow the status (0 = just queue it)")
    ap.add_argument("--status")
    ap.add_argument("--cancel")
    a = ap.parse_args()
    tok = token()
    if a.status or a.cancel:
        path = "/api/compose/" + (a.status or a.cancel)
        it = call(tok, "POST", path, {"status": "cancelled"}) if a.cancel else call(tok, "GET", path)
        print(json.dumps({k: v for k, v in it["item"].items() if k != "text"}))
        return
    text = sys.stdin.read().strip()
    if not a.chat or not text:
        sys.exit("usage: compose.py CHAT_ID < message.txt")
    it = call(tok, "POST", "/api/compose", {"chat_id": a.chat, "text": text, "send": not a.no_send, "mode": a.mode})["item"]
    print("queued", it["id"], "(%d chars)" % len(text), flush=True)
    last, end = "queued", time.time() + a.wait
    while time.time() < end:
        time.sleep(2)
        it = call(tok, "GET", "/api/compose/" + it["id"])["item"]
        if it["status"] != last:
            last = it["status"]
            print(time.strftime("%H:%M:%S"), last, it.get("note") or "", flush=True)
        if last not in ("queued", "claimed"):
            return
    print("still", last, "after", a.wait, "s")


if __name__ == "__main__":
    main()
