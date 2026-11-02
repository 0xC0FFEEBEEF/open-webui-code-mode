#!/bin/bash
# Install the Code-mode bug fixer on VM 129 (run as root from a directory holding code-fixer.py, viewer.html,
# log.html, report-bug and code-fixer.service). Needs fixer-prep.sh to have run (sudoers, fixer.env, ~/.local).
# Everything runs as coder from coder-owned copies, like the other Code-mode tools, so a fix can update them.
set -euo pipefail
cd "$(dirname "$0")"

LIB=/home/coder/.local/lib/code-mode
install -d -o coder -g coder -m 0750 /home/coder/fixes
for f in code-fixer.py viewer.html log.html; do install -o coder -g coder -m 0644 "$f" "$LIB/$f"; done
install -o coder -g coder -m 0755 report-bug /home/coder/.local/bin/report-bug
install -m 0644 code-fixer.service /etc/systemd/system/code-fixer.service

# Open WebUI on cryoai calls the API and the user's browser loads the viewer: same exposure as the preview (:4097).
systemctl daemon-reload
systemctl enable code-fixer.service
systemctl restart code-fixer.service
sleep 1
systemctl is-active code-fixer.service
