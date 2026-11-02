"""Hand a job to Code mode's opencode and follow it to the end (run on cryoai; password on stdin).

Logs one line per step. Permission requests are refused WITH a reason (so the run continues instead of
stalling for an absent human); questions get REPLY. Ends when the run ends.
Usage: <password> | python run_brief.py BASE DIRECTORY "PROMPT" [--session SID] [--model provider/id]
       <password> | python run_brief.py BASE - - --session SID --watch   (re-attach to a running session)
"""
import argparse, asyncio, json, sys, time
import httpx

REFUSAL = ("Not allowed in this unattended run. Do not delete or rewrite history; find another way "
           "(e.g. leave scratch files, use a new directory) and carry on.")
REPLY = "Use your best judgement, follow BRIEF.md, and carry on."
STALL = 240  # seconds without any event before the stream is re-opened


async def main(a):
    auth = ("opencode", sys.stdin.readline().strip())
    t0 = time.time()

    def log(*parts):
        print(f"{time.time() - t0:7.1f}s", *parts, flush=True)

    async with httpx.AsyncClient(base_url=a.base, auth=auth, timeout=httpx.Timeout(30, read=None)) as c:
        sid = a.session
        if not sid:
            body = {"location": {"directory": a.directory}}
            if a.model:
                p, _, m = a.model.partition("/")
                body["model"] = {"id": m, "providerID": p}
            r = await c.post("/api/session", json=body)
            r.raise_for_status()
            sid = r.json()["data"]["id"]
        log("session", sid)
        tools, steps, text = {}, 0, ""
        if a.watch:
            active = (await c.get("/api/session/active")).json().get("data") or []
            if sid not in json.dumps(active):
                log("session is not running; nothing to watch")
                return
        async def answer_pending():  # anything asked while nobody was listening
            for pr in (await c.get(f"/api/session/{sid}/permission")).json().get("data") or []:
                log("PERMISSION (pending)", pr.get("action"), pr.get("resources"), "-> refused with reason")
                await c.post(f"/api/session/{sid}/permission/{pr['id']}/reply", json={"decision": "reject", "message": REFUSAL})
            for f in (await c.get(f"/api/session/{sid}/form")).json().get("data") or []:
                log("QUESTION (pending)", json.dumps(f.get("fields"))[:300])
                await c.post(f"/api/session/{sid}/form/{f['id']}/reply",
                             json={"answer": {x["key"]: REPLY for x in f.get("fields") or []}})

        async def running():
            return sid in json.dumps((await c.get("/api/session/active")).json().get("data") or [])

        # The event stream can stall silently (seen 2026-10-06: 30 min of nothing while the run went on), so a
        # quiet stream is dropped after STALL seconds and re-opened while the session is still running.
        first, ended = not a.watch, False
        while not ended:
            try:
                async with c.stream("GET", "/api/event", timeout=httpx.Timeout(30, read=STALL)) as ev:
                    if first:
                        (await c.post(f"/api/session/{sid}/prompt", json={"text": a.prompt})).raise_for_status()
                        first = False
                    else:
                        await answer_pending()
                    async for line in ev.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            e = json.loads(line[5:])
                        except ValueError:
                            continue
                        d, t = e.get("data") or {}, e.get("type", "")
                        if (d.get("sessionID") or (d.get("form") or {}).get("sessionID")) != sid:
                            continue
                        if t == "session.tool.input.started":
                            tools[d.get("id")] = d.get("name")
                        elif t == "session.tool.called":
                            log("TOOL", tools.get(d.get("id"), "?"), json.dumps(d.get("input"), ensure_ascii=False)[:220])
                        elif t == "session.tool.failed":
                            log("  FAILED", json.dumps(d.get("error"))[:200])
                        elif t == "session.tool.success":
                            out = "\n".join(p.get("text", "") for p in d.get("content") or [] if isinstance(p, dict))
                            log("  ok", out.replace("\n", " | ")[:200])
                        elif t == "session.text.ended":
                            text = d.get("text", "")
                            log("SAYS", text.replace("\n", " ")[:300])
                        elif t == "session.step.ended":
                            steps += 1
                            tok = d.get("tokens") or {}
                            log(f"step {steps} ({tok.get('input', 0)} in / {tok.get('output', 0)} out)")
                        elif t == "session.step.failed":
                            log("STEP FAILED", json.dumps(d.get("error"))[:200])
                        elif t == "permission.asked":
                            log("PERMISSION", d.get("action"), d.get("resources"), "-> refused with reason")
                            await c.post(f"/api/session/{sid}/permission/{d['id']}/reply",
                                         json={"decision": "reject", "message": REFUSAL})
                        elif t == "form.created":
                            f = d.get("form") or {}
                            log("QUESTION", json.dumps(f.get("fields"))[:300])
                            await c.post(f"/api/session/{sid}/form/{f['id']}/reply",
                                         json={"answer": {x["key"]: REPLY for x in f.get("fields") or []}})
                        elif "compaction" in t and not t.endswith(".delta"):  # start/end only; deltas stream by the hundred
                            log("COMPACTION", t)
                        elif t.startswith("session.execution.") and t != "session.execution.started":
                            log("END", t, json.dumps(d)[:200])
                            ended = True
                            break
            except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
                if first:
                    raise
                if not await running():
                    log("END session is no longer running (seen after the event stream dropped)")
                    break
                log(f"(event stream quiet/dropped: {type(exc).__name__}; re-attached)")
        print("\n===== FINAL MESSAGE =====\n" + text)


ap = argparse.ArgumentParser()
ap.add_argument("base"); ap.add_argument("directory"); ap.add_argument("prompt")
ap.add_argument("--session"); ap.add_argument("--model"); ap.add_argument("--watch", action="store_true")
asyncio.run(main(ap.parse_args()))
