"""Attached images reach the coding agent (pipe 0.24.0).

A user attached a screenshot to a Code chat. Open WebUI hands it to the pipe as a `files` record whose url names its
own upload storage - a place that does not exist on the coding machine - and the pipe's prompt was text-only, so the
agent got nothing but a file id pointing at nowhere (fixer report 20261019-0744-8042: the agent could not see the
screenshot the user attached). The pipe now hands the bytes over as opencode prompt `files`; checked against the
deployed opencode, a base64 data URL lands on the user message as image media for a vision-capable model.

The harness is the fake opencode the review tests use; its sessions record the `files` list of every prompt in
session["prompt_files"].
"""
import asyncio
import os
import sys
import types

import pytest

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "owui"))
sys.path.insert(0, here)
from code_mode import Pipe, data_image, image_entry  # noqa: E402
from fake_opencode import FakeOpencode  # noqa: E402

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="  # a real 1x1 png
DATA = f"data:image/png;base64,{PNG}"
TAG = '<attached_files>\n<file type="file" url="fid-1" content_type="image/png" name="image.png"/>\n</attached_files>'
RECORD = {"id": "fid-1", "url": "fid-1", "type": "image", "content_type": "image/png", "name": "image.png"}


# ---- the small decoders and the pairing

def test_data_image_and_image_entry():
    assert data_image(DATA) == ("image/png", PNG)
    assert data_image("data:image/webp;base64,AAAA") == ("image/webp", "AAAA")
    assert data_image("a0887bb1-e4de-491a-acd1-db3e9125433f") is None  # an Open WebUI storage id, not bytes
    assert data_image("https://host/x.png") is None  # opencode would not fetch a remote url either
    assert image_entry(RECORD) and image_entry({"content_type": "image/webp"})
    assert not image_entry({"type": "file", "content_type": "application/pdf"}) and not image_entry(None)


def test_attach_want_pairs_one_image_across_record_and_part():
    p = Pipe()  # _attach_want is pure: it needs no valves, no server
    messages = [{"role": "user", "files": [RECORD],
                 "content": [{"type": "text", "text": TAG + "\n\nthis is what happens"},
                             {"type": "image_url", "image_url": {"url": DATA}}]}]
    want = p._attach_want(messages, [], set())
    assert len(want) == 1  # record and part name the same image: its bytes, file id and name in one candidate
    assert (want[0]["key"], want[0]["name"], want[0]["id"], want[0]["b64"]) == ("fid-1", "image.png", "fid-1", PNG)
    assert p._attach_want(messages, [], {"fid-1"}) == []  # already handed over: nothing is sent to the agent twice


# ---- whole turns against the fake opencode

@pytest.fixture
def rig(tmp_path):
    """One pipe + fake opencode pair per test, same valves as test_review.run_turn uses."""
    made = []

    def build(models=None, **valves):
        fake = FakeOpencode(tmp_path / "home")
        fake.agents = {"build", "ask"}
        if models is not None:
            fake.models = models
        fake.start()
        p = Pipe()
        v = p.valves
        v.OPENCODE_URL, v.OPENCODE_PASSWORD, v.PROJECTS_ROOT = fake.url, "x", str(tmp_path / "projects")
        v.ASK_FIRST, v.PREVIEW_SECRET, v.LIVE_URL, v.VIEW_TOKEN, v.FEATURES_TOKEN, v.FIXER_TOKEN = False, "", "", "", "", ""
        for key, value in valves.items():
            setattr(v, key, value)
        os.makedirs(v.PROJECTS_ROOT, exist_ok=True)
        made.append(fake)
        return p, fake

    yield build
    for fake in made:
        fake.stop()


def turn(p, fake, messages, att=None, user=None):
    async def emit(e):
        pass

    async def go():
        out = []
        async for chunk in p._run(list(messages), "chat1", {"id": "m", "providerID": "slerm"},
                                  emit, None, picked="slerm-turing.m",  # the same picked id test_review.run_turn uses
                                  att=att or [], user=user):
            out.append(chunk)
        return "".join(out)

    return asyncio.run(asyncio.wait_for(go(), 60))


def chat_prompts(fake):
    """(prompt texts, files lists) of the chat's own opencode session - a review session never carries owui_chat_id."""
    for s in list(fake.sessions.values()) + fake.removed:
        if (s.get("metadata") or {}).get("owui_chat_id") == "chat1":
            return s["prompts"], s["prompt_files"]
    raise AssertionError("the pipe never ran a session for this chat")


def with_image(extra_text="this is what happens when I click the button"):
    """A user message as Open WebUI sends it once a screenshot is attached: the citation tag in the text, the file
    record, and the bytes as an image content part (its middleware converts the stored image to base64 for model calls)."""
    return {"role": "user", "files": [dict(RECORD)],
            "content": [{"type": "text", "text": TAG + "\n\n" + extra_text},
                        {"type": "image_url", "image_url": {"url": DATA}}]}


def test_the_image_lands_as_a_prompt_file(rig):
    p, fake = rig()
    turn(p, fake, [with_image()], att=[dict(RECORD)])
    prompts, files = chat_prompts(fake)
    assert files[0] == [{"uri": DATA, "name": "image.png", "description": "Attached by the user in Open WebUI"}]
    # the citation tag names Open WebUI's storage, which the agent cannot read: it now names the attachment instead
    assert 'url="attached with this message"' in prompts[0] and 'url="fid-1"' not in prompts[0]
    for s in fake.sessions.values():  # delivered, so no later turn hands the same bytes over again
        if (s.get("metadata") or {}).get("owui_chat_id") == "chat1":
            assert s["metadata"]["owui_files"] == ["fid-1"]


def test_nothing_is_handed_over_twice(rig):
    p, fake = rig()
    user1 = with_image()
    turn(p, fake, [user1], att=[dict(RECORD)])
    messages = [user1, {"role": "assistant", "content": "looking"},
                {"role": "user", "files": [dict(RECORD)], "content": [{"type": "text", "text": "and now?"}]}]
    turn(p, fake, messages)
    prompts, files = chat_prompts(fake)
    assert files[1] == []  # Open WebUI re-sends the same record with the history: the session meta already knows it
    assert "attachment_note" not in prompts[1]


def test_an_attachment_from_an_earlier_message_arrives_first_time(rig):
    """The message was seen by a pipe that had no attachment support; now the bytes come with the next turn."""
    p, fake = rig()
    turn(p, fake, [{"role": "user", "content": "look at this screenshot"}])
    prompts, files = chat_prompts(fake)
    assert files[0] == [] and "attachment_note" not in prompts[0]  # what the old pipe did: nothing to hand over
    messages = [with_image("look at this screenshot"), {"role": "assistant", "content": "sure"},
                {"role": "user", "content": [{"type": "text", "text": "why is the button still broken?"}]}]
    turn(p, fake, messages)
    prompts, files = chat_prompts(fake)
    assert files[1] == [{"uri": DATA, "name": "image.png", "description": "Attached by the user in Open WebUI"}]
    assert "attached to an earlier message" in prompts[1] and "first time now" in prompts[1]


def test_a_text_only_model_is_told_about_the_image(rig):
    p, fake = rig(models=[{"id": "m", "providerID": "slerm", "capabilities": {"input": ["text"]}}])
    turn(p, fake, [with_image()], att=[dict(RECORD)])
    prompts, files = chat_prompts(fake)
    assert files[0] == []
    assert "image.png" in prompts[0]
    assert "takes no image input" in prompts[0]
    for s in fake.sessions.values():  # not delivered: if the user picks a vision model next turn, it gets the image
        if (s.get("metadata") or {}).get("owui_chat_id") == "chat1":
            assert not s["metadata"].get("owui_files")


def test_a_storage_only_image_is_read_from_open_webui(rig):
    """A record with no bytes beside it: the pipe reads the file inside the Open WebUI process (its own storage).
    Here that import cannot work (Open WebUI lives on another machine), so the test stands in for it."""
    p, fake = rig()
    asked = []

    async def storage(cand, user=None):
        asked.append((cand["id"], user))
        return "image/png", PNG

    p._attach_bytes = storage
    messages = [{"role": "user", "files": [dict(RECORD)], "content": [{"type": "text", "text": TAG + "\n\nsee this"}]}]
    turn(p, fake, messages, user=USER)
    prompts, files = chat_prompts(fake)
    assert asked == [("fid-1", USER)]  # it went looking for the record's id, not for a guessed path, as this user
    assert files[0] == [{"uri": DATA, "name": "image.png", "description": "Attached by the user in Open WebUI"}]


def test_an_unreadable_image_is_said_so(rig):
    p, fake = rig()
    messages = [{"role": "user", "files": [dict(RECORD)], "content": [{"type": "text", "text": TAG + "\n\nsee this"}]}]
    turn(p, fake, messages)
    prompts, files = chat_prompts(fake)
    assert files[0] == []  # no open_webui module to import here -> the plain note, not a silence
    assert "image.png" in prompts[0] and "could not be read" in prompts[0]


def test_an_image_bigger_than_the_valve_is_left_out_said_so(rig):
    p, fake = rig(ATTACH_MAX_MB=1)
    big = {"role": "user", "files": [{"id": "fid-big", "url": "fid-big", "type": "image",
                                      "content_type": "image/png", "name": "big.png"}],
           "content": [{"type": "text", "text": "a huge screenshot"},
                       {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}{'A' * 2_000_000}"}}]}
    turn(p, fake, [big])
    prompts, files = chat_prompts(fake)
    assert files[0] == []
    assert "big.png" in prompts[0] and "bigger than 1 MB" in prompts[0]


def test_at_most_eight_images_per_turn(rig):
    p, fake = rig()
    parts = [{"type": "text", "text": "ten screenshots at once"}]
    parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}{'A' * i}"}} for i in range(10)]
    turn(p, fake, [{"role": "user", "content": parts}])
    prompts, files = chat_prompts(fake)
    assert len(files[0]) == 8  # bytes with no file record beside them still reach the agent, named in turn
    assert files[0][0]["name"] == "image-1.png" and files[0][7]["name"] == "image-8.png"
    assert "2 more attached image(s) were left out" in prompts[0]


# ---- pipe(): Open WebUI's entry point hands this turn's files to the run

def test_pipe_hands_this_turns_attached_files_to_the_run():
    p = Pipe()
    p.valves.MODELS, p.valves.FEATURES_TOKEN = "slerm/m", ""
    got = {}

    def fake_run(messages, chat_id, model, emit, call, **kw):
        got.update(kw)

        async def gen():
            yield "ok"

        return gen()

    p._run = fake_run
    meta = {"chat_id": "local:chat9", "message_id": "m9", "files": [RECORD]}
    body = {"model": "code_mode.m", "messages": [{"role": "user", "content": "look at the picture"}], "metadata": meta}

    async def drain(gen):
        return "".join([c async for c in gen])

    user = {"id": "u1", "role": "user"}
    assert asyncio.run(drain(asyncio.run(p.pipe(body, user, None)))) == "ok"
    assert got["att"] == [RECORD]  # the files ride in the body's metadata (what Open WebUI's middleware builds)
    got.clear()
    asyncio.run(p.pipe({"model": "code_mode.m", "messages": body["messages"]}, user, None, __metadata__=meta))
    assert got["att"] == [RECORD]  # ...or arrive only as __metadata__, which the pipe is also given


# ---- the read itself, against stand-ins for Open WebUI 0.8.10's own modules
#
# The first build awaited Files.get_file_by_id, which is a plain function in Open WebUI 0.8.10 (models/files.py; its
# own async GET /files/{id}/content calls it without await). The TypeError was swallowed and every storage-only image
# came out as "could not be read". The tests above stand in for _attach_bytes as a whole, so they could not see it.

USER = {"id": "u1", "role": "user"}


@pytest.fixture
def owui(tmp_path, monkeypatch):
    """open_webui.models.files / storage.provider / models.users / utils.access_control.files as plain functions,
    the way 0.8.10 has them. state["shared"] = file ids has_access_to_file allows; state["async"] = async models."""
    upload = tmp_path / "uploads"
    upload.mkdir()
    (upload / "fid-1_image.png").write_bytes(__import__("base64").b64decode(PNG))
    state = {"shared": set(), "async": False, "calls": []}

    class File:
        def __init__(self, owner):
            self.id, self.user_id, self.path = "fid-1", owner, str(upload / "fid-1_image.png")
            self.meta = {"name": "image.png", "content_type": "image/png"}

    def maybe_async(value):
        if not state["async"]:
            return value

        async def later():
            return value
        return later()

    def get_file_by_id(fid, db=None):
        state["calls"].append(("get_file_by_id", fid))
        return maybe_async(File(state.get("owner", "u1")) if fid == "fid-1" else None)

    def get_user_by_id(uid, db=None):
        return maybe_async(types.SimpleNamespace(id=uid, role="user"))

    def has_access_to_file(fid, access, user, db=None):
        state["calls"].append(("has_access_to_file", fid, access, user.id))
        return maybe_async(fid in state["shared"])

    mods = {
        "open_webui": types.ModuleType("open_webui"),
        "open_webui.models": types.ModuleType("open_webui.models"),
        "open_webui.models.files": types.ModuleType("open_webui.models.files"),
        "open_webui.models.users": types.ModuleType("open_webui.models.users"),
        "open_webui.storage": types.ModuleType("open_webui.storage"),
        "open_webui.storage.provider": types.ModuleType("open_webui.storage.provider"),
        "open_webui.utils": types.ModuleType("open_webui.utils"),
        "open_webui.utils.access_control": types.ModuleType("open_webui.utils.access_control"),
        "open_webui.utils.access_control.files": types.ModuleType("open_webui.utils.access_control.files"),
    }
    mods["open_webui.models.files"].Files = types.SimpleNamespace(get_file_by_id=get_file_by_id)
    mods["open_webui.models.users"].Users = types.SimpleNamespace(get_user_by_id=get_user_by_id)
    mods["open_webui.storage.provider"].Storage = types.SimpleNamespace(get_file=lambda p: p)  # local storage
    mods["open_webui.utils.access_control.files"].has_access_to_file = has_access_to_file
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return state


def read(cand, user):
    return asyncio.run(Pipe()._attach_bytes(cand, user))


def test_a_storage_only_image_is_read_with_open_webuis_plain_model_calls(owui):
    assert read(dict(RECORD), USER) == ("image/png", PNG)
    assert owui["calls"][0] == ("get_file_by_id", "fid-1")


def test_the_read_also_works_where_the_model_calls_are_async(owui):
    owui["async"] = True
    assert read(dict(RECORD), USER) == ("image/png", PNG)


def test_only_a_user_open_webui_lets_read_the_file_gets_it(owui):
    owui["owner"] = "someone-else"
    assert read(dict(RECORD), USER) is None  # not theirs, not shared with them: Open WebUI would answer 404 too
    assert ("has_access_to_file", "fid-1", "read", "u1") in owui["calls"]
    assert read(dict(RECORD), {"id": "admin1", "role": "admin"}) == ("image/png", PNG)  # an admin may
    owui["shared"].add("fid-1")
    assert read(dict(RECORD), USER) == ("image/png", PNG)  # shared with them (a shared chat, a knowledge base...)


def test_no_user_or_no_file_reads_nothing(owui):
    assert read(dict(RECORD), None) is None
    assert read(dict(RECORD), {}) is None
    assert read({**RECORD, "id": "gone"}, USER) is None
    assert ("get_file_by_id", "fid-1") not in owui["calls"]  # no user: Open WebUI's storage is not even asked


def test_a_whole_turn_hands_the_storage_only_image_over(rig, owui):
    p, fake = rig()
    messages = [{"role": "user", "files": [dict(RECORD)], "content": [{"type": "text", "text": TAG + "\n\nsee this"}]}]
    turn(p, fake, messages, user=USER)
    prompts, files = chat_prompts(fake)
    assert files[0] == [{"uri": DATA, "name": "image.png", "description": "Attached by the user in Open WebUI"}]
    assert "could not be read" not in prompts[0]


def test_the_pipe_passes_the_turns_user_to_the_run():
    p = Pipe()
    p.valves.MODELS, p.valves.FEATURES_TOKEN = "slerm/m", ""
    got = {}

    def fake_run(messages, chat_id, model, emit, call, **kw):
        got.update(kw)

        async def gen():
            yield "ok"
        return gen()

    p._run = fake_run
    body = {"model": "code_mode.m", "messages": [{"role": "user", "content": "look"}],
            "metadata": {"chat_id": "local:c", "message_id": "m1", "files": [RECORD]}}

    async def drain(gen):
        return "".join([c async for c in gen])
    assert asyncio.run(drain(asyncio.run(p.pipe(body, USER, None)))) == "ok"
    assert got["user"] == USER
