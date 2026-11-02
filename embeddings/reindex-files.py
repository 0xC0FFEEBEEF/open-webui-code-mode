#!/usr/bin/env python3
"""Re-embed Open WebUI's existing uploads with the embedding model it is set to now.

Run on cryoai as owui after changing the embedding model, so old chats' files search properly
again. Uses Open WebUI's own API (POST /api/v1/files/<id>/data/content/update with the stored
text), which deletes the file's old vectors and embeds it afresh. One file at a time; files with
identical text run back to back so the embed proxy's cache makes the copies nearly free.

    python3 -I reindex-files.py [--dry-run] [--only FILE_ID] [--state FILE]

Progress goes to stdout; finished file ids go to the state file so a rerun skips them.
The admin token is minted in-process from ~/.webui_secret_key and never printed.
"""
import argparse, base64, hashlib, hmac, json, sqlite3, sys, time, urllib.error, urllib.request
from pathlib import Path

DATA = Path.home() / ".open-webui"
API = "http://127.0.0.1:8080/api/v1"
PROXY_STATS = "http://127.0.0.1:8099/stats"


def token():
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    key = (Path.home() / ".webui_secret_key").read_text().strip().encode()
    uid = sqlite3.connect(f"file:{DATA}/webui.db?mode=ro", uri=True).execute(
        "select id from user where role='admin' order by created_at limit 1").fetchone()[0]
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "." + b64(
        json.dumps({"id": uid, "exp": int(time.time()) + 600}).encode())
    return head + "." + b64(hmac.new(key, head.encode(), hashlib.sha256).digest())


def call(path, body, timeout):
    req = urllib.request.Request(API + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def current_model():
    req = urllib.request.Request(API + "/retrieval/embedding", headers={"Authorization": "Bearer " + token()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["RAG_EMBEDDING_MODEL"]


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="re-embed just this file id (for testing)")
    ap.add_argument("--state", default=str(Path.home() / "owui-embed-reindex" / "done.txt"))
    args = ap.parse_args()
    state = Path(args.state)
    state.parent.mkdir(parents=True, exist_ok=True)
    done = set(state.read_text().split()) if state.exists() else set()

    vdb = sqlite3.connect(f"file:{DATA}/vector_db/chroma.sqlite3?mode=ro", uri=True)
    have = {r[0] for r in vdb.execute("select name from collections")}
    db = sqlite3.connect(f"file:{DATA}/webui.db?mode=ro", uri=True)
    todo = []
    for fid, name, data in db.execute("select id, filename, data from file"):
        content = (json.loads(data or "{}") or {}).get("content") or ""
        if args.only and fid != args.only:
            continue
        if content and f"file-{fid}" in have and fid not in done:
            todo.append((hashlib.sha256(content.encode()).hexdigest(), len(content), fid, name))
    todo.sort()
    model = current_model()
    total = sum(t[1] for t in todo)
    say(f"model {model}: {len(todo)} files to re-embed, {total:,} chars ({len(done)} already done)")
    if args.dry_run:
        for h, n, fid, name in todo:
            print(f"  {h[:8]} {n:>10,}  {fid}  {name}")
        return

    failed = []
    for i, (h, n, fid, name) in enumerate(todo, 1):
        t = time.time()
        content = (json.loads(db.execute("select data from file where id = ?", (fid,)).fetchone()[0]) or {})["content"]
        try:
            call(f"/files/{fid}/data/content/update", {"content": content}, timeout=7200)
            # The endpoint logs and swallows errors, so check the vectors really are new.
            r = call("/retrieval/query/doc", {"collection_name": f"file-{fid}", "query": "summary", "k": 1}, timeout=300)
            meta = (r.get("metadatas") or [[{}]])[0]
            ok = bool(meta) and model in str(meta[0].get("embedding_config", ""))
        except Exception as e:
            ok, meta = False, [{"error": repr(e)[:200]}]
        if ok:
            with state.open("a") as f:
                f.write(fid + "\n")
        else:
            failed.append((fid, name))
        try:
            with urllib.request.urlopen(PROXY_STATS, timeout=5) as s:
                cache = json.load(s)
        except Exception:
            cache = {}
        say(f"[{i}/{len(todo)}] {'ok  ' if ok else 'FAIL'} {time.time() - t:6.1f}s {n:>10,} chars  {name}"
            f"  (proxy cache hits {cache.get('cache_hits', '?')})" + ("" if ok else f"  {meta[0] if meta else 'no vectors'}"))
        time.sleep(2)
    say(f"finished: {len(todo) - len(failed)} ok, {len(failed)} failed")
    for fid, name in failed:
        say(f"  failed: {fid} {name}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
