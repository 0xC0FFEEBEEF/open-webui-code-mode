#!/usr/bin/env bash
# live_check.sh -- end-to-end smoke test of the code-features service against
# COPIES of the four real repos. See BRIEF.md "Small changes ... (part C)".
#
# It clones the four real main checkouts into a temp projects dir, symlinks each
# clone's .venv to the real one, builds a temp FEATURES_DIR / FEATURES_HOME with
# the current install targets pre-populated, and starts the real code-features
# service (stdlib only) on port 4101. It then drives a full chat:
#   create -> edit two components -> feature ready -> deploy -> verify
#   -> rollback -> verify -> re-deploy -> verify -> discard a second feature.
# Every request and result is printed. Nothing under /home/coder/projects is
# touched outside the temp dir (the workspaces are created under it).
set -u

SRC=/home/coder/projects
CODE_FEATURES="$SRC/code-features"
FEATURE_CLI="$CODE_FEATURES/feature"
PORT=4101
TOKEN="live-check-token"

ROOT="$(mktemp -d)"
PROJ="$ROOT/projects"
HOME_DIR="$ROOT/home"
RESTART_LOG="$ROOT/restarts.log"
FIXER_INDEX="$ROOT/fixes.jsonl"
CHECK="$ROOT/check.py"
COMPS="$ROOT/components.json"
SRV_OUT="$ROOT/srv.out"

mkdir -p "$PROJ" "$HOME_DIR"

SERVER_PID=""
cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
    rm -rf "$ROOT"
}
trap cleanup EXIT

echo "### temp workspace: $ROOT"

# A trivial component "test": passes unless the file contains the word BROKEN.
cat > "$CHECK" <<'PY'
import sys
try:
    with open(sys.argv[1]) as f:
        c = f.read()
except OSError:
    c = ""
sys.exit(1 if "BROKEN" in c else 0)
PY

# Fake restart command: records the unit it was asked to restart.
cat > "$ROOT/restart.sh" <<'SH'
#!/bin/sh
echo "$1" >> "$RESTART_LOG"
SH
chmod +x "$ROOT/restart.sh"
: > "$FIXER_INDEX"   # empty fixer index

# 1. Clone the four real repos; symlink each clone's .venv to the real one.
echo "### cloning the four real repos into $PROJ"
for name in desktop-cli code-preview code-fixer code-mode; do
    git clone -q "$SRC/$name" "$PROJ/$name" && echo "  cloned $name"
    if [ -d "$SRC/$name/.venv" ] && [ -d "$PROJ/$name/.venv" ]; then
        ln -sfn "$SRC/$name/.venv" "$PROJ/$name/.venv"
        echo "  symlinked .venv for $name"
    fi
done

# 2. components.json: the real structure (names, branches, labels, install
#    targets, restart units, remote), but a lightweight test command so `feature
#    ready`/deploy exercise the service without running the real pytest suites or
#    touching anything outside the temp dir.
cat > "$COMPS" <<JSON
[
  {"name": "desktop-cli", "label": "Desktop tool", "branch": "master",
   "test": ["python3", "$CHECK", "desktop"],
   "install": [{"from": "desktop", "to": "~/.local/bin/desktop", "mode": "0755"}], "restart": []},
  {"name": "code-preview", "label": "Preview server", "branch": "master",
   "test": ["python3", "$CHECK", "code-preview.py"],
   "install": [{"from": "code-preview.py", "to": "~/.local/lib/code-mode/code-preview.py", "mode": "0644"}],
   "restart": ["code-preview"]},
  {"name": "code-fixer", "label": "Bug fixer", "branch": "main",
   "test": ["python3", "$CHECK", "code-fixer.py"],
   "install": [{"from": "code-fixer.py", "to": "~/.local/lib/code-mode/code-fixer.py", "mode": "0644"},
               {"from": "viewer.html", "to": "~/.local/lib/code-mode/viewer.html", "mode": "0644"},
               {"from": "log.html", "to": "~/.local/lib/code-mode/log.html", "mode": "0644"},
               {"from": "report-bug", "to": "~/.local/bin/report-bug", "mode": "0755"}],
   "restart": ["code-fixer"]},
  {"name": "code-mode", "label": "Open WebUI side", "branch": "main",
   "test": ["python3", "$CHECK", "owui/code_mode.py"],
   "install": [{"from": "agents/AGENTS.md", "to": "~/.config/opencode/AGENTS.md", "mode": "0644"}],
   "restart": [], "remote": "owui/"}
]
JSON

# 3. Pre-populate the install targets with the current real files' content, so
#    deploy has something to back up and we can see the line change.
install_one() {   # <src-repo> <src-file> <dest-relative-to-home>
    local rel="$2" dest="$HOME_DIR/$3"
    mkdir -p "$(dirname "$dest")"
    cp "$SRC/$1/$rel" "$dest"
}
install_one desktop-cli     desktop                         .local/bin/desktop
install_one code-preview    code-preview.py                 .local/lib/code-mode/code-preview.py
install_one code-fixer      code-fixer.py                   .local/lib/code-mode/code-fixer.py
install_one code-fixer      viewer.html                     .local/lib/code-mode/viewer.html
install_one code-fixer      log.html                        .local/lib/code-mode/log.html
install_one code-fixer      report-bug                      .local/bin/report-bug
install_one code-mode       agents/AGENTS.md                .config/opencode/AGENTS.md
echo "### install targets pre-populated under $HOME_DIR"

# 4. Start the real service on port 4101 with a test token. Env vars win over any
#    /etc/code-mode/features.env, so this is fully isolated.
export FEATURES_PORT="$PORT"
export FEATURES_TOKEN="$TOKEN"
export FEATURES_DIR="$ROOT/features"
export FEATURES_PROJECTS="$PROJ"
export FEATURES_HOME="$HOME_DIR"
export FEATURES_COMPONENTS="$COMPS"
export FEATURES_RESTART_CMD="$ROOT/restart.sh"
export FEATURES_FIXER_INDEX="$FIXER_INDEX"

python3 "$CODE_FEATURES/code-features.py" >"$SRV_OUT" 2>&1 &
SERVER_PID=$!
echo "### started code-features (pid $SERVER_PID) on port $PORT"
for _ in $(seq 1 50); do
    curl -s "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
    sleep 0.1
done
echo "### GET /api/health:"; curl -s "http://127.0.0.1:$PORT/api/health"; echo

# -- HTTP helpers -----------------------------------------------------------
API="http://127.0.0.1:$PORT"
AUTH="Authorization: Bearer $TOKEN"
req() {   # <METHOD> <path> [json-body]   prints the request line and response
    echo "### $1 $2" >&2
    local out
    case "$1" in
        POST|PUT|PATCH)
            out="$(curl -s -X "$1" "$API$2" -H "$AUTH" \
                -H 'Content-Type: application/json' ${3:+-d "$3"})" ;;
        *) out="$(curl -s -X "$1" "$API$2" -H "$AUTH")" ;;
    esac
    printf '%s\n' "$out"
}
jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
wait_op() {   # <op id> -> prints the finished op.json
    local opid="$1" b st
    while :; do
        b="$(curl -s "$API/api/ops/$opid" -H "$AUTH")"
        st="$(printf '%s' "$b" | jget 'd["op"]["status"]')"
        [ "$st" = "running" ] || break
        sleep 0.2
    done
    printf '%s\n' "$b"
}
check() {   # <description> <0|1 result>
    if [ "$2" = "1" ]; then echo "PASS: $1"; else echo "FAIL: $1" >&2; exit 1; fi
}

# 5. Create a feature for chat "live-check".
BODY="$(req POST /api/features '{"chat_id":"live-check"}')"
N="$(printf '%s' "$BODY" | jget 'd["feature"]["n"]')"
DIR="$PROJ/features/live-check"
echo "### feature #$N, workspace $DIR"

# 6. Edit two components (add a comment line) and commit in each worktree.
MARKER="livecheck-$(date +%s)"
for pair in "desktop-cli desktop" "code-mode owui/code_mode.py"; do
    set -- $pair
    echo "  editing $1 ($2)"
    printf '%s   # added by live_check.sh\n' "$MARKER" >> "$DIR/$1/$2"
    ( cd "$DIR/$1" && git config user.email "live@check" \
        && git config user.name "live check" \
        && git add -A && git commit -q -m "live check: comment in $2" )
done

# 7. feature ready (run from inside the workspace, env pointing at port 4101).
echo; echo "### feature ready"
( cd "$DIR" && python3 "$FEATURE_CLI" ready \
      "Live check: comment change" \
      "Adds a marker comment to desktop and code_mode, then deploys/rolls back." )

# 8. Deploy and wait for the op.
DEPLOY="$(req POST /api/features/$N/deploy)"
OPID="$(printf '%s' "$DEPLOY" | jget 'd["op"]')"
echo "### deploy op: $OPID"; DONE="$(wait_op "$OPID")"; printf '%s\n' "$DONE"
check "deploy op ok" "$([ "$(printf '%s' "$DONE" | jget 'd["op"]["status"]')" = ok ] && echo 1 || echo 0)"

# 9. Verify: installed desktop has the new line; remote changed only code_mode.
echo; echo "### installed desktop tail:"; tail -n 2 "$HOME_DIR/.local/bin/desktop"
check "installed desktop has the new line" \
    "$(grep -qF "$MARKER" "$HOME_DIR/.local/bin/desktop" && echo 1 || echo 0)"
CHANGED="$(printf '%s' "$DONE" | jget 'd["op"]["remote"]["changed"]')"
echo "### remote.changed = $CHANGED"
check "remote.changed == [\"owui/code_mode.py\"]" \
    "$([ "$CHANGED" = "['owui/code_mode.py']" ] && echo 1 || echo 0)"

# 10. Roll back and check the line is gone.
RB="$(req POST /api/features/$N/rollback)"
RBPID="$(printf '%s' "$RB" | jget 'd["op"]')"
RBONE="$(wait_op "$RBPID")"; printf '%s\n' "$RBONE"
check "rollback op ok" "$([ "$(printf '%s' "$RBONE" | jget 'd["op"]["status"]')" = ok ] && echo 1 || echo 0)"
check "installed desktop lost the line after rollback" \
    "$(! grep -qF "$MARKER" "$HOME_DIR/.local/bin/desktop" && echo 1 || echo 0)"

# 11. Deploy again and check the line is back.
DEPLOY2="$(req POST /api/features/$N/deploy)"
OPID2="$(printf '%s' "$DEPLOY2" | jget 'd["op"]')"
DONE2="$(wait_op "$OPID2")"; printf '%s\n' "$DONE2"
check "re-deploy op ok" "$([ "$(printf '%s' "$DONE2" | jget 'd["op"]["status"]')" = ok ] && echo 1 || echo 0)"
check "installed desktop has the line again after re-deploy" \
    "$(grep -qF "$MARKER" "$HOME_DIR/.local/bin/desktop" && echo 1 || echo 0)"

# 12. A second feature: commit a change, then discard it.
BODY2="$(req POST /api/features '{"chat_id":"live-check-2"}')"
N2="$(printf '%s' "$BODY2" | jget 'd["feature"]["n"]')"
DIR2="$PROJ/features/live-check-2"
echo "  editing feature #$N2"; printf '%s\n' "$MARKER" >> "$DIR2/desktop-cli/desktop"
( cd "$DIR2/desktop-cli" && git config user.email "live@check" \
    && git config user.name "live check" \
    && git add -A && git commit -q -m "will be discarded" )
echo; echo "### discard feature #$N2"
DISC="$(req POST /api/features/$N2/discard)"
DISC_OP="$(printf '%s' "$DISC" | jget 'd["op"]')"
DISC_DONE="$(wait_op "$DISC_OP")"; printf '%s\n' "$DISC"
printf '%s\n' "$DISC_DONE"
FEAT2="$(curl -s "$API/api/features/$N2" -H "$AUTH")"
FEAT2_ST="$(printf '%s' "$FEAT2" | jget 'd["feature"]["status"]')"
echo "### feature #$N2 status after discard = $FEAT2_ST"
check "feature #$N2 discarded" "$([ "$FEAT2_ST" = discarded ] && echo 1 || echo 0)"

# 13. Print the op logs.
echo; echo "### op logs"
for oid in "$OPID" "$RBPID" "$OPID2"; do
    echo "--- op $oid ---"
    curl -s "$API/api/ops/$oid" -H "$AUTH" | jget 'd["log"]'
done

# 14. Stop the service.
echo; echo "### stopping service"
kill "$SERVER_PID" 2>/dev/null
wait "$SERVER_PID" 2>/dev/null
echo "### live check complete; temp dir $ROOT removed on exit."
