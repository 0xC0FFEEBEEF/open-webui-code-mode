#!/usr/bin/env python3
"""Tests for the read views in code-live.py: the shell-command parser (which files a command reads and which
lines of them it asked for) and the file views built from them. Pure functions plus the captured turn; no server.

    python3 -m unittest -v test_read.py
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("code_live", os.path.join(HERE, "code-live.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

read_targets = mod.read_targets
read_tool_target = mod.read_tool_target


def brief(targets):
    """What a parse gave, in the order the command named it: (path, how, ranges, tail, grep, limit, cd)."""
    return [(t["path"], t["how"], t["ranges"], t["tail"], t["grep"], t["limit"], t["cd"]) for t in targets]


def one(command):
    """The single view a command should produce, as one tuple."""
    ts = read_targets(command)
    assert len(ts) == 1, ts
    return brief(ts)[0]


def paths(command):
    return [t["path"] for t in read_targets(command)]


class WholeFileTests(unittest.TestCase):
    def test_cat_names_its_files(self):
        self.assertEqual(one("cat a.py"), ("a.py", "cat", None, None, False, None, ""))
        self.assertEqual(paths("cat a.py b/c.py"), ["a.py", "b/c.py"])
        self.assertEqual(paths("cat -n a.py"), ["a.py"])          # numbered output, still the whole file
        self.assertEqual(paths("cat -- a.py"), ["a.py"])

    def test_nl(self):
        self.assertEqual(one("nl -ba HANDOFF.md"), ("HANDOFF.md", "nl", None, None, False, None, ""))

    def test_more_files_and_quoted_names(self):
        self.assertEqual(paths("cat 'my file.py' \"other.py\""), ["my file.py", "other.py"])


class HeadTailTests(unittest.TestCase):
    def test_head_forms(self):
        self.assertEqual(one("head a.py"), ("a.py", "head", [[1, 10]], None, False, None, ""))
        self.assertEqual(one("head -n 20 a.py"), ("a.py", "head", [[1, 20]], None, False, None, ""))
        self.assertEqual(one("head -5 frontend/src/App.tsx"),
                         ("frontend/src/App.tsx", "head", [[1, 5]], None, False, None, ""))
        self.assertEqual(one("head -n +40 a.py"), ("a.py", "head", [[40, None]], None, False, None, ""))

    def test_tail_forms(self):
        self.assertEqual(one("tail a.py"), ("a.py", "tail", None, 10, False, None, ""))
        self.assertEqual(one("tail -n 8 HANDOFF.md"), ("HANDOFF.md", "tail", None, 8, False, None, ""))
        self.assertEqual(one("tail -8 HANDOFF.md"), ("HANDOFF.md", "tail", None, 8, False, None, ""))
        self.assertEqual(one("tail -n +30 a.py"), ("a.py", "tail", [[30, None]], None, False, None, ""))

    def test_more_than_one_file(self):
        self.assertEqual(paths("tail -5 a.log b.log"), ["a.log", "b.log"])
        self.assertEqual([t["tail"] for t in read_targets("tail -5 a.log b.log")], [5, 5])

    def test_options_that_are_not_line_counts(self):
        for cmd in ("head -c 200 a.py", "tail -f a.log", "tail -n 5 -f a.log", "head -z a.py", "head -n"):
            self.assertEqual(read_targets(cmd), [], cmd)


class SedTests(unittest.TestCase):
    def test_range_forms(self):
        self.assertEqual(one("sed -n '60,120p' backend/auth/service.py"),
                         ("backend/auth/service.py", "sed", [[60, 120]], None, False, None, ""))
        self.assertEqual(one("sed -n '7p' a.py"), ("a.py", "sed", [[7, 7]], None, False, None, ""))
        self.assertEqual(one("sed -n '40,$p' a.py"), ("a.py", "sed", [[40, None]], None, False, None, ""))
        self.assertEqual(one("sed -n 3,9p a.py"), ("a.py", "sed", [[3, 9]], None, False, None, ""))

    def test_several_ranges_in_one_script_or_several_e(self):
        self.assertEqual(one("sed -n '1,10p;40,45p' a.py"), ("a.py", "sed", [[1, 10], [40, 45]], None, False, None, ""))
        self.assertEqual(one("sed -e '1,5p' -e '9,12p' -n a.py"),
                         ("a.py", "sed", [[1, 5], [9, 12]], None, False, None, ""))
        self.assertEqual(one("sed -n '1,3p;2,6p' a.py"), ("a.py", "sed", [[1, 6]], None, False, None, ""))  # joined

    def test_one_script_two_files(self):
        self.assertEqual(paths("sed -n '1,40p' a.py b.py"), ["a.py", "b.py"])

    def test_flags_that_still_print_the_range(self):
        self.assertEqual(one("sed -ne '1,5p' a.py"), ("a.py", "sed", [[1, 5]], None, False, None, ""))
        self.assertEqual(one("sed -En '1,5p' a.py"), ("a.py", "sed", [[1, 5]], None, False, None, ""))

    def test_anything_else_prints_nothing_we_can_name(self):
        for cmd in ("sed -i 's/a/b/' a.py", "sed -n 's/a/b/p' a.py", "sed -n '5d' a.py",
                    "sed -n '/start/,/end/p' a.py", "sed -n '1~2p' a.py", "sed -n 'p' a.py",
                    "sed -n '120,60p' a.py", "sed -n '1,5p'", "sed -n '1,5p' -"):
            self.assertEqual(read_targets(cmd), [], cmd)

    def test_without_n_sed_prints_the_whole_file_whatever_the_script_says(self):
        for cmd in ("sed '1,5p' a.py", "sed -e '1,5p' a.py"):
            self.assertEqual(one(cmd), ("a.py", "sed", None, None, False, None, ""), cmd)


class GrepTests(unittest.TestCase):
    def test_numbered_grep_of_named_files(self):
        t = read_targets('grep -n "bcrypt" backend/auth/service.py')[0]
        self.assertEqual((t["path"], t["how"], t["grep"], t["many"]),
                         ("backend/auth/service.py", "grep", True, False))
        self.assertIsNone(t["ranges"])                      # the numbers come from the output
        two = read_targets('grep -n "signup" frontend/src/App.tsx frontend/src/lib/api.ts')
        self.assertEqual([t["path"] for t in two], ["frontend/src/App.tsx", "frontend/src/lib/api.ts"])
        self.assertTrue(all(t["many"] for t in two))

    def test_combined_and_long_flags(self):
        self.assertEqual(paths('grep -in "x" a.py'), ["a.py"])
        self.assertEqual(paths('grep -nE "a|b" a.py'), ["a.py"])
        self.assertEqual(paths('grep --line-number x a.py'), ["a.py"])
        self.assertEqual(paths('grep -n --color=auto x a.py'), ["a.py"])
        self.assertEqual(paths('grep -nC 2 x a.py'), ["a.py"])   # context lines arrive with the output

    def test_a_pattern_is_not_a_file(self):
        t = read_targets('grep -n "a b" a.py')[0]
        self.assertEqual(t["path"], "a.py")
        t = read_targets('grep -n -e "one" -e "two" a.py')[0]
        self.assertEqual(t["path"], "a.py")

    def test_without_numbering_or_files_or_plain_it_is_not_a_view(self):
        for cmd in ('grep "x" a.py',                        # no -n: no line numbers to highlight
                    'grep -rn "x" backend/', 'grep -r "x" backend', 'grep -R "x" .',
                    'grep -rn "x" backend --include="*.py"',
                    'grep -c "x" a.py', 'grep -n "x"', 'grep -n "x" backend/',
                    'grep -n "x" $F', 'grep -n "x" routers/*.py', 'grep -n x .',
                    'grep -n "x" a.py | grep -v ".venv"'):      # piped into something that changes the text
            self.assertEqual(read_targets(cmd), [], cmd)


class AwkTests(unittest.TestCase):
    def test_the_two_numbered_forms(self):
        self.assertEqual(one("awk 'NR>=60 && NR<=120' a.py"),
                         ("a.py", "awk", [[60, 120]], None, False, None, ""))
        self.assertEqual(one("awk 'NR==60,NR==120' a.py"),
                         ("a.py", "awk", [[60, 120]], None, False, None, ""))
        self.assertEqual(one("awk 'NR<=120 && NR>=60' a.py"),
                         ("a.py", "awk", [[60, 120]], None, False, None, ""))
        self.assertEqual(one("awk -F, 'NR>=2 && NR<=9' data.csv"),
                         ("data.csv", "awk", [[2, 9]], None, False, None, ""))

    def test_any_other_program(self):
        for cmd in ("awk '{print $1}' a.py", "awk 'NR==3' a.py", "awk 'BEGIN{print 1}' a.py",
                    "awk 'NR>=2' a.py", "awk 'NR>=2 && NR<=9'"):
            self.assertEqual(read_targets(cmd), [], cmd)


class CdTests(unittest.TestCase):
    def test_a_cd_sets_the_directory_later_paths_are_read_from(self):
        self.assertEqual(one("cd sub && cat x"), ("x", "cat", None, None, False, None, "sub"))
        self.assertEqual(one("cd a/b && head -3 x")[6], "a/b")
        self.assertEqual(one("cd a && cd b && cat x")[6], "a/b")
        self.assertEqual(one("cd /home/p/project && cat x")[6], "/home/p/project")
        self.assertEqual(one("cd /home/p && cd sub && cat x")[6], "/home/p/sub")

    def test_a_cd_we_cannot_follow_turns_reads_after_it_into_nothing(self):
        self.assertEqual(read_targets("cd $DIR && cat x"), [])
        self.assertEqual(read_targets("cd && cat x"), [])
        self.assertEqual(read_targets("cd a && cd ../b && cat x")[0]["cd"], "b")   # .. folded, as the shell folds it
        self.assertEqual(read_targets("cd a 2>/dev/null && cat x")[0]["cd"], "a")


class PipeTests(unittest.TestCase):
    def test_trailing_head_keeps_the_view_and_notes_the_cut(self):
        t = read_targets('grep -n "signup" a.py | head -50')[0]
        self.assertEqual(t["limit"], 50)
        t = read_targets("cat a.py | head -20")[0]
        self.assertEqual((t["path"], t["ranges"], t["limit"]), ("a.py", None, 20))
        t = read_targets("cat a.py | head -n 20")[0]
        self.assertEqual(t["limit"], 20)
        t = read_targets("head -30 a.py | head -5")[0]
        self.assertEqual((t["ranges"], t["limit"]), ([[1, 30]], 5))

    def test_any_other_consumer_voids_the_producer(self):
        self.assertEqual(read_targets("cat a.py | grep x"), [])
        self.assertEqual(read_targets("cat a.py | wc -l"), [])
        self.assertEqual(read_targets("grep -n x a.py | cat"), [])
        self.assertEqual(read_targets("head -30 a.py | tail -5"), [])

    def test_a_consumer_that_names_its_own_files_still_reads_them(self):
        self.assertEqual(paths("cat a.py | sed -n '1,5p' b.py"), ["b.py"])

    def test_a_head_that_names_no_files_is_only_a_filter(self):
        self.assertEqual(read_targets("git ls-files | grep -iE x | head"), [])


class RedirectionTests(unittest.TestCase):
    def test_redirections_do_not_change_what_was_read(self):
        self.assertEqual(paths("cat a.py 2>/dev/null"), ["a.py"])
        self.assertEqual(paths("cat a.py > /tmp/out 2>&1"), ["a.py"])
        self.assertEqual(read_targets("sleep 1; .venv/bin/python x.py > log 2>&1 < /dev/null"), [])
        self.assertEqual(read_targets("cd sub && cat x 2>/dev/null || find . -name x")[0]["path"], "x")

    def test_stdin_is_not_a_file(self):
        self.assertEqual(read_targets("cat -"), [])
        self.assertEqual(read_targets("grep -n x -"), [])


class NotAReadTests(unittest.TestCase):
    def test_commands_that_read_no_file_this_way(self):
        for cmd in ("ls", "ls -la backend", "python x.py", ".venv/bin/python -m uvicorn main:app",
                    "curl -s localhost:8000/api/health", "git diff --stat", "git diff -- a.py | head -120",
                    "echo hello", "pwd", "find backend -name 'service.py'", "touch a.py",
                    "fuser -k 8000/tcp", "desktop type 2 \"x\" --enter 2>&1 | head -30", "wc -l a.py",
                    "cat *.py", "cat $F", "cat ${F}", "cat `which x`", "cat ~/a.py", "cat a[1].py",
                    "cat 'a b'/*.py", "sudo cat a.py", "time cat a.py", "command cat a.py"):
            self.assertEqual(read_targets(cmd), [], cmd)

    def test_heredoc_poisons_the_whole_command(self):
        for cmd in ("cat >> HANDOFF.md <<'EOF'\nbody\nEOF\necho done",
                    "python - <<EOF\nprint(1)\nEOF",
                    "cat a.py <<EOF"):
            self.assertEqual(read_targets(cmd), [], cmd)

    def test_process_substitution_and_unbalanced_quotes(self):
        self.assertEqual(read_targets("cat <(head -3 a.py)"), [])
        self.assertEqual(paths("cat a.py"), ["a.py"])
        self.assertEqual(read_targets("cat 'a.py"), [])            # unbalanced quotes
        self.assertEqual(read_targets('grep -n "x" a.py && sed -n "1,2p'), [])


class NeverRaisesTests(unittest.TestCase):
    def test_anything_at_all_comes_back_as_a_list(self):
        junk = ["", "   ", "\n", ";", "&&", "||", "|", "&", "cat", "cat -", "'''", '"""', "$(x)",
                "cat a.py &&", ";;;&", "\\", "'", '"', "sed -n ''", "grep -n", "awk", "head -",
                "cat $(echo a.py)", "cat a.py > ", "x | ", "cat a; b; c", None, 5, 0, [], {}, True,
                "cat a.py; cat b.py", "tail -n +", "head -n +a", "\x00", "a" * 5000]
        for c in junk:
            self.assertIsInstance(read_targets(c), list, repr(c))

    def test_parsing_is_only_about_text(self):
        # nothing here exists on disk, and none of it is run: a parse never looks at the filesystem
        self.assertEqual(paths("cat /definitely/not/here.py"), ["/definitely/not/here.py"])
        self.assertEqual(read_targets("cd . && cat .."), [])      # . and .. are directories, not files
        self.assertEqual(one("cd .. && cat x"), ("x", "cat", None, None, False, None, ".."))


class ReadToolTargetTests(unittest.TestCase):
    def test_offset_and_limit_are_a_line_range(self):
        t = read_tool_target({"path": "frontend/src/App.tsx", "offset": 85, "limit": 90})
        self.assertEqual((t["path"], t["how"], t["ranges"]), ("frontend/src/App.tsx", "read", [[85, 174]]))
        self.assertEqual(read_tool_target({"path": "main.py", "offset": 35, "limit": 50})["ranges"], [[35, 84]])
        self.assertEqual(read_tool_target({"path": "a.py", "offset": 7})["ranges"], [[7, None]])

    def test_no_offset_or_limit_is_the_whole_file(self):
        t = read_tool_target({"path": "backend/routers/service.py"})
        self.assertEqual((t["path"], t["ranges"], t["cd"]), ("backend/routers/service.py", None, ""))

    def test_input_that_names_no_file(self):
        for inp in ({"offset": 5}, None, {}, "a.py", {"path": ""}, {"path": 5}, {"path": True},
                    {"path": "$F"}, {"path": "*.py"}, {"path": "."}, {"path": "sub/"}):
            self.assertIsNone(read_tool_target(inp), repr(inp))

    def test_offset_and_limit_arriving_as_text_still_count(self):
        self.assertEqual(read_tool_target({"path": "a.py", "offset": "35"})["ranges"], [[35, None]])
        self.assertEqual(read_tool_target({"path": "a.py", "offset": "1", "limit": "12"})["ranges"], [[1, 12]])
        self.assertEqual(read_tool_target({"path": "a.py", "offset": 0, "limit": -4})["ranges"], None)


def fixture_tool_calls(fx):
    """The captured turn's shell and read calls, in the order they appear in the history."""
    out = []
    for msg in fx["messages"]:
        if msg.get("type") != "assistant":
            continue
        for p in msg.get("content") or []:
            if p.get("type") == "tool" and p.get("name") in ("shell", "read"):
                out.append(p)
    return out


class FixtureCommandTests(unittest.TestCase):
    """Every shell command of the captured turn through the parser: each one parses, and the reads the agent
    really did come out as the ranges they asked for."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "fixtures", "learnlab-iq3-turn.json")) as fh:
            cls.fx = json.load(fh)
        cls.calls = fixture_tool_calls(cls.fx)

        def command(c):
            inp = (c.get("state") or {}).get("input")       # a dropped call keeps a string, not its input
            return inp.get("command") if c["name"] == "shell" and isinstance(inp, dict) else None

        cls.cmds = [command(c) for c in cls.calls]
        cls.SESSION = cls.fx["session"]["directory"]

    def test_the_capture_has_shell_and_read_calls_to_parse(self):
        self.assertGreater(len(self.calls), 20)
        self.assertGreater(sum(1 for c in self.cmds if c), 15)

    def test_no_command_of_the_capture_raises_or_parses_to_a_crash(self):
        for c in self.cmds:
            if c:
                self.assertIsInstance(read_targets(c), list, c[:80])

    def test_a_call_whose_input_the_capture_dropped_reads_nothing(self):
        dropped = [c for c, cmd in zip(self.calls, self.cmds)
                   if c["name"] == "shell" and cmd and cmd.startswith("[dropped")]
        self.assertTrue(dropped, "the capture drops the input of some shell calls")
        for c in dropped:
            self.assertEqual(read_targets(c["state"]["input"]["command"]), [], c["id"])

    def test_cat_of_a_file_is_that_file(self):
        cmd = next(c for c in self.cmds if c and "cat backend/routers/auth.py" in c)
        self.assertEqual(brief(read_targets(cmd)),
                         [("backend/routers/auth.py", "cat", None, None, False, None, self.SESSION)])

    def test_the_three_seds_of_one_call_give_three_ranges(self):
        cmd = next(c for c in self.cmds if c and "sed -n '60,120p'" in c)
        got = brief(read_targets(cmd))
        self.assertEqual([(t[0], t[2]) for t in got],
                         [("backend/auth/service.py", [[60, 120]]),
                          ("backend/routers/auth.py", [[55, 120]]),
                          ("backend/services/db.py", [[1, 40]])])
        self.assertTrue(all(t[6] == self.SESSION for t in got))

    def test_a_grep_of_two_files_cut_by_head(self):
        cmd = next(c for c in self.cmds if c and c.rstrip().endswith("| head -50"))
        got = read_targets(cmd)
        self.assertEqual([t["path"] for t in got], ["frontend/src/App.tsx", "frontend/src/lib/api.ts"])
        self.assertTrue(all(t["grep"] and t["many"] and t["limit"] == 50 for t in got))

    def test_one_call_can_read_in_several_ways(self):
        cmd = next(c for c in self.cmds if c and "cat backend/requirements.txt 2>/dev/null" in c)
        got = brief(read_targets(cmd))
        self.assertEqual([(t[0], t[1], t[5]) for t in got],
                         [("backend/services/db.py", "grep", 20), ("backend/requirements.txt", "cat", None)])

    def test_recursive_and_glob_greps_of_the_capture_get_no_view(self):
        for frag in ('grep -rn "password\\|bcrypt', '--include="*.tsx"', "routers/*.py",
                     'grep -rn "from routers.service', "grep -iE"):
            cmd = next(c for c in self.cmds if c and frag in c)
            self.assertEqual([t for t in read_targets(cmd) if t["how"] == "grep"], [], frag)

    def test_head_and_tail_of_the_capture(self):
        self.assertEqual(brief(read_targets(next(c for c in self.cmds if c and "head -5 frontend" in c))),
                         [("frontend/src/App.tsx", "head", [[1, 5]], None, False, None, self.SESSION)])
        self.assertEqual(brief(read_targets(next(c for c in self.cmds if c and "tail -8 HANDOFF.md" in c))),
                         [("HANDOFF.md", "tail", None, 8, False, None, self.SESSION)])

    def test_paths_the_window_cannot_read_are_named_anyway_and_refused_later(self):
        cmd = next(c for c in self.cmds if c and "tail -20 /tmp/llb-8000.log" in c)
        named = [t["path"] for t in read_targets(cmd)]
        self.assertIn("/tmp/llb-8000.log", named)          # outside the session directory: the read refuses it

    def test_the_heredoc_and_desktop_calls_of_the_capture_get_nothing(self):
        for frag in ("<<'EOF'", "desktop type 2", "desktop click 4", "git diff --stat",
                     "curl -s localhost:8000/api/health && curl"):
            cmd = next(c for c in self.cmds if c and frag in c)
            self.assertEqual(read_targets(cmd), [], frag)

    def test_read_calls_of_the_capture(self):
        got = [read_tool_target((c.get("state") or {}).get("input")) for c in self.calls if c["name"] == "read"]
        self.assertEqual([t["ranges"] for t in got if t],
                         [[[85, 174]], [[165, 194]], None, [[40, 59]], [[1, 45]], [[35, 84]]])
        self.assertEqual([t["path"] for t in got if t],
                         ["frontend/src/App.tsx", "backend/services/db.py", "backend/routers/service.py",
                          "frontend/src/lib/api.ts", "frontend/src/App.tsx", "backend/main.py"])



def text(n):
    """A file of n lines whose line i reads "line i"."""
    return "\n".join("line %d" % i for i in range(1, n + 1))


def target(command, i=0):
    """The i-th read target of a command, for building a view from it."""
    ts = read_targets(command)
    assert ts, command
    return ts[i]


def write_file(path, body):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


class ViewTextTests(unittest.TestCase):
    """view_of: the file's text, the asked-for lines marked, and how much of the file it carries."""

    def test_a_small_file_is_carried_whole(self):
        v = mod.view_of(text(3), target("cat a.py"))
        self.assertEqual((v["from"], v["total"], v["ranges"]), (1, 3, [[1, 3]]))
        self.assertEqual(v["lines"], ["line 1", "line 2", "line 3"])
        self.assertEqual((v["how"], v["note"]), ("cat", ""))

    def test_an_empty_file_gives_no_view(self):
        self.assertIsNone(mod.view_of("", target("cat a.py")))

    def test_crlf_lines_are_numbered_like_lf_ones(self):
        v = mod.view_of("a\r\nb\r\nc", target("tail -n 2 a.py"))
        self.assertEqual((v["total"], v["ranges"], v["lines"]), (3, [[2, 3]], ["a", "b", "c"]))

    def test_ranges_are_cut_to_the_file_and_joined_when_they_touch(self):
        self.assertEqual(mod.view_of(text(107), target("sed -n '60,120p' a.py"))["ranges"], [[60, 107]])
        self.assertEqual(mod.view_of(text(20), target("sed -n '1,5p;3,8p' a.py"))["ranges"], [[1, 8]])
        self.assertIsNone(mod.view_of(text(100), target("sed -n '200,300p' a.py")))   # every line past the end

    def test_head_tail_and_awk_ranges(self):
        self.assertEqual(mod.view_of(text(20), target("head -n 5 a.py"))["ranges"], [[1, 5]])
        self.assertEqual(mod.view_of(text(10), target("tail -n 8 a.py"))["ranges"], [[3, 10]])
        self.assertEqual(mod.view_of(text(5), target("tail -n 20 a.py"))["ranges"], [[1, 5]])
        self.assertEqual(mod.view_of(text(50), target("head -n +40 a.py"))["ranges"], [[40, 50]])
        self.assertEqual(mod.view_of(text(9), target("awk 'NR==3,NR==7' a.py"))["ranges"], [[3, 7]])

    def test_the_read_tool_marks_the_lines_it_read(self):
        v = mod.view_of(text(200), read_tool_target({"path": "a.py", "offset": 10, "limit": 4}))
        self.assertEqual((v["ranges"], v["how"]), ([[10, 13]], "read"))
        self.assertEqual(mod.view_of(text(200), read_tool_target({"path": "a.py"}))["ranges"], [[1, 200]])


class ViewWindowTests(unittest.TestCase):
    """A file too big to carry whole: the view shows what was read plus room around it, capped."""

    def test_a_window_around_the_read_lines(self):
        v = mod.view_of(text(5000), target("sed -n '2000,2100p' a.py"))
        self.assertEqual((v["from"], len(v["lines"]), v["ranges"]), (1800, 501, [[2000, 2100]]))
        self.assertEqual(v["lines"][-1], "line 2300")
        self.assertEqual(v["note"], "showing lines 1800-2300 of 5000")

    def test_a_whole_read_of_a_big_file_is_capped(self):
        v = mod.view_of(text(5000), target("cat a.py"))
        self.assertEqual((v["from"], len(v["lines"]), v["ranges"]), (1, mod.VIEW_MAX_LINES, [[1, 2000]]))
        self.assertEqual(v["note"], "showing lines 1-2000 of 5000")

    def test_a_window_never_runs_past_the_end_of_the_file(self):
        v = mod.view_of(text(5000), target("sed -n '4990,5000p' a.py"))
        self.assertEqual((v["from"], v["lines"][-1], v["ranges"]), (4790, "line 5000", [[4990, 5000]]))

    def test_a_file_that_fits_is_never_cut(self):
        v = mod.view_of(text(1500), target("sed -n '700,710p' a.py"))
        self.assertEqual((v["from"], len(v["lines"]), v["note"]), (1, 1500, ""))


class ViewLimitTests(unittest.TestCase):
    """A trailing `| head -n N`: only the first N lines the command printed ever reached the agent."""

    def test_only_the_printed_lines_are_marked(self):
        v = mod.view_of(text(30), target("cat a.py | head -n 5"))
        self.assertEqual((v["ranges"], v["note"]), ([[1, 5]], "the command only printed the first 5 lines"))

    def test_a_limit_after_a_tail_cuts_the_end_of_the_printed_lines(self):
        self.assertEqual(mod.view_of(text(20), target("tail -n 6 a.py | head -n 2"))["ranges"], [[15, 16]])

    def test_a_limit_bigger_than_the_read_changes_nothing(self):
        v = mod.view_of(text(10), target("cat a.py | head -n 50"))
        self.assertEqual((v["ranges"], v["note"]), ([[1, 10]], ""))


class GrepViewTests(unittest.TestCase):
    """grep chose which lines matched, so a grep view's marks come from the call's own output."""

    def test_the_line_numbers_in_the_output_become_the_marks(self):
        t = target("grep -n get_user backend/auth/service.py")
        v = mod.view_of(text(40), t, "12: def get_user(u):\n30:     u = get_user(x)")
        self.assertEqual(v["ranges"], [[12, 12], [30, 30]])

    def test_context_lines_count_too(self):
        t = target("grep -n -C 1 get_user a.py")
        self.assertEqual(mod.view_of(text(50), t, "41-something\n42: get_user\n43-other")["ranges"], [[41, 43]])

    def test_several_files_only_mark_their_own_matches(self):
        ts = read_targets("grep -n get_user a.py b.py")
        self.assertTrue(all(t["many"] for t in ts))
        out = "a.py:3: x\nb.py:9: y\na.py:11: z"
        self.assertEqual(mod.view_of(text(20), ts[0], out)["ranges"], [[3, 3], [11, 11]])
        self.assertEqual(mod.view_of(text(20), ts[1], out)["ranges"], [[9, 9]])

    def test_a_grep_that_matched_nothing_gives_no_view(self):
        self.assertIsNone(mod.view_of(text(20), target("grep -n nosuchthing a.py"), "no matches"))

    def test_two_reads_of_one_file_merge_their_marks_into_one_view(self):
        ts = read_targets("sed -n '2,4p' a.py && grep -n line a.py")
        v = mod.view_of(text(20), ts, "3: line 3\n9: line 9")
        self.assertEqual(v["ranges"], [[2, 4], [9, 9]])              # sed's lines and grep's lines, one view
        self.assertEqual(v["how"], "sed + grep")

    def test_marks_survive_the_head_of_a_pipe(self):
        t = target("grep -n get_user a.py | head -n 2")
        self.assertEqual(mod.view_of(text(40), t, "12: x\n30: y")["ranges"], [[12, 12], [30, 30]])

    def test_text_that_is_not_a_match_line_is_not_read_as_one(self):
        t = target("grep -n get_user a.py")
        self.assertEqual(mod.view_of(text(10), t, "output header\n3: get_user")["ranges"], [[3, 3]])

    def test_a_result_that_arrived_whole_says_nothing_about_a_cut(self):
        self.assertEqual(mod.view_of(text(40), target("grep -n get_user a.py"), "12: x")["note"], "")

    def test_a_cut_result_says_that_part_of_it_is_missing(self):
        # the window keeps the head and the tail of a long result: the marks in the middle never arrive, and
        # the view has to say so rather than look like it shows every match.
        out = mod.cap_output("\n".join("%d: match" % i for i in range(1, 400)), 500)
        self.assertIn("characters)", out)
        v = mod.view_of(text(500), target("grep -n match a.py"), out)
        self.assertTrue(v["ranges"])
        self.assertEqual(v["note"], "the result was cut short, so some matched lines are not marked")


class PathGuardTests(unittest.TestCase):
    """What a view may read: a real file inside the session's own directory, and nothing else."""

    def setUp(self):
        mod.FIXTURE_NO_DISK = False
        self.tmp = tempfile.mkdtemp(prefix="live-views-")
        self.sess = os.path.join(self.tmp, "session")
        os.makedirs(os.path.join(self.sess, "sub"))
        write_file(os.path.join(self.sess, "a.py"), text(12))
        write_file(os.path.join(self.sess, "sub", "x.py"), text(4))
        write_file(os.path.join(self.tmp, "outside.py"), text(3))
        write_file(os.path.join(self.sess, "empty.py"), "")
        with open(os.path.join(self.sess, "big.py"), "w", encoding="utf-8") as fh:
            fh.write("x" * (mod.DIFF_MAX_FILE + 10))      # bigger than the guard lets a view read
        with open(os.path.join(self.sess, "bin.dat"), "wb") as fh:
            fh.write(b"abc\x00def")
        os.symlink(os.path.join(self.tmp, "outside.py"), os.path.join(self.sess, "link.py"))
        mod.READS.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        mod.READS.clear()

    def test_paths_inside_the_session_resolve_and_other_paths_are_refused(self):
        self.assertEqual(mod.view_rel(self.sess, "a.py", ""), "a.py")
        self.assertEqual(mod.view_rel(self.sess, "x.py", "sub"), "sub/x.py")
        self.assertEqual(mod.view_rel(self.sess, "sub/../a.py", ""), "a.py")
        self.assertEqual(mod.view_rel(self.sess, os.path.join(self.sess, "a.py"), ""), "a.py")
        for bad in ("../outside.py", os.path.join(self.tmp, "outside.py"), ".", "..", "sub/", "-", ""):
            self.assertIsNone(mod.view_rel(self.sess, bad, ""), bad)
        self.assertIsNone(mod.view_rel("", "a.py", ""))

    def test_a_view_after_the_call_says_the_file_is_as_it_is_now(self):
        v = mod.file_view("c1", target("sed -n '3,5p' a.py"), self.sess, None, False)
        self.assertEqual((v["path"], v["now"], v["ranges"]), ("a.py", True, [[3, 5]]))
        self.assertEqual(v["lines"][2], "line 3")

    def test_a_view_taken_while_the_call_runs_is_not_marked_now(self):
        self.assertFalse(mod.file_view("c1", target("cat a.py"), self.sess, None, True)["now"])

    def test_one_call_reads_a_file_once_even_if_the_file_changes_after(self):
        first = mod.file_view("c1", target("cat a.py"), self.sess, None, True)
        write_file(os.path.join(self.sess, "a.py"), text(6))
        again = mod.file_view("c1", target("tail -n 2 a.py"), self.sess, None, True)
        self.assertEqual(first["total"], again["total"])            # the same content, read once
        self.assertEqual(again["ranges"], [[11, 12]])

    def test_two_calls_read_the_file_as_it_was_when_each_one_ran(self):
        mod.file_view("c1", target("cat a.py"), self.sess, None, True)
        write_file(os.path.join(self.sess, "a.py"), text(6))
        self.assertEqual(mod.file_view("c2", target("cat a.py"), self.sess, None, True)["total"], 6)

    def test_a_file_the_guard_refuses_gets_no_view(self):
        for cmd in ("cat ../outside.py", "cat link.py", "cat bin.dat", "cat big.py", "cat nosuch.py",
                    "cat empty.py"):
            ts = read_targets(cmd)
            self.assertTrue(ts, cmd)                                 # the command names the file...
            self.assertIsNone(mod.file_view("c1", ts[0], self.sess, None, False), cmd)   # ...the read refuses it

    def test_a_directory_is_not_a_file_a_view_can_show(self):
        for cmd in ("cat .", "cat ..", "cat sub/"):
            self.assertEqual(read_targets(cmd), [], cmd)          # it names no file, so there is nothing to read

    def test_a_file_that_is_not_there_yet_can_be_found_by_the_next_look(self):
        self.assertIsNone(mod.file_view("c1", target("cat new.py"), self.sess, None, True))
        write_file(os.path.join(self.sess, "new.py"), text(3))
        self.assertEqual(mod.file_view("c1", target("cat new.py"), self.sess, None, True)["total"], 3)

    def test_in_fixture_mode_nothing_is_read_off_disk(self):
        mod.FIXTURE_NO_DISK = True
        try:
            self.assertIsNone(mod.file_view("c1", target("cat a.py"), self.sess, None, False))
        finally:
            mod.FIXTURE_NO_DISK = False


class CallTargetsTests(unittest.TestCase):
    def test_only_read_and_shell_calls_name_files(self):
        self.assertEqual(len(mod.call_targets("shell", {"command": "cat a.py b.py"})), 2)
        self.assertEqual(len(mod.call_targets("read", {"path": "a.py", "offset": 2, "limit": 3})), 1)
        for name, inp in (("grep", {"pattern": "x", "path": "a.py"}), ("edit", {"path": "a.py"}),
                          ("write", {"path": "a.py"}), ("execute", {"code": "open('a.py').read()"}),
                          ("webfetch", {"url": "https://x/a.py"}), ("shell", {"command": ""}),
                          ("shell", None), ("read", {"path": ""}), ("shell", {"command": "grep -rn x backend"})):
            self.assertIsNone(mod.call_targets(name, inp), name)

    def test_a_command_that_reads_nothing_named_names_nothing(self):
        self.assertIsNone(mod.call_targets("shell", {"command": "ls -la && wc -l *.py"}))


def assistant_message(calls):
    """A finished turn shaped the way opencode reports it, for reading the views back out of the history."""
    return {"id": "msg_test", "type": "assistant", "time": {"created": 1, "completed": 2},
            "content": [{"type": "tool", "id": cid, "name": name,
                         "state": {"status": "completed", "input": inp,
                                   "content": [{"type": "text", "text": out}]}}
                        for cid, name, inp, out in calls]}


class SnapshotViewTests(unittest.TestCase):
    """A finished turn: the same views, worked out from the history instead of from live events."""

    def setUp(self):
        mod.FIXTURE_NO_DISK = False
        self.tmp = tempfile.mkdtemp(prefix="live-snap-")
        for name in "abcde":
            write_file(os.path.join(self.tmp, name + ".py"), text(30))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        mod.READS.clear()

    def test_read_and_shell_items_carry_their_views_and_other_items_do_not(self):
        msg = assistant_message([("c1", "shell", {"command": "sed -n '4,6p' a.py"}, "line 4"),
                                 ("c2", "read", {"path": "b.py", "offset": 5, "limit": 3}, "5: line 5"),
                                 ("c3", "edit", {"path": "c.py", "oldString": "", "newString": ""}, "ok"),
                                 ("c4", "shell", {"command": "ls -la"}, "")])
        parts = mod.step_from_message(msg, None, mod.READS, self.tmp)["parts"]
        self.assertEqual([p["name"] for p in parts], ["shell", "read", "edit", "shell"])
        self.assertEqual([p["views"][0]["ranges"] for p in parts[:2]], [[[4, 6]], [[5, 7]]])
        self.assertTrue(all("views" not in p for p in parts[2:]), [p.get("views") for p in parts[2:]])
        self.assertTrue(parts[0]["views"][0]["now"])          # read off disk afterwards: the file as it is now

    def test_a_call_that_reads_several_files_gets_at_most_four_views(self):
        msg = assistant_message([("c1", "shell", {"command": "cat a.py b.py c.py d.py e.py"}, "")])
        parts = mod.step_from_message(msg, None, mod.READS, self.tmp)["parts"]
        views = parts[0]["views"]
        self.assertEqual(len(views), mod.VIEWS_PER_CALL)
        self.assertEqual([v["path"] for v in views], ["a.py", "b.py", "c.py", "d.py"])

    def test_no_session_directory_means_no_views(self):
        msg = assistant_message([("c1", "shell", {"command": "cat a.py"}, "")])
        self.assertNotIn("views", mod.step_from_message(msg, None, mod.READS, None)["parts"][0])

    def test_a_view_of_a_call_that_failed_is_still_the_file_it_tried_to_read(self):
        msg = {"id": "m", "type": "assistant", "content": [{"type": "tool", "id": "c1", "name": "shell",
                "state": {"status": "error", "input": {"command": "cat a.py"},
                          "error": {"message": "no such file"}}}]}
        parts = mod.step_from_message(msg, None, mod.READS, self.tmp)["parts"]
        self.assertEqual(parts[0]["views"][0]["ranges"], [[1, 30]])

    def test_a_finished_grep_call_takes_its_marks_from_the_output_it_left(self):
        msg = assistant_message([("c1", "shell", {"command": 'grep -n "line 5" a.py'}, "5: line 5\n12: line 5 x")])
        parts = mod.step_from_message(msg, None, mod.READS, self.tmp)["parts"]
        self.assertEqual(parts[0]["views"][0]["ranges"], [[5, 5], [12, 12]])


class FixtureViewTests(unittest.TestCase):
    """The captured turn read back the way a finished window reads it: the reads the agent made become file
    views - and only those. Nothing is invented, and nothing is read off this machine's disk."""

    @classmethod
    def setUpClass(cls):
        cls.fx, cls.chat, cls.start, cls.calls, cls.seeded = mod.fixture_prep(
            os.path.join(HERE, "fixtures", "learnlab-iq3-turn.json"))
        cls.sess = cls.fx["session"]["directory"]
        mod.FIXTURE_NO_DISK = True       # from here on: a file the capture did not record gets no view
        cls.parts = []
        for msg in cls.fx["messages"]:
            if msg.get("type") != "assistant":
                continue
            for p in mod.step_from_message(msg, mod.BEFORE, mod.READS, cls.sess)["parts"]:
                if p["p"] == "tool":
                    cls.parts.append(p)

    @classmethod
    def tearDownClass(cls):
        mod.READS.clear()
        mod.BEFORE.clear()
        mod.FIXTURE_NO_DISK = False

    def views_of(self, fragment):
        """The views of the first call whose command (or read path) contains `fragment`."""
        for p in self.parts:
            inp = p.get("input")
            if not isinstance(inp, dict):
                continue
            text = inp.get("command") or inp.get("path") or ""
            if fragment in text:
                return p.get("views") or []
        raise AssertionError("no call with " + fragment)

    def test_reads_become_views_and_other_calls_do_not(self):
        with_views = [p for p in self.parts if p.get("views")]
        self.assertEqual(len(with_views), 14)
        self.assertEqual([p["name"] for p in with_views if p["name"] not in ("read", "shell")], [])

    def test_every_view_names_a_file_the_capture_recorded_and_lines_that_exist(self):
        known = set(self.fx["files"])
        for p in self.parts:
            for v in p.get("views") or []:
                self.assertIn(v["path"], known, v["path"])
                self.assertEqual(v["total"], len(v["lines"]), v["path"])     # the view carries what it counts
                self.assertTrue(v["ranges"], v["path"])
                for s, e in v["ranges"]:
                    self.assertTrue(1 <= s <= e <= v["total"], (v["path"], s, e, v["total"]))

    def test_no_view_of_the_capture_is_read_off_this_machine(self):
        # a fixture reads the capture, never the disk: every view is the text the capture holds, not the
        # file as it sits here now, and a file the capture did not record gets no view at all (see the
        # cat .gitignore case below).
        for p in self.parts:
            for v in p.get("views") or []:
                self.assertFalse(v["now"], v["path"])   # nothing was read after the call: it is the capture's text

    def test_a_whole_file_read_shows_the_whole_file(self):
        v = self.views_of("cat backend/routers/auth.py")[0]
        total = len(mod.split_lines(self.fx["files"]["backend/routers/auth.py"]["before"]))
        self.assertEqual((v["path"], v["from"], v["total"], v["ranges"]),
                         ("backend/routers/auth.py", 1, total, [[1, total]]))

    def test_a_sed_range_past_the_end_of_the_file_is_cut_to_it(self):
        v = self.views_of("sed -n '60,120p' backend/auth/service.py")[0]
        total = len(mod.split_lines(self.fx["files"]["backend/auth/service.py"]["before"]))
        self.assertLess(total, 120)
        self.assertEqual((v["total"], v["ranges"]), (total, [[60, total]]))
        self.assertEqual(v["lines"][59], '            "INSERT INTO sessions (user_id, token, expires_at) '
                                         'VALUES (?, ?, ?)",')

    def test_the_three_reads_of_one_call_are_three_views_of_three_files(self):
        vs = self.views_of("sed -n '1,40p' backend/services/db.py")
        self.assertEqual([(v["path"], v["how"], v["ranges"][0][0]) for v in vs],
                         [("backend/auth/service.py", "sed", 60), ("backend/routers/auth.py", "sed", 55),
                          ("backend/services/db.py", "sed", 1)])

    def test_a_grep_view_marks_the_lines_the_capture_output_named(self):
        vs = self.views_of('grep -n "signup\\|login\\|password\\|Password" frontend/src/App.tsx')
        self.assertEqual([(v["path"], v["how"]) for v in vs],
                         [("frontend/src/App.tsx", "grep"), ("frontend/src/lib/api.ts", "grep")])
        for v in vs:
            self.assertTrue(v["ranges"])
            self.assertLessEqual(len(v["ranges"]), 50)          # the call piped its output through head -50
        first = vs[0]
        line = first["lines"][first["ranges"][0][0] - first["from"]]
        self.assertIn("signup", line)                           # line 93 of App.tsx is what grep matched
        self.assertEqual(first["ranges"][0], [93, 93])

    def test_a_single_file_grep_view_marks_its_own_lines(self):
        v = self.views_of('grep -n "bcrypt\\|aiosqlite\\|password" backend/auth/service.py')[0]
        self.assertEqual((v["path"], v["ranges"]), ("backend/auth/service.py", [[1, 1], [20, 20]]))

    def test_a_file_shown_twice_in_one_turn_shows_how_the_agent_had_changed_it(self):
        before = self.views_of("cat backend/auth/service.py")[0]        # read at the start of the turn
        after = self.views_of('grep -n "bcrypt')[0]                     # read again, after three edits
        self.assertEqual(before["total"], 107)
        self.assertEqual(after["total"], 83)
        self.assertLess(after["total"], before["total"])

    def test_a_head_of_a_file_marks_its_first_lines(self):
        v = self.views_of("head -5 frontend/src/App.tsx")[0]
        self.assertEqual((v["path"], v["how"], v["ranges"]), ("frontend/src/App.tsx", "head", [[1, 5]]))

    def test_a_read_outside_the_session_directory_gets_no_view(self):
        self.assertEqual(self.views_of("tail -20 /tmp/llb-8000.log"), [])

    def test_calls_the_capture_dropped_and_reads_it_cannot_name_get_no_view(self):
        for frag in ("[dropped from the fixture]", "cat .gitignore", "git ls-files backend/data",
                     "python - <<", "check 2>&1", "git ls-files | grep", "cat >> HANDOFF.md", "curl -s"):
            self.assertEqual(self.views_of(frag), [], frag)

    def test_the_read_tool_calls_of_the_capture_get_their_own_ranges(self):
        reads = [p for p in self.parts if p["name"] == "read"]
        self.assertEqual(len(reads), 6)
        for p in reads:
            v = p["views"][0]
            total = len(mod.split_lines(mod.READS.get("%s\x00%s" % (p["id"], v["path"]))))
            inp = p["input"]
            off = inp.get("offset") or 1
            end = total if inp.get("limit") is None else min(off + inp["limit"] - 1, total)
            self.assertEqual((v["from"], v["total"], v["ranges"]), (1, total, [[off, end]]), v["path"])
            self.assertEqual(v["how"], "read")
        self.assertEqual([(p["views"][0]["path"], p["views"][0]["ranges"]) for p in reads],
                         [("frontend/src/App.tsx", [[85, 174]]), ("backend/services/db.py", [[165, 194]]),
                          ("backend/routers/service.py", [[1, 11]]), ("frontend/src/lib/api.ts", [[40, 59]]),
                          ("frontend/src/App.tsx", [[1, 45]]), ("backend/main.py", [[35, 77]])])


class LiveViewEventsTests(unittest.TestCase):
    """A tool call that reads files while the window is open sends one view event per file, after the tool
    event it belongs to, and a grep only once the output that names its lines has arrived."""

    def setUp(self):
        mod.FIXTURE_NO_DISK = False
        self.tmp = tempfile.mkdtemp(prefix="live-events-")
        write_file(os.path.join(self.tmp, "a.py"), text(30))
        write_file(os.path.join(self.tmp, "b.py"), text(9))
        mod.READS.clear()
        self.reads = {}          # what views_of_event remembers about this call, as the live window does

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        mod.READS.clear()

    def views_for(self, shaped, names=None):
        return mod.views_of_event(shaped, names or {}, self.tmp, self.reads)

    def test_a_command_gets_its_views_when_its_input_is_known(self):
        got = self.views_for({"k": "tool", "id": "c1", "name": "shell", "status": "running",
                              "input": {"command": "sed -n '2,4p' a.py && cat b.py"}})
        self.assertEqual([(v["k"], v["id"], v["path"], v["ranges"]) for v in got],
                         [("view", "c1", "a.py", [[2, 4]]), ("view", "c1", "b.py", [[1, 9]])])
        self.assertFalse(any(v["now"] for v in got))            # captured while the call ran: what the agent read

    def test_one_call_reading_the_same_file_twice_gets_one_view_with_both_ranges(self):
        first = self.views_for({"k": "tool", "id": "c1", "name": "shell", "status": "running",
                                "input": {"command": "sed -n '1,5p' a.py; sed -n '20,25p' a.py"}})
        self.assertEqual([(v["path"], v["how"], v["ranges"]) for v in first],
                         [("a.py", "sed", [[1, 5], [20, 25]])])  # the two reads share one view of the file
        again = self.views_for({"k": "tool", "id": "c1", "status": "completed", "out": "x"})
        self.assertEqual(again, [])                               # already sent: nothing on completion either

    def test_a_whole_file_read_of_a_file_makes_the_whole_file_the_range(self):
        got = self.views_for({"k": "tool", "id": "c1", "name": "shell", "status": "running",
                              "input": {"command": "cat a.py && sed -n '1,3p' a.py"}})
        self.assertEqual([(v["how"], v["ranges"]) for v in got], [("cat + sed", [[1, 30]])])

    def test_a_read_and_a_grep_of_the_same_file_share_one_view_after_the_output(self):
        started = self.views_for({"k": "tool", "id": "c1", "name": "shell", "status": "running",
                                  "input": {"command": 'sed -n \'1,3p\' a.py && grep -n "line 20" a.py'}})
        self.assertEqual(started, [])                            # the grep's marks need the output
        done = self.views_for({"k": "tool", "id": "c1", "status": "completed", "out": "20: line 20"})
        self.assertEqual([(v["how"], v["ranges"]) for v in done], [("sed + grep", [[1, 3], [20, 20]])])

    def test_a_grep_waits_for_the_output_that_names_its_lines(self):
        started = self.views_for({"k": "tool", "id": "c1", "name": "shell", "status": "running",
                                  "input": {"command": 'grep -n "line 5" a.py'}})
        self.assertEqual(started, [])
        done = self.views_for({"k": "tool", "id": "c1", "status": "completed", "out": "5: line 5\n9: line 9"})
        self.assertEqual([v["ranges"] for v in done], [[[5, 5], [9, 9]]])

    def test_a_failed_grep_and_a_progress_event_add_nothing(self):
        self.views_for({"k": "tool", "id": "c1", "name": "shell", "status": "running",
                        "input": {"command": 'grep -n "line 5" a.py'}})
        self.assertEqual(self.views_for({"k": "tool", "id": "c1", "out": "running…"}), [])
        self.assertEqual(self.views_for({"k": "tool", "id": "c1", "status": "error", "err": "boom"}), [])

    def test_a_file_the_command_creates_part_way_through_is_caught_when_the_call_ends(self):
        started = self.views_for({"k": "tool", "id": "c1", "name": "shell", "status": "running",
                                  "input": {"command": "python3 build.py && cat gen.py"}})
        self.assertEqual(started, [])                            # gen.py is not there yet
        write_file(os.path.join(self.tmp, "gen.py"), text(4))
        done = self.views_for({"k": "tool", "id": "c1", "status": "completed", "out": "built"})
        self.assertEqual([(v["path"], v["ranges"]) for v in done], [("gen.py", [[1, 4]])])

    def test_a_read_tool_call_gets_its_view_when_it_starts(self):
        got = self.views_for({"k": "tool", "id": "c1", "name": "read", "status": "running",
                              "input": {"path": "a.py", "offset": 6, "limit": 2}})
        self.assertEqual([(v["path"], v["how"], v["ranges"]) for v in got], [("a.py", "read", [[6, 7]])])

    def test_edit_write_and_desktop_calls_get_no_views(self):
        for name, inp in (("edit", {"path": "a.py", "oldString": "line 5", "newString": "x"}),
                          ("write", {"path": "c.py", "content": "hi"}),
                          ("shell", {"command": "desktop click 4 2>&1 | tail -12"})):
            self.assertEqual(self.views_for({"k": "tool", "id": "c1", "name": name, "status": "running",
                                             "input": inp}), [])

    def test_views_stop_after_the_cap(self):
        for name in "cde":
            write_file(os.path.join(self.tmp, name + ".py"), text(4))
        got = self.views_for({"k": "tool", "id": "c1", "name": "shell", "status": "running",
                              "input": {"command": "cat a.py b.py c.py d.py"}})
        self.assertEqual(len(got), mod.VIEWS_PER_CALL)

    def test_a_tool_event_without_an_id_gets_nothing(self):
        self.assertEqual(self.views_for({"k": "tool", "name": "shell", "status": "running",
                                         "input": {"command": "cat a.py"}}), [])

if __name__ == "__main__":
    unittest.main(verbosity=2)
