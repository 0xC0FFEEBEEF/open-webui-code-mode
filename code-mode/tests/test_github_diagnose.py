"""Diagnose: the GitHub panel's "what is wrong with my project?" check, run for real against the fake GitHub and
the fake sandbox VM (see test_github_attach.py for how the playground is built). Diagnose must explain what it
finds in plain words, carry the action that answers it, and change nothing: no pull, no merge, no push."""
import json
import os

import pytest

from test_github_attach import ADD, DELETE, Env, run  # noqa: E402
from fake_github import TOKEN  # noqa: E402

MAP = "map.py"
MAP_TEXT = "a map of the coast"
MAP_SUBJECT = "add map.py"
MINE = "my own change here"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.fake.stop()
    e.vm.stop()
    assert TOKEN not in json.dumps(e.vm.commands), "the token reached the VM in a command"
    assert TOKEN not in json.dumps(e.answers), "an answer contained the token"
    assert e.key_folders() == [], "a key folder was left on the VM"
    assert all(not keys for keys in e.fake.keys.values()), "a deploy key was left on GitHub"
    assert len([k for k in e.fake.key_log if k[0] == "add"]) == len([k for k in e.fake.key_log if k[0] == "delete"])


@pytest.fixture
def attached(env):
    """A chat working on octo/alpha: a real clone, clean, on main. Its clone key was added and removed."""
    env.chat("chat-1")
    assert env.attach("chat-1", "octo/alpha")["ok"]
    return env.folder("chat-1")


def write(folder, name, text):
    with open(os.path.join(folder, name), "w") as f:
        f.write(text)


def commit(folder, message):
    """A commit in the chat's project, as the agent or the user would make one."""
    run("git", "add", "-A", cwd=folder)
    run("git", "-c", "user.name=octo", "-c", "user.email=o@example.com", "commit", "-q", "-m", message, cwd=folder)


def ids(res):
    return [f["id"] for f in res["findings"]]


def action(res, fid):
    return next(f["action"] for f in res["findings"] if f["id"] == fid)


def text(res, fid):
    return next(f["text"] for f in res["findings"] if f["id"] == fid)


def counts(env, chat="chat-1"):
    """(commits the project has that GitHub does not, the other way round), read straight from the project."""
    ahead, behind = run("git", "rev-list", "--left-right", "--count", "HEAD...origin/main",
                        cwd=env.folder(chat)).split()
    return int(ahead), int(behind)


def bare(env, repo, *args):
    """git, in the repository as GitHub has it."""
    return run("git", *args, cwd=env.remotes + repo + ".git")


def test_a_clone_that_matches_github_says_so(env, attached):
    res = env.op("diagnose", chat_id="chat-1")
    assert res["ok"] and res["linked"] and res["fetched"] is True
    assert res["findings"] == [] and res["summary"] == "The project and GitHub match."
    assert res["brief"].startswith("GitHub check: octo/alpha branch main")
    assert env.fake.key_log == [ADD, DELETE, ADD, DELETE]  # the clone's key, then diagnose's own
    assert all(k[2] is True for k in env.fake.key_log), "diagnose asked for a key that could write"


def test_diagnose_changes_nothing(env, attached):
    write(attached, "notes.md", "half-written\n")
    before = run("git", "rev-parse", "HEAD", cwd=attached)
    res = env.op("diagnose", chat_id="chat-1")
    assert "uncommitted" in ids(res)
    assert run("git", "rev-parse", "HEAD", cwd=attached) == before  # nothing committed or moved
    assert run("git", "status", "--porcelain", cwd=attached) == "?? notes.md"  # and the file is still there
    assert not res["findings"][0]["action"] or action(res, "uncommitted") == ""


def test_commits_github_has_that_this_chat_lacks(env, attached):
    env.push_commit("octo/alpha", MAP, MAP_TEXT, branch="main")
    before = run("git", "rev-parse", "HEAD", cwd=attached)
    res = env.op("diagnose", chat_id="chat-1")
    assert ids(res) == ["behind"] and action(res, "behind") == "pull"
    assert "1 commit(s) on main" in text(res, "behind") and MAP_SUBJECT in text(res, "behind")
    # read-only: nothing was pulled, and the project is where it was
    assert run("git", "rev-parse", "HEAD", cwd=attached) == before
    assert run("git", "status", "--porcelain", cwd=attached) == ""
    assert counts(env) == (0, 1)


def test_a_diverged_branch_names_both_sides_and_the_safe_fix(env, attached):
    write(attached, "notes.md", "my own work\n")
    commit(attached, MINE)
    env.push_commit("octo/alpha", MAP, MAP_TEXT, branch="main")
    res = env.op("diagnose", chat_id="chat-1")
    assert ids(res) == ["diverged"] and action(res, "diverged") == "agent"
    assert MINE in text(res, "diverged") and MAP_SUBJECT in text(res, "diverged")
    assert "Push is refused" in text(res, "diverged")
    brief = res["brief"]
    assert brief.startswith("GitHub check: octo/alpha branch main")
    assert f"- {text(res, 'diverged')}" in brief
    assert "Your commits, not on GitHub: " in brief and "GitHub's commits, not here: " in brief
    assert MINE in brief and MAP_SUBJECT in brief
    assert "git merge origin/main" in brief  # the safe fix the brief offers
    assert "Never rewrite history" in brief and "force-push" in brief
    # the divergence is still there for the agent to fix: nothing was merged, moved or pushed
    assert counts(env) == (1, 1)
    assert bare(env, "octo/alpha", "rev-parse", "main") == run("git", "rev-parse", "origin/main", cwd=attached)


def test_commits_the_project_has_that_github_lacks(env, attached):
    write(attached, "notes.md", "my own work\n")
    commit(attached, MINE)
    res = env.op("diagnose", chat_id="chat-1")
    assert ids(res) == ["ahead"] and action(res, "ahead") == "push"
    assert MINE in text(res, "ahead")
    assert "1 commit(s) not yet on GitHub" in text(res, "ahead")


def test_work_that_is_not_committed_yet(env, attached):
    write(attached, "notes.md", "unfinished\n")
    res = env.op("diagnose", chat_id="chat-1")
    assert ids(res) == ["uncommitted"] and action(res, "uncommitted") == ""
    assert text(res, "uncommitted") == "1 file(s) in the project are not committed yet."


def test_a_project_that_is_not_on_github_yet(env):
    folder = env.chat("chat-1")
    write(folder, "app.py", "print('hello')\n")
    res = env.op("diagnose", chat_id="chat-1")
    assert res["ok"] and not res["linked"] and res["fetched"] is False
    assert ids(res) == ["unlinked"] and action(res, "unlinked") == ""
    assert "not on GitHub yet" in text(res, "unlinked")
    assert res["brief"].startswith("GitHub check: this chat's project is not on GitHub yet")
    assert env.fake.key_log == []  # nothing to compare, so no key and no fetch


def test_an_own_project_given_an_empty_repo_is_ready_to_push(env):
    folder = env.chat("chat-1")
    write(folder, "app.py", "print('hello')\n")
    commit(folder, "first version")
    assert env.attach("chat-1", "octo/gamma")["ok"]  # the pipe's "own project into an empty repo" path
    res = env.op("diagnose", chat_id="chat-1")
    assert res["ok"] and res["repo"] == "octo/gamma"
    assert ids(res) == ["no_branch"] and action(res, "no_branch") == "push"
    assert "Pushing uploads your first commits there." in text(res, "no_branch")


def test_a_detached_head_cannot_be_compared(env, attached):
    run("git", "checkout", "-q", run("git", "rev-parse", "HEAD", cwd=attached), cwd=attached)
    res = env.op("diagnose", chat_id="chat-1")
    assert res["ok"] and ids(res) == ["no_state"] and action(res, "no_state") == "agent"
    assert "not on a branch" in text(res, "no_state")
    assert env.fake.key_log == [ADD, DELETE]  # no branch to fetch, so no new key


def test_when_github_no_longer_has_the_repo(env, attached):
    env.fake.repos = [r for r in env.fake.repos if r["full_name"] != "octo/alpha"]  # deleted on GitHub
    res = env.op("diagnose", chat_id="chat-1")
    assert res["ok"] and ids(res) == ["no_repo"] and action(res, "no_repo") == ""
    assert "nowhere to be pushed" in text(res, "no_repo")
    assert env.fake.key_log == [ADD, DELETE]  # no key was added for a repository that is gone


def test_a_chat_with_no_project_folder(env):
    res = env.op("diagnose", chat_id="never-ran")
    assert not res["ok"] and "no project folder" in res["error"]


def test_an_unknown_chat(env):
    res = env.op("diagnose", chat_id="not a chat id")
    assert not res["ok"] and "chat id" in res["error"]


def test_diagnose_needs_a_signed_in_user(env):
    env.pipe._gh_forget(env.pipe._gh_load())
    res = env.op("diagnose", chat_id="chat-1")
    assert res["signed_out"] and not res["ok"]
