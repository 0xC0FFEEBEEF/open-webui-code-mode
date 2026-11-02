#!/usr/bin/env bash
# live_check.sh -- start code-preview.py on port 4098 against a throwaway root
# holding one chat folder, then curl -i through the documented scenarios.
# Everything (requests, headers, server logs) is printed to stdout.
set -u

PORT=4098
SECRET="livecheck-secret"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(mktemp -d)"
CHAT="$ROOT/demo"
BASE="http://127.0.0.1:$PORT"

mkdir -p "$CHAT/sub" "$CHAT/.git"

# --- build one chat folder -------------------------------------------------
cat > "$CHAT/index.html" <<'HTML'
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>demo home</title></head>
<body><h1>Live demo index.html</h1><p>See style.css.</p></body></html>
HTML
cat > "$CHAT/style.css" <<'CSS'
body { font-family: system-ui, sans-serif; margin: 2rem; }
h1 { color: teal; }
CSS
cat > "$CHAT/sub/readme.txt" <<'TXT'
This subfolder has no index.html, so the server should return a file listing.
TXT
cat > "$CHAT/sub/data.json" <<'JSON'
{"a": 1, "b": [2, 3]}
JSON
echo "[core]" > "$CHAT/.git/config"

# sig = first 32 hex chars of HMAC-SHA256(secret, chat_id)
SIG="$(python3 -c "import hmac,hashlib,sys
print(hmac.new(sys.argv[1].encode(), sys.argv[2].encode(), hashlib.sha256).hexdigest()[:32])" "$SECRET" "demo")"
echo "chat_id=demo  sig=$SIG"
echo "root=$ROOT"
echo

# --- start the server ------------------------------------------------------
PREVIEW_SECRET="$SECRET" PREVIEW_ROOT="$ROOT" PREVIEW_PORT="$PORT" \
    python3 "$SCRIPT_DIR/code-preview.py" >"$ROOT/server.out" 2>"$ROOT/server.err" &
SRV=$!

# wait for the port to come up
for _ in $(seq 1 50); do
    if curl -s -o /dev/null "$BASE/p/demo/$SIG/__version"; then break; fi
    sleep 0.1
done

echo "=================== GET /p/demo/$SIG/            (the page) ==================="
curl -s -i "$BASE/p/demo/$SIG/"
echo
echo
echo "=================== GET .../style.css          (the css) ====================="
curl -s -i "$BASE/p/demo/$SIG/style.css"
echo
echo
echo "=================== GET /p/demo/$SIG/sub/      (a listing) ==================="
curl -s -i "$BASE/p/demo/$SIG/sub/"
echo
echo
echo "=================== GET .../badsig/            (403 bad sig) =================="
curl -s -i "$BASE/p/demo/$(printf '0%.0s' {1..32})/"
echo
echo
echo "=================== GET .../.git/config        (404 dotdir) ==================="
curl -s -i "$BASE/p/demo/$SIG/.git/config"
echo
echo
echo "=================== GET .../__version  (BEFORE edit) =========================="
curl -s -i "$BASE/p/demo/$SIG/__version"
echo
echo
echo "--- editing index.html (appending a line) --------------------------------- "
echo "<!-- edited at $(date -u +%Y-%m-%dT%H:%M:%SZ) -->" >> "$CHAT/index.html"
echo
echo "=================== GET .../__version  (AFTER edit) =========================="
curl -s -i "$BASE/p/demo/$SIG/__version"
echo
echo
echo "=================== server stdout =========================================="
cat "$ROOT/server.out"
echo
echo "=================== server stderr (one line per request) ==================="
cat "$ROOT/server.err"

# --- stop the server -------------------------------------------------------
kill "$SRV" 2>/dev/null
wait "$SRV" 2>/dev/null
echo
echo "server stopped (scratch root left at $ROOT)"
