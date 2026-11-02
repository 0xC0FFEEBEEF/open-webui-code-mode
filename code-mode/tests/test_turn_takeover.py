"""A message sent while the chat's reply still streams: the newest turn drives the chat's one opencode session, the
older reply ends with a note and starts nothing more (pipe 0.29.1).

Why: on 2026-10-27 the user sent two messages while a turn ran. Open WebUI started a Code turn for each, all on the
chat's one session, so two turns followed one run: each sent its own automatic check, and when the run ended each
started its own reviewer on the one model slot. The older reviewer ran on for its full 15 minutes after its reply
was over ("one of which is still actively running")."""
import asyncio
import os
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "owui"))
sys.path.insert(0, here)
import code_mode  # noqa: E402
from test_review import all_sessions, chat_setup  # noqa: E402


def two_turns(pipe, first, second, gap):
    """The first message's turn, and `gap` seconds later the second's, both running at once as Open WebUI runs them."""
    async def one(messages, delay):
        await asyncio.sleep(delay)
        out, t0 = [], time.monotonic()

        async def emit(e):
            pass
        async for chunk in pipe._run(messages, "chat1", {"id": "m", "providerID": "slerm"}, emit, None,
                                     picked="slerm-turing.m"):
            out.append(chunk)
        return "".join(out), time.monotonic() - t0 + delay

    async def go():
        return await asyncio.gather(one(first, 0), one(second, gap))
    return asyncio.run(asyncio.wait_for(go(), 60))


def builder(seen, slow_first=0.0, review_sleep=0.0):
    def script(session, text):
        if session["agent"] == "review":
            seen["reviews"].append(text)
            return ([("sleep", review_sleep)] if review_sleep else []) + [("text", "REVIEW: PASS")]
        seen["builder"].append(text)
        n = len(seen["builder"])
        path = os.path.join(session["directory"], "app.py")

        def edit():
            with open(path, "a") as f:
                f.write("".join("# change %d line %d\n" % (n, i) for i in range(40)))
        return ([("sleep", slow_first)] if n == 1 and slow_first else []) + [
            ("do", edit), ("tool", "edit", {"filePath": path}, "ok"), ("text", "Done %d." % n)]
    return script


FIRST = [{"role": "user", "content": "add a settings page"}]
SECOND = FIRST + [{"role": "assistant", "content": ""}, {"role": "user", "content": "and make it dark"}]


def test_a_message_sent_mid_turn_takes_the_run_over_and_only_it_reviews(tmp_path):
    seen = {"reviews": [], "builder": []}
    pipe, fake, _ = chat_setup(tmp_path, builder(seen, slow_first=2.0))
    try:
        (older, _), (newer, _) = two_turns(pipe, list(FIRST), list(SECOND), 0.7)
    finally:
        fake.stop()
    assert "Your next message reached the agent while this was running" in older
    assert "**Reviewed" not in older and "Review" not in older.split("Your next message")[1]
    assert "Your next message" not in newer and "**Reviewed" in newer
    assert len(seen["reviews"]) == 1, "one reviewer, the newest turn's"
    assert "and make it dark" in seen["reviews"][0]
    assert sum(1 for s in all_sessions(fake) if s["agent"] == "review") == 1
    own = [s for s in all_sessions(fake) if (s.get("metadata") or {}).get("owui_chat_id") == "chat1"]
    assert len(own) == 1 and len(own[0]["prompts"]) == 2, "no check or send-back from the older turn"
    assert "chat1" not in code_mode.CHAT_TURNS, "the newest turn clears its mark when it ends"


def test_a_review_round_stops_when_the_next_message_comes(tmp_path):
    """The older turn is reviewing when the next message arrives: its reviewer stops within seconds instead of running
    to REVIEW_MINUTES, and its reply ends with the note, not a review verdict."""
    seen = {"reviews": [], "builder": []}
    pipe, fake, _ = chat_setup(tmp_path, builder(seen, review_sleep=25.0))
    try:
        (older, older_s), (newer, _) = two_turns(pipe, list(FIRST), list(SECOND), 1.5)
    finally:
        fake.stop()
    assert len(seen["reviews"]) >= 1 and "and make it dark" not in seen["reviews"][0]
    assert older_s < 12, f"the older reply ended {older_s:.1f} s in: its reviewer was not stopped"
    assert "Your next message reached the agent while this was running" in older and "**Reviewed" not in older
