"""Model choice (pipe 0.9.3): fresh model lookup, ask before switching, titles never load a model.

Also the pattern for testing the pipe here: a throwaway HTTP server stands in for the services it calls, and
Pipe() runs with its valves pointed at it. Run with `sh check.sh` (or `feature test`).
"""
import asyncio, json, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "owui"))
import code_mode
from code_mode import Pipe

FULL = [{"providerID": "slerm", "id": "ornith-1.5-35b-a3b"}, {"providerID": "slerm", "id": "new-model"}]
OLD = {("slerm", "ornith-1.5-35b-a3b")}
state = {"answers": [], "hits": []}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        state["hits"].append(self.path)
        if self.path == "/api/model":
            data = state["answers"].pop(0) if len(state["answers"]) > 1 else state["answers"][0]
            body = json.dumps({"data": data}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers()
    do_POST = do_GET

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d" % srv.server_address[1]

real_sleep = asyncio.sleep
async def fast_sleep(s): await real_sleep(0.01)

ok = True
def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (" | " + extra if extra else ""))

async def picked(p, mid):
    import httpx
    async with httpx.AsyncClient(base_url=URL) as c:
        return await p._picked_model(c, mid)

async def run(p, mid, call):
    out, st = [], []
    async def emit(e): st.append(e["data"].get("description"))
    async for chunk in p._run([{"role": "user", "content": "hi"}], "t-chat", {"id": "ornith-1.5-35b-a3b", "providerID": "slerm"},
                              emit, call, picked=mid):
        out.append(chunk)
    return "".join(out), st

async def main():
    # 1. the 10-11 case: list from before the restart (stale), first answer empty, then the full list
    p = Pipe(); p.valves.OPENCODE_URL = URL
    p._known, p._known_at = set(OLD), time.monotonic() - 10_000
    state["answers"] = [[], [], FULL]; state["hits"].clear()
    r = await picked(p, "slerm-turing.new-model")
    check("stale list + empty first answers -> new model accepted", r == {"id": "new-model", "providerID": "slerm"}, f"asked {len(state['hits'])}x")
    # 2. a recent list that predates the model: looked up afresh instead of turned down
    p = Pipe(); p._known, p._known_at = set(OLD), time.monotonic()
    state["answers"] = [FULL]; state["hits"].clear()
    r = await picked(p, "slerm-turing.new-model")
    check("recent list without the model -> fresh lookup -> accepted", r and len(state["hits"]) == 1)
    # 3. known model with a recent list: no request at all
    state["hits"].clear()
    r = await picked(p, "slerm-turing.ornith-1.5-35b-a3b")
    check("known model, recent list -> no lookup", r and not state["hits"])
    # 4. model really missing from a fresh list
    state["answers"] = [FULL]; state["hits"].clear()
    r = await picked(p, "slerm-turing.not-there")
    check("missing from a fresh list -> turned down", r is None and len(state["hits"]) == 1)
    # 5. opencode lists nothing at all: let opencode decide
    p = Pipe(); state["answers"] = [[]]; state["hits"].clear()
    r = await picked(p, "slerm-turing.whatever")
    check("opencode lists nothing -> left to opencode", r == {"id": "whatever", "providerID": "slerm"}, f"asked {len(state['hits'])}x")
    # 5b. partial list right after a restart (another provider's models first), then the full one
    p = Pipe(); p._known, p._known_at = set(OLD), time.monotonic() - 10_000
    state["answers"] = [[], [{"providerID": "opencode", "id": "x"}], FULL + [{"providerID": "opencode", "id": "x"}]]; state["hits"].clear()
    r = await picked(p, "slerm-turing.new-model")
    check("empty, then partial, then full list -> accepted", r == {"id": "new-model", "providerID": "slerm"}, f"asked {len(state['hits'])}x")
    # 5c. the provider never shows up
    p = Pipe(); state["answers"] = [[{"providerID": "opencode", "id": "x"}]]; state["hits"].clear()
    r = await picked(p, "slerm-turing.new-model")
    check("provider never listed -> turned down after 4 lookups", r is None and len(state["hits"]) == 4)
    # 6. other prefix (not a slerm model)
    r = await picked(p, "openai.gpt-x")
    check("model outside MODEL_PREFIXES -> turned down", r is None)

    # _run: never switches on its own
    state["answers"] = [FULL]
    p = Pipe(); p.valves.OPENCODE_URL = URL
    state["hits"].clear()
    out, st = await run(p, "slerm-turing.not-there", None)
    check("no browser to ask -> not started, no session touched", out.startswith("**Not started.**") and not any(h.startswith("/api/session") for h in state["hits"]), out[:70])
    asked = []
    async def no(e): asked.append(e); return False
    state["hits"].clear()
    out, st = await run(p, "slerm-turing.not-there", no)
    check("asked, answer no -> not started", out.startswith("**Not started.**") and len(asked) == 1 and asked[0]["type"] == "confirmation" and not any(h.startswith("/api/session") for h in state["hits"]))
    print("     dialog:", asked[0]["data"]["title"], "|", asked[0]["data"]["message"].replace("\n", " "))
    async def boom(e): raise TimeoutError("no reply")
    out, st = await run(p, "slerm-turing.not-there", boom)
    check("dialog times out -> not started", out.startswith("**Not started.**"))
    async def yes(e): return True
    state["hits"].clear()
    out, st = await run(p, "slerm-turing.not-there", yes)
    check("asked, answer yes -> goes on to open the session with the other model", any(h.startswith("/api/session") for h in state["hits"]), out[:80])
    state["hits"].clear(); asked.clear()
    out, st = await run(p, "slerm-turing.new-model", no)
    check("available model -> no question, session opened", not asked and any(h.startswith("/api/session") for h in state["hits"]))
    out, st = await run(p, "code_mode.ornith-1.5-35b-a3b", no)
    check("the Code pipe's own entry as the pick -> asks; no -> not started", len(asked) == 1 and out.startswith("**Not started.**"), out[:70])
    asked.clear(); state["hits"].clear()
    out, st = await run(p, "", no)
    check("no model with the turn -> asks; no -> not started, no session touched",
          len(asked) == 1 and asked[0]["data"]["title"] == "No model was picked" and out.startswith("**Not started.**")
          and not any(h.startswith("/api/session") for h in state["hits"]), out[:70])
    print("     dialog:", asked[0]["data"]["title"], "|", asked[0]["data"]["message"].replace("\n", " "))
    state["hits"].clear()
    out, st = await run(p, "", yes)
    check("no model with the turn, answer yes -> says so and opens the session",
          "No model was picked; you chose ornith-1.5-35b-a3b" in out and any(h.startswith("/api/session") for h in state["hits"]), out[:80])
    # titles
    p = Pipe()
    check("title fallback is the CPU task model", p.valves.TASK_MODEL == "slerm-turing.tasks-llama3.2-1b" and "ornith" not in p.valves.TASK_MODEL)

def test_model_choice():
    asyncio.sleep = fast_sleep  # the pipe waits between model-list retries; restored below
    try:
        asyncio.run(main())
    finally:
        asyncio.sleep = real_sleep
    assert ok, "see the FAIL lines above"
