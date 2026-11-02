#!/usr/bin/env python3
"""Tests for the git diff parsing in code-live.py (pure functions, no server, no git).

    python3 -m unittest -v test_gitdiff.py
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("code_live", os.path.join(HERE, "code-live.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

parse_git_diff = mod.parse_git_diff
is_git_diff_command = mod.is_git_diff_command
gitdiff_of_call = mod.gitdiff_of_call
full_content_text = mod.full_content_text
content_text = mod.content_text
step_from_message = mod.step_from_message
GIT_ROW_CAP = mod.GIT_ROW_CAP

# a small realistic git diff: a.py changed, b.py new, c.py deleted
DIFF = "\n".join([
    "diff --git a/a.py b/a.py",
    "index 1111111..2222222 100644",
    "--- a/a.py",
    "+++ b/a.py",
    "@@ -1,4 +1,4 @@",
    " def a():",
    "-    return 0",
    "+    return 1",
    "     # end",
    "diff --git a/b.py b/b.py",
    "new file mode 100644",
    "index 0000000..3333333",
    "--- /dev/null",
    "+++ b/b.py",
    "@@ -0,0 +1,2 @@",
    "+print('b')",
    "+x = 1",
    "diff --git a/c.py b/c.py",
    "deleted file mode 100644",
    "index 4444444..0000000",
    "--- a/c.py",
    "+++ /dev/null",
    "@@ -1,2 +0,0 @@",
    "-gone 1",
    "-gone 2",
])


def files_of(text):
    files, note = parse_git_diff(text)
    return {f["path"]: f for f in files}, note


def rows(f):
    return [r for h in f["hunks"] for r in h["lines"]]


class DetectTests(unittest.TestCase):
    def test_git_diff_and_show_commands_are_detected(self):
        for cmd in ("git diff", "git diff --cached", "git --no-pager diff HEAD", "git show",
                    "git -C /tmp/x diff", "git --git-dir /r/.git diff a.py", "cd repo && git diff",
                    "git diff | head -50", "GIT_PAGER=cat git show HEAD~1", "git -c color.ui=never diff"):
            self.assertTrue(is_git_diff_command(cmd), cmd)

    def test_other_commands_are_not(self):
        for cmd in ("cat changes.diff", "git status", "git grep diff file", "git log -p",
                    "echo 'git diff'", "git show-ref", "grep diff a.py", "python show.py",
                    "git", ""):
            self.assertFalse(is_git_diff_command(cmd), cmd)
        self.assertFalse(is_git_diff_command(None))

    def test_unbalanced_quotes_are_not_a_diff_and_do_not_raise(self):
        # shlex raises ValueError on these; it used to take the whole live view down ("Live view lost opencode")
        for cmd in ('git diff "a.py', 'echo "it is git show', "git show HEAD:'x.py"):
            self.assertFalse(is_git_diff_command(cmd), cmd)


class ParseTests(unittest.TestCase):
    def test_every_file_of_the_diff_is_there(self):
        files, note = files_of(DIFF)
        self.assertIsNone(note)
        self.assertEqual(sorted(files), ["a.py", "b.py", "c.py"])
        for name in files:
            self.assertTrue(files[name]["hunks"])
            self.assertEqual(files[name]["added"], sum(1 for r in rows(files[name]) if r[0] == "+"))
            self.assertEqual(files[name]["removed"], sum(1 for r in rows(files[name]) if r[0] == "-"))

    def test_a_changed_line_has_its_real_numbers(self):
        f = files_of(DIFF)[0]["a.py"]
        self.assertEqual(f["added"], 1)
        self.assertEqual(f["removed"], 1)
        self.assertEqual(rows(f), [
            [" ", 1, 1, "def a():", []],
            ["-", 2, None, "    return 0", [[11, 12]]],
            ["+", None, 2, "    return 1", [[11, 12]]],
            [" ", 3, 3, "    # end", []],
        ])

    def test_a_new_file_is_all_added(self):
        f = files_of(DIFF)[0]["b.py"]
        self.assertEqual((f["added"], f["removed"]), (2, 0))
        self.assertEqual([(r[2], r[3]) for r in rows(f)], [(1, "print('b')"), (2, "x = 1")])

    def test_a_deleted_file_is_all_removed(self):
        f = files_of(DIFF)[0]["c.py"]
        self.assertEqual((f["added"], f["removed"]), (0, 2))
        self.assertEqual([(r[1], r[3]) for r in rows(f)], [(1, "gone 1"), (2, "gone 2")])

    def test_inserted_lines_shift_the_new_numbers(self):
        text = "\n".join([
            "diff --git a/b.py b/b.py", "--- a/b.py", "+++ b/b.py",
            "@@ -3,4 +3,6 @@",
            " b 3", " b 4", " b 5",
            "+new 1", "+new 2",
            " b 6", " b 7",
        ])
        f = files_of(text)[0]["b.py"]
        self.assertEqual([(r[2], r[3]) for r in rows(f) if r[0] == "+"], [(6, "new 1"), (7, "new 2")])
        self.assertIn((6, 8, "b 6"), [(r[1], r[2], r[3]) for r in rows(f) if r[0] == " "])
        self.assertEqual(f["hunks"][0]["old_start"], 3)
        self.assertEqual(f["hunks"][0]["new_start"], 3)

    def test_hunk_header_without_counts_and_no_newline_note(self):
        text = "\n".join([
            "diff --git a/s.py b/s.py", "--- a/s.py", "+++ b/s.py",
            "@@ -1 +1 @@",
            "-one", "\\ No newline at end of file",
            "+one ", "\\ No newline at end of file",
        ])
        f = files_of(text)[0]["s.py"]
        self.assertEqual([r[0] for r in rows(f)], ["-", "+"])
        self.assertEqual(f["hunks"][0]["old_start"], 1)
        self.assertEqual(f["hunks"][0]["new_start"], 1)

    def test_commit_header_lines_do_not_become_rows(self):
        text = "\n".join([
            "commit 3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a",
            "Author: Someone <s@example.com>",
            "Date:   Sun Oct 18 12:00:00 2026 +0000",
            "",
            "    fix a.py (+ keep this line out of the diff)",
            "",
            "diff --git a/a.py b/a.py", "--- a/a.py", "+++ b/a.py",
            "@@ -1,1 +1,1 @@",
            "-old", "+new",
        ])
        files, note = files_of(text)
        self.assertEqual(list(files), ["a.py"])
        self.assertEqual([r[3] for r in rows(files["a.py"])], ["old", "new"])

    def test_not_a_diff_parses_to_nothing(self):
        self.assertEqual(parse_git_diff("total 3\n a.py | 2 +-\n"), ([], None))
        self.assertEqual(parse_git_diff(""), ([], None))
        self.assertEqual(parse_git_diff("hello\nworld\n"), ([], None))

    def test_row_cap_stops_and_says_so(self):
        body = "".join("+line %d\n" % i for i in range(GIT_ROW_CAP + 500))
        text = "diff --git a/big.py b/big.py\n--- /dev/null\n+++ b/big.py\n@@ -0,0 +1,2500 @@\n" + body
        files, note = parse_git_diff(text)
        self.assertEqual(len(files), 1)
        self.assertEqual(len(rows(files[0])), GIT_ROW_CAP)
        self.assertEqual(files[0]["added"], GIT_ROW_CAP)
        self.assertTrue(note)
        self.assertEqual(gitdiff_of_call({"command": "git diff"},
                                         [{"type": "text", "text": text}])[1], note)

    def test_quoted_paths_and_timestamps(self):
        text = "\n".join([
            "diff --git a/my file.py b/my file.py",
            "--- a/my file.py\t2026-10-18",
            "+++ b/my file.py\t2026-10-19",
            "@@ -1,1 +1,1 @@",
            "-x", "+y",
        ])
        self.assertEqual(list(files_of(text)[0]), ["my file.py"])


class CallTests(unittest.TestCase):
    def test_cat_of_a_diff_file_gets_nothing(self):
        files, note = gitdiff_of_call({"command": "cat changes.diff"}, [{"type": "text", "text": DIFF}])
        self.assertIsNone(files)
        self.assertIsNone(note)

    def test_git_command_with_non_diff_output_gets_nothing(self):
        files, note = gitdiff_of_call({"command": "git diff --stat"},
                                      [{"type": "text", "text": " a.py | 2 +-\n 1 file changed"}])
        self.assertIsNone(files)

    def test_full_content_is_parsed_past_the_output_cap(self):
        body = "".join("+padding line %04d %s\n" % (i, "x" * 40) for i in range(120))
        text = "diff --git a/e.py b/e.py\n--- a/e.py\n+++ b/e.py\n@@ -1,1 +1,121 @@\n keep\n" + body
        self.assertGreater(len(text), len(content_text([{"type": "text", "text": text}], "shell")))
        files, note = gitdiff_of_call({"command": "git diff"}, [{"type": "text", "text": text}])
        self.assertIsNone(note)
        self.assertEqual(files[0]["added"], 120)
        self.assertEqual(full_content_text([{"type": "text", "text": text}]), text)

    def test_step_from_message_adds_gitdiff_to_completed_shell_calls(self):
        msg = {"id": "msg_1", "type": "assistant", "content": [
            {"type": "tool", "id": "call_g", "name": "shell", "state": {
                "status": "completed", "input": {"command": "git diff --cached"},
                "content": [{"type": "text", "text": DIFF}]}},
            {"type": "tool", "id": "call_c", "name": "shell", "state": {
                "status": "completed", "input": {"command": "cat changes.diff"},
                "content": [{"type": "text", "text": DIFF}]}}]}
        parts = step_from_message(msg)["parts"]
        self.assertEqual(sorted(f["path"] for f in parts[0]["gitdiff"]), ["a.py", "b.py", "c.py"])
        self.assertNotIn("gitdiff", parts[1])
        self.assertNotIn("gitdiff", step_from_message({
            "id": "m", "type": "assistant", "content": [
                {"type": "tool", "id": "x", "name": "shell", "state": {
                    "status": "running", "input": {"command": "git diff"}}}]}
        )["parts"][0])


if __name__ == "__main__":
    unittest.main()
