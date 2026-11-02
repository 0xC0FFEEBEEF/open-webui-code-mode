"""The end-of-turn review: a turn that changed files is checked by a separate session against what the user asked
for; findings go back to the builder once, its fixes are reviewed again, and what is still open shows in the reply.

Why: on 2026-10-13 a build passed its own checks while it had made up three rooms, dropped the old page's menu,
left its test clicks behind, never built the part that was asked for and parked "could not test" at the bottom. The
user said none of it in the chat. tests/fake_opencode.py plays opencode (sessions, the event stream, real shell
commands), so the flow runs here without a model."""
import asyncio
import json
import os
import subprocess
import sys

import httpx

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "owui"))
sys.path.insert(0, here)
from code_mode import Pipe, REVIEW_REF, parse_review, review_fix_prompt, review_note, review_prompt  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402

FAIL = ("REVIEW: FAIL\n"
        "- [invented] config.yaml seeds three rooms nobody asked for — config.yaml:36\n"
        "- [removed] the sidebar's New session button is gone — templates/index.html (old line 47)\n")


# ---- reading the reviewer's reply

SHEET = """ASKED | improve the ui | built | templates/index.html rewritten
ASKED | conversations carry on between the NPCs in a room | half built | app.py:399 only player to one NPC
HAD | NPC picker (#npcSelect) | gone | no select in the new page
HAD | Random NPC (#btnRandom) | gone | nothing replaces it
HAD | New session (#btnNewSession) | gone | nothing replaces it
HAD | sidebar toggle | gone | the sidebar itself is gone
HAD | Settings (#btnSettings) | hard to find | a bare icon, templates/index.html:28
HAD | /api/message | still there | app.py:399
HAD | chat box (#messageInput) | replaced | each room has its own box now, templates/index.html:60
ADDED | the default voice "en_US-libritts-high" | already there | config.yaml:24 before and after
ADDED | rooms main_hall, back_room, town_square | not asked for | config.yaml:36-50
TESTING | room guard_tower_15f7ad in the running app | left behind | curl api/state lists it, no file defines it
TESTING | /tmp/final-check.png | cleaned up | not in the project
CLAIM | "desktop verify passes with zero errors" | false | the last verify failed
CLAIM | "Ollama isn't running, NPC replies not tested" | admits it was not done | summary, last paragraph
CLAIM | "the dropdown opens" | true | static/app.js:560
CLAIM | "it looks clean" | could not check | no screenshot here
REVIEW: PASS"""


def test_the_worksheet_decides_whatever_the_last_line_says():
    passed, found = parse_review(SHEET)
    assert passed is False
    kinds = [f.split("]")[0] + "]" for f in found]
    assert kinds == ["[removed]", "[invented]", "[left behind]", "[given up]", "[not built]", "[not true]"]
    assert found[0].startswith("[removed] 5 things: NPC picker (#npcSelect) (gone)") and "Settings (#btnSettings) (hard to find)" in found[0]
    assert found[1] == "[invented] rooms main_hall, back_room, town_square (not asked for) - config.yaml:36-50"
    assert "guard_tower_15f7ad" in found[2] and "Ollama" in found[3] and "half built" in found[4] and "zero errors" in found[5]
    for fine in ("/api/message", "final-check", "dropdown opens", "messageInput", "libritts", "looks clean"):
        assert not any(fine in f for f in found), fine


def test_made_up_content_is_a_finding():
    passed, found = parse_review('ADDED | rooms "Kitchen" and "Hall" | made up | config.yaml:36; the user named no rooms\nREVIEW: FAIL')
    assert passed is False and found[0].startswith('[invented] rooms "Kitchen" and "Hall" (made up) - config.yaml:36')
    assert parse_review('ADDED | units g and ml | user\'s own words | "amounts in g and ml"\nREVIEW: PASS') == (True, [])


def test_a_worksheet_with_nothing_wrong_passes_and_a_table_is_read_too():
    assert parse_review("ASKED | a clock | built | index.html:10\nHAD | nothing before | still there | -\nREVIEW: PASS") == (True, [])
    assert parse_review("ASKED | a clock | built | index.html:10") == (None, [])
    table = ("| Tag | Item | Status | Evidence |\n|---|---|---|---|\n| **ASKED** | dark mode | missing | no toggle anywhere |\n"
             "| HAD | Export button | still there | list.html:40 |\n\n**REVIEW: FAIL**")
    assert parse_review(table) == (False, ["[not built] dark mode (missing) - no toggle anywhere"])


def test_findings_only_in_its_own_list_still_count():
    passed, found = parse_review("ASKED | a clock | built | index.html:10\n- [left behind] debug prints in app.js\nREVIEW: FAIL")
    assert passed is False and found == ["[left behind] debug prints in app.js"]


def test_pass_and_fail_are_read():
    assert parse_review("REVIEW: PASS") == (True, [])
    passed, found = parse_review(FAIL)
    assert passed is False
    assert found[0].startswith("[removed] the sidebar")
    assert found[1].startswith("[invented] config.yaml seeds three rooms")


def test_markdown_and_thinking_around_the_verdict():
    passed, found = parse_review("<think>REVIEW: PASS maybe?</think>**REVIEW: FAIL**\n1. **[Not built]** NPCs never talk to each other")
    assert passed is False and found == ["[not built] NPCs never talk to each other"]


def test_a_pass_that_lists_findings_is_a_fail():
    passed, found = parse_review("REVIEW: PASS\n- [left behind] a Guard Tower room from testing")
    assert passed is False and len(found) == 1


def test_a_fail_in_its_own_words_keeps_its_bullets():
    passed, found = parse_review("REVIEW: FAIL\n- The settings button has no label\n- Ollama was never tested")
    assert passed is False and found == ["The settings button has no label", "Ollama was never tested"]


def test_no_verdict_is_not_a_pass():
    assert parse_review("I looked around and it seems fine.") == (None, [])
    assert parse_review("") == (None, [])


def test_many_of_a_kind_become_one_line_and_ten_is_the_most():
    found = parse_review("REVIEW: FAIL\n" + "\n".join(f"- [invented] thing {n}" for n in range(30)))[1]
    assert len(found) == 1 and found[0].startswith("[invented] 30 things: thing 0; thing 1;")
    kinds = ["removed", "invented", "left behind", "given up", "not built", "not true"]
    found = parse_review("REVIEW: FAIL\n" + "\n".join(f"- [{k}] {k} {n}" for k in kinds for n in range(3)))[1]
    assert len(found) == 10


# ---- what the reviewer and the builder are told, and what the user reads

def test_the_reviewer_gets_the_users_words_and_the_snapshot():
    p = review_prompt(["improve the ui", "I think I like C"], ["> Three directions? — **Show me three**"],
                      "VERIFY: PASS, all done", ["app.py", "static/app.js"], True, [])
    assert "1. improve the ui" in p and "2. I think I like C" in p
    assert "Show me three" in p and "VERIFY: PASS, all done" in p and "static/app.js" in p
    assert REVIEW_REF in p and "first review" not in p and "worksheet lines" in p


def test_the_reviewer_gets_the_old_controls_and_the_verify_record():
    p = review_prompt(["x"], [], "done", ["index.html"], True, [], "== index.html (before)\n12:<button id=\"btnExport\">Export",
                      "`desktop verify` did NOT pass after the last change (`desktop verify` still fails).")
    assert "What the project had before" in p and 'id="btnExport"' in p
    assert "Code mode's own record" in p and "did NOT pass" in p
    assert "What the project had before" not in review_prompt(["x"], [], "done", [], True, [])


def test_without_a_snapshot_and_on_the_second_round():
    p = review_prompt(["x"], [], "", [], False, ["[invented] three rooms"])
    assert REVIEW_REF not in p and "no snapshot" in p
    assert "first review" in p and "- [invented] three rooms" in p


def test_the_builder_is_told_what_to_do_per_kind():
    p = review_fix_prompt(["[removed] the menu", "[given up] Ollama was not tested"])
    assert "- [removed] the menu" in p and "not a message from the user" in p
    for kind in ("removed:", "invented:", "left behind:", "given up:", "not built:", "not true:"):
        assert kind in p


def test_the_note_in_the_reply():
    assert "Reviewed" in review_note({"passed": True, "findings": []}, 0)
    note = review_note({"passed": False, "findings": ["[invented] three rooms", "[removed] the menu"]}, 1)
    assert "2 things still open" in note and "after 1 automatic send-back" in note and "three rooms" in note
    assert "did not finish" in review_note({"passed": None, "findings": [], "error": "it gave no verdict"}, 0)


# ---- the whole turn, against the fake opencode

def run_turn(tmp_path, script, messages=None, diff=None, agents=("build", "ask", "review"), existing=None, **valves):
    """existing: {file: text} the chat's project already holds when the turn starts."""
    home = tmp_path / "home"
    for name, text in (existing or {}).items():
        path = home / "projects" / "chats" / "chat1" / name
        os.makedirs(path.parent, exist_ok=True)
        path.write_text(text)
    fake = FakeOpencode(home)
    fake.agents, fake.script, fake.diff = set(agents), script, diff or []
    fake.prompt_status = valves.pop("_prompt_status", 200)
    fake.models = valves.pop("_models", fake.models)
    fake.start()
    p = Pipe()
    v = p.valves
    v.OPENCODE_URL, v.OPENCODE_PASSWORD, v.PROJECTS_ROOT = fake.url, "x", str(home / "projects")
    v.ASK_FIRST, v.PREVIEW_SECRET, v.LIVE_URL, v.VIEW_TOKEN, v.FEATURES_TOKEN, v.FIXER_TOKEN = False, "", "", "", "", ""
    for key, value in valves.items():
        setattr(v, key, value)
    os.makedirs(v.PROJECTS_ROOT, exist_ok=True)
    statuses = []
    fake.embeds = []
    fake.status_events = []

    async def emit(e):
        if e["type"] == "status":
            fake.status_events.append(e["data"])
            statuses.append(e["data"]["description"])
        elif e["type"] == "embeds":
            fake.embeds.extend(e["data"]["embeds"])

    async def go():
        out = []
        async for chunk in p._run(messages or [{"role": "user", "content": "add a settings page"}], "chat1",
                                  {"id": "m", "providerID": "slerm"}, emit, None, picked="slerm-turing.m"):
            out.append(chunk)
        return "".join(out)

    try:
        text = asyncio.run(asyncio.wait_for(go(), 60))
    finally:
        fake.stop()
    return text, statuses, fake, str(home / "projects" / "chats" / "chat1")


def builder_and_reviewer(tmp_path, verdicts):
    """A builder that edits app.py on every prompt, and a reviewer that answers with `verdicts` in turn."""
    seen = {"reviews": [], "builder": []}

    def script(session, text):
        if session["agent"] == "review":
            seen["reviews"].append(text)
            return [("tool", "shell", {"command": f"git diff --stat {REVIEW_REF}"}, " app.py | 1 +"),
                    ("text", verdicts[min(len(seen["reviews"]), len(verdicts)) - 1])]
        seen["builder"].append(text)
        path = os.path.join(session["directory"], "app.py")

        def edit():
            with open(path, "a") as f:
                f.write("# change %d\n" % len(seen["builder"]))
        return [("do", edit), ("tool", "edit", {"filePath": path}, "ok"), ("text", "Done: the settings page is in.")]
    return script, seen


def all_sessions(fake):
    """Every session the fake ever held: the live ones and the ones the pipe deleted."""
    return list(fake.sessions.values()) + fake.removed


def sid_of(url) -> str:
    """The session a live window follows (its `?s=`)."""
    import urllib.parse
    return (urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("s") or [""])[0]


def test_findings_go_back_once_and_the_fixes_are_reviewed(tmp_path):
    script, seen = builder_and_reviewer(tmp_path, [FAIL, "REVIEW: PASS"])
    text, statuses, fake, project = run_turn(tmp_path, script)
    assert len(seen["reviews"]) == 2 and len(seen["builder"]) == 2
    assert "add a settings page" in seen["reviews"][0] and "Done: the settings page is in." in seen["reviews"][0]
    assert "[invented] config.yaml seeds three rooms" in seen["builder"][1]          # the send-back names the findings
    assert "first review" in seen["reviews"][1] and "[removed] the sidebar" in seen["reviews"][1]
    assert "**Reviewed:**" in text and "still open" not in text
    assert any(s.startswith("Review: 2 things to fix") for s in statuses)
    assert any(s.startswith("Reviewing · shell: git diff --stat") for s in statuses)
    assert statuses[-1].endswith("· reviewed")
    assert sum(1 for s in fake.sessions.values() if s["agent"] == "review") == 2       # kept after the turn: the window replays until the next message
    assert not [s for s in fake.removed if s["agent"] == "review"]                       # the turn removes no review session of its own
    # the project as it was before the turn is a commit of its own, and the user's index was left alone
    old = subprocess.run(["git", "-C", project, "show", f"{REVIEW_REF}:app.py"], capture_output=True, text=True)
    assert old.returncode != 0                                                         # app.py did not exist before
    assert subprocess.run(["git", "-C", project, "rev-parse", "-q", "--verify", REVIEW_REF], capture_output=True).returncode == 0
    assert subprocess.run(["git", "-C", project, "status", "--short"], capture_output=True, text=True).stdout.strip() == "?? app.py"
    log = os.path.join(str(tmp_path), "home", ".local", "state", "code-mode", "review", "chat1.jsonl")
    rounds = [json.loads(line) for line in open(log)]
    assert [r["passed"] for r in rounds] == [False, True] and len(rounds[0]["findings"]) == 2


def test_each_send_back_gets_its_own_live_window(tmp_path):
    """The live window follows one prompt and folds when its run ends; a send-back is a new prompt in the same reply,
    so it gets a fresh window, while the review still diffs from the user's message (2026-10-17: the bar sat at
    24m 15s while the agent kept working after an automatic check)."""
    import urllib.parse
    script, seen = builder_and_reviewer(tmp_path, [FAIL, "REVIEW: PASS"])
    text, statuses, fake, _ = run_turn(tmp_path, script, LIVE_URL="http://live:4099", PREVIEW_SECRET="s")
    reviews = {s["id"] for s in all_sessions(fake) if s["agent"] == "review"}
    windows = [u for u in fake.embeds if isinstance(u, str) and u.startswith("http://live:4099/w/chat1/")
               and sid_of(u) not in reviews]                                          # the builder's own windows
    prompts = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["m"][0] for u in windows]
    assert len(windows) == 2 and len(set(prompts)) == 2, fake.embeds
    assert "[invented] config.yaml seeds three rooms" in seen["builder"][1]          # the send-back itself is unchanged
    assert len(seen["reviews"]) == 2                                                  # and its fixes were reviewed


def test_every_review_round_is_watched_in_its_own_live_window(tmp_path):
    """The reviewer is watchable: each round opens a live window of its own the moment it starts (so the user sees it
    read files and run `git diff`), the window is signed for the chat it reviews, and the session says which chat and
    which round it is (`owui_review_of`, `owui_review_round`, never `owui_chat_id`)."""
    script, seen = builder_and_reviewer(tmp_path, [FAIL, "REVIEW: PASS"])
    text, statuses, fake, _ = run_turn(tmp_path, script, LIVE_URL="http://live:4099", PREVIEW_SECRET="s")
    reviews = {s["id"]: s for s in all_sessions(fake) if s["agent"] == "review"}
    assert len(reviews) == 2, all_sessions(fake)
    windows = [u for u in fake.embeds if isinstance(u, str) and u.startswith("http://live:4099/w/chat1/")]
    watched = [u for u in windows if sid_of(u) in reviews]
    assert {sid_of(u) for u in watched} == set(reviews), "one window per round, on its own review session"
    assert all("?s=" in u and "&m=msg_1" in u for u in watched), "each window follows the review's own prompt"
    kinds = ["review" if sid_of(u) in reviews else "build" for u in windows]
    assert kinds == ["build", "review", "build", "review"], "the windows come as the rounds come: %r" % windows
    for sid, s in reviews.items():
        assert s["metadata"].get("owui_review_of") == "chat1" and "owui_chat_id" not in s["metadata"], s["metadata"]
    assert sorted(s["metadata"].get("owui_review_round") for s in reviews.values()) == [1, 2]


def chat_setup(tmp_path, script, **valves):
    """A fake opencode and a pipe set up for SEVERAL Code turns in one chat (the way Open WebUI keeps one pipe
    between the turns of a chat). Returns (pipe, fake, the chat's project folder)."""
    home = tmp_path / "home"
    fake = FakeOpencode(home)
    fake.agents, fake.script = {"build", "ask", "review"}, script
    fake.start()
    p = Pipe()
    v = p.valves
    v.OPENCODE_URL, v.OPENCODE_PASSWORD, v.PROJECTS_ROOT = fake.url, "x", str(home / "projects")
    v.ASK_FIRST, v.PREVIEW_SECRET, v.LIVE_URL, v.VIEW_TOKEN, v.FEATURES_TOKEN, v.FIXER_TOKEN = False, "", "", "", "", ""
    for key, value in valves.items():
        setattr(v, key, value)
    os.makedirs(v.PROJECTS_ROOT, exist_ok=True)
    return p, fake, os.path.join(v.PROJECTS_ROOT, "chats", "chat1")


def chat_turn(pipe, fake, messages):
    """One Code turn on a pipe and a fake prepared by chat_setup; keeps the chat's history as Open WebUI does."""
    async def go():
        out = []

        async def emit(e):  # the pipe awaits every emit
            pass
        async for chunk in pipe._run(list(messages), "chat1", {"id": "m", "providerID": "slerm"}, emit, None,
                                     picked="slerm-turing.m"):
            out.append(chunk)
        return "".join(out)
    text = asyncio.run(asyncio.wait_for(go(), 60))
    messages.append({"role": "assistant", "content": text})
    return text


def test_the_next_turn_clears_the_older_review_sessions_only(tmp_path):
    """C: the next message in the chat clears the review sessions of the earlier turns - their window has been
    replayable until now. And only those: not this turn's review session, not the chat's own session, not a session
    in the same folder that reviews a different chat."""
    script, seen = builder_and_reviewer(tmp_path, [FAIL, "REVIEW: PASS", "REVIEW: PASS"])
    pipe, fake, folder = chat_setup(tmp_path, script)
    try:
        messages = [{"role": "user", "content": "add a settings page"}]
        chat_turn(pipe, fake, messages)
        first = {s["id"] for s in all_sessions(fake) if s["agent"] == "review"}
        assert len(first) == 2 and not [s for s in fake.removed if s["agent"] == "review"]
        # a review session of another chat, sitting in this chat's folder: the clear must leave it alone
        other = httpx.post(fake.url + "/api/session", auth=("opencode", "x"), timeout=10,
                           json={"location": {"directory": folder},
                                 "metadata": {"owui_review_of": "chat2"}}).json()["data"]["id"]
        messages.append({"role": "user", "content": "now add a dark mode"})
        chat_turn(pipe, fake, messages)
        assert {s["id"] for s in fake.removed if s["agent"] == "review"} == first, \
            "the earlier rounds' sessions go, and only they do"
        assert not [s for s in fake.removed if (s.get("metadata") or {}).get("owui_chat_id")], \
            "the chat's own session is never removed"
        now = {s["id"] for s in fake.sessions.values() if s["agent"] == "review"}
        assert len(now) == 1 and not (now & first), "this turn's review session stays for its replay"
        assert other in fake.sessions, "a review session of another chat is not this chat's to clear"
        own = [s for s in fake.sessions.values() if (s.get("metadata") or {}).get("owui_chat_id") == "chat1"]
        assert len(own) == 1, "both turns ran in the chat's one session"
        assert seen["builder"][0].startswith("add a settings page") and seen["builder"][-1].startswith("now add a dark mode")
    finally:
        fake.stop()


def test_a_clear_that_fails_is_logged_and_the_turn_goes_on(tmp_path, capsys, monkeypatch):
    """C: clearing the older review sessions is a courtesy, not a step the turn depends on: when opencode will not
    list them the turn still builds, reviews and answers - and the log line never repeats what the valves hold."""
    script, seen = builder_and_reviewer(tmp_path, ["REVIEW: PASS"])
    real = httpx.AsyncClient.get
    listing = {"n": 0}

    async def get(self, url, **kw):
        if url == "/api/session" and (kw.get("params") or {}).get("directory"):
            listing["n"] += 1
            if listing["n"] == 2:  # the first is the pipe finding the chat's own session; the second is its clear
                raise httpx.ConnectError("opencode would not answer")
        return await real(self, url, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    text, statuses, fake, _ = run_turn(tmp_path, script, OPENCODE_PASSWORD="a-token-the-log-must-not-carry")
    fake.stop()
    assert len(seen["reviews"]) == 1 and "**Reviewed:**" in text, text
    log = capsys.readouterr().out
    assert "review sessions" in log and "ConnectError" in log, log
    assert "a-token-the-log-must-not-carry" not in log and "opencode would not answer" not in log


def test_what_is_still_open_is_shown_and_it_is_sent_back_only_once(tmp_path):
    script, seen = builder_and_reviewer(tmp_path, [FAIL, "REVIEW: FAIL\n- [removed] the sidebar's New session button is still gone"])
    text, statuses, _, _ = run_turn(tmp_path, script)
    assert len(seen["reviews"]) == 2 and len(seen["builder"]) == 2
    assert "Review: 1 thing still open" in text and "after 1 automatic send-back" in text
    assert "New session button is still gone" in text
    assert statuses[-1].endswith("· review: 1 open")


def test_a_clean_review_ends_the_turn(tmp_path):
    script, seen = builder_and_reviewer(tmp_path, ["REVIEW: PASS"])
    text, statuses, _, _ = run_turn(tmp_path, script)
    assert len(seen["reviews"]) == 1 and len(seen["builder"]) == 1 and "**Reviewed:**" in text


def test_a_turn_that_changed_nothing_is_not_reviewed(tmp_path):
    seen = []

    def script(session, text):
        seen.append(session["agent"])
        return [("tool", "read", {"filePath": "README.md"}, "..."), ("text", "It reads the config at start-up.")]
    text, statuses, _, _ = run_turn(tmp_path, script)
    assert seen == ["build"] and "Review" not in text and "review" not in statuses[-1]


def test_switched_off(tmp_path):
    script, seen = builder_and_reviewer(tmp_path, [FAIL])
    text, _, _, project = run_turn(tmp_path, script, AUTO_REVIEW=0)
    assert not seen["reviews"] and "Review" not in text
    assert subprocess.run(["git", "-C", project, "rev-parse", "-q", "--verify", REVIEW_REF], capture_output=True).returncode != 0


def test_an_ideas_board_is_not_reviewed(tmp_path):
    script, seen = builder_and_reviewer(tmp_path, [FAIL])
    text, _, _, _ = run_turn(tmp_path, script, diff=[{"file": "ideas/index.html", "additions": 400},
                                                     {"file": "ideas/a.html", "additions": 300}])
    assert not seen["reviews"] and "Review" not in text


def test_a_small_tweak_is_not_reviewed_but_a_real_change_is(tmp_path):
    script, seen = builder_and_reviewer(tmp_path, ["REVIEW: PASS"])
    text, _, _, _ = run_turn(tmp_path / "small", script, diff=[{"file": "styles.css", "additions": 3, "deletions": 2}])
    assert not seen["reviews"] and "Review" not in text
    text, _, _, _ = run_turn(tmp_path / "big", script, diff=[{"file": "app.py", "additions": 80, "deletions": 45}])
    assert len(seen["reviews"]) == 1 and "app.py" in seen["reviews"][0] and "**Reviewed:**" in text
    script, seen = builder_and_reviewer(tmp_path, ["REVIEW: PASS"])
    run_turn(tmp_path / "every", script, diff=[{"file": "styles.css", "additions": 1}], REVIEW_MIN_LINES=0)
    assert len(seen["reviews"]) == 1


def test_a_review_that_breaks_does_not_break_the_turn(tmp_path):
    def script(session, text):
        if session["agent"] == "review":
            return [("fail", "model not loaded")]
        path = os.path.join(session["directory"], "app.py")
        return [("do", lambda: open(path, "a").close()), ("tool", "edit", {"filePath": path}, "ok"), ("text", "Done.")]
    text, statuses, _, _ = run_turn(tmp_path, script)
    assert "Done." in text and "Review did not finish" in text and "model not loaded" in text
    assert statuses[-1].endswith("· not reviewed")


def test_no_review_agent_installed(tmp_path):
    script, seen = builder_and_reviewer(tmp_path, [FAIL])
    text, _, _, _ = run_turn(tmp_path, script, agents=("build",))
    assert not seen["reviews"] and "Review did not finish" in text and "not installed" in text


def test_the_reviewer_is_handed_what_the_old_page_had(tmp_path):
    seen = []

    def script(session, text):
        if session["agent"] == "review":
            seen.append(text)
            return [("text", "HAD | Export button (#btnExport) | gone | nothing exports any more\nREVIEW: FAIL")]
        if "Independent review" in text:
            return [("text", "It is back.")]
        path = os.path.join(session["directory"], "index.html")

        def rewrite():
            with open(path, "w") as f:
                f.write("<h1>Recipes</h1>\n<button id=\"btnAdd\">Add</button>\n")
        return [("do", rewrite), ("tool", "write", {"filePath": path}, "ok"), ("text", "Redesigned.")]
    old = '<h1>Recipes</h1>\n<button id="btnAdd">Add</button>\n<button id="btnExport">Export</button>\n<a href="about.html">About</a>\n'
    text, statuses, _, project = run_turn(tmp_path, script, existing={"index.html": old}, AUTO_VERIFY=0)
    assert 'id="btnExport"' in seen[0] and "about.html" in seen[0] and "== index.html (before)" in seen[0]
    assert "Review: 1 thing still open" in text and "Export button" in text      # the builder changed nothing after it
    assert subprocess.run(["git", "-C", project, "show", f"{REVIEW_REF}:index.html"], capture_output=True, text=True).stdout == old
