"""Review policy from the 2026-10-23 performance handoff: a review round's deadline holds while the reviewer keeps
streaming, the reviewer runs at the builder's thinking level, the second round is a recheck and not a new review,
and scratch the reviewer itself calls harmless or outside the project is shown but never sent back."""
import asyncio
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "owui"))
sys.path.insert(0, here)
import code_mode  # noqa: E402
from code_mode import Pipe, harmless_leftover, review_fix_prompt, review_prompt  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402
import test_review as tr  # noqa: E402  (as a module, so pytest does not collect its tests twice)

HARMLESS = ("REVIEW: FAIL\n"
            "TESTING | /tmp/gh-test bare repo from the push test | left behind | harmless scratch outside the repo\n")
REAL = ("REVIEW: FAIL\n"
        "TESTING | room guard_tower_15f7ad in the running app | left behind | curl api/state lists it\n")


# ---- which findings go back

def test_scratch_the_reviewer_calls_harmless_is_not_a_send_back():
    assert harmless_leftover("[left behind] /tmp/gh-test - harmless, outside the repo")
    assert harmless_leftover("[left behind] build log - git-ignored runtime data")


def test_leftovers_that_matter_still_go_back():
    assert not harmless_leftover("[left behind] room guard_tower_15f7ad in the running app")
    assert not harmless_leftover("[left behind] /tmp/creds.json - holds the test token")              # a secret
    assert not harmless_leftover("[left behind] test settings - outside the repo but user-visible")
    assert not harmless_leftover("[left behind] 4 things: /tmp/a; room x; room y; room z")             # merged line
    assert not harmless_leftover("[not true] tests pass - harmless claim? no, pytest fails")           # other kinds
    assert not harmless_leftover("[left behind] /var/tmp/x - it is not harmless")


def test_the_second_round_is_a_recheck():
    p = review_prompt(["x"], [], "", [], False, ["[invented] three rooms"])
    assert "recheck, not a new review" in p and "Do not redo the whole review" in p
    assert "Do the five steps again" not in p and "- [invented] three rooms" in p


def test_the_builder_may_leave_scratch_outside_the_project():
    assert "Scratch outside the project, such as /tmp, can stay." in review_fix_prompt(["[left behind] x"])


# ---- whole turns, against the fake opencode

def run(tmp_path, script, effort=None, **valves):
    home = tmp_path / "home"
    fake = FakeOpencode(home)
    fake.agents, fake.script, fake.diff = {"build", "ask", "review"}, script, []
    fake.start()
    p = Pipe()
    v = p.valves
    v.OPENCODE_URL, v.OPENCODE_PASSWORD, v.PROJECTS_ROOT = fake.url, "x", str(home / "projects")
    v.ASK_FIRST, v.PREVIEW_SECRET, v.LIVE_URL, v.VIEW_TOKEN, v.FEATURES_TOKEN, v.FIXER_TOKEN = False, "", "", "", "", ""
    for key, value in valves.items():
        setattr(v, key, value)
    os.makedirs(v.PROJECTS_ROOT, exist_ok=True)
    fake.embeds, statuses = [], []

    async def emit(e):
        if e["type"] == "status":
            statuses.append(e["data"]["description"])

    async def go():
        out = []
        async for chunk in p._run([{"role": "user", "content": "add a settings page"}], "chat1",
                                  {"id": "m", "providerID": "slerm"}, emit, None, picked="slerm-turing.m",
                                  effort=effort):
            out.append(chunk)
        return "".join(out)

    try:
        text = asyncio.run(asyncio.wait_for(go(), 60))
    finally:
        fake.stop()
    return text, statuses, fake


def test_a_harmless_leftover_ends_the_turn_with_a_note(tmp_path):
    script, seen = tr.builder_and_reviewer(tmp_path, [HARMLESS, "REVIEW: PASS"])
    text, statuses, _ = run(tmp_path, script)
    assert len(seen["builder"]) == 1 and len(seen["reviews"]) == 1                     # no send-back, no 2nd round
    assert "**Reviewed:**" in text and "not sent back" in text and "/tmp/gh-test" in text
    assert statuses[-1].endswith("· reviewed")


def test_a_real_leftover_is_still_sent_back(tmp_path):
    script, seen = tr.builder_and_reviewer(tmp_path, [REAL, "REVIEW: PASS"])
    text, _, _ = run(tmp_path, script)
    assert len(seen["builder"]) == 2 and "guard_tower_15f7ad" in seen["builder"][1]


def test_the_reviewer_thinks_at_the_builders_level(tmp_path):
    script, _ = tr.builder_and_reviewer(tmp_path, ["REVIEW: PASS"])
    _, _, fake = run(tmp_path, script, effort="low")
    models = {s["agent"]: s["model"] for s in tr.all_sessions(fake)}
    assert models["review"].get("variant") == "low", models


def test_with_no_level_picked_the_reviewer_gets_opencodes_default(tmp_path):
    script, _ = tr.builder_and_reviewer(tmp_path, ["REVIEW: PASS"])
    _, _, fake = run(tmp_path, script)
    assert all("variant" not in s["model"] for s in tr.all_sessions(fake) if s["agent"] == "review")


def test_the_review_deadline_holds_while_events_keep_coming(tmp_path, monkeypatch):
    """Before 0.26.0 the deadline was only the get() timeout, so a reviewer that never paused for a second could
    run past REVIEW_MINUTES. Here every look at the clock moves it 40 s on, so the 1-minute round is over after
    two events even though the reviewer is still talking."""
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 40
        return clock["t"]
    monkeypatch.setattr(code_mode, "review_clock", tick)
    talk = "REVIEW: PASS"

    def script(session, text):
        if session["agent"] == "review":
            return [("tool", "shell", {"command": "git diff --stat"}, " app.py | 1 +")] * 4 + [("text", talk)]
        path = os.path.join(session["directory"], "app.py")
        return [("do", lambda: open(path, "a").write("x\n")), ("tool", "edit", {"filePath": path}, "ok"),
                ("text", "Done.")]
    text, _, fake = run(tmp_path, script, REVIEW_MINUTES=1)
    assert "Review did not finish" in text and "did not finish in 1 minutes" in text
    assert "**Reviewed:**" not in text
