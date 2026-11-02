"""The GitHub button's pipe ops, against tests/fake_github.py (which answers the way the real GitHub does).
The page side is clicked through in tests/page/test_github_panel.cjs."""
import asyncio
import json
import os
import stat
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "owui"))
sys.path.insert(0, HERE)
from code_mode import Pipe  # noqa: E402
from fake_github import TOKEN, FakeGitHub  # noqa: E402

ADMIN = {"id": "u1", "role": "admin"}


@pytest.fixture
def gh(tmp_path):
    """gh(op, **args) -> the pipe's answer, the way the page calls it. gh.fake is the fake GitHub, gh.saved() the
    state file. Whatever a test does, no answer may ever contain the token."""
    fake = FakeGitHub().start()
    pipe = Pipe()
    pipe.valves.GITHUB_API = pipe.valves.GITHUB_WEB = fake.url
    pipe.valves.GITHUB_STATE = str(tmp_path / "cfg" / "github.json")
    answers = []

    def call(op, user=ADMIN, **args):
        res = asyncio.run(pipe.pipe({"model": "code_mode.x", "code_github": dict(args, op=op)}, user, None))
        answers.append(res)
        return res

    def saved():
        with open(pipe.valves.GITHUB_STATE) as f:
            return json.load(f)

    call.fake, call.pipe, call.saved = fake, pipe, saved
    yield call
    fake.stop()
    assert TOKEN not in json.dumps(answers), "an op returned the token"


def sign_in(gh):
    assert gh("set_client", client_id=gh.fake.client_id)["ok"]
    assert gh("login_start")["ok"]
    res = gh("login_poll")
    assert res.get("signed_in"), res
    return res


def test_first_run_then_client_id(gh):
    assert gh("status") == {"ok": True, "client_id_set": False, "signed_in": False}
    assert not gh("login_start")["ok"]  # nothing to sign in with yet
    assert not gh("set_client", client_id="")["ok"]
    assert not gh("set_client", client_id="has spaces in it")["ok"]
    assert gh("set_client", client_id="  Iv1.testclient ")["ok"]
    assert gh.saved()["client_id"] == "Iv1.testclient"
    assert gh("status") == {"ok": True, "client_id_set": True, "signed_in": False}


def test_sign_in_waits_at_githubs_pace(gh):
    gh("set_client", client_id=gh.fake.client_id)
    gh.fake.slow_down, gh.fake.pending = 1, 2
    start = gh("login_start")
    assert start["ok"] and start["user_code"] == "WDJB-MJHT" and start["interval"] == 5
    assert start["verification_uri"].endswith("/login/device")
    assert "device_code" not in start  # the page has no use for it
    assert gh("login_poll") == {"ok": True, "pending": True, "interval": 10}  # slow_down: GitHub's new interval
    assert gh("login_poll") == {"ok": True, "pending": True, "interval": 10}
    assert gh("login_poll") == {"ok": True, "pending": True, "interval": 10}
    done = gh("login_poll")
    assert done["ok"] and done["signed_in"] and done["login"] == "octo"
    saved = gh.saved()
    assert saved["access_token"] == TOKEN and saved["client_id"] == gh.fake.client_id
    assert "device_code" not in saved  # the finished sign-in leaves nothing behind
    status = gh("status")
    assert status["signed_in"] and status["login"] == "octo"
    assert not gh("login_poll")["ok"]  # nothing under way any more


@pytest.mark.parametrize("setting, words", [("expire", "expired"), ("deny", "cancelled")])
def test_sign_in_that_does_not_finish(gh, setting, words):
    gh("set_client", client_id=gh.fake.client_id)
    gh("login_start")
    setattr(gh.fake, setting, True)
    res = gh("login_poll")
    assert not res["ok"] and res["restart"] and words in res["error"]
    assert "access_token" not in gh.saved() and "device_code" not in gh.saved()
    assert gh("login_start")["ok"]  # and a new one can start


def test_sign_in_setup_mistakes_get_plain_words(gh):
    gh("set_client", client_id="Iv1.someoneelse")
    res = gh("login_start")
    assert not res["ok"] and res["bad_client"] and "Client ID" in res["error"]
    gh("set_client", client_id=gh.fake.client_id)
    gh.fake.device_flow_enabled = False
    res = gh("login_start")
    assert not res["ok"] and "Enable Device Flow" in res["error"]


def test_state_file_is_private(gh):
    sign_in(gh)
    path = gh.pipe.valves.GITHUB_STATE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert os.listdir(os.path.dirname(path)) == ["github.json"]  # no temp file left behind


def test_repos_span_pages_latest_push_first(gh):
    sign_in(gh)
    gh.fake.max_per_page = 2
    res = gh("repos")
    assert res["ok"] and [r["full_name"] for r in res["repos"]] == ["octo/alpha", "octo/beta", "octo/gamma"]
    assert res["repos"][0] == {"full_name": "octo/alpha", "private": True, "description": "First project",
                               "default_branch": "main", "pushed_at": "2026-10-10T12:00:00Z",
                               "html_url": "https://github.com/octo/alpha"}
    assert res["repos"][2]["description"] == ""  # GitHub's null becomes ""
    pages = [r for r in gh.fake.requests if r["path"] == "/user/repos"]
    assert len(pages) == 2 and pages[0]["query"]["sort"] == "pushed"


def test_create_repo_is_private_unless_unticked(gh):
    sign_in(gh)
    res = gh("create_repo", name="  new-thing ", description="made by a test")
    assert res["ok"] and res["repo"]["full_name"] == "octo/new-thing" and res["repo"]["private"] is True
    assert gh.fake.repo("octo/new-thing")["description"] == "made by a test"
    assert gh("create_repo", name="open-thing", private=False)["repo"]["private"] is False
    assert gh("create_repo", name="odd", private=None)["repo"]["private"] is True  # only an explicit false is public
    taken = gh("create_repo", name="new-thing")
    assert not taken["ok"] and "name already exists on this account" in taken["error"]
    assert not gh("create_repo", name="bad name!")["ok"]
    assert not gh("create_repo", name="")["ok"]


def test_set_visibility(gh):
    sign_in(gh)
    res = gh("set_visibility", full_name="octo/alpha", private=False)
    assert res["ok"] and res["repo"]["private"] is False and gh.fake.repo("octo/alpha")["private"] is False
    assert gh("set_visibility", full_name="octo/alpha", private=True)["repo"]["private"] is True
    assert not gh("set_visibility", full_name="octo/nope", private=True)["ok"]
    before = len(gh.fake.requests)
    for bad in ("alpha", "octo/alpha/extra", "../..", "octo/..", "octo/al pha", ""):
        assert not gh("set_visibility", full_name=bad, private=True)["ok"], bad
    assert not gh("set_visibility", full_name="octo/alpha")["ok"]  # private must be said
    assert len(gh.fake.requests) == before  # none of those reached GitHub


def test_dead_token_signs_out_and_keeps_the_client_id(gh):
    sign_in(gh)
    gh.fake.revoked = True
    res = gh("repos")
    assert not res["ok"] and res["signed_out"]
    assert gh.saved() == {"client_id": gh.fake.client_id}
    assert gh("repos")["signed_out"]  # and without a token nothing is even asked of GitHub
    gh.fake.revoked = False
    sign_in(gh)
    gh.fake.revoked = True
    status = gh("status")  # opening the panel notices it too
    assert status["ok"] and not status["signed_in"] and status["client_id_set"]
    assert gh.saved() == {"client_id": gh.fake.client_id}


def test_logout_forgets_the_token_only(gh):
    sign_in(gh)
    assert gh("logout") == {"ok": True}
    assert gh.saved() == {"client_id": gh.fake.client_id}
    assert gh("status") == {"ok": True, "client_id_set": True, "signed_in": False}


def test_another_client_id_forgets_the_old_sign_in(gh):
    sign_in(gh)
    gh("set_client", client_id="Iv1.another0")
    assert gh.saved() == {"client_id": "Iv1.another0"}


def test_admins_only_and_unknown_ops(gh):
    sign_in(gh)
    for user in ({"id": "u2", "role": "user"}, {}, None):
        res = gh("repos", user=user)
        assert not res["ok"] and "admin" in res["error"]
    for op in ("nope", "", None, "status; rm", "__class__", 7):
        res = gh(op)
        assert isinstance(res, dict) and not res["ok"], op
    res = asyncio.run(gh.pipe.pipe({"model": "code_mode.x", "code_github": "status"}, ADMIN, None))
    assert isinstance(res, dict) and not res["ok"]  # a malformed call still gets a dict, never an exception


def test_github_down_is_an_answer_not_an_exception(gh):
    sign_in(gh)
    gh.fake.stop()
    res = gh("repos")
    assert not res["ok"] and "could not be reached" in res["error"]
    status = gh("status")  # the panel still opens, showing who was signed in
    assert status["ok"] and status["signed_in"] and status["login"] == "octo"
    gh.fake.start()  # for the fixture's stop()
