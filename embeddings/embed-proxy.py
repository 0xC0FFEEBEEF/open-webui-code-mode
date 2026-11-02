#!/usr/bin/env python3
"""Front door for Open WebUI's embeddings (llama-server behind it on cryoai).

It does two things llama-server does not:
- Cuts an input that is longer than the model's context instead of failing the
  whole request (LM Studio on CYCLOPS did the same). Open WebUI sends the whole
  chat message as the search query when its task model is down, so a long pasted
  log would otherwise lose the attached file's context.
- Keeps recent embeddings in memory, so re-uploading the same file is instant.

Only POST /v1/embeddings is handled; GET /health and /v1/models pass through, and
GET /stats shows request, cache-hit and truncation counts since start.
"""
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from array import array
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("EMBED_UPSTREAM", "http://127.0.0.1:8098")
HOST, PORT = os.environ.get("EMBED_LISTEN", "127.0.0.1:8099").rsplit(":", 1)
MAX_TOKENS = int(os.environ.get("EMBED_MAX_TOKENS", "2040"))  # model context 2048, minus BOS/EOS headroom
CACHE_ENTRIES = int(os.environ.get("EMBED_CACHE_ENTRIES", "20000"))  # ~3 KB each for 768 dims

_cache: "OrderedDict[bytes, array]" = OrderedDict()
_lock = threading.Lock()
stats = {"requests": 0, "inputs": 0, "cache_hits": 0, "truncated": 0}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def upstream(path, body=None, timeout=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(UPSTREAM + path, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fit(text):
    """Return text cut to MAX_TOKENS tokens. A token covers at least one UTF-8 byte,
    so anything with at most MAX_TOKENS bytes is already safe and skips the tokenizer."""
    if len(text.encode("utf-8")) <= MAX_TOKENS:
        return text
    tokens = upstream("/tokenize", {"content": text, "add_special": False}, timeout=60)["tokens"]
    if len(tokens) <= MAX_TOKENS:
        return text
    with _lock:
        stats["truncated"] += 1
    log(f"truncated an input from {len(tokens)} to {MAX_TOKENS} tokens")
    return upstream("/detokenize", {"tokens": tokens[:MAX_TOKENS]}, timeout=60)["content"]


def cache_get(key):
    with _lock:
        v = _cache.get(key)
        if v is not None:
            _cache.move_to_end(key)
        return v


def cache_put(key, vec):
    with _lock:
        _cache[key] = array("f", vec)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the journal quiet; errors are logged explicitly
        pass

    def reply(self, code, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/stats":
            with _lock:
                snapshot = {**stats, "cached": len(_cache)}
            return self.reply(200, snapshot)
        if self.path not in ("/health", "/v1/models", "/models"):
            return self.reply(404, {"error": {"message": "not found"}})
        try:
            with urllib.request.urlopen(UPSTREAM + self.path, timeout=10) as r:
                return self.reply(r.status, r.read())
        except urllib.error.HTTPError as e:
            return self.reply(e.code, e.read())
        except Exception as e:
            return self.reply(502, {"error": {"message": f"embedding server unreachable: {e}"}})

    def do_POST(self):
        if self.path not in ("/v1/embeddings", "/embeddings"):
            return self.reply(404, {"error": {"message": "not found"}})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            inputs = req.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            if not isinstance(inputs, list) or not all(isinstance(x, str) for x in inputs):
                return self.reply(400, {"error": {"message": "input must be a string or a list of strings"}})
            with _lock:
                stats["requests"] += 1
                stats["inputs"] += len(inputs)

            keys = [hashlib.sha256(t.encode("utf-8")).digest() for t in inputs]
            vecs = [cache_get(k) for k in keys]
            missing = [i for i, v in enumerate(vecs) if v is None]
            with _lock:
                stats["cache_hits"] += len(inputs) - len(missing)
            tokens = 0
            if missing:
                out = upstream("/v1/embeddings", {"input": [fit(inputs[i]) for i in missing], "model": "local"})
                got = sorted(out["data"], key=lambda e: e["index"])
                if len(got) != len(missing):
                    raise RuntimeError(f"upstream returned {len(got)} embeddings for {len(missing)} inputs")
                for i, e in zip(missing, got):
                    vecs[i] = e["embedding"]
                    cache_put(keys[i], e["embedding"])
                tokens = (out.get("usage") or {}).get("prompt_tokens", 0)

            return self.reply(200, {
                "object": "list",
                "model": req.get("model") or "local",
                "data": [{"object": "embedding", "index": i, "embedding": list(v)} for i, v in enumerate(vecs)],
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
            })
        except urllib.error.HTTPError as e:
            body = e.read()
            log(f"upstream error {e.code}: {body[:300]!r}")
            return self.reply(e.code, body)
        except Exception as e:
            log(f"error: {e!r}")
            return self.reply(502, {"error": {"message": f"embedding proxy error: {e}"}})


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, int(PORT)), Handler)
    srv.daemon_threads = True
    log(f"embed-proxy on {HOST}:{PORT} -> {UPSTREAM}, max {MAX_TOKENS} tokens, cache {CACHE_ENTRIES}")
    srv.serve_forever()
