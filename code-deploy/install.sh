#!/bin/bash
# Install code-deploy (the Features page + Open WebUI-side installer) on cryoai. Run as owui from this directory,
# with the service in src/ (code-deploy.py, page.html, code-deploy.service):
#   ssh -i ~/.ssh/id_ed25519 owui@ai-sandbox 'sudo cat /etc/code-mode/features.env' | ssh cryoai 'bash ~/cd-install/install-deploy.sh'
# stdin carries FEATURES_TOKEN=... from the VM (written to a 0600 env file, never printed). The systemd unit needs
# root: hl-sudo cryoai "install -m 0644 /home/owui/cd-install/src/code-deploy.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now code-deploy"
set -euo pipefail
cd "$(dirname "$0")"
install -d -m 0700 ~/.local/lib/code-deploy ~/.config/code-deploy ~/.local/state/code-deploy
install -m 0644 src/code-deploy.py src/page.html ~/.local/lib/code-deploy/
if [ ! -t 0 ]; then
    token=$(grep -m1 '^FEATURES_TOKEN=' || true)
    if [ -n "$token" ]; then
        ( umask 077
          { echo "# code-deploy settings (see the brief); the token is the VM's /etc/code-mode/features.env"
            echo "FEATURES_URL=http://ai-sandbox:4100"
            echo "$token"; } > ~/.config/code-deploy/env )
        echo "env written (token not shown)"
    fi
fi
python3 -m py_compile ~/.local/lib/code-deploy/code-deploy.py && echo "code-deploy.py compiles"
