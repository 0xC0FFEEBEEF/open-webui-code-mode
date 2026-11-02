#!/usr/bin/env python3
"""Tests for the diff engine in code-live.py (pure functions, no server, no files).

    python3 -m unittest -v test_diff.py
"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("code_live", os.path.join(HERE, "code-live.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

build_diff = mod.build_diff
split_lines = mod.split_lines
jstr = mod.jstr
apply_edit = mod.apply_edit
pair_marks = mod.pair_marks
diff_of_call = mod.diff_of_call
input_val = mod.input_val
MSG_RE = mod.MSG_RE


def rows_of(d):
    return [r for h in d["hunks"] for r in h["lines"]]


def kinds(d):
    return [r[0] for r in rows_of(d)]


class SplitLinesTests(unittest.TestCase):
    def test_lf(self):
        self.assertEqual(split_lines("a\nb\n"), ["a", "b"])
        self.assertEqual(split_lines("a\nb"), ["a", "b"])  # missing final newline
        self.assertEqual(split_lines("only"), ["only"])
        self.assertEqual(split_lines(""), [])

    def test_crlf_does_not_shift_numbers(self):
        self.assertEqual(split_lines("a\r\nb\r\n"), ["a", "b"])
        self.assertEqual(split_lines("a\r\nb"), ["a", "b"])  # CRLF and no final newline
        self.assertEqual(split_lines("\n"), [""])  # one empty line


class JstrTests(unittest.TestCase):
    def test_closed_and_partial(self):
        raw = '{"path": "a.py", "content": "hello wor'
        self.assertEqual(jstr(raw, "path"), ("a.py", True))
        self.assertEqual(jstr(raw, "content"), ("hello wor", False))
        self.assertIsNone(jstr(raw, "oldString"))  # key has not arrived
        self.assertIsNone(jstr('{"content": 1}', "content"))  # not a string value

    def test_escapes(self):
        self.assertEqual(jstr('{"p": "a\\"b\\n\\tc"}', "p"), ('a"b\n\tc', True))
        self.assertEqual(jstr('{"p": "\\u0041x"}', "p"), ("Ax", True))

    def test_splits_mid_escape(self):
        self.assertEqual(jstr('{"p": "a\\', "p"), ("a", False))  # the backslash itself is the last typed char
        self.assertEqual(jstr('{"p": "a\\u12', "p"), ("a", False))  # partial \\uXXXX


class ApplyEditTests(unittest.TestCase):
    def test_first_occurrence(self):
        self.assertEqual(apply_edit("x\ny\nx\ny\n", "y", "z"), "x\nz\nx\ny\n")

    def test_replace_all(self):
        self.assertEqual(apply_edit("x\ny\nx\ny\n", "y", "z", True), "x\nz\nx\nz\n")

    def test_not_found(self):
        self.assertIsNone(apply_edit("a\nb\n", "nope", "z"))
        self.assertIsNone(apply_edit("a\nb\n", "", "z"))


class PairMarksTests(unittest.TestCase):
    def test_small_change(self):
        old, new = "    return 0", "    return 1"
        mo, mn = pair_marks(old, new)
        self.assertEqual((mo, mn), ([[11, 12]], [[11, 12]]))

    def test_dissimilar_lines(self):
        self.assertIsNone(pair_marks("aaa bbb ccc", "totally different stuff here"))

    def test_marks_fall_on_whole_pieces_of_the_line(self):
        # Two names that share the end of the line: marking characters one by one leaves the shared
        # "e_session_token" plain and marks the leftovers "creat" / "issu", which reads like a typo.
        old, new = "    create_session_token,", "    issue_session_token,"
        self.assertEqual(pair_marks(old, new), ([[4, 10]], [[4, 9]]))
        self.assertEqual((old[4:10], new[4:9]), ("create", "issue"))
        # letters and digits are separate pieces, so only the number is marked
        self.assertEqual(pair_marks("count5 = 1", "count6 = 1"), ([[5, 6]], [[5, 6]]))
        # an inserted run of words is marked whole
        self.assertEqual(pair_marks("username TEXT UNIQUE NOT NULL,",
                                    "username TEXT UNIQUE NOT NULL COLLATE NOCASE,")[1], [[29, 44]])


class HunkTests(unittest.TestCase):
    def test_edit_found_context_and_numbers(self):
        a = "\n".join("l%d" % i for i in range(1, 11)) + "\n"
        b = a.replace("l5", "L5")
        d = build_diff(a, b)
        self.assertEqual(d["more_old"], 0)
        self.assertIsNone(d["note"])
        h = d["hunks"]
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["old_start"], h[0]["new_start"]), (2, 2))  # 3 context lines before
        self.assertEqual(kinds(d), [" ", " ", " ", "-", "+", " ", " ", " "])
        self.assertEqual([r[1] for r in rows_of(d) if r[1] is not None], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual([r[2] for r in rows_of(d) if r[2] is not None], [2, 3, 4, 5, 6, 7, 8])
        rm = rows_of(d)[3]
        self.assertEqual((rm[0], rm[3]), ("-", "l5"))
        self.assertEqual(rm[4], [[0, 1]])  # "l5" -> "L5": only the l/L is marked on both sides

    def test_edit_at_line_1_and_at_last_line(self):
        a = "\n".join("l%d" % i for i in range(1, 11)) + "\n"
        d1 = build_diff(a, a.replace("l1\n", "L1\n"))  # anchored so "l10" is not caught
        self.assertEqual((d1["hunks"][0]["old_start"], d1["hunks"][0]["new_start"]), (1, 1))
        self.assertEqual(kinds(d1), ["-", "+", " ", " ", " "])
        d2 = build_diff(a, a.replace("l10", "L10"))
        self.assertEqual(d2["hunks"][0]["old_start"], 7)  # lines 7-9 are the 3 context lines before line 10
        self.assertEqual(kinds(d2), [" ", " ", " ", "-", "+"])

    def test_two_close_edits_join_far_ones_split(self):
        a = "\n".join("l%d" % i for i in range(1, 21)) + "\n"
        close = a.replace("l5", "L5").replace("l9", "L9")  # 3 identical lines between: joined
        d = build_diff(a, close)
        self.assertEqual(len(d["hunks"]), 1)
        self.assertEqual(d["hunks"][0]["old_start"], 2)
        far = a.replace("l2\n", "L2\n").replace("l10", "L10")  # 7 identical lines between: split ("l20" not caught)
        d = build_diff(a, far)
        self.assertEqual(len(d["hunks"]), 2)
        self.assertEqual([h["old_start"] for h in d["hunks"]], [1, 7])

    def test_crlf_and_missing_final_newline_keep_numbers(self):
        a = "hdr\nkeep\r\nchange me\nkeep2"  # CRLF in the middle, no final newline
        b = "hdr\nkeep\r\nCHANGED\nkeep2"
        d = build_diff(a, b)
        kinds_, texts = kinds(d), [r[3] for r in rows_of(d)]
        self.assertEqual(kinds_, [" ", " ", "-", "+", " "])
        self.assertEqual(texts, ["hdr", "keep", "change me", "CHANGED", "keep2"])  # \r stripped, numbers intact
        self.assertEqual([r[1] for r in rows_of(d) if r[1] is not None], [1, 2, 3, 4])

    def test_big_file_hunks_only(self):
        a = "\n".join("l%d" % i for i in range(1, 5001)) + "\n"
        b = a.replace("l2500", "L2500")
        d = build_diff(a, b)
        self.assertEqual(len(rows_of(d)), 8)  # 3 context + -/+ + 3 context, never the whole file
        self.assertEqual(d["hunks"][0]["old_start"], 2497)  # lines 2497-2499 are the leading context

    def test_diff_row_cap_gets_a_note(self):
        a = "\n".join("o%d" % i for i in range(1, 5001)) + "\n"
        b = "\n".join("n%d" % i for i in range(1, 2001)) + "\n" + "\n".join("o%d" % i for i in range(2001, 5001)) + "\n"
        d = build_diff(a, b)
        self.assertTrue(d["note"])
        self.assertLessEqual(len(rows_of(d)), mod.DIFF_MAX_LINES)
        self.assertTrue(len(d["hunks"]) > 1)  # long hunks are chunked

    def test_word_marks_only_on_similar_pairs(self):
        a = "same line here\naaa bbb ccc\n"
        b = "same line HERE\nxxx yyy zzz www\n"
        d = build_diff(a, b)
        rm, ad = rows_of(d)[0], rows_of(d)[1]
        self.assertEqual(rm[4], [[10, 14]])  # "here" -> "HERE"
        self.assertEqual(ad[4], [[10, 14]])
        self.assertEqual(rows_of(d)[2][4], [])  # dissimilar pair: whole-line colours only
        self.assertEqual(rows_of(d)[3][4], [])


class StreamingWriteTests(unittest.TestCase):
    def test_more_old_lines_fold_out(self):
        a = "def a():\n    return 0\n\ndef b():\n    return 1\n"
        typed = "def a()\n"  # two lines typed so far
        d = build_diff(a, typed, fold_tail=True)
        self.assertEqual(d["more_old"], 4)
        self.assertEqual(kinds(d), ["-", "+"])  # "def a():" being retyped; the untouched old tail is the faint line
        self.assertEqual(d["hunks"][0]["old_start"], 1)

    def test_new_file_all_green(self):
        d = build_diff("", "x\ny\n", fold_tail=True)
        self.assertEqual(kinds(d), ["+", "+"])
        self.assertEqual([r[2] for r in rows_of(d)], [1, 2])
        self.assertEqual([r[1] for r in rows_of(d)], [None, None])
        self.assertEqual(d["more_old"], 0)


class DiffOfCallTests(unittest.TestCase):
    BEFORE = "def a():\n    return 0\n\n# tail\n"

    def test_edit_found(self):
        inp = {"path": "a.py", "oldString": "    return 0", "newString": "    return 1"}
        d = diff_of_call(self.BEFORE, "edit", inp)
        self.assertEqual(d["path"], "a.py")
        self.assertEqual(d["note"], None)
        rm = [r for r in rows_of(d) if r[0] == "-"][0]
        ad = [r for r in rows_of(d) if r[0] == "+"][0]
        self.assertEqual((rm[1], ad[2]), (2, 2))
        self.assertEqual((rm[4], ad[4]), ([[11, 12]], [[11, 12]]))

    def test_edit_not_found_gives_the_note(self):
        inp = {"path": "a.py", "oldString": "    return 99", "newString": "x"}
        d = diff_of_call(self.BEFORE, "edit", inp)
        self.assertEqual(d["hunks"], [])
        self.assertEqual(d["note"], "could not place this edit in the file")

    def test_edit_partial_input(self):
        raw = '{"newString": "    return 1", "oldStri'  # oldString key still arriving
        self.assertIsNone(diff_of_call(self.BEFORE, "edit", raw, streaming=True))
        raw = '{"path": "a.py", "oldString": "    return 0", "newString": "    return 1'  # value still typing
        d = diff_of_call(self.BEFORE, "edit", raw, streaming=True)
        self.assertEqual([r[3] for r in rows_of(d) if r[0] == "+"], ["    return 1"])  # green line grows
        self.assertIsNone(diff_of_call(self.BEFORE, "edit", '{"oldString": "zz', streaming=True))  # not placeable yet

    def test_a_path_still_being_typed_is_not_a_path(self):
        # "{"path": "a" is not a file name: a diff named for it would be a lie, and reading it would cache the
        # wrong file as the "before" (CallTracker checks the same thing before it reads).
        self.assertIsNone(diff_of_call(self.BEFORE, "edit", '{"path": "a', streaming=True))
        self.assertIsNone(diff_of_call(self.BEFORE, "write", '{"path": "a.py', streaming=True))
        self.assertIsNotNone(diff_of_call(self.BEFORE, "write", '{"path": "a.py"}', streaming=True))

    def test_write_streaming_then_complete(self):
        raw = '{"path": "a.py", "content": "def a():'
        d = diff_of_call(self.BEFORE, "write", raw, streaming=True)
        self.assertEqual(d["more_old"], 3)
        inp = {"path": "a.py", "content": "def a():\n    pass\n"}
        d = diff_of_call(self.BEFORE, "write", inp)
        self.assertEqual(d["more_old"], 0)  # completion: no fold, the rest really is deleted
        self.assertEqual([r[3] for r in rows_of(d) if r[0] == "-"], ["    return 0", "", "# tail"])

    def test_write_new_file(self):
        d = diff_of_call("", "write", {"path": "new.py", "content": "x\ny\n"})
        self.assertEqual(kinds(d), ["+", "+"])

    def test_newstring_in_five_deltas_grows_the_hunk(self):
        """A newString arriving in five deltas gives growing hunks as it is typed."""
        deltas = ['{"path": "a.py", "oldString": "    return 0", "newString": "',
                  '    result',
                  ' = []',
                  '\\n    for x',
                  ' in src:\\n        result.append(x)"}']
        raw, seen = "", []
        for d in deltas:
            raw += d
            diff = diff_of_call(self.BEFORE, "edit", raw, streaming=True)
            self.assertIsNotNone(diff)  # oldString is placed from the first delta on
            seen.append(sum(1 for r in rows_of(diff) if r[0] == "+"))
        self.assertIsNone(diff["note"])
        self.assertEqual(seen, [1, 1, 1, 2, 3])  # the green lines grow with what has been typed (stage 1: the empty part leaves a blank line)

    def test_unknown_before_and_other_tools(self):
        self.assertIsNone(diff_of_call(None, "edit", {"path": "a", "oldString": "a", "newString": "b"}))
        self.assertIsNone(diff_of_call("x", "shell", {"command": "ls"}))

    def test_capped_input_note(self):
        big = "x" * 100
        d = diff_of_call(self.BEFORE, "write", {"path": "a.py", "content": big + "\n… (200 more characters)"})
        self.assertEqual(d["note"], "file too large for a live diff")
        d = diff_of_call(self.BEFORE, "edit", '{"path": "a.py", "oldString": "x"\n… (200 more characters)')
        self.assertEqual(d["note"], "file too large for a live diff")


class ReadWithinTests(unittest.TestCase):
    """A diff may only read files inside the session's own project directory."""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.realpath(tempfile.mkdtemp())
        cls.outside = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(cls.root, "a.py"), "w") as fh:
            fh.write("hello\n")
        os.makedirs(os.path.join(cls.root, "sub"))
        with open(os.path.join(cls.outside, "secret"), "w") as fh:
            fh.write("another project\n")
        os.symlink(os.path.join(cls.outside, "secret"), os.path.join(cls.root, "link"))
        os.symlink(cls.outside, os.path.join(cls.root, "dirlink"))

    def test_reads_files_inside_the_directory(self):
        self.assertEqual(mod.read_within(self.root, "a.py"), "hello\n")

    def test_refuses_every_way_out(self):
        for rel in ("../secret", os.path.join(self.outside, "secret"), "/etc/passwd",
                    "link", "dirlink/secret", "sub/../../outside-secret"):
            self.assertIsNone(mod.read_within(self.root, rel), rel)
        self.assertIsNone(mod.read_within(None, "a.py"))     # a session without a directory reads nothing
        self.assertIsNone(mod.read_within(self.root, ""))
        self.assertIsNone(mod.read_within(self.root, None))

    def test_inside_paths_normalise_normally(self):
        self.assertEqual(mod.read_within(self.root, "sub/../a.py"), "hello\n")  # .. inside the directory is fine
        self.assertEqual(mod.read_within(self.root, "./a.py"), "hello\n")

    def test_a_file_that_is_not_there_yet_is_empty(self):
        self.assertEqual(mod.read_within(self.root, "new/deep.py"), "")  # a new write shows all green

    def test_directories_binary_and_oversize_files_are_refused(self):
        self.assertIsNone(mod.read_within(self.root, "sub"))
        self.assertIsNone(mod.read_within(self.root, "."))
        with open(os.path.join(self.root, "bin"), "wb") as fh:
            fh.write(b"a\0b")
        self.assertIsNone(mod.read_within(self.root, "bin"))
        big = os.path.join(self.root, "big")
        with open(big, "wb") as fh:
            fh.write(b"x" * (mod.DIFF_MAX_FILE + 1))
        self.assertIsNone(mod.read_within(self.root, "big"))
        os.remove(big)


class BeforeReaderTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        with open(os.path.join(self.root, "a.py"), "w") as fh:
            fh.write("first\n")
        self.r = mod.BeforeReader()

    def test_the_first_read_wins_even_after_the_file_changes(self):
        self.assertEqual(self.r.ensure("c1", self.root, "a.py"), "first\n")
        with open(os.path.join(self.root, "a.py"), "w") as fh:
            fh.write("second\n")
        self.assertEqual(self.r.ensure("c1", self.root, "a.py"), "first\n")   # the same call keeps its before
        self.assertEqual(self.r.ensure("c2", self.root, "a.py"), "second\n")  # a new call reads the new state

    def test_a_refused_read_is_kept_as_none(self):
        self.assertIsNone(self.r.ensure("c3", self.root, "../secret"))
        self.assertTrue(self.r.has("c3"))          # known to be unreadable: no diff, and no repeat attempts
        self.assertIsNone(self.r.ensure("c3", self.root, "a.py"))

    def test_entry_cap_drops_the_oldest(self):
        for i in range(mod.BEFORE_MAX + 5):
            self.r.put("c%d" % i, "x")
        self.assertEqual(len(self.r.cache), mod.BEFORE_MAX)
        self.assertFalse(self.r.has("c0"))
        self.assertTrue(self.r.has("c%d" % (mod.BEFORE_MAX + 4)))

    def test_byte_cap_drops_the_oldest(self):
        r = mod.BeforeReader(max_bytes=5000)         # a reader of its own size: the cap is per reader
        for i in range(4):
            r.put("c%d" % i, "x" * 2000)
        self.assertEqual(r.total, 4000)
        r.put("c4", "x" * 2000)
        self.assertLessEqual(r.total, 5000)
        self.assertFalse(r.has("c0"))
        self.assertTrue(r.has("c4"))


class SnapshotDiffTests(unittest.TestCase):
    """A snapshot or replay attaches the same diff to a tool call whose file was read (cached before);
    without a cached before it looks exactly as it did before the diff work."""

    MSG = {"id": "msg_a", "type": "assistant", "time": {"created": 1}, "content": [
        {"type": "tool", "id": "c1", "name": "edit", "state": {"status": "completed",
         "input": {"path": "a.py", "oldString": "one\n", "newString": "ONE\n"},
         "content": [{"type": "text", "text": "ok"}]}},
        {"type": "tool", "id": "c2", "name": "write", "state": {"status": "completed",
         "input": {"path": "b.py", "content": "brand new\n"}}},
        {"type": "tool", "id": "c3", "name": "shell", "state": {"status": "completed",
         "input": {"command": "ls"}, "output": "a.py"}}]}

    def test_cached_calls_get_their_diff(self):
        r = mod.BeforeReader()
        r.put("c1", "one\ntwo\n")
        r.put("c2", "")            # new file
        r.put("c3", "whatever")   # not an edit/write: no diff
        parts = mod.step_from_message(self.MSG, r)["parts"]
        d = parts[0]["diff"]
        self.assertEqual(d["path"], "a.py")
        self.assertEqual([[row[0], row[3]] for row in d["hunks"][0]["lines"]],
                         [["-", "one"], ["+", "ONE"], [" ", "two"]])
        self.assertEqual([[row[0], row[3]] for row in parts[1]["diff"]["hunks"][0]["lines"]], [["+", "brand new"]])
        self.assertNotIn("diff", parts[2])

    def test_unknown_before_and_no_reader_keep_the_old_view(self):
        r = mod.BeforeReader()
        r.put("c1", None)   # the read was refused
        parts = mod.step_from_message(self.MSG, r)["parts"]
        self.assertFalse(any("diff" in p for p in parts))
        self.assertFalse(any("diff" in p for p in mod.step_from_message(self.MSG)["parts"]))

    def test_a_replay_shows_the_note_for_an_edit_that_does_not_fit(self):
        r = mod.BeforeReader()
        r.put("c1", "totally different\n")
        d = mod.step_from_message(self.MSG, r)["parts"][0]["diff"]
        self.assertEqual(d["hunks"], [])
        self.assertEqual(d["note"], "could not place this edit in the file")


def fixture_calls(fx):
    """The captured turn's edit/write calls, in the order they appear in the history."""
    out = []
    for msg in fx["messages"]:
        if msg.get("type") != "assistant":
            continue
        for p in msg.get("content") or []:
            if p.get("type") == "tool" and p.get("name") in ("edit", "write"):
                out.append(p)
    return out


def call_path(p):
    """The file a captured call names ("" when the capture dropped its input, so nothing can be diffed)."""
    inp = (p.get("state") or {}).get("input")
    return input_val(inp, "path")[0] if isinstance(inp, dict) else ""


class FixtureTurnTests(unittest.TestCase):
    """The captured turn replayed through the before cache: every call is diffed against the file as it was at that
    call, the way the live window does it while the agent works."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "fixtures", "learnlab-iq3-turn.json")) as fh:
            cls.fx = json.load(fh)
        cls.calls = fixture_calls(cls.fx)

    def setUp(self):
        mod.BEFORE.clear()
        self.seeded, self.chained = mod.fixture_seed(self.fx)

    def tearDown(self):
        mod.BEFORE.clear()

    def test_the_capture_starts_at_a_user_prompt_and_has_calls_to_replay(self):
        prompt, count = mod.fixture_turn(self.fx)
        self.assertTrue(MSG_RE.match(prompt))
        self.assertEqual(count, len(self.calls))
        self.assertTrue(any(p["name"] == "edit" for p in self.calls))

    def test_the_fixture_chat_id_comes_from_the_session_directory(self):
        self.assertEqual(mod.fixture_chat(self.fx),
                         os.path.basename(self.fx["session"]["directory"].rstrip("/")))

    def test_calls_with_usable_input_get_a_before_and_the_dropped_one_does_not(self):
        usable = [p for p in self.calls if call_path(p)]
        self.assertEqual(self.seeded, len(usable))
        self.assertGreaterEqual(len(usable), 1)
        for p in self.calls:
            if not call_path(p):
                self.assertFalse(mod.BEFORE.has(p["id"]), p["id"])  # no usable input: no before, no diff

    def test_each_call_diffs_against_the_file_as_it_was_at_that_call(self):
        removed = 0
        for p in self.calls:
            if not mod.BEFORE.has(p["id"]):
                continue
            inp = (p.get("state") or {}).get("input")
            before = mod.BEFORE.get(p["id"])
            d = diff_of_call(before, p["name"], inp)
            self.assertIsNotNone(d, p["id"])
            old_lines = split_lines(before)
            for kind, no, nno, text, _ in rows_of(d):
                if kind in (" ", "-"):
                    self.assertEqual(text, old_lines[no - 1], "%s line %d" % (p["id"], no))
                if kind == "-":
                    removed += 1
        self.assertGreater(removed, 0, "the captured turn's edits should remove lines from existing files")

    def test_a_second_edit_of_a_file_is_diffed_against_the_first_edit_s_result(self):
        """db.py is edited twice in the capture. The second diff must count lines from the file as the first edit
        left it, not as the capture found it."""
        path = "backend/services/db.py"
        edits = [p for p in self.calls if call_path(p) == path]
        self.assertEqual(len(edits), 2, "the capture edits %s twice" % path)
        first, second = edits
        b1, b2 = mod.BEFORE.get(first["id"]), mod.BEFORE.get(second["id"])
        self.assertEqual(b1, self.fx["files"][path]["before"], "the first call sees the file as the turn found it")
        self.assertNotEqual(b2, b1, "the later call should see the earlier call's result")
        self.assertEqual(b2, apply_edit(b1, first["state"]["input"]["oldString"],
                                        first["state"]["input"]["newString"],
                                        bool(first["state"].get("replaceAll"))))
        d2 = diff_of_call(b2, "edit", second["state"]["input"])
        self.assertTrue(rows_of(d2))
        self.assertEqual(len(split_lines(b2)) + 1, len(split_lines(b1)))  # the first edit netted one line out
        line = split_lines(b2)[d2["hunks"][0]["old_start"] - 1]  # the line the second diff starts from
        self.assertEqual(split_lines(b1).index(line) + 1, d2["hunks"][0]["old_start"] + 1)

    def test_replaying_the_calls_lands_on_the_files_the_calls_explain(self):
        """Replaying every edit/write in order lands on the content the capture recorded. The one file it cannot
        explain is the file that also had a call whose input the capture dropped - so at most one may be off."""
        touched = sorted({call_path(p) for p in self.calls if call_path(p)})
        self.assertIn("backend/services/db.py", touched)  # the file with two usable edits
        off = [p for p in touched if self.chained.get(p) != self.fx["files"].get(p, {}).get("after")]
        self.assertLessEqual(len(off), len(self.calls) - self.seeded, "only files with a dropped call may differ")
        db = "backend/services/db.py"
        self.assertEqual(self.chained[db], self.fx["files"][db]["after"],
                         "the two edits of %s replayed in order should land on the captured after" % db)

    def test_the_fixture_answers_the_message_endpoints_the_snapshot_needs(self):
        get = mod.fixture_oc_get(self.fx)
        sid = self.fx["session"]["id"]
        self.assertEqual(get("/api/session/active"), {"data": []})  # a captured turn is over
        self.assertEqual(get("/api/session/%s" % sid)["data"]["id"], sid)
        want = self.fx["messages"][0]["id"]
        self.assertEqual(get("/api/session/%s/message/%s" % (sid, want))["data"]["id"], want)
        self.assertEqual(len(get("/api/session/%s/message?limit=100" % sid)["data"]), len(self.fx["messages"]))
        small = get("/api/session/%s/message?limit=5" % sid)
        self.assertEqual(len(small["data"]), 5)
        self.assertIn("cursor", small)
        nxt = get("/api/session/%s/message?limit=5&cursor=%s" % (sid, small["cursor"]["next"]))["data"]
        self.assertEqual([m["id"] for m in nxt[:5]], [m["id"] for m in self.fx["messages"][5:10]])
        desc = get("/api/session/%s/message?limit=1&order=desc" % sid)["data"]
        self.assertEqual(desc[0]["id"], max(m["id"] for m in self.fx["messages"]))
        with self.assertRaises(LookupError):
            get("/api/session/other/message/msg_x")

    def test_a_replayed_edit_carries_its_diff_in_the_snapshot_step(self):
        get = mod.fixture_oc_get(self.fx)
        sid = self.fx["session"]["id"]
        msg_id = next(m["id"] for m in self.fx["messages"]
                      if any(p.get("type") == "tool" and p.get("name") == "edit" for p in (m.get("content") or [])))
        msg = get("/api/session/%s/message/%s" % (sid, msg_id))["data"]
        item = next(p for p in msg["content"] if p.get("type") == "tool" and p.get("name") == "edit")
        step = mod.step_from_message(msg, mod.BEFORE)
        tool = next(t for t in step["parts"] if t["p"] == "tool" and t.get("id") == item["id"])
        self.assertTrue(tool["diff"], "an edit whose before was cached should carry hunks")
        self.assertTrue(rows_of(tool["diff"]))


class FixtureFilesTests(unittest.TestCase):
    """The fixture's own before/after pairs must diff cleanly through the same engine."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "fixtures", "learnlab-iq3-turn.json")) as fh:
            cls.files = json.load(fh)["files"]

    def test_changed_files_diff_without_exceptions(self):
        changed = 0
        for path, pair in self.files.items():
            before, after = pair["before"], pair["after"]
            if before == after:
                continue
            changed += 1
            d = build_diff(before, after)
            self.assertTrue(d["hunks"], path)
            old_lines, new_lines = split_lines(before), split_lines(after)
            for h in d["hunks"]:
                for kind, no, nno, text, _ in h["lines"]:
                    if kind in (" ", "-"):
                        self.assertEqual(text, old_lines[no - 1], "%s line %d" % (path, no))
                    if kind in (" ", "+"):
                        self.assertEqual(text, new_lines[nno - 1], "%s line %d" % (path, nno))
        self.assertGreater(changed, 0)

    def test_db_py_hunks_match_its_before(self):
        pair = self.files["backend/services/db.py"]
        d = build_diff(pair["before"], pair["after"])
        self.assertTrue(d["hunks"])
        old_lines = split_lines(pair["before"])
        first = d["hunks"][0]["lines"][0]
        self.assertEqual(first[3], old_lines[first[1] - 1])  # the hunk header names a real line of the before file


if __name__ == "__main__":
    unittest.main(verbosity=2)
