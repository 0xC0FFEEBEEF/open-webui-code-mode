"""A fake opencode for tests: the shell API the pipe uses to run commands on the sandbox VM, played by REALLY running
them with bash under a throwaway home folder. Point the pipe's OPENCODE_URL at `fake.url`.

    fake = FakeOpencode(tmp_dir).start()   # tmp_dir is $HOME for every command (and the default working folder)
    ... the pipe runs git, ssh-keygen, ... through it ...
    fake.commands                           # [{"command", "cwd", "status", "exit", "output"}, ...] in start order
    fake.stop()

How the real one behaves (measured on the VM, 2026-10-11; the fake does the same):
- Every call needs HTTP Basic auth (user "opencode"); without it the answer is 401.
- POST /api/shell with JSON {"command": str, "cwd": str (optional), "timeout": milliseconds (optional)} answers 200
  {"data": {"id": "sh_...", "status": "running", ...}} straight away. It does NOT wait for the command.
- A cwd that does not exist: HTTP 500 with an empty body. So make folders with a command that has no cwd.
  With no cwd the command runs in the home folder. HOME is set; PATH has git, ssh and ssh-keygen.
- GET /api/shell/{id} answers {"data": {"status": "running" | "exited" | "timeout" | "killed", "exit": int or null}}.
  Poll until the status is not "running". After a timeout the status is "timeout" and exit is null.
- GET /api/shell/{id}/output answers {"data": {"output": stdout and stderr together, "cursor", "size", "truncated"}}.
- DELETE /api/shell/{id} answers 204 with no body. Delete every command when done: its output is kept until then.
- An unknown id: 404.

Sessions and the event stream (added 2026-10-13 for the end-of-turn review; shapes as the pipe reads them):
- POST /api/session {"model", "location": {"directory"}, "metadata"} -> {"data": {"id", ...}}; GET /api/session
  ?directory= lists that folder's sessions; GET / PATCH {"metadata"} / DELETE /api/session/{id}.
- POST /api/session/{id}/agent {"agent"}: 204, or 404 for an agent that is not in `fake.agents`.
- POST /api/session/{id}/shell {"command"} really runs it in the session's folder, then the stream says
  session.shell.ended.
- POST /api/session/{id}/prompt {"text", optional "files"} answers {"data": {"id": "msg_N"}} at once; the bodies'
  files land in session["prompt_files"] (what a test asserts an attachment against); what the "model" then does comes
  from `fake.script(session, text)`: a list of steps, played on the event stream, ending in
  session.execution.succeeded. Steps: ("text", words), ("tool", name, input, result[, seconds it runs]),
  ("event", type, data), ("sleep", seconds), ("usage", tokens), ("do", callable), ("fail", message). No script = it
  just succeeds.
- GET /api/event is the server-sent event stream (first "server.connected"); every event carries its sessionID.
- GET /api/session/{id}/diff answers {"data": fake.diff}.
- Approvals and questions (added 2026-10-26 for the page's question box): the step ("ask", {"action", "resources"})
  or ("form", {"fields": [...]}) puts a request in fake.permissions / fake.forms, says permission.asked /
  form.created, and the run waits there until it is answered: POST .../permission/{id}/reply or .../form/{id}/reply
  (recorded in fake.replies), or DELETE .../form/{id}. GET /api/session/{id}/permission and /form list what waits;
  GET /api/session/active lists the sessions whose run is going. fake.withdraw(id) drops a request the way an answer
  from somewhere else does (the run goes on). POST .../interrupt answers {"interrupted": bool} like opencode 2.0.18
  (false when nothing runs) and, when something ran, ends the run with session.execution.interrupted.
    fake.sessions   # live sessions by id: {"id", "directory", "metadata", "agent", "prompts": [...],
                    # "prompt_files": [the files list of each prompt, [] when it sent none]}
    fake.removed    # sessions the pipe deleted, in order (same dicts)
    fake.models     # what GET /api/model answers; give an entry capabilities {"input": [...]} to gate on vision
"""
import json
import os
import queue
import subprocess
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpencode:
    def __init__(self, home):
        self.home = str(home)
        os.makedirs(self.home, exist_ok=True)
        self.commands = []   # every command ever started (kept after DELETE, for the test to read)
        self.deleted = []    # ids the pipe deleted
        self._jobs = {}
        self._srv = None
        self.url = ""
        self.sessions = {}        # live sessions by id
        self.removed = []         # sessions the pipe deleted
        self.agents = {"build"}   # agents a session may be switched to
        self.models = [{"id": "m", "providerID": "slerm"}]   # GET /api/model answers this
        self.script = None        # script(session, text) -> steps; see the module docstring
        self.diff = []            # what a session's /diff answers
        self.compactions = []
        self.compaction_status = 200
        self.prompt_status = 200  # anything else: POST .../prompt fails with it (opencode refusing the prompt)
        self._subs = []           # one queue per open /api/event stream
        self._stop = False
        self.permissions = {}     # per_... -> {"id", "sessionID", "action", "resources", "event": threading.Event}
        self.forms = {}           # frm_... -> {"id", "sessionID", "fields", "event"}
        self.replies = []         # (kind, id, body) in the order they came
        self.running = set()      # sessions whose run is going (GET /api/session/active)
        self.interrupted = set()  # runs told to stop: _play ends at its next step

    def withdraw(self, rid):
        req = self.permissions.pop(rid, None) or self.forms.pop(rid, None)
        if req:
            req["event"].set()

    def emit(self, sid, kind, **data):
        for q in list(self._subs):
            q.put({"type": kind, "data": dict(data, sessionID=sid)})

    def _play(self, session, text):
        sid = session["id"]
        self.running.add(sid)
        self.interrupted.discard(sid)
        try:
            self._play_steps(session, text)
        finally:
            self.running.discard(sid)

    def _wait_answer(self, sid, req):
        while not req["event"].wait(0.05):
            if sid in self.interrupted or self._stop:
                return False
        return sid not in self.interrupted

    def _play_steps(self, session, text):
        sid = session["id"]
        for n, step in enumerate(self.script(session, text) if self.script else []):
            if sid in self.interrupted:
                return
            if step[0] == "ask":  # ("ask", {"action", "resources"}): a permission request; waits for its reply
                rid = "per_%s%d" % (uuid.uuid4().hex[:10], n)
                req = dict(step[1], id=rid, sessionID=sid, event=threading.Event())
                self.permissions[rid] = req
                self.emit(sid, "permission.asked", id=rid, action=req.get("action"), resources=req.get("resources"))
                if not self._wait_answer(sid, req):
                    return
                continue
            if step[0] == "form":  # ("form", {"fields": [...]}): the question tool; waits for its reply
                rid = "frm_%s%d" % (uuid.uuid4().hex[:10], n)
                req = dict(step[1], id=rid, sessionID=sid, event=threading.Event())
                self.forms[rid] = req
                self.emit(sid, "form.created", form={"id": rid, "fields": req.get("fields") or []})
                if not self._wait_answer(sid, req):
                    return
                continue
            if step[0] == "text":
                self.emit(sid, "session.text.delta", delta=step[1])
                self.emit(sid, "session.text.ended", text=step[1])
            elif step[0] == "tool":  # ("tool", name, input, result[, seconds it runs])
                call = "call_%s_%d" % (len(session["prompts"]), n)
                self.emit(sid, "session.tool.input.started", id=call, name=step[1])
                self.emit(sid, "session.tool.called", id=call, input=step[2])
                if len(step) > 4:
                    time.sleep(step[4])
                self.emit(sid, "session.tool.success", id=call, content=[{"type": "text", "text": step[3]}])
            elif step[0] == "event":  # ("event", type, data): any other event, as opencode sends it
                self.emit(sid, step[1], **(step[2] if len(step) > 2 else {}))
            elif step[0] == "sleep":  # ("sleep", seconds): the model says nothing for a while
                time.sleep(step[1])
            elif step[0] == "do":
                step[1]()
            elif step[0] == "usage":
                self.emit(sid, "session.step.ended", tokens=step[1])
            elif step[0] == "fail":
                return self.emit(sid, "session.execution.failed", error={"message": step[1]})
        if sid not in self.interrupted:
            self.emit(sid, "session.execution.succeeded")

    def _session_shell(self, session, command):
        env = {"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(["bash", "-c", command], cwd=session["directory"], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        self.emit(session["id"], "session.shell.ended")

    def _run(self, job, timeout):
        env = {"HOME": self.home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        try:
            p = subprocess.run(["bash", "-c", job["command"]], cwd=job["cwd"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            job.update(output=p.stdout.decode(errors="replace"), exit=p.returncode, status="exited")
        except subprocess.TimeoutExpired as e:
            job.update(output=(e.stdout or b"").decode(errors="replace"), exit=None, status="timeout")

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                body = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(code)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _job(self):
                parts = self.path.split("?")[0].strip("/").split("/")  # api shell <id> [output]
                return fake._jobs.get(parts[2]) if len(parts) >= 3 and parts[:2] == ["api", "shell"] else None

            def _authed(self):
                if (self.headers.get("Authorization") or "").startswith("Basic "):
                    return True
                self._send(401, {"_tag": "UnauthorizedError", "message": "Unauthorized"})
                return False

            @staticmethod
            def _view(job):
                return {"id": job["id"], "status": job["status"], "command": job["command"], "cwd": job["cwd"],
                        "shell": "/bin/bash", "exit": job["exit"], "metadata": {}}

            def _session(self):
                """(session or None, the rest of the path) for /api/session/<id>[/<what>...]."""
                parts = self.path.split("?")[0].strip("/").split("/")
                if parts[:2] != ["api", "session"] or len(parts) < 3:
                    return None, []
                return fake.sessions.get(parts[2]), parts[3:]

            def _session_post(self, body):
                parts = self.path.split("?")[0].strip("/").split("/")
                if len(parts) == 2:  # create
                    folder = (body.get("location") or {}).get("directory") or fake.home
                    s = {"id": "ses_" + uuid.uuid4().hex[:16], "directory": folder, "prompts": [], "prompt_files": [],
                         "agent": "build", "metadata": dict(body.get("metadata") or {}), "model": body.get("model") or {}}
                    fake.sessions[s["id"]] = s
                    return self._send(200, {"data": s})
                s, rest = self._session()
                if not s:
                    return self._send(404, {"message": "Session not found"})
                what = rest[0] if rest else ""
                if what == "agent":
                    if body.get("agent") not in fake.agents:
                        return self._send(404, {"message": "Agent not found"})
                    s["agent"] = body["agent"]
                    return self._send(204)
                if what == "model":
                    s["model"] = body.get("model") or {}
                    return self._send(204)
                if what == "compact":
                    fake.compactions.append((s["id"], body))
                    return self._send(fake.compaction_status, {"data": {"delivery": body.get("delivery")}})
                if what == "shell":
                    threading.Thread(target=fake._session_shell, args=(s, str(body.get("command") or "")), daemon=True).start()
                    return self._send(200, {"data": {}})
                if what == "prompt":
                    if fake.prompt_status != 200:
                        return self._send(fake.prompt_status, {"message": "prompt refused"})
                    s["prompts"].append(str(body.get("text") or ""))
                    s["prompt_files"].append(list(body.get("files") or []))
                    threading.Thread(target=fake._play, args=(s, s["prompts"][-1]), daemon=True).start()
                    return self._send(200, {"data": {"id": "msg_%d" % len(s["prompts"])}})
                if what == "interrupt":
                    hit = s["id"] in fake.running and s["id"] not in fake.interrupted
                    if hit:
                        fake.interrupted.add(s["id"])
                        fake.emit(s["id"], "session.execution.interrupted")
                    return self._send(200, {"interrupted": hit})
                if what in ("permission", "form") and len(rest) >= 3 and rest[2] == "reply":
                    store = fake.permissions if what == "permission" else fake.forms
                    req = store.pop(rest[1], None)
                    if not req:
                        return self._send(404, {"message": "Not found"})
                    fake.replies.append((what, rest[1], body))
                    req["event"].set()
                return self._send(204)  # permission and form replies

            def _events(self):
                q = queue.Queue()
                fake._subs.append(q)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(b'data: {"type": "server.connected"}\n\n')
                    self.wfile.flush()
                    while not fake._stop:
                        try:
                            e = q.get(timeout=0.1)
                        except queue.Empty:
                            continue
                        self.wfile.write(("data: " + json.dumps(e) + "\n\n").encode())
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    fake._subs.remove(q)

            def do_PATCH(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode() or "{}")
                if not self._authed():
                    return
                s, _ = self._session()
                if not s:
                    return self._send(404, {"message": "Session not found"})
                s["metadata"] = dict(body.get("metadata") or s["metadata"])
                self._send(200, {"data": s})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if not self._authed():
                    return
                if self.path.split("?")[0].startswith("/api/session"):
                    return self._session_post(json.loads(raw or "{}"))
                if self.path.split("?")[0] != "/api/shell":
                    return self._send(404, {"message": "Not Found"})
                body = json.loads(raw or "{}")
                cwd = body.get("cwd") or fake.home
                if not os.path.isdir(cwd):
                    return self._send(500)
                job = {"id": "sh_" + uuid.uuid4().hex[:20], "command": str(body.get("command") or ""), "cwd": cwd,
                       "status": "running", "exit": None, "output": ""}
                fake._jobs[job["id"]] = job
                fake.commands.append(job)
                timeout = (body.get("timeout") or 120000) / 1000
                threading.Thread(target=fake._run, args=(job, timeout), daemon=True).start()
                self._send(200, {"location": {"directory": fake.home}, "data": dict(self._view(job), status="running", exit=None)})

            def do_GET(self):
                if not self._authed():
                    return
                path, _, query = self.path.partition("?")
                if path == "/api/event":
                    return self._events()
                if path == "/api/model":
                    return self._send(200, {"data": fake.models})
                if path == "/api/session":
                    folder = (urllib.parse.parse_qs(query).get("directory") or [""])[0]
                    return self._send(200, {"data": [s for s in fake.sessions.values() if s["directory"] == folder]})
                if path == "/api/session/active":
                    return self._send(200, {"data": {sid: {"type": "running"} for sid in fake.running}})
                if path.startswith("/api/session/"):
                    s, rest = self._session()
                    if not s:
                        return self._send(404, {"message": "Session not found"})
                    if rest[:1] in (["permission"], ["form"]):
                        store = fake.permissions if rest[0] == "permission" else fake.forms
                        if len(rest) > 1:
                            req = store.get(rest[1])
                            return self._send(200, {"data": {k: v for k, v in req.items() if k != "event"}}) if req \
                                else self._send(404, {"message": "Not found"})
                        return self._send(200, {"data": [{k: v for k, v in r.items() if k != "event"}
                                                         for r in list(store.values()) if r["sessionID"] == s["id"]]})
                    return self._send(200, {"data": fake.diff if rest[:1] == ["diff"] else s})
                job = self._job()
                if not job:
                    return self._send(404, {"_tag": "ShellNotFoundError", "message": "Shell command not found"})
                if self.path.split("?")[0].endswith("/output"):
                    size = len(job["output"].encode())
                    return self._send(200, {"data": {"output": job["output"], "cursor": size, "size": size, "truncated": False}})
                self._send(200, {"location": {"directory": fake.home}, "data": self._view(job)})

            def do_DELETE(self):
                if not self._authed():
                    return
                if self.path.split("?")[0].startswith("/api/session/"):
                    s, rest = self._session()
                    if s and not rest:
                        fake.removed.append(fake.sessions.pop(s["id"]))
                    if s and rest[:1] == ["form"] and len(rest) > 1:
                        req = fake.forms.pop(rest[1], None)
                        if req:
                            fake.replies.append(("form-deleted", rest[1], None))
                            req["event"].set()
                    return self._send(204)
                job = self._job()
                if not job:
                    return self._send(404, {"_tag": "ShellNotFoundError", "message": "Shell command not found"})
                fake._jobs.pop(job["id"], None)
                fake.deleted.append(job["id"])
                self._send(204)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self._stop = True
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
