#!/bin/sh
# The Open WebUI side can't run here (Open WebUI lives on cryoai), so the checks are: every Python file
# compiles, the page script parses, and the pipe/tool keep the frontmatter Open WebUI reads.
set -e
cd "$(dirname "$0")"
for f in owui/*.py; do python3 -m py_compile "$f"; done
node --check owui/code-toggle.js
# A stray comma after Field(...) makes a valve default a tuple; pydantic accepts it and the pipe fails at run time.
python3 - <<'PY'
import ast, sys
for f in ("owui/code_mode.py", "owui/desktop_code_tool.py"):
    for node in ast.walk(ast.parse(open(f).read())):
        if isinstance(node, ast.ClassDef) and node.name in ("Valves", "UserValves"):
            bad = [n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.value, ast.Tuple)]
            if bad:
                sys.exit("%s: valve default is a tuple (stray comma?): %s" % (f, ", ".join(bad)))
PY
for f in owui/code_mode.py owui/desktop_code_tool.py; do
  head -12 "$f" | grep -q '^version: ' || { echo "$f: frontmatter has no version line" >&2; exit 1; }
done
# Pipe logic that needs no Open WebUI (model choice, ...) has tests in tests/. They run with the main checkout's
# .venv (httpx, pydantic, pytest), which a feature worktree shares.
main="$(git rev-parse --path-format=absolute --git-common-dir)/.."
py="$main/.venv/bin/python"
if ls tests/test_*.py >/dev/null 2>&1; then
  [ -x "$py" ] || { echo "check.sh: no .venv in the main checkout (python3 -m venv .venv; pip install httpx pydantic pytest)" >&2; exit 1; }
  "$py" -m pytest -q -p no:cacheprovider tests
fi
# Page-script tests: tests/page/test_*.cjs load owui/code-toggle.js into a fake Open WebUI page (jsdom) and click
# through it; see tests/page/harness.cjs. jsdom lives in the main checkout's node_modules, shared the same way.
if ls tests/page/test_*.cjs >/dev/null 2>&1; then
  [ -d "$main/node_modules/jsdom" ] || { echo "check.sh: no jsdom in the main checkout (npm install)" >&2; exit 1; }
  for t in tests/page/test_*.cjs; do
    echo "== $t"
    NODE_PATH="$main/node_modules" CODE_MODE_PY="$py" node "$t"
  done
fi
echo "check.sh: ok"
