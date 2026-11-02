#!/usr/bin/env python3
"""Tests for the page half of "reads as file views": live.html, loaded in jsdom (tests/page.cjs), fed the
events code-live.py really produces, then read back as what the page is actually showing.

Nothing here imitates the server: the finished window gets the snapshot() a finished window would get, the
live window gets events put through the server's own shape_event + views_of_event, and every view comes from
the real view-building code. The harness dumps tabs, file views, marked line ranges and Log entries.

    python3 -m unittest -v test_page.py       # needs node and a node_modules with jsdom
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("code_live", os.path.join(HERE, "code-live.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HARNESS = os.path.join(HERE, "tests", "page.cjs")

# jsdom is installed for the code-mode tooling, not inside this folder: find a node_modules that has it.
NODE = shutil.which("node")
NODE_MODULES = None
for cand in ("/home/coder/projects/code-mode/node_modules",
             os.path.join(HERE, "node_modules"),
             os.path.join(os.path.dirname(HERE), "node_modules")):
    if os.path.isdir(os.path.join(cand, "jsdom")):
        NODE_MODULES = cand
        break
NEEDS = "needs node plus jsdom (looked in code-mode's node_modules and this project's); none found"
HAVE = bool(NODE and NODE_MODULES)


def run_page(actions):
    """Run the jsdom harness with these actions and return its dump (what the page is showing)."""
    fd, path = tempfile.mkstemp(prefix="page-actions-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(actions, fh)
        env = dict(os.environ, NODE_PATH=NODE_MODULES)
        r = subprocess.run([NODE, HARNESS, path], capture_output=True, text=True,
                           cwd=HERE, env=env, timeout=180)
        if r.returncode != 0:
            raise AssertionError("the page harness failed:\n" + (r.stderr or r.stdout)[-2500:])
        return json.loads(r.stdout)
    finally:
        os.unlink(path)


def tools_of(snap):
    """Every tool item in snapshot order - the same order the Log lists them in."""
    out = []
    for st in snap["steps"]:
        for p in st["parts"]:
            if p["p"] == "tool":
                out.append(p)
    return out


def text(n):
    return "\n".join("line %d" % i for i in range(1, n + 1))


def write_file(path, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def log_of(dump):
    return dump["log"]


def entry(dump, i):
    e = dump["log"][i]
    return e


def view_entry(dump, needle):
    """The Log entry whose read-view label mentions `needle` (a file or a stretch of lines)."""
    hits = [e for e in dump["log"] if any(needle in h for h in e["hk"])]
    assert hits, "no Log entry with %r in %r" % (needle, [e["hk"] for e in dump["log"]])
    assert len(hits) == 1, [e["hk"] for e in hits]
    return hits[0]


def stage_of(dump, needle):
    """The staged file view whose header mentions `needle`."""
    hits = [v for v in dump["stage"] if needle in v["fh"]]
    assert hits, "no staged view with %r in %r" % (needle, [v["fh"] for v in dump["stage"]])
    assert len(hits) == 1, [v["fh"] for v in hits]
    return hits[0]


@unittest.skipUnless(HAVE, NEEDS)
class FinishedWindowPageTests(unittest.TestCase):
    """A finished window over the captured turn: the Log shows each read as its file with the read lines
    marked - small reads inline, big reads behind a "show", and never the command's raw output again."""

    @classmethod
    def setUpClass(cls):
        cls._oc_get, cls._no_disk = mod.oc_get, mod.FIXTURE_NO_DISK
        cls.fx, cls.chat, cls.start, cls.calls, cls.seeded = mod.fixture_prep(
            os.path.join(HERE, "fixtures", "learnlab-iq3-turn.json"))
        mod.oc_get = mod.fixture_oc_get(cls.fx)
        mod.FIXTURE_NO_DISK = True              # read the capture, not this machine's disk
        cls.snap = mod.snapshot(cls.fx["session"]["id"], cls.start, settle=True)
        cls.tools = tools_of(cls.snap)
        cls.read_app = next(i for i, p in enumerate(cls.tools)
                            if p["name"] == "read" and (p.get("views") or [{}])[0].get("ranges") == [[85, 174]])
        cls.out = run_page([{"ev": cls.snap}, {"dump": "done"},
                            {"clickLog": cls.read_app}, {"dump": "open"}])

    @classmethod
    def tearDownClass(cls):
        mod.oc_get, mod.FIXTURE_NO_DISK = cls._oc_get, cls._no_disk
        mod.READS.clear()
        mod.BEFORE.clear()

    def dump(self, key):
        return self.out[key]

    def test_the_page_finishes_clean_and_shows_no_stage(self):
        self.assertEqual(self.out["errors"], [])
        self.assertIn("done", self.dump("done")["mode"])
        self.assertEqual(self.dump("done")["stage"], [])   # a finished window has no Follow stage to draw

    def test_every_recorded_read_gets_a_label_naming_file_lines_and_verb(self):
        d = self.dump("done")
        # diff hunks also render headers, and they all start with "@@": the read-view labels are the rest
        labels = [h for e in d["log"] for h in e["hk"] if not h.startswith("@@")]
        self.assertEqual(len(labels), 18)                  # 18 views over 14 calls (the fixture's ground truth)
        self.assertEqual(len([e for e in d["log"] if any(not h.startswith("@@") for h in e["hk"])]), 14)
        self.assertIn("frontend/src/App.tsx · lines 85–174 · read",
                      view_entry(d, "85–174 · read")["hk"])
        self.assertIn("backend/auth/service.py · the whole file · 107 lines · cat",
                      view_entry(d, "107 lines · cat")["hk"])
        # a captured turn shows what the agent read then, never "as it is now"
        self.assertEqual([h for h in labels if "as it is now" in h], [])

    def test_a_small_read_shows_its_lines_inline_and_names_what_it_left_out(self):
        d = self.dump("done")
        e = view_entry(d, "2 lines in 2 places matched by grep")   # the grep that matched lines 1 and 20
        self.assertEqual(e["marked"], [[1, 1], [20, 20]])
        self.assertIn("lines 5–16 not shown", e["gaps"])    # a grep line gets the same 3 lines of context
        self.assertIn("lines 24–83 not shown", e["gaps"])   # and only gaps of 4 lines or more are folded
        self.assertEqual(e["lines"], 11)                    # lines 1–4 and 17–23 on screen

    def test_a_big_read_collapses_to_what_was_read_and_opens_on_click(self):
        d, o = self.dump("done"), self.dump("open")
        e = entry(d, self.read_app)
        self.assertEqual(e["hk"], ["frontend/src/App.tsx · lines 85–174 · read"])
        self.assertEqual(e["marked"], [])
        self.assertEqual(e["gaps"], ["lines 85–174 read · show"])
        self.assertFalse(e["open"])
        big = entry(o, self.read_app)
        self.assertTrue(big["open"])
        self.assertEqual(big["marked"], [[85, 174]])
        self.assertEqual(big["lines"], 96)                  # lines 82–177: the read plus three around each end
        self.assertIn("lines 1–81 not shown", big["gaps"])
        self.assertIn("lines 178–995 not shown", big["gaps"])

    def test_a_read_view_replaces_the_call_output_instead_of_repeating_it(self):
        d = self.dump("done")
        self.assertTrue(view_entry(d, "85–174 · read")["outHidden"])     # the raw "85: ..." output collapses
        plain = next((i for i, p in enumerate(self.tools)
                      if p["name"] == "shell" and not p.get("views") and isinstance(p.get("input"), dict)
                      and " desktop " not in p["input"].get("command", "") and p.get("out")), None)
        self.assertIsNotNone(plain, "the fixture has no plain shell call to compare against")
        e = entry(d, plain)
        self.assertEqual(e["hk"], [])
        self.assertFalse(e["outHidden"])                    # a command that read no file still shows its output
        self.assertTrue(e["out"].strip())

    def test_head_only_reads_are_marked_as_only_being_the_first_lines(self):
        e = view_entry(self.dump("done"), "lines 1–5 · head")
        self.assertEqual(e["marked"], [[1, 5]])
        self.assertIn("lines 6–995 not shown", e["gaps"])


@unittest.skipUnless(HAVE, NEEDS)
class LiveWindowPageTests(unittest.TestCase):
    """A window open while the agent works: each shell/read call that opens a file gets a file tab of its own,
    with exactly the lines its command asked for marked, and the stage following the file it touched last."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="live-page-")
        write_file(os.path.join(self.tmp, "big.py"), text(2500))
        write_file(os.path.join(self.tmp, "small.py"), text(9))
        write_file(os.path.join(self.tmp, "a.py"), text(30))
        write_file(os.path.join(self.tmp, "d.py"), text(8))
        mod.READS.clear()
        mod.FIXTURE_NO_DISK = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        mod.READS.clear()

    def build(self):
        """The raw opencode events of a short live turn, put through the server's own shaping and view code -
        the same sequence the live window's handler sends, view events after the tool event they belong to."""
        def raw(t, **d):
            return {"type": t, "created": int(time.time() * 1000), "data": dict(d)}
        grepped = 'sed -n \'85,100p\' big.py && grep -n "line 5" small.py'
        events = [
            raw("session.step.started", assistantMessageID="m1", model={"id": "test-model"}),
            raw("session.tool.input.started", id="c1", name="shell", assistantMessageID="m1"),
            raw("session.tool.input.ended", id="c1", assistantMessageID="m1", text='{"command": "%s"}' % grepped),
            raw("session.tool.called", id="c1", assistantMessageID="m1", input={"command": grepped}),
            raw("session.tool.success", id="c1", assistantMessageID="m1",
                content=[{"type": "text", "text": "line 85\nline 86\n5: line 5\n9: line 9"}]),
            raw("session.tool.input.started", id="c2", name="read", assistantMessageID="m1"),
            raw("session.tool.called", id="c2", assistantMessageID="m1",
                input={"path": "a.py", "offset": 20, "limit": 3}),
            raw("session.tool.success", id="c2", assistantMessageID="m1",
                content=[{"type": "text", "text": "20: line 20\n21: line 21\n22: line 22"}]),
            raw("session.tool.input.started", id="c3", name="shell", assistantMessageID="m1"),
            raw("session.tool.called", id="c3", assistantMessageID="m1", input={"command": "ls -1"}),
            raw("session.tool.success", id="c3", assistantMessageID="m1",
                content=[{"type": "text", "text": "a.py\nbig.py\nd.py\nsmall.py"}]),
            raw("session.tool.input.started", id="c4", name="shell", assistantMessageID="m1"),
            raw("session.tool.called", id="c4", assistantMessageID="m1",
                input={"command": 'grep -n "line 99" small.py'}),
            raw("session.tool.failed", id="c4", assistantMessageID="m1", error={"message": "no match"}),
            raw("session.tool.input.started", id="c5", name="shell", assistantMessageID="m1"),
            raw("session.tool.called", id="c5", assistantMessageID="m1",
                input={"command": "sed -n '2,4p' d.py && sed -n '6,8p' d.py"}),
            raw("session.tool.success", id="c5", assistantMessageID="m1",
                content=[{"type": "text", "text": "line 2\nline 3\nline 4\nline 6\nline 7\nline 8"}]),
        ]
        names, call_reads, actions = {}, {}, [{"ev": {"k": "snap", "steps": [], "notes": [], "running": True,
                                                      "since": int(time.time() * 1000), "now": int(time.time() * 1000)}}]
        for e in events:
            shaped = mod.shape_event(e, names)
            if not shaped:
                continue
            actions.append({"ev": shaped})
            for v in mod.views_of_event(shaped, names, self.tmp, call_reads):
                actions.append({"ev": v})
        actions.append({"dump": "live"})
        return actions

    def test_files_get_tabs_named_after_the_read_and_the_stage_follows_the_last_one(self):
        out = run_page(self.build())
        self.assertEqual(out["errors"], [])
        d = out["live"]
        self.assertIn("live", d["mode"])
        tabs = [t["text"] for t in d["tabs"]]
        self.assertIn("▤big.py", tabs)
        self.assertIn("▤small.py", tabs)
        self.assertIn("▤a.py", tabs)
        self.assertIn("▤d.py", tabs)                      # one call reading one file twice shares a tab, and one view
        self.assertNotIn("▤d.py ×2", tabs)
        focused = [t["text"] for t in d["tabs"] if t["on"]]
        self.assertEqual(focused, ["▤d.py"])              # the stage lands on the file the agent touched last

    def test_a_windowed_view_marks_the_asked_lines_and_names_what_it_could_not_send(self):
        d = run_page(self.build())["live"]
        v = stage_of(d, "big.py")
        self.assertIn("lines 85–100 · sed", v["fh"])
        self.assertFalse(any("as it is now" in x["fh"] for x in d["stage"]))  # read live: the text the agent read
        self.assertEqual(v["marked"], [[85, 100]])
        self.assertEqual(v["lines"], 300)                                     # lines 1–300: asked lines ± 200
        self.assertEqual(v["gaps"], ["lines 301–2500 not shown"])

    def test_a_grep_view_marks_the_lines_the_output_matched(self):
        d = run_page(self.build())["live"]
        v = stage_of(d, "small.py")
        self.assertIn("2 lines in 2 places matched by grep", v["fh"])
        self.assertEqual(v["marked"], [[5, 5], [9, 9]])
        self.assertEqual(v["gaps"], [])                                       # the whole 9-line file is on screen

    def test_a_call_that_reads_one_file_twice_gets_one_view_with_both_ranges(self):
        d = run_page(self.build())["live"]
        v = stage_of(d, "d.py")
        self.assertIn("6 lines in 2 places · sed", v["fh"])     # sed [2,4] + sed [6,8], marked in one view
        self.assertEqual(v["marked"], [[2, 4], [6, 8]])
        self.assertEqual(v["lines"], 8)                                    # the whole 8-line file on screen
        self.assertEqual(v["gaps"], [])

    def test_a_command_that_reads_no_file_keeps_its_output_and_a_failure_gets_no_view(self):
        d = run_page(self.build())["live"]
        ls = log_of(d)[2]                                       # c3: ls
        self.assertEqual(ls["hk"], [])
        self.assertFalse(ls["outHidden"])
        self.assertIn("big.py", ls["out"])
        self.assertIn("big.py", ls["summary"] + ls["out"])      # it still lives in the Terminal, not on a file tab
        fail = log_of(d)[3]                                     # c4: the failed grep
        self.assertEqual(fail["hk"], [])
        self.assertFalse(fail["outHidden"])
        self.assertIn("no match", fail["out"])
        shell_view = log_of(d)[0]                               # c1 got views: its raw output collapses
        self.assertTrue(shell_view["outHidden"])


@unittest.skipUnless(HAVE, NEEDS)
class FinishedFromDiskPageTests(unittest.TestCase):
    """A finished *live* session opened afterwards: its views were read off disk, so the Log says plainly
    that they show the file as it is now, not as the agent saw it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="page-now-")
        write_file(os.path.join(self.tmp, "small.py"), text(9))
        mod.READS.clear()
        mod.FIXTURE_NO_DISK = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        mod.READS.clear()

    def test_a_disk_read_of_a_finished_turn_is_labelled_as_the_file_as_it_is_now(self):
        msg = {"id": "msg_page", "type": "assistant", "time": {"created": 1, "completed": 2},
               "content": [{"type": "tool", "id": "c1", "name": "read",
                            "state": {"status": "completed", "input": {"path": "small.py", "offset": 3, "limit": 3},
                                      "content": [{"type": "text", "text": "3: line 3\n4: line 4\n5: line 5"}]}}]}
        step = mod.step_from_message(msg, None, mod.READS, self.tmp)
        self.assertTrue(step["parts"][0]["views"][0]["now"])       # read from disk after the turn: the file as it is now
        snap = {"k": "snap", "steps": [step], "notes": [], "running": False, "since": 1,
                "now": int(time.time() * 1000)}
        out = run_page([{"ev": snap}, {"dump": "done"}])
        self.assertEqual(out["errors"], [])
        e = out["done"]["log"][0]
        self.assertEqual(e["hk"], ["small.py · lines 3–5 · read the file as it is now"])
        self.assertEqual(e["marked"], [[3, 5]])
        self.assertEqual(e["gaps"], ["lines 6–9 not shown"])   # the 2-line gap above is too short to fold: it shows
        self.assertEqual(e["lines"], 5)                        # lines 1–5 on screen, the read 3–5 marked

    def test_grep_lines_get_context_and_a_short_gap_is_shown_rather_than_folded(self):
        write_file(os.path.join(self.tmp, "f.py"), text(14))
        msg = {"id": "msg_page2", "type": "assistant", "time": {"created": 1, "completed": 2},
               "content": [{"type": "tool", "id": "c1", "name": "shell",
                            "state": {"status": "completed",
                                      "input": {"command": 'grep -n "line 3" f.py && grep -n "line 11" f.py'},
                                      "content": [{"type": "text", "text": "3: line 3\n11: line 11"}]}}]}
        step = mod.step_from_message(msg, None, mod.READS, self.tmp)
        self.assertEqual([(v["how"], v["ranges"]) for v in step["parts"][0]["views"]],
                         [("grep", [[3, 3], [11, 11]])])       # both greps of one file, one view
        snap = {"k": "snap", "steps": [step], "notes": [], "running": False, "since": 1,
                "now": int(time.time() * 1000)}
        out = run_page([{"ev": snap}, {"dump": "done"}])
        self.assertEqual(out["errors"], [])
        e = out["done"]["log"][0]
        self.assertIn("matched by grep", e["hk"][0])
        self.assertEqual(e["marked"], [[3, 3], [11, 11]])
        self.assertEqual(e["gaps"], [])                        # context fills lines 4–10; the whole file shows
        self.assertEqual(e["lines"], 14)                       # nothing folded: no single-line markers


DIFF2 = """diff --git a/a.py b/a.py
index 1234567..89abcde 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@
 context line
-line 2
+LINE 2
 context tail
diff --git a/b.py b/b.py
new file mode 100644
index 0000000..3456789
--- /dev/null
+++ b/b.py
@@ -0,0 +1,2 @@
+added line
+another line
"""

BIG_DIFF = ("diff --git a/big.py b/big.py\nindex 1234567..89abcde 100644\n--- a/big.py\n+++ b/big.py\n"
            "@@ -1,25 +1,25 @@\n" + "\n".join("-old %d" % i for i in range(1, 26))
            + "\n" + "\n".join("+new %d" % i for i in range(1, 26)) + "\n")


@unittest.skipUnless(HAVE, NEEDS)
class GitDiffPageTests(unittest.TestCase):
    """A git diff/show call in the window: the page draws it as per-file diffs - a tab per changed file in
    Follow, "path · -a +b" headers in the Log - and does not repeat the command's raw output below.
    The events fed to the page are the ones code-live.py really sends: shaped events, views, then the
    gitdiff event built by its own parser."""

    def raw(self, t, **d):
        return {"type": t, "created": int(time.time() * 1000), "data": dict(d)}

    def build(self, git=True, cat=False, big=False, fail=False):
        """The live sequence for a shell call running git diff --cached (and optionally a plain cat of the
        same text, or a git call that fails), with the server's own gitdiff event right after the completed
        tool event."""
        out_text = BIG_DIFF if big else DIFF2
        events = [self.raw("session.step.started", assistantMessageID="m1", model={"id": "test-model"})]
        if fail:
            events += [
                self.raw("session.tool.input.started", id="cf", name="shell", assistantMessageID="m1"),
                self.raw("session.tool.called", id="cf", assistantMessageID="m1",
                         input={"command": "git diff HEAD~1"}),
                self.raw("session.tool.failed", id="cf", assistantMessageID="m1",
                         error={"message": "fatal: bad revision 'HEAD~1'"}),
            ]
        if git:
            events += [
                self.raw("session.tool.input.started", id="cg", name="shell", assistantMessageID="m1"),
                self.raw("session.tool.input.ended", id="cg", assistantMessageID="m1",
                         text='{"command": "git diff --cached"}'),
                self.raw("session.tool.called", id="cg", assistantMessageID="m1",
                         input={"command": "git diff --cached"}),
                self.raw("session.tool.success", id="cg", assistantMessageID="m1",
                         content=[{"type": "text", "text": out_text}]),
            ]
        if cat:
            events += [
                self.raw("session.tool.input.started", id="cc", name="shell", assistantMessageID="m1"),
                self.raw("session.tool.called", id="cc", assistantMessageID="m1",
                         input={"command": "cat changes.diff"}),
                self.raw("session.tool.success", id="cc", assistantMessageID="m1",
                         content=[{"type": "text", "text": out_text}]),
            ]
        names, call_reads, actions = {}, {}, [{"ev": {"k": "snap", "steps": [], "notes": [], "running": True,
                                                      "since": int(time.time() * 1000), "now": int(time.time() * 1000)}}]
        cmds = {"cg": "git diff --cached", "cc": "cat changes.diff"}  # what each call was named by its input event
        for e in events:
            shaped = mod.shape_event(e, names)
            if not shaped:
                continue
            actions.append({"ev": shaped})
            for v in mod.views_of_event(shaped, names, self.tmp, call_reads):
                actions.append({"ev": v})
            if shaped["k"] == "tool" and shaped.get("status") == "completed":
                # the server's own decision: the command it saw, and the full output of this event
                files, note = mod.gitdiff_of_call({"command": cmds.get(shaped["id"])},
                                                   (e.get("data") or {}).get("content"))
                if files:
                    ev = {"k": "gitdiff", "id": shaped["id"], "files": files}
                    if note:
                        ev["note"] = note
                    actions.append({"ev": ev})
        actions.append({"dump": "live"})
        return actions

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="page-git-")
        mod.READS.clear()
        mod.FIXTURE_NO_DISK = False

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        mod.READS.clear()

    def test_a_git_diff_call_gets_a_tab_per_changed_file_and_follows_the_last_one(self):
        out = run_page(self.build())
        self.assertEqual(out["errors"], [])
        d = out["live"]
        tabs = [t["text"] for t in d["tabs"]]
        self.assertIn("✎a.py", tabs)
        self.assertIn("✎b.py", tabs)
        focused = [t["text"] for t in d["tabs"] if t["on"]]
        self.assertEqual(focused, ["✎b.py"])              # the stage lands on the last changed file
        va, vb = stage_of(d, "a.py"), stage_of(d, "b.py")
        self.assertEqual(va["fh"], "a.py−1 +1")           # the file and what the patch changed
        self.assertEqual(vb["fh"], "b.py−0 +2")           # a new file: nothing removed, only additions
        self.assertEqual(va["lines"], 4)                  # context, -1 +1, context
        self.assertEqual(vb["lines"], 2)
        self.assertEqual(va["gaps"], [])

    def test_the_log_names_each_changed_file_and_does_not_repeat_the_raw_output(self):
        d = run_page(self.build())["live"]
        e = log_of(d)[0]
        self.assertEqual(e["hk"], ["a.py · −1 +1", "@@ lines 1–3 @@", "b.py · −0 +2", "@@ lines 1–2 @@"])
        self.assertEqual(e["lines"], 6)                   # both files' hunks drawn inline
        self.assertTrue(e["outHidden"])                   # the raw diff text is not shown again below
        self.assertIn("git diff --cached", e["summary"] + e["out"])  # the command itself is still named

    def test_a_failed_git_call_shows_its_error_and_opens_no_file_tabs(self):
        out = run_page(self.build(git=False, fail=True))
        self.assertEqual(out["errors"], [])
        d = out["live"]
        self.assertEqual([t["text"] for t in d["tabs"] if t["text"].startswith("✎")], [])
        e = log_of(d)[0]
        self.assertFalse(e["outHidden"])                    # errors are shown even under the git-diff machinery
        self.assertIn("bad revision", e["out"])

    def test_a_cat_of_a_diff_file_gets_no_file_tabs_and_keeps_its_output(self):
        out = run_page(self.build(git=False, cat=True))
        self.assertEqual(out["errors"], [])
        d = out["live"]
        self.assertEqual([t["text"] for t in d["tabs"] if t["text"].startswith("✎")], [])
        e = log_of(d)[0]
        self.assertEqual(e["hk"], [])
        self.assertFalse(e["outHidden"])                  # a plain cat still shows what it printed
        self.assertIn("+another line", e["out"])          # (what fits after the fold-in tail)

    def test_a_long_patch_collapses_to_a_count_and_opens_on_click(self):
        out = run_page(self.build(big=True) + [{"clickLog": 0}, {"dump": "open"}])
        self.assertEqual(out["errors"], [])
        shut, opened = out["live"]["log"][0], out["open"]["log"][0]
        self.assertEqual(shut["hk"], ["big.py · −25 +25"])
        self.assertEqual(shut["lines"], 0)
        self.assertEqual(shut["gaps"], ["50 lines of changes · show"])
        self.assertEqual(opened["lines"], 50)             # clicking the gap draws the patch itself
        self.assertEqual(opened["gaps"], [])

    def test_a_finished_window_draws_the_gitdiff_recorded_in_its_snapshot(self):
        msg = {"id": "msg_git", "type": "assistant", "time": {"created": 1, "completed": 2},
               "model": {"id": "test-model"},
               "content": [{"type": "tool", "id": "c1", "name": "shell",
                            "state": {"status": "completed", "input": {"command": "git diff --cached"},
                                      "content": [{"type": "text", "text": DIFF2}]}}]}
        step = mod.step_from_message(msg)
        self.assertTrue(step["parts"][0]["gitdiff"])      # recorded by the server's own history reader
        snap = {"k": "snap", "steps": [step], "notes": [], "running": False, "since": 1,
                "review": {"round": 2}, "now": int(time.time() * 1000)}
        out = run_page([{"ev": snap}, {"dump": "done"}])
        self.assertEqual(out["errors"], [])
        d = out["done"]
        e = d["log"][0]
        self.assertEqual(e["hk"], ["a.py · −1 +1", "@@ lines 1–3 @@", "b.py · −0 +2", "@@ lines 1–2 @@"])
        self.assertEqual(e["lines"], 6)
        self.assertTrue(e["outHidden"])

    def test_the_header_names_a_review_session_where_the_model_would_stand(self):
        snap = {"k": "snap", "steps": [], "notes": [], "running": False, "since": 1,
                "review": {"round": 2}, "now": int(time.time() * 1000)}
        d = run_page([{"ev": snap}, {"dump": "done"}])["done"]
        self.assertEqual(d["model"], "Review · round 2")
        self.assertEqual(d["modelClass"], "model review")   # drawn in the amber accent, not the grey model name
        d = run_page(self.build())["live"]
        self.assertEqual(d["model"], "test-model")
        self.assertEqual(d["modelClass"], "model")          # the chat's own window is unchanged


@unittest.skipUnless(HAVE, NEEDS)
class TerminalFollowTests(unittest.TestCase):
    """The Terminal tab follows what the agent runs (10-25, the user: "auto scroll to what its running ... allowing me
    to scroll up and when i scroll all the way back down it will auto scroll"). Run with a fake layout (tests/page.cjs
    fakeLayout): views 300 px tall, 18 px a line, scroll events like a browser's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="term-follow-")
        write_file(os.path.join(self.tmp, "a.py"), text(30))
        mod.READS.clear()
        mod.FIXTURE_NO_DISK = False
        self.names, self.reads = {}, {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        mod.READS.clear()

    def ev(self, t, **d):
        """One raw opencode event through the server's own shaping (and view building), as page actions."""
        shaped = mod.shape_event({"type": t, "created": int(time.time() * 1000), "data": dict(d, assistantMessageID="m1")},
                                 self.names)
        if not shaped:
            return []
        return [{"ev": shaped}] + [{"ev": v} for v in mod.views_of_event(shaped, self.names, self.tmp, self.reads)]

    def shell(self, cid, cmd, out=None):
        """A shell call; with out=None it is still running."""
        a = self.ev("session.tool.input.started", id=cid, name="shell")
        a += self.ev("session.tool.called", id=cid, input={"command": cmd})
        if out is not None:
            a += self.ev("session.tool.success", id=cid, content=[{"type": "text", "text": out}])
        return a

    def start(self):
        now = int(time.time() * 1000)
        return ([{"layout": True}, {"ev": {"k": "snap", "steps": [], "notes": [], "running": True, "since": now, "now": now}}]
                + self.ev("session.step.started", model={"id": "test-model"}))

    @staticmethod
    def output(n):
        return "\n".join("build %d: step %d of 12" % (n, i) for i in range(1, 13))

    @staticmethod
    def term(d):
        v = [x for x in d["views"] if "vterm" in x["cls"]]
        assert len(v) == 1, d["views"]
        return v[0]

    def assertAtEnd(self, d, what):
        v = self.term(d)
        self.assertTrue(v["on"], what + ": the Terminal is on screen")
        self.assertGreater(v["end"], 0, what + ": the Terminal is taller than its window (the test needs that)")
        self.assertEqual(v["top"], v["end"], what + ": the Terminal shows its end, " + json.dumps(v))

    def test_the_terminal_shows_the_command_running_now_also_after_a_file_tab_had_the_stage(self):
        a = self.start()
        for i in range(1, 9):
            a += self.shell("c%d" % i, "make step%d" % i, self.output(i))
        a += [{"dump": "many"}]
        a += self.ev("session.tool.input.started", id="r1", name="read")
        a += self.ev("session.tool.called", id="r1", input={"path": "a.py", "offset": 20, "limit": 3})
        a += self.ev("session.tool.success", id="r1", content=[{"type": "text", "text": "20: line 20\n21: line 21\n22: line 22"}])
        a += [{"dump": "read"}]
        a += self.shell("c9", "cargo test --workspace") + [{"dump": "back"}]
        out = run_page(a)
        self.assertEqual(out["errors"], [])
        self.assertAtEnd(out["many"], "after 8 commands")
        self.assertFalse(out["many"]["latest"], "no Latest button while it follows")
        self.assertFalse(self.term(out["read"])["on"], "the file the agent read took the stage")
        self.assertAtEnd(out["back"], "the next command brings the Terminal back")
        self.assertIn("cargo test --workspace", self.term(out["back"])["text"])
        self.assertIn("running", self.term(out["back"])["text"])

    def test_scrolling_up_stops_following_and_scrolling_back_down_follows_again(self):
        a = self.start()
        for i in range(1, 7):
            a += self.shell("c%d" % i, "make step%d" % i, self.output(i))
        a += [{"scrollView": 0, "to": 40}, {"dump": "up"}]
        a += self.shell("c7", "make step7", self.output(7)) + self.shell("c8", "make step8") + [{"dump": "upMore"}]
        a += [{"scrollView": 0, "to": "end"}, {"dump": "down"}]
        a += self.shell("c9", "make step9", self.output(9)) + [{"dump": "follows"}]
        a += [{"scrollView": 0, "to": 100}, {"dump": "up2"}, {"click": "#vjump"}, {"dump": "button"}]
        a += self.shell("c10", "make step10") + [{"dump": "after"}]
        out = run_page(a)
        self.assertEqual(out["errors"], [])
        up, more = self.term(out["up"]), self.term(out["upMore"])
        self.assertEqual(up["top"], 40)
        self.assertFalse(up["follow"])
        self.assertTrue(out["up"]["latest"], "scrolled up: the Latest button shows")
        self.assertEqual(more["top"], 40, "new commands leave the place the person scrolled to alone")
        self.assertGreater(more["end"], up["end"])
        self.assertTrue(self.term(out["down"])["follow"], "back at the end: it follows again")
        self.assertFalse(out["down"]["latest"])
        self.assertAtEnd(out["follows"], "the next command after scrolling back down")
        self.assertTrue(out["up2"]["latest"])
        self.assertAtEnd(out["button"], "the Latest button")
        self.assertFalse(out["button"]["latest"])
        self.assertAtEnd(out["after"], "a command after the Latest button")

    def test_a_tab_the_person_scrolled_up_in_keeps_its_place_when_it_comes_back(self):
        a = self.start()
        for i in range(1, 7):
            a += self.shell("c%d" % i, "make step%d" % i, self.output(i))
        a += [{"scrollView": 0, "to": 60}]
        a += self.ev("session.tool.input.started", id="r1", name="read")
        a += self.ev("session.tool.called", id="r1", input={"path": "a.py", "offset": 20, "limit": 3})
        a += self.ev("session.tool.success", id="r1", content=[{"type": "text", "text": "20: line 20\n21: line 21\n22: line 22"}])
        a += self.shell("c7", "make step7") + [{"dump": "back"}]
        out = run_page(a)
        self.assertEqual(out["errors"], [])
        v = self.term(out["back"])
        self.assertTrue(v["on"])
        self.assertEqual(v["top"], 60, "still where the person left it: " + json.dumps(v))
        self.assertTrue(out["back"]["latest"])


class LiveFlag(unittest.TestCase):
    """In a chat the window tells Open WebUI its height; with it, whether it shows the turn that runs now. The Code page
    script uses that to bring the running window into view when the chat's scroll-down arrow is pressed (10-24)."""

    def snap(self, running):
        now = int(time.time() * 1000)
        return {"ev": {"k": "snap", "steps": [], "notes": [], "running": running, "since": now, "now": now,
                       "model": "test-model"}}

    def test_a_running_turn_says_live_and_stops_saying_it_when_it_ends(self):
        out = run_page([{"embedded": True}, {"dump": "start"}, self.snap(True), {"dump": "running"},
                        {"ev": {"k": "end", "how": "done"}}, {"dump": "ended"}])
        self.assertEqual(out["errors"], [])
        heights = lambda d: [m for m in d["posted"] if m.get("type") == "iframe:height"]
        # before the server answers, the window's starting look is not a running turn
        self.assertFalse(any(m.get("live") for m in heights(out["start"])), heights(out["start"]))
        self.assertIs(heights(out["running"])[-1]["live"], True)
        self.assertIs(heights(out["ended"])[-1]["live"], False)
        self.assertTrue(all(isinstance(m["height"], int) for m in heights(out["ended"])))

    def test_a_finished_turn_never_says_live(self):
        out = run_page([{"embedded": True}, self.snap(False), {"dump": "done"}])
        self.assertEqual(out["errors"], [])
        self.assertFalse(any(m.get("live") for m in out["done"]["posted"]), out["done"]["posted"])


class CompactionOverlayFeed(unittest.TestCase):
    """While opencode compacts, the window hands the Code page one state for its overlay (10-25): reading the history
    (with the tape once), writing the summary, reading the new context back in, then done."""

    def snap(self):
        now = int(time.time() * 1000)
        return {"ev": {"k": "snap", "steps": [], "notes": [], "running": True, "since": now, "now": now, "model": "m"}}

    def test_the_page_gets_each_phase_and_the_tape_once(self):
        tape = [{"k": "sum", "s": "The summary from the last compaction", "w": 900}, {"k": "sh", "s": "cargo test", "w": 50}]
        out = run_page([{"embedded": True}, self.snap(),
                        {"ev": {"k": "compact", "ph": "start", "t0": int(time.time() * 1000)}},
                        {"ev": {"k": "compact", "ph": "tape", "lines": tape}},
                        {"ev": {"k": "cprog", "state": "reading", "read": 117392, "total": 199335, "cached": 0}},
                        {"ev": {"k": "cprog", "state": "reading", "read": 150000, "total": 199335}},
                        {"wait": 300}, {"dump": "reading"},
                        {"ev": {"k": "compact", "ph": "delta", "d": "## Objective\n"}},
                        {"ev": {"k": "compact", "ph": "delta", "d": "- Fork the CLI"}},
                        {"ev": {"k": "cprog", "state": "generating", "gen": 12, "rate": 38}},
                        {"wait": 300}, {"dump": "writing"},
                        {"ev": {"k": "compact", "ph": "end", "text": "## Objective\n- Fork the CLI", "tokens": {"output": 9},
                                "recent": 400}},
                        {"ev": {"k": "cprog", "state": "reading", "read": 20000, "total": 53501}},
                        {"wait": 300}, {"dump": "readin"},
                        {"ev": {"k": "text0", "id": "msg_9", "o": 0}},
                        {"wait": 300}, {"dump": "done"}])
        self.assertEqual(out["errors"], [])
        states = lambda d: [m for m in d["posted"] if m.get("type") == "code:compaction"]
        r = states(out["reading"])
        self.assertEqual([m["state"]["ph"] for m in r][0], "read")
        self.assertEqual(sum(1 for m in r if "tape" in m), 1, "the tape goes once")
        self.assertEqual([m for m in r if "tape" in m][0]["tape"], tape)
        last = r[-1]["state"]
        self.assertEqual((last["prog"]["read"], last["prog"]["total"]), (150000, 199335))
        self.assertEqual(last["ssd"], 117392, "the first read position of a compaction is the part the engine had")
        w = states(out["writing"])[-1]["state"]
        self.assertEqual((w["ph"], w["text"], w["prog"]["gen"]), ("write", "## Objective\n- Fork the CLI", 12))
        i = states(out["readin"])[-1]["state"]
        self.assertEqual((i["ph"], i["tokens"], i["recent"], i["prog"]["total"]), ("readin", {"output": 9}, 400, 53501))
        d = states(out["done"])[-1]["state"]
        self.assertEqual(d["ph"], "done", "the next step's first word: the new context is read")
        self.assertEqual(len(states(out["done"])), len(states(out["readin"])) + 1, "nothing after done")
        self.assertRegex(out["readin"]["caption"]["text"], r"^Compacted")   # the window's own caption still says it

    def test_a_turn_stopped_mid_compaction_tells_the_page_it_failed(self):
        out = run_page([{"embedded": True}, self.snap(), {"ev": {"k": "compact", "ph": "start"}},
                        {"ev": {"k": "end", "how": "interrupted"}}, {"wait": 300}, {"dump": "end"}])
        self.assertEqual(out["errors"], [])
        last = [m for m in out["end"]["posted"] if m.get("type") == "code:compaction"][-1]["state"]
        self.assertEqual((last["ph"], last["err"]), ("fail", "the turn ended"))

    def test_a_window_opened_mid_compaction_starts_the_overlay_where_it_is(self):
        now = int(time.time() * 1000)
        snap = {"ev": {"k": "snap", "steps": [], "notes": [], "running": True, "since": now, "now": now,
                       "compacting": {"ph": "end", "t0": now - 400000, "text": "## S", "tokens": {"output": 3}, "recent": 9}}}
        out = run_page([{"embedded": True}, snap, {"wait": 300}, {"dump": "open"}])
        self.assertEqual(out["errors"], [])
        st = [m for m in out["open"]["posted"] if m.get("type") == "code:compaction"][-1]["state"]
        self.assertEqual((st["ph"], st["text"]), ("readin", "## S"))
        self.assertLess(abs(st["since"] - (now - 400000)), 5000, "its clock starts when the compaction did")

    def test_a_window_in_its_own_tab_posts_nothing(self):
        out = run_page([self.snap(), {"ev": {"k": "compact", "ph": "start"}}, {"wait": 300}, {"dump": "solo"}])
        self.assertEqual(out["errors"], [])
        self.assertEqual([m for m in out["solo"]["posted"] if m.get("type") == "code:compaction"], [])


class ReviewOverlayFeed(unittest.TestCase):
    """A review window hands the Code page its round for the review overlay (10-25): the brief, the engine's progress
    while it is read, the commands, the worksheet rows read the way the Code pipe reads them, and the verdict. A builder
    window that got a review's findings hands those over."""

    BRIEF = ("[Code mode review - you are the reviewer here, not the engineer who did the work]\n\n"
             "Conversation context, oldest first (earlier requests are background, not a new checklist):\n1. strip ChatGPT\n2. continue\n\n"
             "Current task to review:\ncontinue\n\nReview this task.\n\nThe engineer's closing summary (claims to check, not facts):\nAll done.\n\n"
             "Files this turn changed: a.rs, b.rs, c.rs\n\nReply in the form your instructions give.")

    def snap(self, **extra):
        now = int(time.time() * 1000)
        return {"ev": dict({"k": "snap", "steps": [], "notes": [], "running": True, "since": now - 5000, "now": now,
                            "review": {"round": 2}, "brief": self.BRIEF}, **extra)}

    def test_a_review_round_reaches_the_page_brief_commands_worksheet_verdict(self):
        sheet = ("Worksheet\n\nTAG | item | status | evidence\n---|---|---|---\n"
                 "ASKED | Strip ChatGPT login | built | cli/src/lib.rs\n"
                 "ASKED | ChatGPT stripped from everything | half built (explicitly staged) | core/src/auth.rs\n"
                 "CLAIM | Re-submitted job 4709 | true | slerm-job list\n\n")
        out = run_page([{"embedded": True}, self.snap(),
                        {"ev": {"k": "cprog", "state": "reading", "read": 9000, "total": 12617}},
                        {"wait": 300}, {"dump": "brief"},
                        {"ev": {"k": "step", "id": "m1", "t0": 1}},
                        {"ev": {"k": "tool", "id": "c1", "msg": "m1", "name": "shell", "status": "running",
                                "input": {"command": "cd /home/coder/projects/x && git diff --stat refs/code-mode/turn-start"}}},
                        {"wait": 300}, {"dump": "check"},
                        {"ev": {"k": "text0", "id": "m2", "o": 0}},
                        {"ev": {"k": "text+", "id": "m2", "o": 0, "d": sheet}},
                        {"wait": 300}, {"dump": "sheet"},
                        {"ev": {"k": "text+", "id": "m2", "o": 0, "d": "REVIEW: INCOMPLETE"}},
                        {"ev": {"k": "end", "how": "succeeded"}}, {"wait": 300}, {"dump": "end"}])
        self.assertEqual(out["errors"], [])
        states = lambda d: [m["state"] for m in d["posted"] if m.get("type") == "code:review"]
        b = states(out["brief"])[-1]
        self.assertEqual((b["ph"], b["round"], b["recheck"]), ("brief", 2, False))
        self.assertEqual([r["k"] for r in b["brief"]], ["rules", "task", "asked", "claims", "files", "reply"])
        self.assertEqual(b["brief"][1]["s"], "Current task to review: continue")
        self.assertEqual(b["brief"][2]["s"], "2 requests, oldest first")
        self.assertEqual(b["brief"][4]["s"], "3 files this turn changed")
        self.assertEqual((b["prog"]["read"], b["prog"]["total"]), (9000, 12617))
        c = states(out["check"])[-1]
        self.assertEqual((c["ph"], c["nCmds"], c["cmds"][0]["s"], c["cmds"][0]["k"]),
                         ("check", 1, "git diff --stat refs/code-mode/turn-start", "sh"))
        s = states(out["sheet"])[-1]
        self.assertEqual(s["ph"], "sheet")
        self.assertEqual([(r["tag"], r["kind"]) for r in s["rows"]], [("ASKED", ""), ("ASKED", "not built"), ("CLAIM", "")])
        self.assertIsNone(s["verdict"])
        e = states(out["end"])[-1]
        self.assertEqual((e["ph"], e["verdict"], e["ended"]), ("verdict", "INCOMPLETE", True))

    def test_the_builder_window_after_a_review_hands_the_findings_over_once(self):
        snap = self.snap(review=None, brief=None, handback={"findings": ["[not built] ChatGPT stripped (half built)"]})
        out = run_page([{"embedded": True}, snap, snap, {"wait": 300}, {"dump": "open"}])
        self.assertEqual(out["errors"], [])
        hb = [m for m in out["open"]["posted"] if m.get("type") == "code:review-handback"]
        self.assertEqual(hb, [{"type": "code:review-handback", "findings": ["[not built] ChatGPT stripped (half built)"]}])
        self.assertEqual([m for m in out["open"]["posted"] if m.get("type") == "code:review"], [])

    def test_a_finished_review_posts_nothing(self):
        out = run_page([{"embedded": True}, self.snap(running=False), {"wait": 300}, {"dump": "done"}])
        self.assertEqual(out["errors"], [])
        self.assertEqual([m for m in out["done"]["posted"] if str(m.get("type", "")).startswith("code:review")], [])


class WatchStage(unittest.TestCase):
    """Watch mode (10-25): the Code page shows a second copy of the turn's window as its stage (#fill=1). That copy
    reports only its own state, so the overlays are fed once, by the chat's own window; the chat's window has an eye
    button that asks the page for Watch mode."""

    def snap(self):
        now = int(time.time() * 1000)
        return {"ev": {"k": "snap", "steps": [], "notes": [], "running": True, "since": now - 65000, "now": now, "model": "m"}}

    def test_the_stage_copy_reports_its_state_and_feeds_no_overlay(self):
        out = run_page([{"embedded": True, "hash": "fill=1"}, self.snap(), {"ev": {"k": "compact", "ph": "start"}},
                        {"wait": 1500}, {"dump": "stage"}])
        self.assertEqual(out["errors"], [])
        kinds = [m.get("type") for m in out["stage"]["posted"]]
        self.assertIn("code:watch-state", kinds)
        self.assertNotIn("code:compaction", kinds)
        self.assertNotIn("iframe:height", kinds)
        st = [m for m in out["stage"]["posted"] if m.get("type") == "code:watch-state"][-1]
        self.assertTrue(st["live"])
        self.assertLess(abs(st["since"] - (int(time.time() * 1000) - 65000)), 10000)
        self.assertIn("live", out["stage"]["mode"])

    def test_the_chat_window_eye_button_asks_for_watch_mode(self):
        out = run_page([{"embedded": True}, self.snap(), {"click": "#watch"}, {"dump": "clicked"}])
        self.assertEqual(out["errors"], [])
        self.assertIn({"type": "code:watch"}, out["clicked"]["posted"])
        self.assertNotIn("code:watch-state", [m.get("type") for m in out["clicked"]["posted"]])


class QuietWindow(unittest.TestCase):
    """A quiet live window says what it waits on (performance handoff 10-23): an approval or a compaction is not
    'Waiting for the model', and before the first step the model is reading the prompt."""

    def snap(self):
        return {"ev": {"k": "snap", "steps": [], "notes": [], "running": True, "since": int(time.time() * 1000),
                       "now": int(time.time() * 1000), "model": "test-model"}}

    def test_before_the_first_step_it_is_reading_the_context(self):
        out = run_page([self.snap(), {"wait": 3600}, {"dump": "start"}])
        self.assertEqual(out["errors"], [])
        self.assertRegex(out["start"]["caption"]["text"], r"^Reading context · \ds$")

    def test_a_pending_approval_is_waiting_for_you_not_the_model(self):
        out = run_page([self.snap(),
                        {"ev": {"k": "note", "level": "ask", "text": "Asking for approval in the chat — bash: rm -rf build"}},
                        {"wait": 3600}, {"dump": "asking"},
                        {"ev": {"k": "note", "text": "Approved"}}, {"wait": 3600}, {"dump": "after"}])
        self.assertEqual(out["errors"], [])
        cap = out["asking"]["caption"]
        self.assertRegex(cap["text"], r"^Waiting for your approval in the chat · \ds$")
        self.assertEqual((cap["label"], cap["cls"]), ("asking", "ask"))
        self.assertRegex(out["after"]["caption"]["text"], r"^Reading context · \ds$")   # no step yet: still the prompt

    def test_a_compaction_and_the_read_after_it_are_named(self):
        out = run_page([self.snap(),
                        {"ev": {"k": "note", "text": "Compacting the conversation to fit the context…"}},
                        {"wait": 3600}, {"dump": "compacting"},
                        {"ev": {"k": "note", "text": "Compacted"}}, {"wait": 3600}, {"dump": "after"}])
        self.assertEqual(out["errors"], [])
        self.assertRegex(out["compacting"]["caption"]["text"], r"^Compacting the conversation · \ds$")
        self.assertEqual(out["compacting"]["caption"]["label"], "waiting")


if __name__ == "__main__":
    unittest.main(verbosity=2)
