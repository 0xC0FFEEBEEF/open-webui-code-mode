#!/bin/sh
# Install code-claude on cryoai (run there as owui, from this folder). The unit needs root once:
#   sudo install -m 0644 code-claude.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now code-claude
# Config: ~/.config/code-claude/env (0600) with FIXER_URL and FIXER_TOKEN (the token from /etc/code-mode/fixer.env on VM 129).
# Claude Code itself: ~/.local/bin/claude, signed in once with `claude` -> /login.
set -e
mkdir -p "$HOME/code-claude/runs" "$HOME/.config/code-claude"
install -m 0755 runner.py "$HOME/code-claude/runner.py"
install -m 0644 CLAUDE.md "$HOME/code-claude/CLAUDE.md"
chmod 0700 "$HOME/.config/code-claude"
[ -f "$HOME/.config/code-claude/env" ] || { echo "missing ~/.config/code-claude/env (FIXER_URL, FIXER_TOKEN)" >&2; exit 1; }
echo "installed to $HOME/code-claude"
