#!/bin/bash
# Installs the checkers behind /usr/local/bin/check into /opt/code-check (root-owned, read-only for coder).
# Packages come straight from the npm registry / PyPI and are verified against the registry's sha512.
set -euo pipefail
D=/opt/code-check
mkdir -p "$D/node_modules/@types" "$D/dl"
cd "$D/dl"
fetch_npm() {  # fetch_npm <package> <target dir>
  local pkg="$1" dest="$2" meta tar integ
  meta=$(curl -fsS -m 60 "https://registry.npmjs.org/$pkg/${3:-latest}")
  tar=$(printf %s "$meta" | python3 -c 'import json,sys; print(json.load(sys.stdin)["dist"]["tarball"])')
  integ=$(printf %s "$meta" | python3 -c 'import json,sys; print(json.load(sys.stdin)["dist"]["integrity"])')
  ver=$(printf %s "$meta" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
  curl -fsS -m 300 -o pkg.tgz "$tar"
  got="sha512-$(openssl dgst -sha512 -binary pkg.tgz | base64 -w0)"
  [ "$got" = "$integ" ] || { echo "integrity mismatch for $pkg" >&2; exit 1; }
  rm -rf "$dest"; mkdir -p "$dest"
  tar -xzf pkg.tgz -C "$dest" --strip-components=1
  rm -f pkg.tgz
  echo "$pkg $ver -> $dest"
}
TS5=$(curl -fsS -m 60 -H 'Accept: application/vnd.npm.install-v1+json' https://registry.npmjs.org/typescript | python3 -c '
import json,sys,re
v=[x for x in json.load(sys.stdin)["versions"] if re.fullmatch(r"5\.\d+\.\d+",x)]
print(max(v,key=lambda x:tuple(map(int,x.split(".")))))')
fetch_npm typescript "$D/node_modules/typescript" "$TS5"
fetch_npm @types/node "$D/node_modules/@types/node"
fetch_npm undici-types "$D/node_modules/undici-types"
fetch_npm @biomejs/cli-linux-x64 "$D/biome"
chmod 0755 "$D/biome/biome"
[ -x "$D/venv/bin/python" ] || python3 -m venv "$D/venv"
"$D/venv/bin/pip" -q install --upgrade ruff
DEBIAN_FRONTEND=noninteractive apt-get install -y -q shellcheck >/dev/null
rmdir "$D/dl" 2>/dev/null || true
chown -R root:root "$D"; chmod -R a+rX,go-w "$D"
echo "--- versions"
node "$D/node_modules/typescript/bin/tsc" --version
"$D/biome/biome" --version
"$D/venv/bin/ruff" --version
shellcheck --version | sed -n 2p
du -sh "$D"
