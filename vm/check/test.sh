#!/bin/sh
# Regression check for `check` (run on the VM, where /opt/code-check is installed): the broken sample project must
# fail with the known findings, and the clean one must pass with no errors.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
bad="$(cd "$here/fixtures/bad" && "$here/check" || true)"
for want in "'chartHeight' is not defined" "Did you mean 'refreshAl'" 'app.jss", which does not exist' \
            "Cannot assign to 'total'" 'no element has id="kpp"' "getElementByID" "Undefined name \`reslt\`" \
            "CSS syntax error" "not valid JSON" "shell syntax error" "inside a module script" \
            'static/nope.js", which does not exist' "CHECK: FAIL"; do
  printf '%s' "$bad" | grep -qF -- "$want" || { echo "bad/: missing finding: $want"; exit 1; }
done
good="$(cd "$here/fixtures/good" && "$here/check")" || { echo "good/: reported errors:"; echo "$good"; exit 1; }
printf '%s' "$good" | grep -q "CHECK: PASS - no errors" || { echo "good/: no PASS line"; exit 1; }
echo "check: fixtures ok"
