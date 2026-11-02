#!/bin/bash
# Install code-features (feature chats: workspaces, deploy, rollback) on VM 129. Run as root from this directory
# (vm/, with the service's files in code-features/: copies of /home/coder/projects/code-features plus the unit).
# Idempotent; the token is made once and never printed.
# The manager is root-owned on purpose: it runs as coder, but a feature chat (also coder) can't change it.
set -euo pipefail
cd "$(dirname "$0")"

install -d -m 0755 /usr/local/lib/code-mode
install -m 0644 code-features/code-features.py code-features/components.json code-features/workspace-AGENTS.md /usr/local/lib/code-mode/
install -m 0755 code-features/feature /usr/local/bin/feature
install -d -o coder -g coder -m 0755 /home/coder/features /home/coder/projects/features

if [ ! -f /etc/code-mode/features.env ]; then
    ( umask 027; printf 'FEATURES_TOKEN=%s\n' "$(openssl rand -hex 24)" > /etc/code-mode/features.env )
    chown root:coder /etc/code-mode/features.env
fi
# the feature CLI lives in /usr/local/bin, so it can't find components.json next to itself
grep -q '^FEATURES_COMPONENTS=' /etc/code-mode/features.env || \
    echo 'FEATURES_COMPONENTS=/usr/local/lib/code-mode/components.json' >> /etc/code-mode/features.env

bash fixer-prep.sh                      # sudoers: + restart code-fixer
install -m 0644 code-mode.nft /etc/code-mode/firewall.nft
systemctl reload code-mode-firewall     # not restart: that would restart opencode too
install -m 0644 code-features/code-features.service /etc/systemd/system/code-features.service
systemctl daemon-reload
systemctl enable code-features
systemctl restart code-features
sleep 1
systemctl is-active code-features
curl -s http://127.0.0.1:4100/api/health; echo
echo "code-features installed"
