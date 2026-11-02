"""The question dialog (pipe 0.17.0). opencode's `question` tool reaches the user as Open WebUI's input dialog, one
per question. Confirm with nothing typed means the first option (Open WebUI answers that with True). In the ask turn
the look is not left to the agent's wording: its own question about it is replaced by the fixed design question, or
that question is added when the request is for something with a screen."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "owui"))
from code_mode import DESIGN_QUESTION, Pipe, build_now

DATA = {"key": "q0", "title": "Data", "description": "Which data?", "options": [
    {"label": "Kp and solar wind", "value": "Kp and solar wind"}, {"label": "Everything", "value": "Everything"}]}
USERS = {"key": "q1", "title": "Users", "description": "Who is it for?", "options": [
    {"label": "Just me", "value": "Just me"}, {"label": "Visitors", "value": "Visitors"}]}
LOOK = {"key": "q2", "title": "Design direction", "description": "How should the dashboard look?", "options": [
    {"label": "Dark space theme (Recommended)", "value": "Dark space theme"}, {"label": "Clean light theme", "value": "light"}]}


class Client:
    def __init__(self):
        self.posts, self.deletes = [], []

    async def post(self, path, json=None):
        self.posts.append((path, json))

    async def delete(self, path):
        self.deletes.append(path)


def ask(fields, replies, design=None):
    client, shown = Client(), []

    async def call(event):
        shown.append(event["data"])
        return replies.pop(0)

    async def status(text, done=False):
        pass

    text, ok = asyncio.run(Pipe()._form(client, "ses_1", {"id": "frm_1", "fields": fields}, call, status, design))
    return client, shown, text, ok


def test_empty_confirm_takes_the_first_option_and_the_dialog_says_so():
    client, shown, text, ok = ask([DATA, USERS], [True, "visitors"])  # True = Confirm with the field left empty
    assert ok
    assert client.posts == [("/api/session/ses_1/form/frm_1/reply", {"answer": {"q0": "Kp and solar wind", "q1": "Visitors"}})]
    assert "Kp and solar wind" in shown[0]["message"] and "empty" in shown[0]["message"].lower()
    assert "**Kp and solar wind**" in text


def test_a_number_or_the_start_of_an_option_picks_it_and_anything_else_is_taken_as_written():
    client, _, _, ok = ask([DATA, USERS], ["2", "just"])
    assert ok and client.posts[0][1] == {"answer": {"q0": "Everything", "q1": "Just me"}}
    client, _, _, ok = ask([DATA, USERS], ["only the aurora forecast", "  "])
    assert ok and client.posts[0][1] == {"answer": {"q0": "only the aurora forecast", "q1": "Just me"}}


def test_cancel_or_no_browser_leaves_it_unanswered_and_lists_the_questions():
    for reply in (False, None):
        client, _, text, ok = ask([DATA, USERS], [reply])
        assert not ok and client.posts == [] and client.deletes == ["/api/session/ses_1/form/frm_1"]
        assert "Not answered yet" in text and "1. Which data? (Kp and solar wind / Everything)" in text
        assert "2. Who is it for?" in text


def test_ask_turn_the_agents_own_look_question_is_replaced_by_the_design_question():
    design = {"ui": True, "choice": None}
    client, shown, text, ok = ask([DATA, LOOK], [True, True], design)
    assert ok and len(shown) == 2
    assert shown[1]["title"] == "Design" and shown[1]["message"].startswith(DESIGN_QUESTION)
    assert "Dark space theme" not in shown[1]["message"], "the agent's own style must not be offered as the default"
    assert design["choice"] == "show"
    assert "three design directions" in client.posts[0][1]["answer"]["q2"]


def test_ask_turn_the_design_question_is_added_when_there_is_a_screen_and_the_agent_asked_nothing_about_it():
    design = {"ui": True, "choice": None}
    client, shown, text, ok = ask([DATA], [True, "2"], design)
    assert ok and len(shown) == 2 and design["choice"] == "pick"
    assert client.posts[0][1] == {"answer": {"q0": "Kp and solar wind"}}
    design = {"ui": False, "choice": None}  # a script, a spreadsheet: nothing to design
    _, shown, _, ok = ask([DATA], [True], design)
    assert ok and len(shown) == 1 and design["choice"] is None


def test_outside_the_ask_turn_a_look_question_is_the_agents_own():
    client, shown, _, ok = ask([LOOK], [True])
    assert ok and shown[0]["title"] == "Design direction" and client.posts[0][1] == {"answer": {"q2": "Dark space theme"}}


def test_what_the_build_is_told():
    assert "ideas board" in build_now("show") and "Do not build the real page" in build_now("show")
    assert "pick the design" in build_now("pick")
    assert "minimal, black, hairlines" in build_now("minimal, black, hairlines")
    assert "Build it now" in build_now(None) and "ideas board" not in build_now(None)
