#!/usr/bin/env bash
# live_check_vision.sh -- exercise `desktop see` and `desktop delegate` against a
# real desktop using a FAKE OpenAI-compatible vision server (port 8790), so we
# never call the real vision model (which would unload the model we run on).
#
# The fake server is scripted to: click the input, type a marker into it, click
# the button, then report done. We then `desktop look` and verify the marker
# reached the page. Prints every output and PASS/FAIL, then stops the servers.
#
# Run with:  CODE_DESKTOP=desktop-cli-test ./live_check_vision.sh
set -u

export CODE_DESKTOP=desktop-cli-test
export DESKTOP_API="${DESKTOP_API:-http://127.0.0.1:6080}"
# CONTROL_TOKEN is read from /etc/code-mode/desktop.env if it is not set.

PORT=8765
VISION_PORT=8790
WORK="$(mktemp -d)"
MARKER="vision-test-$(date +%s)-$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SERVER_PID=""
VISION_PID=""

cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
    [ -n "$VISION_PID" ] && kill "$VISION_PID" 2>/dev/null
}
trap cleanup EXIT

# 1. The fake OpenAI-compatible vision server (scripted). It records the requests
#    it receives to $VISION_LOG so the run can show the request shapes.
cat > "$WORK/fake_vision.py" <<'PY'
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = os.environ["VISION_CONFIG"]
LOG = os.environ.get("VISION_LOG", "")

with open(CONFIG) as fh:
    CFG = json.load(fh)

# What the fake model "sees" and returns, in order, for each chat/completions call:
#   1) click the input   2) type the marker   3) click the button   4) done
STEPS = [
    {"thought": "click the input", "action": "click", "id": CFG["input_id"]},
    {"thought": "type the marker into the input",
     "action": "type", "id": CFG["input_id"], "text": CFG["marker"]},
    {"thought": "click the button", "action": "click", "id": CFG["button_id"]},
    {"thought": "task finished", "action": "done",
     "report": "Clicked the input, typed %s into it, clicked the button, and the "
               "value appeared in the output paragraph." % CFG["marker"]},
]

_lock = threading.Lock()
_n = 0

PROSE_SEE = ("This page is the vision live-check app: a heading, a text input "
             "holding the marker, and a button labeled Go. The input currently "
             "contains %s and the output paragraph below the button shows the "
             "same text, so the button's click worked. The button is element 2." %
             CFG["marker"])


def _user_text(req):
    out = []
    for m in req.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and item.get("type") == "text":
                    out.append(item.get("text", ""))
        elif isinstance(c, str):
            out.append(c)
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        # /v1/health probe: does not advance the script counter.
        self._respond(200, {"ok": True})

    def do_POST(self):
        global _n
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode() or "{}")
        except Exception:
            req = {}
        is_see = "Question:" in _user_text(req)
        if LOG:
            # Log a trimmed summary of the request so we can show the shape
            # WITHOUT dumping the base64 screenshot (100+ KB) into the output.
            content = []
            for m in req.get("messages", []):
                c = m.get("content")
                if isinstance(c, list):
                    content.append({"role": m.get("role"),
                                    "kinds": [i.get("type") for i in c]})
                else:
                    content.append({"role": m.get("role"),
                                    "kinds": ["text"]})
            summary = {
                "call": _n,
                "model": req.get("model"),
                "temperature": req.get("temperature"),
                "max_tokens": req.get("max_tokens"),
                "response_format_set": "response_format" in req,
                "is_see": "Question:" in _user_text(req),
                "messages": content,
            }
            with open(LOG, "a") as fh:
                fh.write(json.dumps(summary) + "\n")
                fh.flush()
        with _lock:
            idx, _n = _n, _n + 1
        if is_see:
            payload = PROSE_SEE
        else:
            payload = json.dumps(STEPS[min(idx, len(STEPS) - 1)])
        body = json.dumps({"choices": [{"message": {"content": payload}}]}).encode()
        self._respond(200, json.loads(body))

    def _respond(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
PY

# 2. The test web app (same kind as live_check.sh): an input + a button that
#    echoes the input into a <p>.
cat > "$WORK/index.html" <<HTML
<!doctype html>
<html><head><title>vision live check</title></head>
<body>
<h1>vision live check</h1>
<input id="name" type="text" placeholder="type your name here">
<button id="go">Go</button>
<p id="out"></p>
<script>
document.getElementById('go').addEventListener('click', function () {
  document.getElementById('out').textContent =
    document.getElementById('name').value;
});
</script>
</body></html>
HTML

echo "### serving $WORK on http://localhost:$PORT"
( cd "$WORK" && exec python3 -m http.server "$PORT" ) >"$WORK/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
    curl -s "http://localhost:$PORT/" >/dev/null 2>&1 && break
    sleep 0.1
done

echo
echo "### desktop status"
./desktop status

echo
echo "### desktop open http://localhost:$PORT   (starts the desktop; may take ~20s)"
OPEN_OUT="$(./desktop open "http://localhost:$PORT")"
echo "$OPEN_OUT"

IN_ID="$(printf '%s\n' "$OPEN_OUT" | grep -E '^\[[0-9]+\] input' | head -1 | grep -oE '[0-9]+' | head -1)"
if [ -z "$IN_ID" ]; then
    echo "error: could not find an <input> element number" >&2
    exit 1
fi

BUTTON_ID="$(printf '%s\n' "$OPEN_OUT" | grep -E '^\[[0-9]+\] button' | head -1 | grep -oE '[0-9]+' | head -1)"
if [ -z "$BUTTON_ID" ]; then
    echo "error: could not find a <button> element number" >&2
    exit 1
fi

echo
echo "### input element = $IN_ID, button element = $BUTTON_ID, marker = $MARKER"

# 3. Configure the fake vision server with the element numbers and marker.
cat > "$WORK/vision_config.json" <<JSON
{"input_id": $IN_ID, "button_id": $BUTTON_ID, "marker": "$MARKER"}
JSON

echo
echo "### starting the fake vision server on port $VISION_PORT"
export VISION_CONFIG="$WORK/vision_config.json"
export VISION_LOG="$WORK/requests.log"
: > "$VISION_LOG"
( VISION_API="http://127.0.0.1:$VISION_PORT/v1" \
  VISION_MODEL="fake-vision" \
  python3 "$WORK/fake_vision.py" "$VISION_PORT" ) >"$WORK/vision.log" 2>&1 &
VISION_PID=$!

for _ in $(seq 1 50); do
    curl -s "http://127.0.0.1:$VISION_PORT/v1/health" >/dev/null 2>&1 && break
    sleep 0.1
done

echo
echo "### desktop delegate   (fake vision server drives the desktop)"
DELEGATE_OUT="$(VISION_API="http://127.0.0.1:$VISION_PORT/v1" \
                VISION_MODEL="fake-vision" \
                ./desktop delegate "open the page, type $MARKER into the input, click the button, and report what you see)" \
                --steps 10)"
echo "$DELEGATE_OUT"

echo
echo "### desktop see   (ask the fake vision model what is on the page)"
SEE_OUT="$(VISION_API="http://127.0.0.1:$VISION_PORT/v1" \
           VISION_MODEL="fake-vision" \
           ./desktop see "what is shown on this page and where is the button?")"
echo "$SEE_OUT"

echo
echo "### fake vision server request log (delegate + see calls)"
cat "$VISION_LOG" 2>/dev/null || echo "(no log)"

echo
echo "### desktop look"
LOOK_OUT="$(./desktop look)"
echo "$LOOK_OUT"

echo
echo "### verification"
if printf '%s\n' "$LOOK_OUT" | grep -qF "$MARKER"; then
    echo "PASS: the text the fake vision agent typed ($MARKER) reached the page"
    echo
    echo "### desktop done   (park the session to free memory)"
    ./desktop done
    exit 0
fi
echo "FAIL: the value the fake vision agent typed ($MARKER) did not reach the page" >&2
exit 1
