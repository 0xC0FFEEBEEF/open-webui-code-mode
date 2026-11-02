#!/bin/bash
# Install Code mode's opencode server on the AI-Sandbox VM. Run as root from this directory,
# with the opencode binary and the generated opencode.json copied alongside.
# Idempotent: re-running keeps the existing server password and projects.
set -euo pipefail
cd "$(dirname "$0")"

if ! id coder >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash --comment "Code mode (opencode)" coder
fi
install -d -o coder -g coder -m 0750 /home/coder/projects
install -d -o coder -g coder -m 0700 /home/coder/.config /home/coder/.config/opencode

VERSION="$(./opencode --version | awk '{print $2}')"
install -m 0755 opencode "/usr/local/bin/opencode-$VERSION"
ln -sfn "opencode-$VERSION" /usr/local/bin/opencode

install -o coder -g coder -m 0600 opencode.json /home/coder/.config/opencode/opencode.json
if [ ! -f /home/coder/.config/opencode/service.json ]; then
    umask 077
    printf '{"hostname":"0.0.0.0","port":4096,"password":"%s"}\n' "$(openssl rand -hex 24)" \
        > /home/coder/.config/opencode/service.json
    chown coder:coder /home/coder/.config/opencode/service.json
fi

install -d -m 0755 /etc/code-mode
install -m 0644 code-mode.nft /etc/code-mode/firewall.nft

# Phase 2: test on the desktop. coder gets its own copy of the desktop control token (agent can't read it),
# the `desktop` command, and global instructions that tell the model how to use it.
if [ -f /etc/sandbox-agent/env ]; then
    ( umask 027
      grep -E '^CONTROL_TOKEN=' /etc/sandbox-agent/env > /etc/code-mode/desktop.env
      echo "DESKTOP_API=http://127.0.0.1:6080" >> /etc/code-mode/desktop.env
      # router models that can see: desktop see/delegate use the loaded one of these instead of swapping
      echo "VISION_MODELS=ornith-1.5-35b-a3b,bonsai-27b,gemma-3-4b-it-q4km,gemma4-26b-a4b,holo-3.1-9b,holo4-35b-a3b,nemotron-3.5-lightning-30b-a3b,ornith-1.5-9b,qwen3.5-9b,qwen3.6-35b-a3b-ud-q4kxl,qwen3.8-27b,ui-venus-2-9b" >> /etc/code-mode/desktop.env )
    chown root:coder /etc/code-mode/desktop.env
    chmod 0640 /etc/code-mode/desktop.env
fi
[ -f desktop ] && install -m 0755 desktop /usr/local/bin/desktop
[ -f AGENTS.md ] && install -o coder -g coder -m 0644 AGENTS.md /home/coder/.config/opencode/AGENTS.md

# Phase 3: the preview server (each chat's project, live, in a panel next to the chat). The secret signs the
# per-chat links; the Code pipe's PREVIEW_SECRET valve must hold the same value.
if [ ! -f /etc/code-mode/preview.env ]; then
    ( umask 027; printf 'PREVIEW_SECRET=%s\n' "$(openssl rand -hex 32)" > /etc/code-mode/preview.env )
    chown root:coder /etc/code-mode/preview.env
fi
if [ -f code-preview.py ] && [ -f code-preview.service ]; then
    # coder-owned so the bug fixer can redeploy it; the repo on the VM is the source of truth, so never overwrite
    install -d -o coder -g coder -m 0755 /home/coder/.local /home/coder/.local/lib /home/coder/.local/lib/code-mode
    [ -f /home/coder/.local/lib/code-mode/code-preview.py ] || \
        install -o coder -g coder -m 0644 code-preview.py /home/coder/.local/lib/code-mode/code-preview.py
    install -m 0644 code-preview.service /etc/systemd/system/code-preview.service
    systemctl daemon-reload
    systemctl enable code-preview.service
    systemctl restart code-preview.service
fi
cat > /etc/systemd/system/code-mode-firewall.service <<'EOF'
[Unit]
Description=Code mode - keep the coder user off the LAN and fence the opencode port
Before=opencode-code.service
After=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/code-mode/firewall.nft
# Rule changes: `systemctl reload code-mode-firewall` (a restart would also restart opencode-code)
ExecReload=/usr/sbin/nft -f /etc/code-mode/firewall.nft
ExecStop=/usr/sbin/nft delete table inet code_mode

[Install]
WantedBy=multi-user.target
EOF
install -m 0644 opencode-code.service /etc/systemd/system/opencode-code.service

systemctl daemon-reload
systemctl enable --now code-mode-firewall.service
systemctl reload code-mode-firewall.service
systemctl enable opencode-code.service
systemctl restart opencode-code.service
# Phase 4: the bug fixer (code-fixer.service on :4098 + report-bug); fixer-prep sets up sudoers and its secrets.
if [ -f fixer-prep.sh ] && [ -f install-fixer.sh ] && [ -f code-fixer.py ]; then
    bash fixer-prep.sh
    bash install-fixer.sh
fi
echo "installed opencode $VERSION for coder"
