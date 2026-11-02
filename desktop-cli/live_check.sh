#!/usr/bin/env bash
# live_check.sh -- exercise the `desktop` tool against a real desktop.
#
# Serves a tiny web app (input + a button that echoes the input into a <p>),
# then drives it with `desktop` using a throwaway session. Prints every
# command's output and verifies the typed value ends up on the page.
#
# Run with:  CODE_DESKTOP=desktop-cli-test ./live_check.sh
set -u

export CODE_DESKTOP=desktop-cli-test
export DESKTOP_API="${DESKTOP_API:-http://127.0.0.1:6080}"
# CONTROL_TOKEN is read from /etc/code-mode/desktop.env if it is not set.

PORT=8765
MARKER="agent-test-$(date +%s)-$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
WORK="$(mktemp -d)"
SERVER_PID=""

cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
}
trap cleanup EXIT

# 1. The test web app.
cat > "$WORK/index.html" <<HTML
<!doctype html>
<html><head><title>live check app</title></head>
<body>
<h1>live check</h1>
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

echo
echo "### desktop type $IN_ID \"$MARKER\"   (type into the input, then Enter off)"
TYPE_OUT="$(./desktop type "$IN_ID" "$MARKER")"
echo "$TYPE_OUT"

BUTTON_ID="$(printf '%s\n' "$TYPE_OUT" | grep -E '^\[[0-9]+\] button' | head -1 | grep -oE '[0-9]+' | head -1)"
if [ -z "$BUTTON_ID" ]; then
    echo "error: could not find a <button> element number" >&2
    exit 1
fi

echo
echo "### desktop click $BUTTON_ID"
./desktop click "$BUTTON_ID"

echo
echo "### desktop look"
LOOK_OUT="$(./desktop look)"
echo "$LOOK_OUT"

echo
echo "### verification"
if printf '%s\n' "$LOOK_OUT" | grep -qF "$MARKER"; then
    echo "PASS: page text contains the value typed into the input ($MARKER)"
    echo
    echo "### desktop done   (park the session to free memory)"
    ./desktop done
    exit 0
fi
echo "FAIL: page text did not contain the typed value ($MARKER)" >&2
exit 1
