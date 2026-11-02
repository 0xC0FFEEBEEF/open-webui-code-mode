#!/bin/bash
# Prepare VM 129 for the bug fixer (run as root from this directory). Idempotent.
# Fixes go live through coder-owned copies of the Code-mode tools, so the fixer never needs root:
#   desktop          -> /home/coder/.local/bin/desktop (first in opencode's PATH)
#   preview server   -> /home/coder/.local/lib/code-mode/code-preview.py (code-preview.service runs it as coder)
# The fixer service may read masked service logs and restart the preview server through sudo, nothing else.
set -euo pipefail
cd "$(dirname "$0")"

install -d -o coder -g coder -m 0755 /home/coder/.local /home/coder/.local/bin /home/coder/.local/lib \
    /home/coder/.local/lib/code-mode
[ -f /home/coder/.local/bin/desktop ] || install -o coder -g coder -m 0755 desktop /home/coder/.local/bin/desktop
[ -f /home/coder/.local/lib/code-mode/code-preview.py ] || \
    install -o coder -g coder -m 0644 code-preview.py /home/coder/.local/lib/code-mode/code-preview.py

install -m 0755 code-mode-logs /usr/local/sbin/code-mode-logs
cat > /etc/sudoers.d/code-mode-fixer.new <<'EOF'
# The bug fixer and the feature manager (code-fixer / code-features services, user coder) may read masked Code-mode
# logs and restart the services a fix or a deployed feature changes. opencode itself runs with NoNewPrivileges.
coder ALL=(root) NOPASSWD: /usr/local/sbin/code-mode-logs, /usr/local/sbin/code-mode-logs [0-9]*, /usr/bin/systemctl restart code-preview, /usr/bin/systemctl restart code-fixer
EOF
chmod 0440 /etc/sudoers.d/code-mode-fixer.new
visudo -cf /etc/sudoers.d/code-mode-fixer.new
mv /etc/sudoers.d/code-mode-fixer.new /etc/sudoers.d/code-mode-fixer

if [ ! -f /etc/code-mode/fixer.env ]; then
    ( umask 027
      printf 'FIXER_TOKEN=%s\nFIXER_SECRET=%s\n' "$(openssl rand -hex 24)" "$(openssl rand -hex 32)" > /etc/code-mode/fixer.env )
    chown root:coder /etc/code-mode/fixer.env
fi

sed -i 's#^ExecStart=/usr/bin/python3 /usr/local/lib/code-mode/code-preview.py$#ExecStart=/usr/bin/python3 /home/coder/.local/lib/code-mode/code-preview.py#' \
    /etc/systemd/system/code-preview.service
grep -q '^Environment=PATH=' /etc/systemd/system/opencode-code.service || \
    sed -i 's#^Environment=HOME=/home/coder$#Environment=HOME=/home/coder\nEnvironment=PATH=/home/coder/.local/bin:/usr/local/bin:/usr/bin:/bin#' \
        /etc/systemd/system/opencode-code.service
systemctl daemon-reload
systemctl restart code-preview
echo "fixer prep done"
