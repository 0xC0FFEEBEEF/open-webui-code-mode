"""Ask first: the first Code turn of a chat runs on the `ask` agent, whose only tool is the question dialog (or it
says READY). Why an agent and not a rule: on 2026-10-13 Qwen3.6 Q5 read the rule in AGENTS.md, and then the same
rule next to the user's message, and went straight to work both times; with nothing else to do, it asks."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "owui"))
from code_mode import ask_first, is_ready


def test_first_turn_of_a_chat_asks():
    assert ask_first("I want a dashboard for space weather. Make it look good.", 0, None)


def test_not_on_later_turns_or_in_feature_chats():
    assert not ask_first("make the header smaller", 3, None)
    assert not ask_first("add a GitHub button", 0, {"n": 8})


def test_not_when_the_user_says_not_to_ask():
    for said in ("Build a clock page. No questions, just build it.", "don't ask, pick sensible defaults",
                 "Do not ask me anything", "build it without asking"):
        assert not ask_first(said, 0, None), said


def test_ready_means_build_now():
    assert is_ready("READY: a clock page with the current time in large type")
    assert is_ready("  **READY:** a clock page")
    assert not is_ready("I'll build a dashboard. A few choices:\n1. Which data? I'd use SWPC.")
    assert not is_ready("")
