"""Scripted OpenAI-compatible server for recording opencode's event stream.

Turn 1: call the write tool (hello.py). Turn 2: call the shell tool with `rm -rf build`
(permission "ask" in the throwaway config). Turn 3: stream reasoning + a text answer.
Requests without tools (title/summary jobs) get a short plain answer.
"""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = sys.argv[2]


def log(obj):
    with open(LOG, "a") as f:
        f.write(json.dumps(obj) + "\n")


def chunk(delta, finish=None):
    return {"id": "mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock-coder",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def args_for(tool, wanted):
    props = (tool.get("function", {}).get("parameters") or {}).get("properties", {})
    out = {}
    for k in props:
        lk = k.lower()
        for key, val in wanted.items():
            if key in lk:
                out[k] = val
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "mock-coder", "object": "model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tools = {t["function"]["name"]: t for t in req.get("tools") or []}
        msgs = req.get("messages", [])
        n_tool = sum(1 for m in msgs if m.get("role") == "tool")
        log({"t": time.time(), "tools": {k: v["function"].get("parameters") for k, v in tools.items()},
             "n_msgs": len(msgs), "n_tool": n_tool, "roles": [m.get("role") for m in msgs],
             "last": msgs[-1] if msgs else None})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()

        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode()); self.wfile.flush(); time.sleep(0.05)

        if not tools:
            for w in ["Mock ", "session ", "title"]:
                send(chunk({"content": w}))
            send(chunk({}, "stop"))
        elif n_tool == 0 and "ASK" in json.dumps(msgs[-1]):
            q = {"questions": [{"question": "Which framework should I use?", "header": "Framework",
                                "options": [{"label": "Flask", "description": "small"}, {"label": "FastAPI", "description": "async"}]}]}
            send(chunk({"tool_calls": [{"index": 0, "id": "call_q", "type": "function",
                                        "function": {"name": "question", "arguments": json.dumps(q)}}]}))
            send(chunk({}, "tool_calls"))
        elif n_tool == 0:
            name = "write" if "write" in tools else sorted(tools)[0]
            a = args_for(tools[name], {"path": "hello.py", "content": "print('hello from mock')\n"})
            send(chunk({"role": "assistant", "content": "Creating the file."}))
            send(chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(a)}}]}))
            send(chunk({}, "tool_calls"))
        elif n_tool == 1 and "ASK" not in json.dumps(msgs):
            name = "shell" if "shell" in tools else ("bash" if "bash" in tools else sorted(tools)[0])
            a = args_for(tools[name], {"command": "rm -rf build", "description": "remove build dir"})
            send(chunk({"tool_calls": [{"index": 0, "id": "call_2", "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(a)}}]}))
            send(chunk({}, "tool_calls"))
        else:
            for w in ["Thinking ", "about ", "the ", "result."]:
                send(chunk({"reasoning_content": w}))
            for w in ["Done: ", "wrote ", "`hello.py`", " and tried a cleanup."]:
                send(chunk({"content": w}))
            send({**chunk({}, "stop"), "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
