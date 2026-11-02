"""The "Ideas board" link (pipe 0.13.0): a turn that made or changed ideas/index.html ends with a link to the board,
because the Preview panel opens on the project's own page."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "owui"))
from code_mode import ideas_link

BASE = "http://ai-sandbox:4097/p/chat-1/" + "ab" * 16 + "/"


def test_board_changed_gives_a_link_to_it():
    files = [{"file": "ideas/a-panel.html"}, {"file": "ideas/index.html"}]
    assert ideas_link(files, BASE).startswith(f"\n[Ideas board]({BASE}ideas/)\n")
    assert "Which direction" in ideas_link(files, BASE)


def test_absolute_and_dotted_paths_count_too():
    assert ideas_link([{"file": "./ideas/index.html"}], BASE)
    assert ideas_link([{"file": "/home/coder/projects/chats/chat-1/ideas/index.html"}], BASE)


def test_no_link_without_the_board_or_without_a_preview():
    assert ideas_link([{"file": "index.html"}, {"file": "ideas/a-panel.html"}], BASE) == ""
    assert ideas_link([{"file": "docs/ideas/index.html.bak"}, {"file": "myideas/index.html"}], BASE) == ""
    assert ideas_link([{"file": "ideas/index.html"}], "") == ""
    assert ideas_link([], BASE) == "" and ideas_link(None, BASE) == ""
