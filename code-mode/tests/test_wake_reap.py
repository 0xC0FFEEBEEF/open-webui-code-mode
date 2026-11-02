"""`later` wake-up turns (code-wake sends code_wake: true) must not stop other chats' servers: the user did not
start that turn, and on 2026-10-20 a wake in one chat would otherwise have killed the app open in another's Preview."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "owui"))
from code_mode import reap_command

ROOT, KEEP, PREVIEW = "/home/coder/projects", "abc-123", "/p/abc-123/" + "0" * 32 + "/"


def test_normal_turn_reaps_and_notes_the_preview():
    cmd = reap_command(ROOT, KEEP, PREVIEW)
    assert "kill $pid" in cmd and "/chats/abc-123|" in cmd
    assert "printf %%s '%s'" % PREVIEW in cmd


def test_wake_turn_only_notes_the_preview():
    cmd = reap_command(ROOT, KEEP, PREVIEW, kill=False)
    assert "kill" not in cmd
    assert cmd.startswith('d="${HOME:-/home/coder}/.local/state/code-mode/preview"')


def test_wake_turn_without_a_preview_does_nothing():
    assert reap_command(ROOT, KEEP, "", kill=False) == ""
