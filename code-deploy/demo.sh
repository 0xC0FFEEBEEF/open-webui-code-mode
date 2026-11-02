#!/usr/bin/env bash
#
# demo.sh — bring up the code-deploy demo locally.
#
#   * fake manager       on  http://127.0.0.1:4111   (sample data)
#   * fake open webui    on  http://127.0.0.1:4112
#   * code-deploy service on http://127.0.0.1:4113   (DEPLOY_DEMO=1, serves /harness.html)
#
# Everything the service writes goes into a throwaway temp dir (the fake
# Open WebUI DB, the op records, the installed loader). Run it in the
# background, then look at the page on the desktop:
#
#     ./demo.sh &           # starts and blocks, services stay up
#     ... desktop checks ...
#     <stop it>
#
set -uo pipefail
cd "$(dirname "$0")"

# The fake manager accepts this bearer token (see demo.py).
TOKEN="mgr-secret-token"

STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/code-deploy-demo.XXXXXX")"
LOADER="$STATE_DIR/loader.js"
KEY="$STATE_DIR/secret_key"
DB_ENV="$STATE_DIR/owui.db.env"
: > "$LOADER"
echo "demo-only secret key" > "$KEY"

# Start the fake manager + fake Open WebUI; they publish the OWUI DB path.
python3 demo.py "$DB_ENV" &
DEMO_PID=$!

# Wait for demo.py to publish the DB path.
for _ in $(seq 1 100); do
    [ -s "$DB_ENV" ] && break
    sleep 0.1
done
# shellcheck disable=SC1091
source "$DB_ENV"

FEATURES_URL="http://127.0.0.1:4111" \
FEATURES_TOKEN="$TOKEN" \
OWUI_URL="http://127.0.0.1:4112" \
OWUI_DB="$OWUI_DB" \
WEBUI_SECRET_KEY_FILE="$KEY" \
DEPLOY_PORT=4113 \
DEPLOY_STATE="$STATE_DIR" \
ALLOWED_ORIGINS="http://127.0.0.1:4113" \
LOADER_TARGETS="$LOADER" \
NODE="$(command -v node)" \
DEPLOY_DEMO=1 \
    python3 code-deploy.py serve &
SERVE_PID=$!

cleanup() {
    kill "$DEMO_PID" "$SERVE_PID" 2>/dev/null
    rm -rf "$STATE_DIR"
}
trap cleanup EXIT

# Wait for the service to answer /features/ (always 200, no auth needed).
for _ in $(seq 1 100); do
    code="$(curl -s -o /dev/null -w "%{http_code}" \
        "http://127.0.0.1:4113/features/" 2>/dev/null || echo 000)"
    [ "$code" = "200" ] && break
    sleep 0.1
done

echo "code-deploy demo running:"
echo "  manager     : http://127.0.0.1:4111"
echo "  open webui  : http://127.0.0.1:4112"
echo "  service     : http://127.0.0.1:4113"
echo "  harness     : http://127.0.0.1:4113/harness.html"
echo "  standalone  : http://127.0.0.1:4113/features/"
echo "  fakes pid   : $DEMO_PID"
echo "  service pid : $SERVE_PID"
echo "(kill the demo.sh process to stop everything and clean up)"

wait
