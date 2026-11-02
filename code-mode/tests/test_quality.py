"""Regressions from the Learn installer conversation, including the whole pipe/reviewer loop."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'owui'))
from code_mode import TurnCheck, TurnTiming, parse_review, shell_quality_findings, review_prompt
from test_review import run_turn


def tool(check, command, output='ok', failed=False):
    check.tool('shell', {'command': command}, output, failed)


def edit(check):
    check.tool('edit', {'filePath': 'frontend/src/App.tsx'}, 'ok', False)


def test_authenticated_workflow_counts_after_post_edit_open():
    c = TurnCheck()
    edit(c)
    tool(c, 'desktop open http://localhost:8000')
    tool(c, 'desktop click 3')
    tool(c, 'desktop verify --here --expect "Settings"', 'VERIFY (as it is now, not reloaded): PASS')
    assert not c.pending()
    edit(c)
    tool(c, 'desktop verify --here --expect "Settings"', 'VERIFY (as it is now, not reloaded): PASS')
    assert c.pending(), 'an old tab must not validate another edit'


def test_old_or_failed_open_does_not_certify_a_new_edit():
    for failed in (True, False):
        c = TurnCheck()
        tool(c, 'desktop open http://localhost:8000', failed=failed)
        edit(c)
        tool(c, 'desktop verify --here', 'VERIFY: PASS')
        assert c.pending()
    c = TurnCheck(); edit(c)
    tool(c, 'desktop open http://localhost:8000', failed=True)
    tool(c, 'desktop verify --here', 'VERIFY: PASS')
    assert c.pending()


def test_failed_verify_is_not_a_pass_even_if_output_contains_pass():
    c = TurnCheck(); edit(c)
    tool(c, 'desktop verify http://localhost:8000', 'VERIFY: PASS', failed=True)
    assert c.pending()


def test_fresh_check_then_interactive_screen_and_later_failure():
    c = TurnCheck(); edit(c)
    tool(c, 'desktop verify http://localhost:8000', 'VERIFY: FAIL\nProblems:\nNot visible: Settings')
    tool(c, 'desktop click 3')
    tool(c, 'desktop verify --here', 'VERIFY (as it is now, not reloaded): PASS')
    assert not c.pending()
    tool(c, 'desktop verify --here', 'VERIFY: FAIL\nProblems:\nJavaScript error: broken')
    assert c.pending() and 'JavaScript error' in c.problems


BAD_INSTALL = '''#!/bin/bash
set -euo pipefail
spinner() { while kill -0 "$1" 2>/dev/null; do sleep .1; done; printf success; }
false &
pid=$!
spinner "$pid"
npx tsc --noEmit -qq 2>/dev/null || true
'''


def test_installer_false_success_is_caught_without_a_model():
    found = shell_quality_findings({'install.sh': BAD_INSTALL})
    assert len(found) == 3
    assert any('exit status' in f for f in found)
    assert any('unsupported -qq' in f for f in found)
    assert any('discarded' in f for f in found)


def test_wait_and_propagation_is_not_flagged_and_comments_are_ignored():
    assert shell_quality_findings({'install.sh': '''#!/bin/bash
# npx tsc -qq || true
spinner() { while kill -0 "$1" 2>/dev/null; do sleep .1; done; }
false &
pid=$!
spinner "$pid"
if wait "$pid"; then echo success; else exit 1; fi
npx tsc --noEmit
'''}) == []
    assert shell_quality_findings({'serve.sh': 'server &\necho started\n'}) == []


def test_incomplete_review_never_becomes_green():
    assert parse_review('CLAIM | service restarts | unverified | no systemd here\nREVIEW: INCOMPLETE') == (None, [])
    assert parse_review('CLAIM | service restarts | unverified | no evidence\nREVIEW: PASS') == (None, [])
    assert parse_review('ASKED | thing | built | file:1') == (None, [])
    assert parse_review('ADDED | auth session | necessary state | login\nREVIEW: PASS') == (True, [])


def test_review_scopes_history_and_does_not_destroy_browser_state():
    prompt = review_prompt(['test credentials?', 'add install animation'], [], 'done', ['install.sh'], True, [])
    assert 'Current task to review:\nadd install animation' in prompt
    assert 'earlier requests are background' in prompt
    assert 'Do not run desktop navigation' in prompt


def test_small_installer_change_is_reviewed_and_guard_overrules_false_pass(tmp_path):
    seen = []
    def script(session, text):
        if session['agent'] == 'review':
            seen.append(text)
            return [('text', 'REVIEW: PASS')]
        path = Path(session['directory']) / 'install.sh'
        return [('do', lambda: path.write_text(BAD_INSTALL)),
                ('tool', 'write', {'filePath': str(path)}, 'ok'),
                ('text', 'All checks pass!')]
    text, statuses, _, _ = run_turn(tmp_path, script, diff=[{'file': 'install.sh', 'additions': 1}])
    assert len(seen) == 2
    assert 'background jobs' in text and 'still open' in text
    assert text.index('still open') < text.index('All checks pass!')
    assert "Builder's summary" in text
    assert statuses[-1].startswith('Needs attention')


def test_context_checkpoint_queues_once_until_context_shrinks(tmp_path):
    def script(session, text):
        return [('usage', {'input': n}) for n in (1000, 66000, 70000, 30000, 68000)] + [('text', 'done')]
    # the threshold is set here: the default moved (65,536 -> 98,304 in 0.21.2) and the sizes below are built around 65,536
    text, _, fake, _ = run_turn(tmp_path, script, CONTEXT_COMPACT_TOKENS=65536)
    assert 'done' in text
    # 0.27: a backstop. 66,000 then 70,000 with no compaction between asks once; after it shrinks, a single step past
    # the line (68,000) is opencode's to compact, so nothing more is asked.
    assert len(fake.compactions) == 1
    assert all(body == {'delivery': 'steer'} for _, body in fake.compactions)


def test_backstop_line_is_each_models_own(tmp_path):
    # 10-24: CONTEXT_COMPACT_TOKENS went to 229,376 for a 256k model; a 128k model keeps its 98,304 line
    def script(session, text):
        return [('usage', {'input': n}) for n in (1000, 99000, 100000)] + [('text', 'done')]
    _, _, fake, _ = run_turn(tmp_path, script, CONTEXT_COMPACT_TOKENS=229376,
                             _models=[{"id": "m", "providerID": "slerm", "limit": {"context": 131072}}])
    assert len(fake.compactions) == 1, "a 128k model past 98,304 twice: the backstop asks once"
    _, _, fake, _ = run_turn(tmp_path / "b", script, CONTEXT_COMPACT_TOKENS=229376,
                             _models=[{"id": "m", "providerID": "slerm", "limit": {"context": 262144}}])
    assert not fake.compactions, "a 256k model at 100,000 is nowhere near its 229,376 line"


def test_context_checkpoint_can_be_disabled(tmp_path):
    def script(session, text):
        return [('usage', {'input': 100000}), ('text', 'done')]
    _, _, fake, _ = run_turn(tmp_path, script, CONTEXT_COMPACT_TOKENS=0)
    assert not fake.compactions


def test_turn_timing_reports_non_overlapping_observations():
    ticks = iter((10.0, 11.5, 12.0, 14.0, 15.0, 16.5, 17.0, 19.0, 20.0))
    timing = TurnTiming(lambda: next(ticks))
    timing.event()
    timing.reasoning_started()
    timing.reasoning_ended()
    timing.tool_started("call-1")
    timing.tool_ended("call-1")
    timing.compaction_started()
    timing.compaction_ended()
    timing.step({"input": 7, "output": 3, "cache": {"read": 11, "write": 2}})
    got = timing.completed()
    assert got == {
        "wall_seconds": 10.0,
        "first_event_seconds": 1.5,
        "reasoning_seconds": 2.0,
        "tool_elapsed_seconds": 1.5,
        "permission_seconds": 0.0,
        "compaction_seconds": 2.0,
        "events": 1,
        "steps": 1,
        "tool_calls": 1,
        "tokens": {"input": 7, "output": 3, "cache_read": 11, "cache_write": 2},
        "phase_seconds": {"Working": 1.5},
        "activity": {"reads": 0, "repeat_reads": 0, "edits": 0, "shell_commands": 0, "checks": 0,
                     "checks_failed": 0, "files_read": 0, "files_edited": 0},
        "review": {"rounds": 0, "seconds": 0, "sent_back": 0},
        "auto_checks": 0,
        "compactions": 1,
        "wakes_delivered": 0,
        "stopped": False,
        "incomplete": {"reasoning": False, "tools": 0, "permission": False, "compaction": False},
    }


def test_turn_timing_marks_open_observations_without_counting_them_complete():
    ticks = iter((10.0, 11.0, 12.0, 13.0))
    timing = TurnTiming(lambda: next(ticks))
    timing.reasoning_started()
    timing.tool_started("call-1")
    got = timing.completed()
    assert got["reasoning_seconds"] == 0.0 and got["tool_elapsed_seconds"] == 0.0
    assert got["incomplete"] == {"reasoning": True, "tools": 1, "permission": False, "compaction": False}


def test_turn_timing_status_is_saved_with_a_turn(tmp_path):
    def script(session, text):
        return [
            ("usage", {"input": 5, "output": 2, "cache": {"read": 3}}),
            ("tool", "shell", {"command": "true"}, "ok"),
            ("text", "done"),
        ]
    _, _, fake, _ = run_turn(tmp_path, script)
    events = [e for e in fake.status_events if e.get("action") == "slerm_timing"]
    assert len(events) == 1
    timing = events[0]["timing"]
    assert events[0]["hidden"] is True and events[0]["done"] is True
    assert timing["steps"] == 1 and timing["tool_calls"] == 1
    assert timing["tokens"] == {"input": 5, "output": 2, "cache_read": 3, "cache_write": 0}
