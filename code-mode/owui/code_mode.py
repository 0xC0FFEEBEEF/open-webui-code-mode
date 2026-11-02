"""
title: Code Mode
author: 0xC0FFEEBEEF
description: Code mode - each chat drives its own opencode session (with its own project folder) on the AI sandbox VM. A live window in the reply follows the agent (the desktop, the code it types, files, terminal, web), tool calls and thinking fold away, approvals and questions wait in a box every tab of the chat shows until answered, and every turn ends with the diff. Feature chats extend Code mode itself on a branch, deployed from the Features panel.
version: 0.30.0
requirements: httpx
"""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import importlib
import inspect
import json
import os
import re
import shlex
import time
import urllib.parse
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

import httpx
from pydantic import BaseModel, Field

# The thinking pill's levels, each an opencode model variant defined in opencode.json ("on" = opencode's default).
EFFORT_VARIANTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Stops servers another chat left listening (a process whose working directory is another chat's folder), so a
# model opening localhost:8000 finds its own app, not another chat's (2026-10-08: a storm app left on :8000 by an
# earlier chat fooled two models). Platform services never run from chats/ or features/. {root}, {keep} filled in.
def reap_command(root: str, keep: str, preview: str, kill: bool = True) -> str:
    """The shell command _reap_servers runs: REAP_CMD (unless kill is False) and the Preview-path note."""
    parts = [REAP_CMD.format(root=root, keep=keep)] if kill else []
    if re.fullmatch(r"/p/[A-Za-z0-9_-]+/[0-9a-f]{32}/", preview or ""):
        parts.append(f'd="${{HOME:-/home/coder}}/.local/state/code-mode/preview"; mkdir -p "$d" && '
                     f"printf %s '{preview}' > \"$d/{keep}\"")
    return "; ".join(parts)


REAP_CMD = ("for pid in $(ss -ltnpH 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); do "
            "cwd=$(readlink /proc/$pid/cwd 2>/dev/null) || continue; case \"$cwd\" in "
            "{root}/chats/{keep}|{root}/chats/{keep}/*|{root}/features/{keep}|{root}/features/{keep}/*) ;; "
            "{root}/chats/*|{root}/features/*) kill $pid ;; esac; done; true")
RAG_WRAP_RE = re.compile(r"^\s*### Task:.*?<context>(.*)</context>(.*)$", re.S)
RAG_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def split_rag(text: str) -> tuple[str, str]:
    """(the user's own words, attached-document context) from a possibly RAG-wrapped user message."""
    m = RAG_WRAP_RE.match(text or "")
    if not m:
        return text, ""
    q = RAG_QUERY_RE.search(m.group(2)) or RAG_QUERY_RE.search(text)
    return (q.group(1) if q else m.group(2)).strip(), m.group(1).strip()


def text_of(m: dict) -> str:
    c = m.get("content", "")
    if isinstance(c, list):
        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return re.sub(r"<details.*?</details>", "", c or "", flags=re.S).strip()


IMAGE_PART_TYPES = ("image_url", "input_image")  # how Open WebUI puts an image inside a message for a model call
ATTACH_MAX_FILES = 8                             # attached images one Code turn hands to the agent
DATA_IMAGE_RE = re.compile(r"^data:(image/[-\w.+]*)\s*;\s*base64,([A-Za-z0-9+/=\s]+)$")


def sha1_key(s: str) -> str:
    """Identity of an attachment that has no Open WebUI file id: a fingerprint of its bytes or url."""
    return "sha1:" + hashlib.sha1(s.encode()).hexdigest()[:16]


def image_entry(f) -> bool:
    """Whether an Open WebUI file record names an image the agent ought to be able to see."""
    return isinstance(f, dict) and (f.get("type") == "image"
                                    or str(f.get("content_type") or "").startswith("image/"))


async def awaited(value):
    """value, awaited first when it is awaitable: Open WebUI's model calls are plain functions in 0.8, async in some
    other builds."""
    return await value if inspect.isawaitable(value) else value


FIXER_CONTEXT_MESSAGES = 16  # the chat's last messages a report from the Fixer button carries
FIXER_CONTEXT_CHARS = 8000   # per message: a longer one keeps its head and its tail


def chat_branch(chat: dict) -> list:
    """The messages on a stored chat's current branch, oldest first (history.currentId back through parentId).
    A broken chain - the current message a stub with no parent, as a wake-up collision left chat 00086b8e on
    2026-10-20 - falls back to the flat `messages` list, then to every message by time: a chat that won't load is
    exactly when the fixer needs to read it."""
    hist = (chat or {}).get("history") or {}
    msgs, cur = hist.get("messages") or {}, hist.get("currentId")
    out, seen = [], set()
    while cur and cur in msgs and cur not in seen and isinstance(msgs[cur], dict):
        seen.add(cur)
        out.append(msgs[cur])
        cur = msgs[cur].get("parentId")
    if len(out) > 1 or (out and len(msgs) <= 1):
        return out[::-1]
    flat = [m for m in (chat or {}).get("messages") or [] if isinstance(m, dict)]
    if flat:
        return flat
    by_time = sorted((m for m in msgs.values() if isinstance(m, dict)), key=lambda m: m.get("timestamp") or 0)
    return by_time or out[::-1]


def fixer_context(chat: dict) -> list:
    """The chat as the fixer's context.md: user and assistant messages of the current branch, the last
    FIXER_CONTEXT_MESSAGES, each cut to FIXER_CONTEXT_CHARS. Code replies keep their folded steps: they are evidence."""
    out = []
    for m in chat_branch(chat):
        if m.get("role") not in ("user", "assistant"):
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        c = str(c or "")
        if len(c) > FIXER_CONTEXT_CHARS:
            c = c[:2000] + "\n\n[... cut ...]\n\n" + c[-(FIXER_CONTEXT_CHARS - 2000):]
        out.append({"role": m["role"], "content": c})
    return out[-FIXER_CONTEXT_MESSAGES:]


def data_image(url):
    """(mime, base64 payload) when `url` is a base64 image data URL, else None."""
    if not isinstance(url, str):
        return None
    m = DATA_IMAGE_RE.match(url.strip())
    return (m.group(1), "".join(m.group(2).split())) if m else None


def attr(obj: Any) -> str:
    return html.escape(json.dumps(obj, ensure_ascii=False))


def tool_block(call_id: str, name: str, args: Any, result: str) -> str:
    """A finished tool call, in the markup Open WebUI itself stores for native tool calls."""
    return (f'<details type="tool_calls" done="true" id="{html.escape(call_id)}" name="{html.escape(name)}" '
            f'arguments="{attr(args)}" result="{attr(result)}" files="" embeds="[]">\n'
            f"<summary>Tool Executed</summary>\n</details>\n")


def reasoning_block(text: str, seconds: int) -> str:
    display = html.escape("\n".join(line if line.startswith(">") else f"> {line}" for line in text.splitlines()))
    return (f'<details type="reasoning" done="true" duration="{seconds}">\n'
            f"<summary>Thought for {seconds} seconds</summary>\n{display}\n</details>\n")


def describe_tool(name: str, args: Any) -> str:
    """One line for the status bar: what the tool is doing."""
    a = args if isinstance(args, dict) else {}
    if isinstance(a.get("questions"), list) and a["questions"]:
        return f"{name}: {str(a['questions'][0].get('question', ''))[:90]}"
    for key in ("command", "filePath", "path", "pattern", "url", "query"):  # filePath: opencode's read/edit/write
        if a.get(key):
            return f"{name}: {str(a[key]).splitlines()[0][:90]}"
    return f"{name}: {json.dumps(a, ensure_ascii=False)[:90]}" if a else name


# What a tool call is for, in plain words, for the status line (performance handoff 10-23, stage A: the status said
# "shell: …" for a 10-minute test run and a one-line grep alike). Decided from the call alone; nothing is run.
READ_TOOLS = ("read", "list", "glob", "grep", "codesearch", "lsp")
EDIT_TOOLS = ("write", "edit", "multiedit", "patch", "apply_patch")
CHECK_CMD_RE = re.compile(
    r"(?:^|[;&|(]\s*|\s)(?:(?:\S*/)?(?:pytest|py\.test|tsc|ruff|mypy|shellcheck|eslint|biome|check)(?![\w.-])|"
    r"(?:\S*/)?python3?\s+-m\s+(?:pytest|unittest|mypy|ruff)\b|cargo\s+(?:test|check|clippy|nextest|fmt\s+(?:--all\s+)?--check)\b|"
    r"go\s+(?:test|vet)\b|npm\s+(?:test|run\s+(?:test|lint|check|typecheck))\b|npx\s+(?:tsc|eslint|biome|vitest|jest)\b|"
    r"make\s+(?:test|check)\b|node\s+--test\b|desktop\s+verify\b|(?:ba)?sh\s+\S*(?:check|test)\S*\.sh\b)")
BUILD_CMD_RE = re.compile(r"(?:^|[;&|(]\s*|\s)(?:cargo\s+build|npm\s+(?:run\s+build|ci|i|install)\b|make\b|cmake\b|"
                          r"go\s+build|pip\s+install|uv\s+(?:sync|pip)|docker\s+build|apptainer\s+build)")
JOB_CMD_RE = re.compile(r"(?:^|[;&|(]\s*|\s)(?:slerm-job\s+(?:run|wait|status)|later\s+(?:run|in|until|list))\b")
LOOK_CMD_RE = re.compile(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)*(?:cat|head|tail|less|grep|rg|ls|find|tree|wc|sed\s+-n|awk|"
                         r"git\s+(?:-C\s+\S+\s+)?(?:diff|log|show|status|blame|grep)|file|stat|du|which|jq)\b")


def tool_phase(name: str, args: Any) -> str:
    """The plain step a tool call is part of: Investigating, Editing, Running checks, Building, Waiting for a job, ..."""
    a = args if isinstance(args, dict) else {}
    if name in READ_TOOLS:
        return "Investigating"
    if name in EDIT_TOOLS:
        return "Editing"
    if name in ("webfetch", "websearch"):
        return "Looking it up"
    if name == "task":
        return "Delegating to a helper agent"
    if name in ("todowrite", "todoread"):
        return "Planning"
    if name == "question" or isinstance(a.get("questions"), list):
        return "Asking you"
    cmd = str(a.get("command") or "")
    if not cmd:
        return "Working"
    if JOB_CMD_RE.search(cmd):
        return "Waiting for a job"
    if re.search(r"(?:^|[;&|(]\s*|\s)desktop\s+(?!verify)", cmd):
        return "Using the desktop"
    if CHECK_CMD_RE.search(cmd):
        return "Running checks"
    if BUILD_CMD_RE.search(cmd):
        return "Building"
    if re.search(r"(?:^|[;&|(]\s*|\s)git\s+(?:-C\s+\S+\s+)?(?:commit|push|tag)\b", cmd):
        return "Committing"
    if SHELL_EDIT_RE.search(cmd):
        return "Editing"
    if LOOK_CMD_RE.search(cmd):
        return "Investigating"
    return "Running a command"


def tool_status(name: str, args: Any) -> str:
    """The status line for a tool call: its phase, then what it does."""
    return f"{tool_phase(name, args)} · {describe_tool(name, args)}"


def wait_note(seconds: float, waiting_on: str) -> str:
    """The status line while nothing arrives from opencode: what it is still waiting on, and for how long."""
    return f"{waiting_on} ({duration(seconds)})"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def md_line(text: str, limit: int = 200) -> str:
    """One line of plain markdown-safe text."""
    line = " ".join((text or "").split())
    line = line if len(line) <= limit else line[:limit] + "…"
    return re.sub(r"([*_`<>\[\]])", r"\\\1", line)


def step_line(name: str, args: Any, result: str, failed: bool) -> str:
    """A finished tool call as one list item: what it did, and (for shell commands or failures) what came back."""
    what = describe_tool(name, args).replace("`", "'")
    item = f"- {'✗' if failed else '✓'} `{what}`"
    out = " ".join((result or "").split())
    if out and (failed or name == "shell"):
        item += " — " + md_line(out, 180)
    return item


def steps_block(steps: list, seconds: float) -> str:
    n = sum(1 for s in steps if not s.startswith("- *"))
    return (f"<details>\n<summary>Worked for {duration(seconds)} · {n} step{'s' if n != 1 else ''}</summary>\n\n"
            + "\n".join(steps) + "\n\n</details>\n")


def diff_block(files: list, limit: int) -> str:
    if not files:
        return ""
    adds = sum(f.get("additions", 0) for f in files)
    dels = sum(f.get("deletions", 0) for f in files)
    patch = "".join(f.get("patch", "") for f in files)
    if len(patch) > limit:
        patch = patch[:limit] + f"\n... (diff cut at {limit} characters)\n"
    names = ", ".join(f.get("file", "?") for f in files[:6]) + (" ..." if len(files) > 6 else "")
    return (f"\n\n<details>\n<summary>Changed {len(files)} file{'s' if len(files) != 1 else ''} "
            f"(+{adds} −{dels}): {html.escape(names)}</summary>\n\n```diff\n{patch.rstrip()}\n```\n\n</details>\n")


# ---- ask first (2026-10-13) -----------------------------------------------------------------------------------------
# The user wants the agent to slow down and ask before it builds. A rule in AGENTS.md did not do it, and neither did
# the same rule placed next to the user's message: Qwen3.6 Q5 read both and went straight to work. So the first Code
# turn of a chat runs on the opencode agent `ask` (agents/opencode-agents/ask.md): its only tool is `question`, and
# its prompt's only job is to put 2 to 5 questions, each with options, its own pick first. They reach the user as the
# question dialog (_form below); once answered, or if the agent found nothing open and said READY, the session goes
# back to the normal agent and the build starts in the same turn. A dialog nobody answers (no page open, dismissed)
# ends the turn with the questions as text instead, and the user's next message is the answer.
NO_QUESTIONS_RE = re.compile(r"\b(?:no questions|don'?t ask|do not ask|without asking|just build)\b", re.I)
ASK_AGENT, BUILD_AGENT = "ask", "build"
# One round of questions, and the look is asked by the pipe in fixed words (2026-10-13): Qwen3.6 kept calling
# `question` after its answers came back ("Final check", then another), and worded the design question so that its
# own style was the default. So the first answered form ends the ask turn (the pipe interrupts the agent), and a
# question about the look is replaced by DESIGN_QUESTION, or DESIGN_QUESTION is added when the request has a screen.
LOOK_RE = re.compile(r"\b(?:looks?|design\w*|themes?|styl\w+|colou?rs?|aesthetic\w*|layout|appearance|visual\w*)\b", re.I)
UI_RE = re.compile(r"\b(?:pages?|sites?|websites?|dashboards?|apps?|ui|screens?|interface|front.?end|web|landing|portfolio)\b", re.I)
DESIGN_QUESTION = "Do you want to see three design directions side by side before I build?"
DESIGN_OPTIONS = [{"label": "Show me three", "value": "show", "description": "an ideas board in the Preview, then you choose"},
                  {"label": "Pick one for me", "value": "pick", "description": "build straight away"}]
DESIGN_ANSWERS = {"show": "Show me three design directions side by side first", "pick": "You pick the design direction"}


def build_now(design_choice) -> str:
    """What the normal agent is told once the ask turn is over."""
    start = "[Code mode - not a message from the user] The questions are settled. "
    if design_choice == "show":
        return start + ("The user wants to see three design directions before anything is built. Load the "
                        "`frontend-design` skill and make the ideas board it describes (section 2), with those answers. "
                        "Then stop and ask which direction. Do not build the real page in this turn.")
    how = "Build it now with those answers, the way AGENTS.md describes: plan, small steps, `check`, then `desktop verify`."
    if design_choice == "pick":
        return start + how + (" The user wants you to pick the design direction: load the `frontend-design` skill and "
                              "follow it, with the user's own taste (its section 7) as the direction.")
    if design_choice:
        return start + how + f" For the look, the user said: {design_choice}. Load the `frontend-design` skill and use that as the direction."
    return start + how


def ask_first(said: str, seen: int, feature) -> bool:
    """Is this the turn that asks? The first Code turn of a chat, unless it is a feature chat (their messages are
    briefs) or the user said not to ask."""
    return not (seen or feature or NO_QUESTIONS_RE.search(said or ""))


def is_ready(reply: str) -> bool:
    return (reply or "").lstrip(" *_`#>\n").upper().startswith("READY")


def ideas_link(files: list, preview: str) -> str:
    """A turn that made or changed the ideas board (ideas/index.html: design options side by side, see the
    frontend-design skill) ends with a link to it. The Preview panel opens on the project's own page, so the board
    needs its own way in; the Chat | Code script opens the link in the same panel."""
    for f in files or []:
        name = str(f.get("file", "")).replace("\\", "/")
        if preview and (name in ("ideas/index.html", "./ideas/index.html") or name.endswith("/ideas/index.html")):
            return (f"\n[Ideas board]({preview}ideas/)\n\nWhich direction should I build? Reply with its letter, or "
                    "mix them (\"B, with A's colours\").\n")
    return ""


# "Go to http://localhost:8000" means nothing in the user's browser: that is the coding machine's localhost
# (2026-10-13). In the reply the chat's own app address becomes the address the user can open, PREVIEWS_URL.
LOCAL_URL_RE = re.compile(r"(?<![\w/.@-])(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):(\d{2,5})"
                          r"((?:/[^\s<>\"'`)\]*]*)?)", re.I)
FENCE_RE = re.compile(r"(^[ \t]*```.*?^[ \t]*```[^\n]*$|^[ \t]*~~~.*?^[ \t]*~~~[^\n]*$)", re.S | re.M)


def url_port(url: str) -> Optional[int]:
    m = LOCAL_URL_RE.search(url or "")
    return int(m.group(1)) if m else None


def user_links(text: str, link: str, ports) -> str:
    """`text` with the chat's own app address (localhost on one of `ports`) replaced by `link`, the same app at an
    address the user's browser can open. Other ports stay as written (localhost:11434 in a reply is the user's own
    Ollama, not this app), and so do fenced code blocks: the commands in them run on the coding machine."""
    ports = {int(p) for p in (ports or []) if p}
    if not (text and link and ports):
        return text or ""

    def swap(m):
        if int(m.group(1)) not in ports:
            return m.group(0)
        path = m.group(2) or ""
        kept = path.rstrip(".,;:!?")  # the full stop that ends the sentence is not part of the address
        return link.rstrip("/") + (kept or "/") + path[len(kept):]

    parts = FENCE_RE.split(text)
    return "".join(part if i % 2 else LOCAL_URL_RE.sub(swap, part) for i, part in enumerate(parts))


# The live desktop viewer, same wrapper as the Computer Agent pipe (placeholder until the desktop answers).
VIEWER_WRAPPER = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{{margin:0;background:transparent;font:13px ui-sans-serif,system-ui,sans-serif;color:#c9d3e6}}
#ph{{position:relative;height:360px;border:1px solid #262626;border-radius:14px;overflow:hidden;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(120% 90% at 30% 20%,#1d2533 0%,#111419 55%,#0b0c0f 100%)}}
#ph .glow{{position:absolute;width:60%;aspect-ratio:1;border-radius:50%;filter:blur(60px);opacity:.35;
  background:conic-gradient(from 0deg,#4f8cff,#7a5cff,#2ec5b6,#4f8cff);animation:d 14s linear infinite}}
@keyframes d{{from{{transform:rotate(0) translate(10%) rotate(0)}}to{{transform:rotate(360deg) translate(10%) rotate(-360deg)}}}}
#ph .win{{position:relative;width:52%;aspect-ratio:16/10;border-radius:10px;background:#161a21;border:1px solid #2a3140;
  box-shadow:0 20px 60px rgba(0,0,0,.5);overflow:hidden;animation:w .9s cubic-bezier(.2,.8,.2,1) both}}
@keyframes w{{from{{transform:translateY(14px) scale(.96);opacity:0}}to{{transform:none;opacity:1}}}}
#ph .tb{{height:12%;background:#1d2230;border-bottom:1px solid #2a3140}}
#ph .ln{{height:7%;margin:6% 7% 0;border-radius:4px;background:linear-gradient(90deg,#232a38 0%,#33405a 50%,#232a38 100%);
  background-size:200% 100%;animation:s 1.6s ease-in-out infinite}}
#ph .ln:nth-child(3){{width:60%}} #ph .ln:nth-child(4){{width:75%}}
@keyframes s{{0%{{background-position:100% 0}}100%{{background-position:-100% 0}}}}
#ph .lab{{position:absolute;bottom:8%;left:0;right:0;text-align:center}}
#ph .lab small{{display:block;color:#7d889c;margin-top:4px}}
iframe{{display:none;width:100%;border:0}}
</style></head><body>
<div id="ph"><div class="glow"></div><div class="win"><div class="tb"></div><div class="ln"></div><div class="ln"></div><div class="ln"></div></div>
<div class="lab"><span id="t">Getting the desktop ready…</span><small id="u">The live view appears here in a moment.</small></div></div>
<iframe id="v" title="AI sandbox desktop"></iframe>
<script>
const VIEW = {view!r}, HEALTH = {health!r};
const v = document.getElementById('v'), ph = document.getElementById('ph');
const up = () => parent.postMessage({{type: 'iframe:height', height: document.documentElement.scrollHeight}}, '*');
new ResizeObserver(up).observe(document.body); up();
let tries = 0;
async function load() {{
  try {{ const r = await fetch(HEALTH, {{cache: 'no-store'}}); if (!r.ok) throw 0; v.src = VIEW; }}
  catch (e) {{ tries++; if (tries > 3) document.getElementById('u').textContent = 'Waiting for the sandbox to answer…';
               setTimeout(load, 3000); }}
}}
v.onload = () => {{ if (!v.src) return; ph.style.display = 'none'; v.style.display = 'block'; v.style.height = '640px'; up(); }};
addEventListener('message', (e) => {{
  if (e.source === v.contentWindow && e.data && e.data.type === 'iframe:height') {{ v.style.height = e.data.height + 'px'; up(); }}
}});
load();
</script></body></html>"""


def viewer_embed(base: str, view_token: str, session: str) -> str:
    """Embed a tiny wrapper instead of the viewer URL: it shows a placeholder at once and only loads the
    viewer once the sandbox answers, so a load that lands on a sandbox restart never leaves a blank frame."""
    base = base.rstrip("/")
    return VIEWER_WRAPPER.format(view=f"{base}/view?t={view_token}&s={session}", health=f"{base}/health?s={session}")


# ---- the end-of-turn check (2026-10-09) ---------------------------------------------------------------------------
# Agents kept ending turns with "complete and verified" on pages that were broken: they judged from page text, from
# a cached copy or from their own fallback data. `desktop verify` now gives a verdict they can't talk past, and a turn
# that changed a web app may only end after it passes; otherwise the agent is sent back (AUTO_VERIFY times), and if it
# still fails the reply says so.
# A file whose change needs a look in the browser. Not .json: a schema, lock file or config in a CLI or backend project
# (the Rust CLI's config.schema.json, 2026-10-23) is no page, and a passing unrelated preview proves nothing about it.
WEB_FILE_RE = re.compile(r"\.(?:html?|m?js|cjs|jsx|tsx?|css|scss|vue|svelte|svg)$", re.I)
SHELL_WEB_RE = re.compile(r"[\w./-]+\.(?:html?|m?js|cjs|jsx|tsx?|css|scss|vue|svelte)\b")
SHELL_EDIT_RE = re.compile(r"\bsed\s+-i|\btee\s|(?<![<>&\d])>{1,2}\s*(?!/dev/|/tmp/|&)[\w.-]|\b(?:mv|cp)\s|\bnpm\s+(?:i|install|run\s+build)\b")
VERIFY_RE = re.compile(r"(?:^|[;&|(]\s*|\s)desktop\s+verify\b")
APP_URL_RE = re.compile(r"\bdesktop\s+(?:open|verify)\s+(?:--new-tab\s+)?['\"]?((?:https?://)?(?:localhost|127\.0\.0\.1)(?::\d+)?[^\s'\"]*)")


class TurnCheck:
    """Fed every tool call of a turn: did it change a web app, and does `desktop verify` pass after the last change?"""

    def __init__(self, url: str = ""):
        self.n = self.edited = self.verified = 0
        self.opened = 0
        self.web, self.ok, self.here_failed = False, False, False
        self.url, self.problems = url, ""
        self.records = []

    def tool(self, name: str, args: Any, result: str, failed: bool) -> None:
        self.n += 1
        args = args if isinstance(args, dict) else {}
        cmd = str(args.get("command") or "")
        if name in ("write", "edit", "multiedit", "patch", "apply_patch"):
            if not failed:
                self.edited = self.n
                path = str(args.get("filePath") or args.get("path") or args.get("patchText") or "")
                self.web = self.web or bool(WEB_FILE_RE.search(path) or SHELL_WEB_RE.search(path))
        elif cmd:
            m = APP_URL_RE.search(cmd)
            if m:
                self.url = m.group(1) if "://" in m.group(1) else "http://" + m.group(1)
                self.web = True
                if not failed and re.search(r"\bdesktop\s+open\b", cmd):
                    self.opened = self.n
            if VERIFY_RE.search(cmd):
                passed = not failed and bool(re.search(r"^VERIFY[^:\n]*: PASS", result or "", re.M))
                got = re.search(r"Problems:\n(.*?)(?:\nWarnings:|\nChecked:|\Z)", result or "", re.S)
                problems = got.group(1).strip()[:1500] if got and not passed else ""
                self.records.append(f"Step {self.n}, last edit {self.edited}: "
                                    f"{'current screen (--here)' if '--here' in cmd else 'fresh page'} "
                                    f"{'PASS' if passed else 'FAIL'}" + ("; " + problems if problems else ""))
                self.records = self.records[-6:]
                if "--here" in cmd:
                    self.here_failed = not passed
                    if not passed:
                        self.problems = problems
                    elif max(self.opened, self.verified) > self.edited:
                        # A post-edit open/fresh check followed by login/navigation and --here is a valid
                        # workflow. An old tab alone still cannot certify newly changed code.
                        self.verified, self.ok, self.problems = self.n, True, ""
                else:
                    self.verified, self.ok, self.here_failed, self.problems = self.n, passed, False, problems
            elif SHELL_EDIT_RE.search(cmd) and not failed:
                self.edited = self.n
                self.web = self.web or bool(SHELL_WEB_RE.search(cmd))

    def pending(self) -> str:
        """Why the turn isn't finished yet ('' = nothing to check, or verify passed after the last change)."""
        if not (self.edited and self.web):
            return ""
        if not self.verified:
            return "no `desktop verify` after the last change"
        if self.verified < self.edited:
            return "changes made after the last `desktop verify`"
        if not self.ok:
            return "`desktop verify` still fails"
        return "`desktop verify --here` (after clicking through) still fails" if self.here_failed else ""


def unverified_note(why: str, problems: str, tries: int) -> str:
    note = f"> **⚠ Not verified:** {why}"
    if tries:
        note += f", after {tries} automatic {'retry' if tries == 1 else 'retries'}"
    note += ". The builder's summary is unconfirmed wherever it claims these checks passed."
    if problems:
        note += "\n>\n> Last `desktop verify` found:\n" + "\n".join("> " + line for line in problems.splitlines()[:10])
    return note + "\n\n"


# ---- the end-of-turn review (2026-10-13) --------------------------------------------------------------------------
# What wore the user down was small unasked things adding up, none of which `desktop verify` sees: rooms the agent
# made up and pre-filled, the menu gone after a rewrite, Settings shrunk to an unlabeled icon, leftovers of its own
# test clicks, "could not test X" parked at the bottom of the answer, and the part of the request it never built. The
# user typed none of that in the chat (they said it elsewhere), so it has to be caught before "done". When a turn that
# changed files would end, a second opencode session on the agent `review` (agents/opencode-agents/review.md: it can
# read, search and run commands, not edit) gets what the user asked for, the builder's summary and a git ref of the
# project as it was when the turn started, and fills in a worksheet: one line per thing asked for, per control the
# project had before, per piece of content added, per trace of testing and per claim of the summary, each with a
# status. The lines with a bad status are the findings (parse_review).
# Findings go back to the builder (AUTO_REVIEW times) and are reviewed again; what is still open is shown in the
# reply. Every round is appended to ~/.local/state/code-mode/review/<chat>.jsonl on the VM.
REVIEW_AGENT = "review"
review_clock = time.monotonic  # the review deadline's clock (a test speeds it up)
REVIEW_REF = "refs/code-mode/turn-start"
RISK_FILE_RE = re.compile(r"(?:install|deploy|requirements|pyproject|package(?:-lock)?\.json|auth|settings|service|\.sh$)", re.I)


def shell_quality_findings(files: dict) -> list:
    """Conservative regression checks for two observed installer false-success bugs; not a shell interpreter."""
    findings = []
    for path, source in files.items():
        lines = source.splitlines()
        code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\b(?:tsc|pytest|shellcheck)\b.*\|\|\s*(?:true|:)(?:\s|;|$)", line):
                findings.append(f"[not true] {path}:{n}: a required checker failure is explicitly discarded with || true/colon. "
                                "Propagate its exit status; do not report the build as checked when it fails.")
            if re.search(r"\btsc\b.*(?:^|\s)-qq(?:\s|$)", line):
                findings.append(f"[not built] {path}:{n}: tsc is given unsupported -qq; remove it and require the actual type check to pass.")
        if (re.search(r"(?:install|deploy)", path, re.I) and re.search(r"\bspinner\b", code)
                and re.search(r"&\s*$", code, re.M) and not re.search(r"\bwait\s+[\"'$\w]", code)):
            findings.append(f"[not true] {path}: background jobs are animated but no wait collects their exit status. "
                            "A stopped process can have failed. Use wait, propagate failure, and test a deliberately failing job.")
    return findings
REVIEW_KINDS = "removed|invented|left behind|given up|not built|not true"
# 2026-10-23 (performance handoff): reviewers wrote "[left behind] /tmp/gh-test ... harmless, outside the repo" and the
# builder was still sent back to clean it, a whole extra cycle for nothing. A left-behind finding that says itself it
# is scratch outside the deliverable is shown, not sent back - unless it also names something that does matter.
HARMLESS_RE = re.compile(r"\bharmless\b|\boutside (?:of )?(?:the )?(?:repo|repository|project|deliverable)\b|"
                         r"\b(?:git-?ignored|ignored by git|untracked scratch)\b|(?<![\w.~/])/tmp/", re.I)
STILL_MATTERS_RE = re.compile(r"\bnot harmless\b|secret|password|token|credential|private key|leak|user-visible|"
                              r"visible to (?:the )?user|grows?\b|unbounded|disk|user data|real data|settings", re.I)


def harmless_leftover(finding: str) -> bool:
    """A single [left behind] finding about scratch outside what the user gets. A merged line ("4 things: ...")
    has lost its evidence and always counts."""
    return (finding.startswith("[left behind]") and not re.match(r"\[left behind\] \d+ things:", finding)
            and bool(HARMLESS_RE.search(finding)) and not STILL_MATTERS_RE.search(finding))
REVIEW_VERDICT_RE = re.compile(r"REVIEW:[\s*_`]*(PASS|FAIL|INCOMPLETE)", re.I)
REVIEW_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*\[?\s*(" + REVIEW_KINDS + r")\s*\]?\s*[:—–-]?\s*(\S.*)$", re.I)
QA_LINE_RE = re.compile(r"^> .+ — \*\*.+\*\*\s*$", re.M)  # an answered question dialog, as _form writes it
# The project as it is now, kept as a commit that no branch points at: every file git would track (ignored files and
# the usual heavy folders left out), through a throwaway index, so the user's own index and work tree are untouched.
SNAPSHOT_CMD = (
    'g=$(git rev-parse --absolute-git-dir 2>/dev/null) || exit 0; '
    'export GIT_INDEX_FILE="$g/code-mode-review-index"; cp "$g/index" "$GIT_INDEX_FILE" 2>/dev/null; '
    "git add -A -- . ':(exclude)*node_modules/*' ':(exclude)*venv/*' ':(exclude)*__pycache__/*' >/dev/null 2>&1; "
    't=$(git write-tree 2>/dev/null) && '
    'k=$(git -c user.name=code-mode -c user.email=code-mode@localhost commit-tree "$t" -m "turn start" 2>/dev/null) && '
    f'git update-ref {REVIEW_REF} "$k" && echo SNAPSHOT_OK; rm -f "$GIT_INDEX_FILE"')


# A worksheet line of the reviewer (agents/opencode-agents/review.md): TAG | item | status | evidence. Which
# statuses are a finding, and of what kind. (It lists the items that are fine too: listing is how it skips nothing.
# 2026-10-13, Qwen3.6 Q5 asked only for a list of findings: it reported four true ones and missed the three that
# mattered most, the made-up rooms, the menu that was gone and the part of the request that was never built.)
REVIEW_ROWS = {
    "ASKED": [(r"missing|half|partial|not built|narrower|simpler", "not built")],
    "HAD": [(r"gone|hard to find|removed|missing|hidden|no label", "removed")],
    "ADDED": [(r"made up|made-up|not asked|unasked|invented|engineer", "invented")],
    "TESTING": [(r"left", "left behind")],
    "CLAIM": [(r"false|not true|untrue", "not true"), (r"admit|not done|not tested|gave up|given up|skipp", "given up")],
}
# What the project had before the turn, for the reviewer's HAD lines: every control on the pages the turn changed
# (as the old version had them) and every route line the turn removed. {ref} filled in. Looked up by the pipe so the
# reviewer starts from the full list instead of from what it happens to notice.
BEFORE_CMD = (
    "for f in $(git diff --name-only {ref} -- '*.html' '*.htm' '*.vue' '*.jsx' '*.tsx' '*.svelte' 2>/dev/null | head -12); do "
    'echo "== $f (before)"; git show "{ref}:$f" 2>/dev/null | '
    "grep -n -o -i -E '<(button|a|select|input|textarea|nav|aside|dialog|form|summary)\\b[^>]*>[^<]{{0,60}}' | "
    "sed -E 's/[[:space:]]+/ /g' | cut -c1-170 | head -70; done; "
    'echo "== route lines this turn removed"; '
    "git diff {ref} -- '*.py' '*.js' '*.mjs' '*.ts' 2>/dev/null | "
    "grep -E '^-[^-].*(@[A-Za-z_.]+\\.(get|post|put|delete|patch|route)\\(|\\b(app|router)\\.(get|post|put|delete|patch|use)\\()' | "
    "cut -c1-160 | head -40; true")


def review_rows(text: str) -> tuple[int, list]:
    """(worksheet lines read, the findings among them as (kind, item with its status, evidence))."""
    n, found = 0, []
    for line in (text or "").splitlines():
        cells = [c.strip(" `*") for c in line.strip().strip("|").split("|")]
        tag = re.sub(r"^[\s\-*•\d.)]+", "", cells[0]).upper() if cells else ""
        if tag not in REVIEW_ROWS or len(cells) < 3 or not cells[1] or set(cells[1]) <= set("-: "):
            continue
        n += 1
        status = cells[2].lower()
        for pattern, kind in REVIEW_ROWS[tag]:
            if re.search(pattern, status):
                found.append((kind, " ".join(f"{cells[1]} ({cells[2]})".split()),
                              " ".join(" | ".join(c for c in cells[3:] if c).split())))
                break
    return n, found


def parse_review(text: str) -> tuple[Optional[bool], list]:
    """(passed, findings) from the reviewer's reply. Its worksheet lines decide: a line with a bad status is a
    finding whatever the last line says. A kind with more than three findings becomes one line, so that nine
    missing buttons do not push everything else off the list. Without worksheet lines, `- [kind] ...` lines count
    (and a bare FAIL keeps its plain bullets). passed is None when the reply holds neither lines nor a verdict."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    verdicts = REVIEW_VERDICT_RE.findall(text)
    rows, found = review_rows(text)
    listed, plain = [], []
    for line in text.splitlines():
        got = REVIEW_LINE_RE.match(line.replace("**", ""))
        if got:
            listed.append((got.group(1).lower(), " ".join(got.group(2).split())))
        elif re.match(r"^\s*(?:[-*•]|\d+[.)])\s+\S", line) and "|" not in line and not REVIEW_VERDICT_RE.search(line):
            plain.append(" ".join(re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).split()))
    have = {kind for kind, _, _ in found}
    found += [(kind, what, "") for kind, what in listed if kind not in have]  # only in its own list, not in a line
    if not rows and not verdicts and not found:
        return None, []
    findings = []
    for kind in REVIEW_KINDS.split("|"):
        mine = [(what, why) for k, what, why in found if k == kind]
        if len(mine) > 3:
            findings.append(f"[{kind}] {len(mine)} things: " + "; ".join(what[:90] for what, _ in mine[:14]))
        else:
            findings += [f"[{kind}] {what}" + (f" - {why}" if why else "") for what, why in mine]
    if not findings and re.search(r"\|\s*(?:unverified|could not check|blocked)\s*\|", text, re.I):
        return None, []
    if not findings and verdicts and verdicts[-1].upper() == "FAIL":
        findings = plain or [" ".join(text.split())[:300]]
    if not findings and (not verdicts or verdicts[-1].upper() == "INCOMPLETE"):
        return None, []
    return (not findings), findings[:10]


def review_prompt(requests: list, answers: list, summary: str, files: list, snapshot: bool, previous: list,
                  before: str = "", record: str = "") -> str:
    """What the reviewer is given: the user's own words, the builder's claims, and where to look. before: what the
    project had (BEFORE_CMD's output); record: what Code mode itself knows about `desktop verify` in this turn."""
    parts = ["[Code mode review - you are the reviewer here, not the engineer who did the work]",
             "Conversation context, oldest first (earlier requests are background, not a new checklist):\n"
             + "\n".join(f"{n}. {r}" for n, r in enumerate(requests, 1)),
             "Current task to review:\n" + (requests[-1] if requests else "(not supplied)"),
             "Review this task and regressions in changed behavior. Use earlier requests only to resolve references "
             "such as 'continue' or preserve constraints; do not re-demand already answered questions or unrelated "
             "features. A repository without commits is not evidence that history was deleted: compare the snapshot."]
    if answers:
        parts.append("Questions the user was asked, with their answers:\n" + "\n".join(answers))
    parts.append("The engineer's closing summary (claims to check, not facts):\n" + (summary.strip()[:4000] or "(none)"))
    if files:
        parts.append("Files this turn changed: " + ", ".join(files[:40]))
    if snapshot:
        parts.append(f"The project as it was before this turn is the git ref `{REVIEW_REF}`: start with "
                     f"`git diff --stat {REVIEW_REF}`.")
    else:
        parts.append("There is no snapshot of the project from before this turn. Judge \"removed\" from `git diff HEAD` "
                     "if the project has commits; otherwise skip that check.")
    if before.strip():
        parts.append("What the project had before (check preservation of controls affected by this task):\n" + before.strip()[:6000])
    if record:
        parts.append("Code mode's own record of this turn (it beats the summary): " + record)
    if previous:
        parts.append("A first review found the points below and the engineer was sent back to fix them. This is a "
                     "recheck, not a new review: give each point its line again with its status now, and look for "
                     "regressions only in what the fixes changed (`git diff` of those files). Do not redo the whole "
                     "review.\n" + "\n".join("- " + p for p in previous))
    parts.append("For installers, auth, restart/update and dependency changes, inspect failure handling and actual "
                 "runtime evidence, even for a one-line edit. A live health endpoint does not test a clean install, "
                 "signup, or another machine. A screenshot cannot prove a shell script or service works. "
                 "Do not run desktop navigation: it changes the builder's browser state. Use its recorded check "
                 "results and read-only inspection. If runtime evidence is missing, use 'unverified', not 'true' "
                 "or 'false'. Do not invent a defect just because this environment cannot test deployment.")
    parts.append("Reply in the form your instructions give: the worksheet lines (ASKED, HAD, ADDED, TESTING, CLAIM), "
                 "then `REVIEW: PASS`, `REVIEW: FAIL`, or `REVIEW: INCOMPLETE` as the last line. "
                 "Use INCOMPLETE when required evidence is unavailable and no concrete defect is established.")
    return "\n\n".join(parts)


def review_fix_prompt(findings: list) -> str:
    """What the builder gets when the review found something."""
    return ("[Independent review by Code mode - not a message from the user] A reviewer that did not watch you work "
            "compared the project before and after this turn with what the user asked for. It found:\n\n"
            + "\n".join("- " + f for f in findings) +
            "\n\nGo through them one by one. Check each yourself first (the reviewer can be wrong). If it is right, "
            "fix it at its cause; if it is wrong, say why in one line.\n"
            "- removed: put it back, working and easy to find (visible, with a label).\n"
            "- invented: take it out, so the project holds only what the user asked for or already had.\n"
            "- left behind: clean it up when it is in the project or the running app (test data, test settings, debug "
            "output, temp files in the project). Scratch outside the project, such as /tmp, can stay.\n"
            "- given up: find another way and do it. Only if it truly cannot be done here, say so in the first line "
            "of your answer, with what you tried.\n"
            "- not built: build it. If it is too big for this turn, build its core and say exactly what is missing.\n"
            "- not true: make it true, or take the claim back.\n"
            "Then run the relevant checks (desktop verification only for a changed web workflow), and end with a short answer that says, finding by finding, "
            "what you did.")


def review_note(res: dict, sent: int) -> str:
    """The review's outcome, as it appears in the reply."""
    noted = res.get("noted") or []
    if noted:
        noted = "\n> *Scratch outside the project, not sent back:* " + "; ".join(md_line(f, 200) for f in noted[:5])
    else:
        noted = ""
    if res.get("passed"):
        return ("> **Reviewed:** no actionable issues found in this turn's review. This is not proof of a clean "
                "installation or of workflows that were not exercised." + noted + "\n\n")
    if res.get("passed") is None:
        return (f"> **Review did not finish** ({md_line(res.get('error') or 'it gave no verdict', 160)}), so this "
                "turn was not checked against what you asked for.\n\n")
    n = len(res.get("findings") or [])
    note = f"> **⚠ Review: {n} thing{'s' if n != 1 else ''} still open**"
    if sent:
        note += f", after {sent} automatic send-back{'s' if sent != 1 else ''}"
    note += ". A separate check of this turn against what you asked for found:\n"
    return note + "\n".join("> - " + md_line(f, 900) for f in res["findings"]) + "\n\n"


FEATURE_READY_RE = re.compile(r"(?:^|[;&|(]\s*|\s)feature\s+ready\b")
DESKTOP_RE = re.compile(r"(?:^|[;&|(]\s*|\s)desktop\s+(?:look|open|click|type|key|scroll|back|wait|screenshot|status)\b")


class Events:
    """opencode's global event stream (/api/event), filtered to one session, as an async queue.

    Subscribe before sending the prompt; `ready` is set once the server confirms the subscription."""

    def __init__(self, client: httpx.AsyncClient, session_id: str):
        self.client, self.sid = client, session_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.task: Optional[asyncio.Task] = None

    @staticmethod
    def session_of(e: dict) -> Optional[str]:
        d = e.get("data") or {}
        return d.get("sessionID") or (d.get("form") or {}).get("sessionID")

    async def _read(self):
        try:
            async with self.client.stream("GET", "/api/event") as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        e = json.loads(line[5:])
                    except ValueError:
                        continue
                    if e.get("type") == "server.connected":
                        self.ready.set()
                    elif self.session_of(e) == self.sid:
                        await self.queue.put(e)
        except Exception as ex:  # surfaced to the consumer instead of dying silently
            await self.queue.put({"type": "stream.error", "data": {"message": f"{ex.__class__.__name__}: {ex}"}})
        finally:
            self.ready.set()

    async def __aenter__(self):
        self.task = asyncio.create_task(self._read())
        await asyncio.wait_for(self.ready.wait(), 15)
        return self

    async def __aexit__(self, *exc):
        if self.task:
            self.task.cancel()

    async def get(self, timeout: Optional[float] = None) -> dict:
        return await asyncio.wait_for(self.queue.get(), timeout)


class TurnTiming:
    """Non-secret timing observations for one Code mode turn.

    OpenCode events do not expose a clean, non-overlapping server-side timing
    breakdown. Keep each observed duration separate instead of presenting their
    sum as model time: a tool, compaction, and stream events can overlap.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._started = clock()
        self._first_event: Optional[float] = None
        self._last_event: Optional[float] = None
        self._reasoning_started: Optional[float] = None
        self._compaction_started: Optional[float] = None
        self._permission_started: Optional[float] = None
        self._tools: dict[str, tuple] = {}   # call id -> (started, phase)
        self._seconds = {"reasoning": 0.0, "compaction": 0.0, "permission": 0.0, "tool": 0.0}
        self._events = self._steps = self._tool_calls = 0
        self._tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        # Useful progress, as diagnostics (handoff stage A), not a rule: what was read, re-read unchanged, edited, checked.
        self._phase_seconds: dict[str, float] = {}
        self._read_at: dict[str, int] = {}     # path -> its edit generation when last read
        self._edit_gen: dict[str, int] = {}    # path -> times edited this turn
        self._activity = {"reads": 0, "repeat_reads": 0, "edits": 0, "shell_commands": 0, "checks": 0,
                          "checks_failed": 0}
        self._review = {"rounds": 0, "seconds": 0, "sent_back": 0}
        self._auto_checks = self._compactions = self._wakes = 0
        self.stopped = False

    def event(self):
        self._events += 1
        self._last_event = self._clock()
        if self._first_event is None:
            self._first_event = self._last_event

    def quiet_for(self) -> float:
        """Seconds since the last event from opencode (since the turn started, before the first)."""
        return max(0.0, self._clock() - (self._last_event if self._last_event is not None else self._started))

    def _close(self, name: str, started: Optional[float]) -> Optional[float]:
        if started is None:
            return None
        self._seconds[name] += max(0.0, self._clock() - started)
        return None

    def reasoning_started(self):
        self._reasoning_started = self._close("reasoning", self._reasoning_started)
        self._reasoning_started = self._clock()

    def reasoning_ended(self):
        self._reasoning_started = self._close("reasoning", self._reasoning_started)

    def tool_started(self, call_id: str, phase: str = "Working"):
        if call_id not in self._tools:
            self._tools[call_id] = (self._clock(), phase)
            self._tool_calls += 1

    def tool_ended(self, call_id: str):
        got = self._tools.pop(call_id, None)
        if got is not None:
            spent = max(0.0, self._clock() - got[0])
            self._seconds["tool"] += spent
            self._phase_seconds[got[1]] = self._phase_seconds.get(got[1], 0.0) + spent

    def tool_done(self, name: str, args: Any, failed: bool):
        """Count what a finished tool call did. A read of a file already read, with no edit to it since, is a repeat."""
        a = args if isinstance(args, dict) else {}
        path = str(a.get("filePath") or a.get("path") or "")
        if name == "read" and path:
            self._activity["reads"] += 1
            gen = self._edit_gen.get(path, 0)
            if self._read_at.get(path) == gen:
                self._activity["repeat_reads"] += 1
            self._read_at[path] = gen
        elif name in EDIT_TOOLS and not failed:
            self._activity["edits"] += 1
            if path:
                self._edit_gen[path] = self._edit_gen.get(path, 0) + 1
        cmd = str(a.get("command") or "")
        if cmd:
            self._activity["shell_commands"] += 1
            if tool_phase(name, a) == "Running checks":
                self._activity["checks"] += 1
                self._activity["checks_failed"] += 1 if failed else 0

    def reviewed(self, seconds: float, sent_back: bool):
        self._review["rounds"] += 1
        self._review["seconds"] += int(seconds or 0)
        self._review["sent_back"] += 1 if sent_back else 0

    def auto_checked(self):
        self._auto_checks += 1

    def wake_delivered(self, n: int = 1):
        self._wakes += n

    def permission_started(self):
        self._permission_started = self._close("permission", self._permission_started)
        self._permission_started = self._clock()

    def permission_ended(self):
        self._permission_started = self._close("permission", self._permission_started)

    def compaction_started(self):
        self._compaction_started = self._close("compaction", self._compaction_started)
        self._compaction_started = self._clock()
        self._compactions += 1

    def open_since(self, what: str) -> Optional[float]:
        """Seconds the reasoning / compaction / permission now open has run, or None when none is open."""
        started = {"reasoning": self._reasoning_started, "compaction": self._compaction_started,
                   "permission": self._permission_started}.get(what)
        return None if started is None else max(0.0, self._clock() - started)

    def compaction_ended(self):
        self._compaction_started = self._close("compaction", self._compaction_started)

    def step(self, tokens: dict):
        self._steps += 1
        cache = tokens.get("cache") or {}
        for source, target in (("input", "input"), ("output", "output")):
            value = tokens.get(source, 0)
            if isinstance(value, (int, float)):
                self._tokens[target] += max(0, int(value))
        for source, target in (("read", "cache_read"), ("write", "cache_write")):
            value = cache.get(source, 0)
            if isinstance(value, (int, float)):
                self._tokens[target] += max(0, int(value))

    def completed(self) -> dict:
        now = self._clock()
        return {
            "wall_seconds": round(max(0.0, now - self._started), 3),
            "first_event_seconds": None if self._first_event is None else round(max(0.0, self._first_event - self._started), 3),
            "reasoning_seconds": round(self._seconds["reasoning"], 3),
            "tool_elapsed_seconds": round(self._seconds["tool"], 3),
            "permission_seconds": round(self._seconds["permission"], 3),
            "compaction_seconds": round(self._seconds["compaction"], 3),
            "events": self._events,
            "steps": self._steps,
            "tool_calls": self._tool_calls,
            "tokens": dict(self._tokens),
            "phase_seconds": {k: round(v, 3) for k, v in sorted(self._phase_seconds.items())},
            "activity": dict(self._activity, files_read=len(self._read_at), files_edited=len(self._edit_gen)),
            "review": dict(self._review),
            "auto_checks": self._auto_checks,
            "compactions": self._compactions,
            "wakes_delivered": self._wakes,
            "stopped": self.stopped,
            "incomplete": {
                "reasoning": self._reasoning_started is not None,
                "tools": len(self._tools),
                "permission": self._permission_started is not None,
                "compaction": self._compaction_started is not None,
            },
        }


VM_NO_FOLDER = -2  # Pipe._vm_sh: the working folder does not exist on the VM

# Approvals and questions wait for the page, not for a pop-up (0.28.0). Open WebUI's own dialog (__event_call__) goes
# to the one browser tab that sent the message; once that tab reloads or leaves the chat it is gone for good, and
# Open WebUI waits for its answer forever (no WEBSOCKET_EVENT_CALLER_TIMEOUT is set). On 2026-10-26 that froze a
# reply while the agent sat waiting for an approval nobody could see. Now each one is listed here, the page script
# fetches the list (Pipe._ask_op) in every tab and browser of the chat's owner, and the answer comes back the same way.
PAGE_ASKS: dict = {}    # ask id -> {"id", "chat", "user", "sid", "ref", "type", "data", "t", "fut"}
FIXER_HOLDS: dict = {}  # chat id -> a report the user filed with the Fixer button: {"id", "view", "title", "t", ...}
# Open WebUI lets the user send while a reply still streams, and each message is a Code turn of its own on the chat's ONE
# opencode session: the new prompt joins the run in progress. On 2026-10-27 two turns then drove that one run, each
# sending its own automatic checks and, when it ended, starting its own reviewer: two model runs competing for the one
# slot, the older one going on for 15 minutes after its reply was over. Now the newest turn of a chat drives the run;
# an older one sees it was taken over, ends its reply with a note and starts nothing more (pipe 0.29.1).
CHAT_TURNS: dict = {}   # chat id -> the token of its newest turn whose prompt reached opencode
FIXER_HOLD_MAX_S = 2 * 3600  # an older hold is stale (the fixer caps a run well below this)
FIXER_WAIT_MAX_S = 2 * 3600  # a paused chat stops waiting after this (the fixer 45 min + Claude 60 min fit)
DIRECT_SCAN_S = 4.0     # how long one look at opencode's waiting requests is reused across list calls
_direct_scan: dict = {"t": 0.0, "items": []}
ANSWERED_ELSEWHERE = object()  # the request was answered (or withdrawn) without this turn: nothing left to do


def match_option(options: list, reply):
    """What a typed answer picks from a question's options, the way the question box explains it: nothing typed
    (or Open WebUI's True for an empty Confirm) = the first option, a number = that option, an option's name or a
    unique start of it = that option, anything else = the text as written. None = no answer (Cancel, nothing at all)."""
    if (reply is True or isinstance(reply, str) and not reply.strip()) and options:
        return options[0]
    if not isinstance(reply, str) or not reply.strip():
        return None
    said = reply.strip()
    labels = [str(o.get("label", "")) for o in options]
    if said.isdigit() and 1 <= int(said) <= len(options):
        return options[int(said) - 1]
    same = [o for o, label in zip(options, labels) if label.lower() == said.lower()]
    starts = [o for o, label in zip(options, labels) if label.lower().startswith(said.lower())]
    return same[0] if same else starts[0] if len(starts) == 1 else said


class PageAsk:
    """Stands in for Open WebUI's __event_call__ for one opencode request (a permission or a form): the dialog is
    listed for the page (PAGE_ASKS) and waited for here, with no time limit, since the answer may come from any tab
    or browser, later. A confirmation or input comes back as Open WebUI's dialogs give it (True / False, or the text);
    {"type": "permission"} as {"allow": bool, "message": str}. When opencode no longer has the request (answered from
    an older page, the run was stopped, the session is gone) the answer is ANSWERED_ELSEWHERE."""
    POLL = 3.0  # seconds between looks at opencode while waiting

    def __init__(self, c: httpx.AsyncClient, chat_id: str, user_id: str, sid: str, ref: str):
        self.c, self.chat_id, self.user_id, self.sid, self.ref = c, chat_id, user_id, sid, ref

    async def __call__(self, event: dict):
        fut = asyncio.get_running_loop().create_future()
        aid = "ask_" + os.urandom(8).hex()
        PAGE_ASKS[aid] = {"id": aid, "chat": self.chat_id, "user": self.user_id, "sid": self.sid, "ref": self.ref,
                          "type": str(event.get("type") or "input"), "data": dict(event.get("data") or {}),
                          "t": time.time(), "fut": fut}
        try:
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), self.POLL)
                except asyncio.TimeoutError:
                    if self.ref and not await self.still_open():
                        return ANSWERED_ELSEWHERE
        finally:
            PAGE_ASKS.pop(aid, None)
            if not fut.done():
                fut.cancel()

    async def still_open(self) -> bool:
        """opencode still waits on the request (when it can't tell: yes)."""
        what = "permission" if self.ref.startswith("per") else "form"
        try:
            r = await self.c.get(f"/api/session/{self.sid}/{what}")
            if r.status_code == 404:
                return False
            if r.status_code != 200:
                return True
            data = r.json().get("data")
            return not isinstance(data, list) or any(isinstance(x, dict) and x.get("id") == self.ref for x in data)
        except (httpx.HTTPError, ValueError):
            return True


def fixer_result_text(rec: dict) -> str:
    """A finished fixer report as the agent reads it (the lines report-bug prints when it returns)."""
    lines = [f"Verdict: {rec.get('verdict') or 'unknown'}",
             "Summary: " + (str(rec.get("summary") or "").strip() or "-")]
    esc = rec.get("escalation") or {}
    if esc.get("state") == "done":
        lines.insert(1, f"(The fixer said {(esc.get('fixer') or {}).get('verdict')}, so Claude took over; this is "
                        "Claude's verdict.)")
    elif esc:
        lines.insert(1, f"(Handed to Claude, who did not finish: {esc.get('note') or esc.get('state')}.)")
    if rec.get("verdict") == "cancelled":
        lines.append("The user cancelled it: carry on without the fixer's help.")
    if rec.get("root_cause"):
        lines.append(f"Root cause: {rec['root_cause']}")
    changed = rec.get("changed") or []
    lines.append("Changed:" + ("" if changed else " (none)"))
    lines += [f"  - {c.get('path')}: {c.get('why')}" for c in changed if isinstance(c, dict)]
    if rec.get("tests"):
        lines.append(f"Tests: {rec['tests']}")
    if rec.get("restart"):
        lines.append("Services restarted: " + ", ".join(map(str, rec["restart"])))
    if rec.get("for_the_chat"):
        lines.append(f"What the chat should do next: {rec['for_the_chat']}")
    return "\n".join(lines)


def fixer_resume_prompt(hold: dict, rec: dict, paused: bool = True) -> str:
    """What the agent is told after a report the user filed with the Fixer button: paused = it interrupted a run of
    the agent's (which then carries on), else it ran before this turn started (the result rides with the message)."""
    title = hold.get("title") or "Look into this chat"
    if not paused:
        return (f"<fixer_result>\nBefore this message the user had the bug fixer look into this chat: \"{title}\".\n"
                + fixer_result_text(rec) + "\n</fixer_result>")
    return ("<fixer_result>\nThe user had the bug fixer look into this chat while you worked: "
            f"\"{title}\". You were paused between steps while it ran (the command you were running finished first; "
            "nothing was undone).\n" + fixer_result_text(rec) +
            "\n</fixer_result>\n\nCarry on with what you were doing. If the fixer changed a tool you were using, retry "
            "what failed with it.")


def _gh_finding(fid: str, text: str, action: str) -> dict:
    """One Diagnose finding: what was found, and the panel action that answers it ("" when the fix is the
    agent's git work or the user's decision)."""
    return {"id": fid, "text": text, "action": action}


def _gh_commit(rec: str) -> dict:
    """A `git log --format='%h%x1f%s'` record -> {"short", "subject"}; {} when it is not one."""
    short, sep, subject = rec.partition("\x1f")
    return {"short": short, "subject": subject.strip()[:120]} if sep and short else {}


class GitHubError(Exception):
    """A GitHub call failed; the message is what the panel shows."""


class GitHubSignedOut(GitHubError):
    """GitHub answered 401: the saved token is dead."""


class Pipe:
    class Valves(BaseModel):
        OPENCODE_URL: str = Field("http://ai-sandbox:4096", description="opencode server on the AI sandbox VM")
        OPENCODE_PASSWORD: str = Field("", description="password from /home/coder/.config/opencode/service.json")
        PROJECTS_ROOT: str = Field("/home/coder/projects", description="each chat works in PROJECTS_ROOT/chats/<chat id>")
        MODELS: str = Field("slerm/ornith-1.5-35b-a3b",
                            description="Comma-separated opencode models (provider/model); each becomes a 'Code' model")
        TASK_MODEL: str = Field("slerm-turing.tasks-llama3.2-1b",
                                description="Open WebUI model that writes chat titles/tags for Code chats when the "
                                            "router has nothing loaded. Keep it one that loads nothing: the small CPU "
                                            "model beside the router (blank = no titles)")
        TASK_ROUTER_MODELS: str = Field("http://slerm-head:8090/v1/models",
                                        description="slerm gateway model list: titles/tags use the model the router "
                                                    "already has loaded, else TASK_MODEL (blank = always TASK_MODEL)")
        PERMISSIONS: str = Field("ask", description="When opencode asks to run something risky: ask (pop-up), allow, reject")
        PAGE_ASKS: bool = Field(True, description="Approvals and questions wait in the page's own box, which every tab "
                                                  "and browser of the chat's owner shows until it is answered (off = "
                                                  "Open WebUI's pop-up, lost for good if that tab reloads)")
        FIXER_PAUSE: bool = Field(True, description="A report filed with the Fixer button pauses the chat's running "
                                                    "turn (after the command in flight) until the fixer is done, and "
                                                    "a new turn waits for it")
        COMPACT: bool = Field(True, description="Fold tool calls and thinking into the status timeline and one "
                                                "'Worked for … · N steps' block, so the answer isn't buried")
        SHOW_REASONING: bool = Field(True, description="Show the model's thinking as a folded block (when not COMPACT)")
        CONTEXT_CHARS: int = Field(6000, description="Earlier chat messages handed to opencode when it hasn't seen them")
        ATTACH_MAX_MB: int = Field(12, description="Largest attached image handed to the agent, in MB: bigger ones "
                                                   "are said to be too big in the reply (Open WebUI caps its own uploads earlier)")
        TOOL_RESULT_CHARS: int = Field(4000, description="Tool output kept in each folded tool block")
        DIFF_CHARS: int = Field(20000, description="Diff shown at the end of a turn")
        ASK_FIRST: bool = Field(True, description="The first Code turn of a chat only asks: the agent puts its "
                                "questions (each with its own pick) and builds after the answer. It is skipped when "
                                "the message says not to ask (\"no questions\", \"just build it\")")
        AUTO_VERIFY: int = Field(2, description="A turn that changed a web app ends only after `desktop verify` passes: "
                                                "send the agent back this many times, then mark the reply 'Not "
                                                "verified' (0 = off)")
        AUTO_REVIEW: int = Field(1, description="A turn that changed files is checked by a separate reviewer session "
                                                "against what you asked for (anything removed, made up, left behind, "
                                                "given up on or not built). Send the agent back this many times with "
                                                "what it found, then list what is still open in the reply (0 = off)")
        REVIEW_MIN_LINES: int = Field(30, description="A turn that added and removed fewer lines than this in all is "
                                                      "not reviewed: a small tweak rarely loses or invents anything, "
                                                      "and a review costs minutes (0 = review every turn that changed "
                                                      "a file)")
        REVIEW_MINUTES: int = Field(15, description="Longest one review round may take before it is dropped")
        CONTEXT_COMPACT_TOKENS: int = Field(98304, description="Backstop for opencode's own compaction (which starts "
                                                          "at context - compaction.reserved, the same 98,304 here): "
                                                          "ask for a checkpoint only when a step ends at or past this "
                                                          "size and opencode did not compact after the step before "
                                                          "(0 disables the backstop)")
        HEARTBEAT_SECONDS: int = Field(30, description="While nothing arrives from opencode, the status line says what "
                                                       "it is waiting on and for how long: first after this many "
                                                       "seconds, then at doubling gaps up to 8 minutes (0 = off)")
        LATER_URL: str = Field("http://ai-sandbox:4101", description="code-later (the agent's `later` / `slerm-job "
                                                                    "--later` wake-ups) as Open WebUI reaches it")
        LATER_TOKEN: str = Field("", description="LATER_TOKEN from /etc/code-mode/later.env. With it, a wake-up that "
                                                 "is ready when this chat's turn starts or reaches the end of a run is "
                                                 "handed to the agent in that same turn instead of waiting for the "
                                                 "turn to end, and Stop holds the chat's wake-ups until your next "
                                                 "message (blank = code-wake delivers them after the turn, as before)")
        WAKES_PER_TURN: int = Field(3, description="Most wake-up rounds one turn takes in at the end of a run "
                                                   "(later ones wait for code-wake after the turn)")
        DESKTOP_URL: str = Field("http://ai-sandbox:6080",
                                 description="sandbox-agent as your BROWSER reaches it (live view of the chat's desktop)")
        VIEW_TOKEN: str = Field("", description="VIEW_TOKEN from /etc/sandbox-agent/env (blank = no live view)")
        PREVIEW_URL: str = Field("http://ai-sandbox:4097",
                                 description="Preview server as your BROWSER reaches it (the Preview panel shows the project)")
        PREVIEW_SECRET: str = Field("", description="PREVIEW_SECRET from /etc/code-mode/preview.env (blank = no preview link)")
        PREVIEWS_URL: str = Field("http://{host}/previews",
                                  description="The address a reply gives for the chat's app where the agent wrote its own "
                                              "localhost one: this, then /<chat id>/. {host} = the host this Open WebUI "
                                              "was opened at. code-previews on this machine answers it and forwards the "
                                              "chat's owner to the preview (blank = localhost addresses stay as written)")
        PREVIEWS_HOST: str = Field("cryoai", description="{host} for PREVIEWS_URL when the request came from this "
                                                             "machine itself (scripts), not from a browser")
        LIVE_URL: str = Field("http://ai-sandbox:4099",
                              description="code-live as your BROWSER reaches it: the live window in each reply that follows "
                                          "the agent - desktop, code being typed, files, terminal, web (blank = off; "
                                          "signed with PREVIEW_SECRET; with it on, the desktop shows inside it)")
        FIXER_URL: str = Field("http://ai-sandbox:4098", description="code-fixer (bug reports) as your browser reaches it")
        FIXER_TOKEN: str = Field("", description="FIXER_TOKEN from /etc/code-mode/fixer.env (blank = no Fixer window)")
        FEATURES_URL: str = Field("http://ai-sandbox:4100", description="code-features (feature chats) as Open WebUI reaches it")
        FEATURES_TOKEN: str = Field("", description="FEATURES_TOKEN from /etc/code-mode/features.env (blank = no feature chats)")
        DEPLOY_URL: str = Field("http://cryoai:8095", description="code-deploy (the Features panel) as your browser reaches it")
        FEATURE_MODEL: str = Field("slerm/ornith-1.5-35b-a3b",
                                    description="opencode model offered for a feature chat when the picked model "
                                                "isn't one opencode knows (provider/model); used only if you say yes")
        MODEL_PREFIXES: str = Field("slerm-turing.=slerm",
                                    description="Open WebUI model-id prefix = opencode provider, comma-separated. The "
                                                "Chat | Code toggle sends the picked model; matching ones run the agent")
        GITHUB_API: str = Field("https://api.github.com",
                                description="GitHub API, for the GitHub button (tests point it at a fake)")
        GITHUB_WEB: str = Field("https://github.com", description="GitHub site, for the device-flow sign-in")
        GITHUB_GIT: str = Field("git@github.com:",
                                description="Where git on the VM reaches a repo: this + owner/name.git (tests point it "
                                            "at a folder of bare repositories)")
        GITHUB_STATE: str = Field("~/.config/code-mode/github.json",
                                  description="Where the GitHub button keeps the OAuth App's Client ID and your sign-in "
                                              "token, on this machine only (file mode 0600). Delete it to forget both")

    def __init__(self):
        self.valves = self.Valves()
        self.sessions: dict[str, str] = {}  # chat id -> opencode session id
        self._known: set = set()  # (provider, model) pairs opencode serves; see _picked_model
        self._limits: dict = {}   # (provider, model) -> opencode's context limit for it, for the page's context ring
        self._known_at = -1e9

    def _models(self) -> dict[str, dict]:
        out = {}
        for m in self.valves.MODELS.split(","):
            provider, _, model = m.strip().partition("/")
            if model:
                out[model] = {"id": model, "providerID": provider}
        return out

    def pipes(self):
        # The entry every Code turn is routed through; the model that does the work is the one in the picker
        # (code_model). Its name must not name a model: "Code · ornith-..." over a Qwen run read as a model swap.
        keys = list(self._models())
        return [{"id": k, "name": "Coding agent" if len(keys) == 1 else f"Coding agent · {k}"} for k in keys]

    async def pipe(self, body: dict, __user__: dict, __request__, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                   __event_call__: Callable[[dict], Awaitable[Any]] = None, __task__: Optional[str] = None,
                   __metadata__: Optional[dict] = None):
        if __task__:  # title / tags / follow-ups: a plain model, not the coding agent
            return await self._task(__request__, __user__, body) if self.valves.TASK_MODEL else ""
        if body.get("code_preview") is not None:  # code-previews asking where a chat's preview is (before code_github:
            return self._preview_op(body, __user__)  # its calls carry that too, to stop a pipe older than this one)
        if body.get("code_fixer") is not None:  # the Fixer button (it sends code_github too, so an older pipe answers
            return await self._fixer_op(body, __user__)  # "unknown GitHub op" instead of starting a coding turn)
        if body.get("code_ask") is not None:  # the page's approval / question box (code_github rides along likewise)
            return await self._ask_op(body, __user__)
        if body.get("code_desktop") is not None:  # the Desktop and Upload buttons (0.30.0; code_github rides along too)
            return self._desktop_op(body, __user__)
        if body.get("code_github") is not None:  # the GitHub button's calls: a dict straight back, never a chat turn
            return await self._github(body, __user__, __request__)
        key = body["model"].split(".", 1)[1] if "." in body["model"] else body["model"]
        model = self._models().get(key) or next(iter(self._models().values()))
        # The model picked in the model picker. The pipe's own entry ("Coding agent") is not one: a continue or
        # regenerate of a reply saved under the pipe's name sends it, and such a turn asks before it uses a model.
        picked = str(body.get("code_model") or "")
        if picked.split(".", 1)[0] == body["model"].split(".", 1)[0]:
            picked = ""
        meta = __metadata__ or {}
        chat_id = meta.get("chat_id") or ""
        if not chat_id or chat_id.startswith("local:"):
            chat_id = f"temp-{meta.get('message_id') or int(time.time())}"
        att_files = (body.get("metadata") or meta).get("files") or []  # images the user attached to this turn
        if self.valves.FEATURES_TOKEN and not chat_id.startswith("temp-"):
            feature = await self._feature_of(chat_id, create=bool(body.get("code_feature")))
            if feature is not None:  # a feature chat: the agent extends Code mode itself, in the feature's workspace
                provider, _, fmodel = self.valves.FEATURE_MODEL.partition("/")  # used when the picked model can't run it
                return self._run(body.get("messages", []), chat_id, {"id": fmodel, "providerID": provider},
                                 __event_emitter__, __event_call__, picked=picked, feature=feature,
                                 effort=body.get("code_effort"), att=att_files, user=__user__,
                                 wake=bool(body.get("code_wake")))
        friendly = "" if chat_id.startswith("temp-") else self._friendly_url(chat_id, __request__)
        # The Chat | Code toggle sends the model picked in the model picker; the agent runs on it when it can.
        # code_repo: the GitHub button's "Work on it" - the chat's project is a clone of that repo (see _gh_attach).
        return self._run(body.get("messages", []), chat_id, model, __event_emitter__, __event_call__,
                         picked=picked, effort=body.get("code_effort"),
                         repo=str(body.get("code_repo") or ""), friendly=friendly, att=att_files,
                         user=__user__, wake=bool(body.get("code_wake")))

    # ------------------------------------------------------------------ feature chats

    def _features(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.valves.FEATURES_URL.rstrip("/"), timeout=20,
                                 headers={"Authorization": f"Bearer {self.valves.FEATURES_TOKEN}"})

    async def _feature_of(self, chat_id: str, create: bool) -> Optional[dict]:
        """The chat's feature (made on its first message when the page flagged it), or None for a normal chat.
        A chat flagged as a feature chat never falls back to a normal one: errors come back as {"error": ...}."""
        try:
            async with self._features() as f:
                if create:
                    r = await f.post("/api/features", json={"chat_id": chat_id})
                    r.raise_for_status()
                    return r.json()["feature"]
                r = await f.get("/api/features", params={"chat": chat_id})
                r.raise_for_status()
                found = [x for x in r.json().get("features") or [] if x.get("chat_id") == chat_id]
                return found[0] if found else None
        except (httpx.HTTPError, KeyError, ValueError) as e:
            return {"error": f"{e.__class__.__name__}: {e}"} if create else None

    def _feature_link(self, n: int, ready: bool = False) -> str:
        return f"{self.valves.DEPLOY_URL.rstrip('/')}/features/#f{n}{'-ready' if ready else ''}"

    async def _feature_diff(self, n: int) -> list:
        """The feature's pending changes, as opencode-style diff entries named <folder>/<file>."""
        try:
            async with self._features() as f:
                r = await f.get(f"/api/features/{n}/diff")
                r.raise_for_status()
                return [{**x, "file": f"{x.get('component', '?')}/{x.get('file', '?')}"} for x in r.json().get("files") or []]
        except (httpx.HTTPError, ValueError):
            return []

    async def _task_model(self) -> str:
        """The router's loaded (or loading) model when it has one: a title/tags request for any other model swaps
        it out of the router's single slot, minutes each way, and the chat's prompt cache goes with it. With nothing
        loaded it is TASK_MODEL, the CPU model beside the router: a title must not wake the cluster or load a model
        nobody picked (until 0.9.2 this was Ornith)."""
        url = self.valves.TASK_ROUTER_MODELS
        prefix = self.valves.TASK_MODEL.split(".", 1)[0] + "." if "." in self.valves.TASK_MODEL else ""
        if url and prefix:
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    r = await c.get(url)
                for m in r.json().get("data") or []:
                    if (m.get("slerm") or {}).get("status") in ("loaded", "loading"):
                        return prefix + m["id"]
            except (httpx.HTTPError, ValueError, KeyError, AttributeError):
                pass
        return self.valves.TASK_MODEL

    async def _task(self, request, user: dict, body: dict) -> str:
        from open_webui.models.users import Users
        from open_webui.utils.chat import generate_chat_completion

        form = {"model": await self._task_model(), "messages": body["messages"], "stream": False}
        try:
            res = await generate_chat_completion(request, form, Users.get_user_by_id(user["id"]), bypass_filter=True)
            if hasattr(res, "body"):
                res = json.loads(res.body.decode() or "{}")
            msg = res["choices"][0]["message"]
            return msg.get("content") or msg.get("reasoning_content") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ sessions

    async def _session(self, c: httpx.AsyncClient, chat_id: str, model: dict,
                       feature: Optional[dict] = None, pinned: bool = False) -> tuple[str, dict]:
        """The chat's opencode session (created with its project folder on first use). A feature chat works in
        its feature's workspace, which code-features already made. pinned: the page sent a thinking level, so
        the session's variant must match model's (none given = opencode's "default")."""
        directory = feature["dir"] if feature else f"{self.valves.PROJECTS_ROOT.rstrip('/')}/chats/{chat_id}"
        sid = self.sessions.get(chat_id)
        if not sid:
            r = await c.get("/api/session", params={"directory": directory})
            r.raise_for_status()
            for s in r.json().get("data", []):
                if (s.get("metadata") or {}).get("owui_chat_id") == chat_id:
                    sid = s["id"]
                    break
        if sid:
            r = await c.get(f"/api/session/{sid}")
            if r.status_code == 200:
                info = r.json()["data"]
                self.sessions[chat_id] = sid
                cur = info.get("model") or {}
                if cur.get("id") != model["id"] or \
                        pinned and cur.get("variant", "default") != model.get("variant", "default"):
                    await c.post(f"/api/session/{sid}/model", json={"model": model})
                return sid, info
        if feature:
            r = await c.post("/api/session", json={"model": model, "location": {"directory": directory}, "metadata": {
                "owui_chat_id": chat_id, "owui_feature": feature["n"], "owui_seen": 0}})
            r.raise_for_status()
            info = r.json()["data"]
            self.sessions[chat_id] = info["id"]
            return info["id"], info
        # New chat: make its folder (a git repo, so opencode can diff each turn).
        await self._shell_once(c, f"mkdir -p 'chats/{chat_id}' && git -C 'chats/{chat_id}' init -q")
        r = await c.post("/api/session", json={"model": model, "location": {"directory": directory},
                                               "metadata": {"owui_chat_id": chat_id, "owui_seen": 0}})
        r.raise_for_status()
        info = r.json()["data"]
        self.sessions[chat_id] = info["id"]
        return info["id"], info

    async def _shell_once(self, c: httpx.AsyncClient, cmd: str, timeout: float = 30) -> None:
        """Run one shell command from a short-lived session in PROJECTS_ROOT and wait for it to end."""
        r = await c.post("/api/session", json={"location": {"directory": self.valves.PROJECTS_ROOT}})
        r.raise_for_status()
        setup = r.json()["data"]["id"]
        try:
            async with Events(c, setup) as ev:
                (await c.post(f"/api/session/{setup}/shell", json={"command": cmd})).raise_for_status()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        e = await ev.get(timeout=deadline - time.monotonic())
                    except asyncio.TimeoutError:
                        break
                    if e.get("type", "").endswith("shell.ended"):
                        break
        finally:
            await c.delete(f"/api/session/{setup}")

    async def _reap_servers(self, c: httpx.AsyncClient, chat_id: str, kill: bool = True) -> None:
        """Stop servers other chats left running (see REAP_CMD); this chat's own keep running. Also leaves the chat's
        signed Preview path where `desktop verify` finds it, so it checks the app through the preview server, the way
        the user's Preview panel shows it (2026-10-09). Never fails a turn. kill=False (a `later` wake-up turn, which
        the user did not start): only the Preview path, other chats' servers stay up."""
        keep = re.sub(r"[^A-Za-z0-9_-]", "", chat_id)
        if not keep:
            return
        cmd = reap_command(self.valves.PROJECTS_ROOT.rstrip("/"), keep,
                           urllib.parse.urlsplit(self._preview_url(keep)).path, kill)
        if not cmd:
            return
        try:
            await self._shell_once(c, cmd, 15)
        except Exception:  # noqa: BLE001
            pass

    def _prompt(self, messages: list, seen: int) -> str:
        """The user's message, plus whatever happened in the chat that opencode has not seen yet
        (chat-mode turns before switching to Code, or between Code turns)."""
        last = messages[-1] if messages else {"content": ""}
        said, docs = split_rag(text_of(last))
        seen = min(seen, max(len(messages) - 1, 0))
        earlier = []
        for m in messages[seen:-1]:
            if m.get("role") in ("user", "assistant"):
                t = split_rag(text_of(m))[0] if m.get("role") == "user" else text_of(m)
                if t:
                    earlier.append(f"{m['role']}: {t}")
        parts = []
        context = "\n\n".join(earlier)[-self.valves.CONTEXT_CHARS:]
        if context:
            when = "Said in the chat since your last turn" if seen else "Earlier in this chat, before you were asked to code"
            parts.append(f"<chat_context>\n{when}:\n{context}\n</chat_context>")
        if docs:
            parts.append(f"<attached_material>\n{docs[:self.valves.CONTEXT_CHARS]}\n</attached_material>")
        parts.append(said)
        return "\n\n".join(parts)

    # --------------------------------- attached images (pipe 0.24.0): the user's screenshots reaching the agent

    def _attach_want(self, messages: list, att_files: list, delivered) -> list:
        """Images in this chat's user messages that opencode has not been given yet, oldest first.

        Every shape Open WebUI can send a message in is read: the `image_url` / `input_image` content parts its
        middleware fills in for a model call (base64 data URLs after Open WebUI's own base64 conversion), each
        message's `files` records, and the turn's metadata files. A record and a part naming the same image - the
        usual case, the part carrying the bytes and the record the file id and name - pair into one candidate; a
        data URL with no record stands alone; a record with no data URL has its bytes read later from Open WebUI's
        storage, which this pipe can do because it runs inside that process. `delivered` (the session meta's
        owui_files) is why nothing is ever handed over twice."""
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        want, keys = [], set(delivered)

        def add(cand: dict):
            if cand["key"] not in keys:
                keys.add(cand["key"])
                want.append(cand)

        for i, m in enumerate(messages):
            if m.get("role") != "user":
                continue
            entries = [f for f in (m.get("files") or []) if image_entry(f)]
            if i == last_user:
                entries += [f for f in (att_files or []) if image_entry(f)]
            urls = {f.get("url") for f in entries}
            inline, fetch, parts = [], [], []
            for f in entries:
                b = data_image(f.get("url"))
                (inline if b else fetch).append((f, b))
            if isinstance(m.get("content"), list):
                for p in m.get("content"):
                    if not isinstance(p, dict) or p.get("type") not in IMAGE_PART_TYPES:
                        continue
                    u = p.get("image_url")
                    u = u.get("url") if isinstance(u, dict) else u
                    if not isinstance(u, str) or u in urls:
                        continue
                    b = data_image(u)
                    if b:
                        parts.append((u, b))
                    else:  # a part naming Open WebUI storage with no file record beside it: the url names the file
                        fetch.append(({"id": u.split("/")[-1], "url": u, "name": "", "content_type": "image/png"}, None))
            for f, b in inline:  # bytes in hand: Open WebUI's storage is not touched
                add({"key": str(f.get("id") or sha1_key(b[1])), "name": f.get("name") or "", "mime": b[0],
                     "b64": b[1], "id": f.get("id"), "url": f.get("url"), "msg": i})
            for (f, _), (_, b) in zip(fetch, parts):  # one image twice over: the part's bytes, the record's id and name
                add({"key": str(f.get("id") or sha1_key(b[1])), "name": f.get("name") or "", "mime": b[0],
                     "b64": b[1], "id": f.get("id"), "url": f.get("url"), "msg": i})
            for _, b in parts[len(fetch):]:
                add({"key": sha1_key(b[1]), "name": "", "mime": b[0], "b64": b[1], "id": None, "url": None, "msg": i})
            for f, _ in fetch[len(parts):]:
                add({"key": str(f.get("id") or sha1_key(str(f.get("url")))), "name": f.get("name") or "",
                     "mime": f.get("content_type") or "image/png", "b64": None, "id": f.get("id"),
                     "url": f.get("url"), "msg": i})
        return want

    async def _attach_bytes(self, cand: dict, user: Optional[dict] = None):
        """(mime, base64) for an attached image that arrived as an Open WebUI file id only: read the file the way
        Open WebUI's own GET /files/{id}/content does - this pipe runs inside that process, so its storage models
        are at hand - with the same check: the file is the turn's user's, the user is an admin, or Open WebUI's
        access rules share it with them. Unknown id, gone file, a file the user may not read, no user, or no Open
        WebUI module to import (the tests): None, and the turn says plainly that the image did not come through.
        Open WebUI 0.8's model calls are plain functions (its own async routes call them without await), so a
        result is awaited only when it is awaitable."""
        fid = str(cand.get("id") or "")
        if not fid or not (user or {}).get("id"):
            return None
        for mod in ("open_webui", "apps.webui"):  # 0.6 renamed apps.webui to open_webui; the deployed one may be older
            try:
                files = importlib.import_module(f"{mod}.models.files").Files
            except ImportError:
                continue
            try:
                file = await awaited(files.get_file_by_id(fid))
                if not file or not getattr(file, "path", None):
                    return None
                if not await self._attach_may_read(mod, file, fid, user):
                    print(f"code-mode: attached file {fid} is not readable by this chat's user; left out", flush=True)
                    return None
                try:  # newer builds resolve the stored path through Storage; older ones keep it under UPLOAD_DIR
                    path = importlib.import_module(f"{mod}.storage.provider").Storage.get_file(file.path)
                except ImportError:
                    path = os.path.join(str(importlib.import_module(f"{mod}.env").UPLOAD_DIR), str(file.path).lstrip("/"))
                if not os.path.isfile(path):
                    return None
                with open(path, "rb") as fh:
                    data = fh.read(self.valves.ATTACH_MAX_MB * 1024 * 1024 + 1)
                meta = getattr(file, "meta", None)
                mime = (meta or {}).get("content_type") if isinstance(meta, dict) else None
                return mime or cand.get("mime") or "image/png", base64.b64encode(data).decode()
            except Exception as e:  # noqa: BLE001 - the read failed: the plain note, and the reason in the log
                print(f"code-mode: could not read attached file {fid} ({e.__class__.__name__}: {e})", flush=True)
                return None
        return None

    @staticmethod
    async def _attach_may_read(mod: str, file, fid: str, user: dict) -> bool:
        """Open WebUI's own rule for reading a file's content (routers/files.py): owner, admin, or has_access_to_file."""
        if getattr(file, "user_id", None) == user.get("id") or user.get("role") == "admin":
            return True
        try:
            who = await awaited(importlib.import_module(f"{mod}.models.users").Users.get_user_by_id(user["id"]))
            check = importlib.import_module(f"{mod}.utils.access_control.files").has_access_to_file
            return bool(who) and bool(await awaited(check(fid, "read", who)))
        except Exception as e:  # noqa: BLE001 - no way to check: no read
            print(f"code-mode: could not check access to attached file {fid} ({e.__class__.__name__})", flush=True)
            return False

    async def _model_vision(self, c, model: dict):
        """Whether opencode's model listing says this turn's model takes image input (every opencode model carries
        capabilities.input). None when the listing says nothing about it: then hand the image over anyway - quietly
        dropping a user's attachment is worse than a provider that might refuse it."""
        key = (model.get("providerID"), model.get("id"))
        cache = self.__dict__.setdefault("_vision_cache", {})
        if key in cache:
            return cache[key]
        found = None
        try:
            r = await c.get("/api/model")
            if r.status_code == 200:
                for m in r.json().get("data") or []:
                    if m.get("id") == key[1] and m.get("providerID") == key[0]:
                        caps = (m.get("capabilities") or {}).get("input")
                        found = None if caps is None else ("image" in caps)
                        break
        except (httpx.HTTPError, ValueError):
            pass
        if len(cache) < 64:
            cache[key] = found
        return found

    async def _attach_handover(self, c, messages: list, att_files: list, meta: dict, model: dict,
                               user: Optional[dict] = None):
        """Hand the chat's attached images to opencode as the prompt's `files` (a base64 data URL lands as image
        media on a vision model - checked against the deployed opencode), plus note lines telling the agent what
        came with the message: an image from an earlier message arriving for the first time, one too big, too many,
        unreadable, or a model that can't see images at all. Returns (files, notes, given); given is
        [(key, file id, url)] - record the keys in the session meta and no image is ever handed over twice."""
        want = self._attach_want(messages, att_files, {str(k) for k in (meta.get("owui_files") or [])})
        if not want:
            return [], [], []
        if await self._model_vision(c, model) is False:
            names = ", ".join((w["name"] or "an image") for w in want[:ATTACH_MAX_FILES])
            return [], [f"<attachment_note>The user attached {len(want)} image(s) ({names}), but the model running "
                        f"this chat ({model.get('id')}) takes no image input. Say at the start of your reply that "
                        f"you could not see the attached image, and that picking a vision-capable model in the "
                        f"model picker would let you see it.</attachment_note>"], []
        files, notes, given = [], [], []
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        for w in want[:ATTACH_MAX_FILES]:
            b64, mime = w["b64"], w["mime"] or "image/png"
            if b64 is None and w.get("id"):
                got = await self._attach_bytes(w, user)
                if got:
                    mime, b64 = got
            if b64 is None:
                notes.append(f"<attachment_note>The attached image {w['name'] or w['id']} could not be read from "
                             f"Open WebUI's storage; the user can attach it again.</attachment_note>")
                continue
            if len(b64) * 3 // 4 > self.valves.ATTACH_MAX_MB * 1024 * 1024:
                notes.append(f"<attachment_note>The attached image {w['name'] or w['id']} is bigger than "
                             f"{self.valves.ATTACH_MAX_MB} MB and was not handed over.</attachment_note>")
                continue
            name = w["name"] or f"image-{len(files) + 1}.{mime.split('/')[-1] or 'png'}"
            files.append({"uri": f"data:{mime};base64,{b64}", "name": name,
                          "description": "Attached by the user in Open WebUI"})
            given.append((str(w["key"]), w.get("id"), w.get("url")))
            if w["msg"] != last_user:  # an attachment older than this message: the agent missed it on its own turn
                quote = text_of(messages[w["msg"]])[:80] or "..."
                notes.append(f"<attachment_note>The image {name} was attached to an earlier message of this chat "
                             f"({quote!r}). It reaches you attached to this message - you are seeing it for the "
                             f"first time now.</attachment_note>")
        if len(want) > ATTACH_MAX_FILES:
            notes.append(f"<attachment_note>{len(want) - ATTACH_MAX_FILES} more attached image(s) were left out "
                         f"(at most {ATTACH_MAX_FILES} per turn).</attachment_note>")
        return files, notes, given

    @staticmethod
    def _check_prompt(why: str, check: "TurnCheck", asked: str) -> str:
        """What the agent gets when it ended a turn on a web app without a passing `desktop verify`."""
        url = check.url or "<your app's address>"
        parts = [f"[Automatic check by Code mode - not a message from the user] You ended your turn with {why}. "
                 "The turn isn't finished until `desktop verify` passes after your last change."]
        if check.problems:
            parts.append("The last `desktop verify` found:\n" + check.problems)
        parts.append("Now:\n"
                     f"1. Make sure your app's server is running (started detached, so it outlives your shell) at {url}.\n"
                     f"2. Open or verify {url} after the last edit. For a login-protected screen, authenticate with "
                     "the app's disposable local test account, navigate to that screen, then run "
                     "`desktop verify --here --expect \"<visible controls on this screen>\"`. A post-edit open "
                     "followed by a passing --here check counts; an old tab alone does not.\n"
                     "3. Test each requested behavior separately. --expect describes visible UI only, never an "
                     "installer file or a successful remote deployment. Check scripts, APIs and service behavior "
                     "with appropriate commands and functional tests. A login page or health endpoint alone is not enough.\n"
                     "4. Fix actual defects. Authentication/setup blockers and expected negative-test responses need "
                     "the correct test setup, not changes that hide application errors.\n"
                     "Don't hide errors, swallow exceptions or add fallback/demo data to make it pass. If something "
                     "really can't work (an outside service is down, say), stop and say exactly what and why.\n"
                     "Then finish with a short answer: what you changed and what the passing verify showed.")
        if asked:
            parts.append("What the user asked for in this turn:\n" + asked[:800])
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ one turn

    def _preview_url(self, chat_id: str) -> str:
        """The chat's signed Preview link: grants that one project folder only."""
        v = self.valves
        if not (v.PREVIEW_URL and v.PREVIEW_SECRET):
            return ""
        sig = hmac.new(v.PREVIEW_SECRET.encode(), chat_id.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{v.PREVIEW_URL.rstrip('/')}/p/{chat_id}/{sig}/"

    def _friendly_url(self, chat_id: str, request) -> str:
        """The chat's app at the address the user asked for (2026-10-13): http://<this Open WebUI's host>/previews/
        <chat id>/. code-previews on the Open WebUI machine answers it and forwards the chat's owner to the signed
        link (see _preview_op); the page script opens it in the Preview panel."""
        tpl = self.valves.PREVIEWS_URL.strip()
        if not (tpl and self.valves.PREVIEW_URL and self.valves.PREVIEW_SECRET):
            return ""
        try:
            host = re.sub(r":\d+$", "", (request.headers.get("host") or "").strip())
        except Exception:  # noqa: BLE001
            host = ""
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or host in ("localhost", "127.0.0.1"):
            host = self.valves.PREVIEWS_HOST
        return f"{tpl.replace('{host}', host).rstrip('/')}/{chat_id}/"

    def _owns_chat(self, chat_id: str, user: dict) -> bool:
        try:
            from open_webui.models.chats import Chats
            return bool(Chats.get_chat_by_id_and_user_id(chat_id, (user or {}).get("id") or ""))
        except Exception:  # noqa: BLE001
            return False

    def _preview_op(self, body: dict, user: dict) -> dict:
        """code-previews (the /previews/<chat id>/ address) asks, with the visitor's own Open WebUI sign-in, where a
        chat's preview is. Only the chat's owner is told: the signed link stays the one key to a project."""
        args = body.get("code_preview")
        chat_id = str(args.get("chat") or "") if isinstance(args, dict) else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", chat_id) or not self._owns_chat(chat_id, user):
            return {"ok": False}
        url = self._preview_url(chat_id)
        return {"ok": bool(url), "url": url}

    async def _chat_json(self, chat_id: str, user: dict) -> dict:
        """The stored chat (Open WebUI's chat JSON) when it is this user's, else {}."""
        try:
            from open_webui.models.chats import Chats
            c = await awaited(Chats.get_chat_by_id_and_user_id(chat_id, (user or {}).get("id") or ""))
            return (getattr(c, "chat", None) or {}) if c else {}
        except Exception:  # noqa: BLE001
            return {}

    def _desktop_op(self, body: dict, user: dict) -> dict:
        """The Desktop and Upload buttons in the chat's pill (pipe 0.30.0, Feature #31).
        code_desktop = {"op": "links", "chat_id"} -> {"ok", "view": the chat's desktop viewer (the sandbox's /view,
        the link the live window already carries), "upload": the chat's upload address on code-live (PUT one file a
        request, ?path=<relative path>; signed with PREVIEW_SECRET over "upload:" + chat id, which a live link is not),
        "folder": where the files land in the chat's project ("uploads/", kept out of git)}. A link the valves cannot
        make is "". Admins only, own chats only. Always a dict: an exception reaches the page as null."""
        try:
            v = self.valves
            if (user or {}).get("role") != "admin":
                return {"ok": False, "error": "The desktop and uploads are for admins only."}
            a = body.get("code_desktop")
            a = a if isinstance(a, dict) else {}
            op, chat_id = a.get("op"), str(a.get("chat_id") or "")
            if op != "links":
                return {"ok": False, "error": f"Unknown desktop op: {op}"}
            if not self.GH_CHAT_RE.fullmatch(chat_id) or not self._owns_chat(chat_id, user):
                return {"ok": False, "error": "This chat can't be found."}
            out = {"ok": True, "view": "", "upload": "", "folder": "uploads/"}
            if v.VIEW_TOKEN and v.DESKTOP_URL:
                out["view"] = f"{v.DESKTOP_URL.rstrip('/')}/view?t={v.VIEW_TOKEN}&s={chat_id}"
            if v.PREVIEW_SECRET and v.LIVE_URL:
                sig = hmac.new(v.PREVIEW_SECRET.encode(), ("upload:" + chat_id).encode(), hashlib.sha256).hexdigest()[:32]
                out["upload"] = f"{v.LIVE_URL.rstrip('/')}/u/{chat_id}/{sig}/file"
            return out
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"The desktop links failed: {type(e).__name__}"}

    async def _fixer_op(self, body: dict, user: dict) -> dict:
        """The Fixer button in the chat's pill (pipe 0.25.0): the user asks the bug fixer to look into this chat.
        code_fixer = {"op": "status" | "report" | "active", "chat_id", "details"}.
        active (Watch, every few seconds): {"active": the running report's viewer link or "", "title", "since",
                 "source", "model"}, without asking the gateway which model a new report would use.
        status: {"active": the viewer link of the chat's unfinished report or "", "model": what a new one would run
                 on, "loaded": False = that run loads it (the model in the picker goes out), None = unknown}.
        report: files one (source "user") with the chat's messages and hands back {"view"}; while one is still
                running it hands back that one instead ("existing": True).
        Admins only, own chats only; the fixer's token stays here. Always a dict: an exception reaches the page as null."""
        try:
            v = self.valves
            if (user or {}).get("role") != "admin":
                return {"ok": False, "error": "The Fixer is for admins only."}
            if not (v.FIXER_URL and v.FIXER_TOKEN):
                return {"ok": False, "error": "The Fixer isn't set up (the Code pipe's FIXER_URL / FIXER_TOKEN valves)."}
            a = body.get("code_fixer")
            a = a if isinstance(a, dict) else {}
            op, chat_id = a.get("op"), str(a.get("chat_id") or "")
            if op not in ("status", "report", "active", "cancel", "escalate"):
                return {"ok": False, "error": f"Unknown Fixer op: {op}"}
            if not self.GH_CHAT_RE.fullmatch(chat_id) or not self._owns_chat(chat_id, user):
                return {"ok": False, "error": "This chat can't be found."}
            base = v.FIXER_URL.rstrip("/")
            headers = {"Authorization": f"Bearer {v.FIXER_TOKEN}"}
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as http:
                r = await http.get(f"{base}/api/active", params={"chat": chat_id}, headers=headers)
                active = base + r.json()["view"] if r.status_code == 200 else ""
                if op == "active":  # Watch asks every few seconds: the running report, for the stage (0.28.0)
                    if not active:
                        return {"ok": True, "active": ""}
                    rec = await self._fixer_record(http, str(r.json().get("id") or ""))
                    return {"ok": True, "active": active, "title": str(rec.get("title") or ""),
                            "since": rec.get("created"), "source": rec.get("source") or "",
                            "model": str(rec.get("fixer_model") or ""), "id": str(rec.get("id") or ""),
                            "phase": "claude" if rec.get("status") == "escalated" else "fixer"}
                if op in ("cancel", "escalate"):  # the Fixer view's Cancel / Ask Claude buttons (0.29.0)
                    incident = str(a.get("id") or "")
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", incident):
                        return {"ok": False, "error": "No such report."}
                    rec = await self._fixer_record(http, incident)
                    if rec.get("chat_id") != chat_id:
                        return {"ok": False, "error": "That report belongs to another chat."}
                    if op == "cancel":
                        hold = FIXER_HOLDS.get(chat_id)
                        if hold and hold.get("id") == incident and not hold.get("tried"):
                            FIXER_HOLDS.pop(chat_id, None)  # cancelled before the turn paused for it: no pause
                    r2 = await http.post(f"{base}/api/incidents/{incident}/{op}", headers=headers,
                                         json={"by": "user"})
                    body2 = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else {}
                    if r2.status_code >= 300:
                        return {"ok": False, "error": str(body2.get("error") or f"HTTP {r2.status_code}")}
                    return {"ok": True, "status": str(body2.get("status") or "")}
                if op == "status":
                    p = await http.get(f"{base}/api/pick", headers=headers)
                    pick = p.json() if p.status_code == 200 else {}  # an older fixer has no /api/pick: no model line
                    return {"ok": True, "active": active, "model": str(pick.get("model") or ""),
                            "loaded": pick.get("loaded")}
                if active:
                    return {"ok": True, "view": active, "existing": True}
                details = str(a.get("details") or "").strip()[:20000]
                first = details.splitlines()[0].strip() if details else ""
                title = (first[:117] + "...") if len(first) > 120 else (first or "Look into this chat")
                r = await http.post(f"{base}/api/report", headers=headers, json={
                    "chat_id": chat_id, "source": "user", "reporter_model": "you", "title": title,
                    "details": details, "context": fixer_context(await self._chat_json(chat_id, user))})
                if r.status_code != 201:
                    return {"ok": False, "error": f"The fixer refused it (HTTP {r.status_code}): {r.text[:200]}"}
                got = r.json()
                if v.FIXER_PAUSE and got.get("id"):  # a turn running in this chat pauses for it (see _run)
                    FIXER_HOLDS[chat_id] = {"id": str(got["id"]), "view": base + got["view"], "title": title,
                                            "t": time.time()}
                return {"ok": True, "view": base + got["view"], "id": str(got.get("id") or "")}
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"The fixer can't be reached ({e.__class__.__name__})."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}

    # ------------------------------------------------------------------ approval / question box (pipe 0.28.0)
    # The page script lists what waits for the user in their Code chats and answers it:
    #   code_ask = {"op": "list"}  -> {"ok", "asks": [{"id", "chat_id", "title", "kind", "data", "t", "direct"}]}
    #   code_ask = {"op": "answer", "id", "value", "sid"?}  -> {"ok"} (or {"ok": False, "error", "gone"?})
    # kind "permission": data {"action", "resources"}, value {"allow": bool, "message": str}.
    # kind "question": data {"title", "message", "options": [labels], "placeholder"} (one question, as the pipe asks
    # them), value the text typed, "" (= the first option) or False (skip); direct ones (no turn waits on them) carry
    # data {"fields": [{"key", "title", "message", "options"}]} and take value {"answers": {key: text}}.
    # Only the owner of the chat sees or answers; nothing here starts a turn.

    async def _chat_title(self, chat_id: str, user: dict) -> Optional[str]:
        """The chat's title when it is this user's chat, else None."""
        try:
            from open_webui.models.chats import Chats
            c = await awaited(Chats.get_chat_by_id_and_user_id(chat_id, (user or {}).get("id") or ""))
            return (getattr(c, "title", None) or "Chat") if c else None
        except Exception:  # noqa: BLE001
            return None

    async def _ask_op(self, body: dict, user: dict) -> dict:
        try:
            uid = str((user or {}).get("id") or "")
            a = body.get("code_ask")
            a = a if isinstance(a, dict) else {}
            op = a.get("op")
            if not uid:
                return {"ok": False, "error": "Not signed in."}
            if op == "list":
                waiting = [x for x in list(PAGE_ASKS.values()) if x["user"] == uid and not x["fut"].done()]
                asks = [{"id": x["id"], "chat_id": x["chat"], "kind": "permission" if x["type"] == "permission"
                         else "question", "data": x["data"], "t": int(x["t"]), "direct": False} for x in waiting]
                taken = {x["ref"] for x in PAGE_ASKS.values()}
                asks += [x for x in await self._direct_asks() if x["id"] not in taken]
                titles: dict = {}
                out = []
                for x in asks:
                    if x["chat_id"] not in titles:
                        titles[x["chat_id"]] = await self._chat_title(x["chat_id"], user)
                    if titles[x["chat_id"]] is None:  # someone else's chat
                        continue
                    out.append(dict(x, title=titles[x["chat_id"]]))
                return {"ok": True, "asks": sorted(out, key=lambda x: x["t"])}
            if op == "answer":
                aid = str(a.get("id") or "")
                x = PAGE_ASKS.get(aid)
                if x is not None:
                    if x["user"] != uid:
                        return {"ok": False, "error": "This isn't yours to answer."}
                    if x["fut"].done():
                        return {"ok": False, "error": "This was already answered.", "gone": True}
                    x["fut"].set_result(self._ask_value(x["type"], a.get("value")))
                    return {"ok": True}
                if re.fullmatch(r"(?:per|frm)_[A-Za-z0-9]{1,64}", aid):
                    return await self._direct_answer(user, aid, str(a.get("sid") or ""), a.get("value"))
                return {"ok": False, "error": "This was already answered.", "gone": True}
            return {"ok": False, "error": f"Unknown op: {op}"}
        except Exception as e:  # noqa: BLE001 - the page gets a dict, never null
            return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}

    @staticmethod
    def _ask_value(kind: str, value):
        """The page's answer, cut down to what the waiting dialog expects."""
        if kind == "permission":
            value = value if isinstance(value, dict) else {}
            return {"allow": value.get("allow") is True, "message": str(value.get("message") or "").strip()[:2000]}
        if value is False or value is None:
            return False
        if value is True:
            return True
        return str(value)[:4000]

    async def _direct_asks(self) -> list:
        """Requests opencode holds that no turn is waiting on, in Code chats (oldest first, the chat's owner is checked
        by the caller): a turn from an older pipe, Open WebUI restarted mid-turn, the pipe updated mid-turn. One look
        at opencode is reused for DIRECT_SCAN_S, since every open tab asks every few seconds."""
        if time.monotonic() - _direct_scan["t"] < DIRECT_SCAN_S:
            return _direct_scan["items"]
        items = []
        async with self._vm() as c:
            r = await c.get("/api/session/active")
            active = (r.json().get("data") or {}) if r.status_code == 200 else {}
            for sid in list(active)[:12]:
                s = await c.get(f"/api/session/{sid}")
                info = (s.json().get("data") or {}) if s.status_code == 200 else {}
                chat = str((info.get("metadata") or {}).get("owui_chat_id") or "")
                if not self.GH_CHAT_RE.fullmatch(chat) or chat.startswith("temp-"):
                    continue  # the fixer's, a review's, a script's own session
                p = await c.get(f"/api/session/{sid}/permission")
                for req in (p.json().get("data") or []) if p.status_code == 200 else []:
                    items.append({"id": req.get("id"), "sid": sid, "chat_id": chat, "kind": "permission",
                                  "data": {"action": req.get("action") or "action",
                                           "resources": [str(x) for x in (req.get("resources") or [])][:60]},
                                  "t": int(time.time()), "direct": True})
                f = await c.get(f"/api/session/{sid}/form")
                for form in (f.json().get("data") or []) if f.status_code == 200 else []:
                    fields = [{"key": g.get("key"), "title": g.get("title") or "",
                               "message": g.get("description") or g.get("title") or "?",
                               "options": [str(o.get("label", "")) for o in g.get("options") or []]}
                              for g in form.get("fields") or []]
                    items.append({"id": form.get("id"), "sid": sid, "chat_id": chat, "kind": "question",
                                  "data": {"title": "opencode has a question", "fields": fields},
                                  "t": int(time.time()), "direct": True})
        items = [x for x in items if isinstance(x["id"], str)]
        _direct_scan.update(t=time.monotonic(), items=items)
        return items

    async def _direct_answer(self, user: dict, ref: str, sid: str, value) -> dict:
        """Answer a request no turn waits on, straight to opencode, after checking the session is this user's chat."""
        if not re.fullmatch(r"ses_[A-Za-z0-9]{1,64}", sid):
            return {"ok": False, "error": "This was already answered.", "gone": True}
        async with self._vm() as c:
            s = await c.get(f"/api/session/{sid}")
            info = (s.json().get("data") or {}) if s.status_code == 200 else {}
            chat = str((info.get("metadata") or {}).get("owui_chat_id") or "")
            if not chat or await self._chat_title(chat, user) is None:
                return {"ok": False, "error": "This isn't yours to answer."}
            if ref.startswith("per_"):
                got = self._ask_value("permission", value)
                reply = {"decision": "once" if got["allow"] else "reject"}
                if not got["allow"] and got["message"]:
                    reply["message"] = got["message"]
                r = await c.post(f"/api/session/{sid}/permission/{ref}/reply", json=reply)
            else:
                f = await c.get(f"/api/session/{sid}/form/{ref}")
                form = (f.json().get("data") or {}) if f.status_code == 200 else {}
                answers = (value or {}).get("answers") if isinstance(value, dict) else None
                if value is False or not isinstance(answers, dict):
                    r = await c.delete(f"/api/session/{sid}/form/{ref}")
                else:
                    out = {}
                    for g in form.get("fields") or []:
                        got = match_option(g.get("options") or [], answers.get(g.get("key"), ""))
                        if got is None:
                            got = str(answers.get(g.get("key")) or "")
                        out[g["key"]] = (got.get("value") or got.get("label")) if isinstance(got, dict) else got
                    r = await c.post(f"/api/session/{sid}/form/{ref}/reply", json={"answer": out})
        _direct_scan["t"] = 0.0  # the next list looks again
        if r.status_code == 404 or r.status_code == 409:
            return {"ok": False, "error": "This was already answered.", "gone": True}
        return {"ok": r.status_code < 300} if r.status_code < 300 else \
            {"ok": False, "error": f"opencode refused it (HTTP {r.status_code})"}

    async def _fixer_record(self, http: httpx.AsyncClient, incident: str, wait: int = 0) -> dict:
        """A fixer report's record ({} when it can't be read); wait > 0 blocks up to that many seconds for it to end."""
        base, headers = self.valves.FIXER_URL.rstrip("/"), {"Authorization": f"Bearer {self.valves.FIXER_TOKEN}"}
        path = f"/api/incidents/{incident}" + (f"/wait?timeout={wait}" if wait else "")
        try:
            r = await http.get(base + path, headers=headers)
            return r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def _fixer_wait(self, incident: str, status, started: float) -> dict:
        """Wait for a fixer report to finish (Claude's part too, when the fixer handed it over), saying so on the
        status line. Its record ({} if it vanished; the last record seen after FIXER_WAIT_MAX_S)."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(150, connect=5)) as http:
            misses, rec = 0, {}
            while True:
                got = await self._fixer_record(http, incident, wait=110)
                rec = got or rec
                if got.get("status") == "done":
                    return got
                misses = misses + 1 if not got else 0
                if misses >= 6:  # the fixer is gone (VM down, report removed): stop waiting
                    return {}
                if time.time() - started > FIXER_WAIT_MAX_S:
                    return dict(rec, verdict=rec.get("verdict") or "could_not_fix",
                                summary="Stopped waiting after %d minutes." % (FIXER_WAIT_MAX_S // 60))
                mins = int((time.time() - started) // 60)
                who = "Claude is looking into it" if got.get("status") == "escalated" else "the Fixer is investigating"
                await status(f"Paused - {who} ({mins} min)" if mins else f"Paused - {who}")
                if not got:
                    await asyncio.sleep(10)

    async def _fixer_user_report(self, chat_id: str) -> Optional[dict]:
        """The chat's unfinished report filed with the Fixer button ({"id", "view", "title"}), else None."""
        v = self.valves
        if not (v.FIXER_PAUSE and v.FIXER_URL and v.FIXER_TOKEN):
            return None
        base = v.FIXER_URL.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as http:
                r = await http.get(f"{base}/api/active", params={"chat": chat_id},
                                   headers={"Authorization": f"Bearer {v.FIXER_TOKEN}"})
                if r.status_code != 200:
                    return None
                act = r.json()
                rec = await self._fixer_record(http, str(act.get("id") or ""))
        except (httpx.HTTPError, ValueError):
            return None
        if rec.get("source") != "user" or rec.get("status") == "done":
            return None  # the agent's own report-bug: it waits in that command itself
        return {"id": str(act["id"]), "view": base + str(act.get("view") or ""), "title": str(rec.get("title") or "")}

    async def _app_ports(self, preview: str, check: "TurnCheck") -> set:
        """The ports that are this chat's own app: the server the preview passes requests to right now (the preview
        server reports it), and the address the agent opened on the desktop."""
        ports = {url_port(check.url)}
        try:
            async with httpx.AsyncClient(timeout=4) as pc:
                r = await pc.get(preview + "__version")
                if r.status_code == 200:
                    ports.add(r.json().get("app"))
        except Exception:  # noqa: BLE001
            pass
        return {p for p in ports if isinstance(p, int)}

    def _live_url(self, chat_id: str, sid: str, user_msg: str) -> str:
        """The live window for this turn (code-live on VM 129), signed for this chat only. The fragment carries the
        chat's desktop viewer link (never sent to code-live) so the window can show the desktop when the agent uses it."""
        v = self.valves
        if not (v.LIVE_URL and v.PREVIEW_SECRET and sid and user_msg):
            return ""
        chat = re.sub(r"[^A-Za-z0-9_-]", "", chat_id)[:64]
        sig = hmac.new(v.PREVIEW_SECRET.encode(), ("live:" + chat).encode(), hashlib.sha256).hexdigest()[:32]
        url = f"{v.LIVE_URL.rstrip('/')}/w/{chat}/{sig}/?s={urllib.parse.quote(sid)}&m={urllib.parse.quote(user_msg)}"
        if v.VIEW_TOKEN and v.DESKTOP_URL:
            desk = f"{v.DESKTOP_URL.rstrip('/')}/view?t={v.VIEW_TOKEN}&s={chat}"
            url += "#d=" + urllib.parse.quote(desk, safe="")
        return url

    async def _refresh_known(self, c: httpx.AsyncClient) -> bool:
        """Ask opencode which models it serves. False when it has none to list: just after a restart it answers
        with an empty list until its providers have loaded, so an empty answer is asked again for ~10 s."""
        for attempt in range(6):
            if attempt:
                await asyncio.sleep(2)
            r = await c.get("/api/model")
            r.raise_for_status()
            listed = r.json().get("data") or []
            known = {(m.get("providerID"), m.get("id")) for m in listed}
            if known:
                self._known, self._known_at = known, time.monotonic()
                self._limits = {(m.get("providerID"), m.get("id")): (m.get("limit") or {}).get("context")
                                for m in listed}
                return True
        return False

    async def _picked_model(self, c: httpx.AsyncClient, picked: str) -> Optional[dict]:
        """opencode's model for an Open WebUI model id (via MODEL_PREFIXES), if opencode knows it. The list is kept
        for 5 minutes, but a model missing from it is looked up afresh before it is turned down: the kept list may
        be older than the model (2026-10-11: after an opencode restart the refresh came back empty, the list from
        before was kept, and a newly added model was replaced by Ornith, twice)."""
        for pair in self.valves.MODEL_PREFIXES.split(","):
            prefix, _, provider = pair.strip().partition("=")
            if prefix and provider and picked.startswith(prefix):
                ref = {"id": picked[len(prefix):], "providerID": provider.strip()}
                key = (ref["providerID"], ref["id"])
                if not self._known or time.monotonic() - self._known_at > 300 or key not in self._known:
                    for attempt in range(4):
                        if attempt:
                            await asyncio.sleep(2)
                        if not await self._refresh_known(c):
                            return ref  # opencode lists nothing: let it decide (it fails out loud on a model it lacks)
                        if any(prov == key[0] for prov, _ in self._known):
                            break  # this provider's models are in, so the answer is final (the list fills in
                            #        provider by provider in the first seconds after a restart)
                return ref if key in self._known else None
        return None

    async def _ask_switch(self, call, status, picked: str, other: str) -> bool:
        """Ask before a turn runs on a model other than the one in the model picker. Code mode never changes model
        on its own; no answer (no browser attached, the dialog dismissed or timed out) counts as no."""
        if not call:
            return False
        if picked:
            await status(f"Waiting for your answer: {picked} can't run Code mode")
            title, why = "The picked model can't run Code mode", f"{picked} isn't set up for the coding agent."
        else:
            await status("Waiting for your answer: no model was picked")
            title, why = "No model was picked", ("This turn reached Code mode without a model from the model picker "
                                                 "(a continue or regenerate of a reply saved as \"Coding agent\" "
                                                 "does that).")
        try:
            return bool(await asyncio.wait_for(call({"type": "confirmation", "data": {
                "title": title,
                "message": f"{why}\n\nRun this turn with {other} instead? "
                           f"The cluster then loads {other} in place of whatever model is loaded now."}}), 300))
        except Exception:
            return False

    async def _run(self, messages: list, chat_id: str, model: dict, emit, call,
                   picked: str = "", feature: Optional[dict] = None,
                   effort: Optional[str] = None, repo: str = "", friendly: str = "",
                   att: Optional[list] = None, user: Optional[dict] = None,
                   wake: bool = False) -> AsyncGenerator[str, None]:
        v = self.valves
        if feature and feature.get("error"):
            yield f"This feature chat can't reach code-features at {v.FEATURES_URL} ({feature['error']}). Is VM 129 up?"
            return
        if feature and feature.get("status") == "discarded":
            yield f"Feature #{feature['n']} was discarded. Start a new one with **✦ Feature**."
            return

        beat = {"t": time.monotonic(), "gap": float(v.HEARTBEAT_SECONDS or 0)}

        async def status(text: str, done: bool = False, heartbeat: bool = False):
            # A wait note (heartbeat) doubles the gap to the next one; anything else starts the count again.
            beat["t"] = time.monotonic()
            if heartbeat:
                beat["gap"] = min(beat["gap"] * 2, 480.0)
            else:
                beat["gap"] = float(v.HEARTBEAT_SECONDS or 0)
            if emit:
                await emit({"type": "status", "data": {"description": text, "done": done}})

        auth = ("opencode", v.OPENCODE_PASSWORD)
        async with httpx.AsyncClient(base_url=v.OPENCODE_URL.rstrip("/"), auth=auth,
                                     timeout=httpx.Timeout(30, connect=5, read=None)) as c:
            try:
                await status("Opening the coding session…")
                # Code mode never chooses a model itself: a turn runs on the model picked in the model picker, and
                # anything else (a model opencode doesn't serve, or no model with the turn) asks first.
                ref = await self._picked_model(c, picked) if picked else None
                if ref:
                    model = ref
                else:
                    if not await self._ask_switch(call, status, picked, model["id"]):
                        if picked:
                            yield (f"**Not started.** {picked} isn't set up for the coding agent (opencode on the "
                                   f"sandbox VM doesn't list it), and Code mode doesn't change model without your "
                                   f"say-so. Pick another model in the model picker and send this again.\n")
                            await status("Not started: the picked model can't run Code mode", done=True)
                        else:
                            yield ("**Not started.** No model from the model picker came with this turn, and Code "
                                   "mode doesn't choose one for you. Pick a model in the model picker and send this "
                                   "again.\n")
                            await status("Not started: no model was picked", done=True)
                        return
                    yield (f"*{picked} isn't available to the coding agent; you chose {model['id']} for this turn.*\n\n"
                           if picked else f"*No model was picked; you chose {model['id']} for this turn.*\n\n")
                if effort is not None:  # the page's thinking pill: an opencode variant, or "on" for the default
                    model = {"id": model["id"], "providerID": model["providerID"]}
                    if effort in EFFORT_VARIANTS:
                        model["variant"] = effort
                sid, info = await self._session(c, chat_id, model, feature, pinned=effort is not None)
                # The reviewer thinks at the builder's level: the pill's variant, or the one the session was left at
                # (it used to get opencode's default whatever the user picked).
                variant = model.get("variant") if effort is not None else ((info or {}).get("model") or {}).get("variant")
                review_model = {"id": model["id"], "providerID": model["providerID"]}
                if variant and variant != "default":
                    review_model["variant"] = variant
            except httpx.HTTPError as e:
                yield f"Code mode can't reach opencode at {v.OPENCODE_URL} ({e.__class__.__name__}). Is VM 129 (AI-Sandbox) running?"
                await status("opencode unreachable", done=True)
                return
            await self._reap_servers(c, chat_id, kill=not wake)
            note = ""
            if repo and not feature:
                # The page sends the chat's repo with every Code turn. In a chat that already existed the panel
                # cloned it when the user picked it (op attach); in a new chat this turn does (or a later one, if the
                # first could not: not signed in, GitHub down). After that it is one quick look at the folder.
                await status(f"Opening {repo[:100]}…")
                got = await self._gh_attach(c, chat_id, repo)
                if not got.get("ok"):
                    yield (f"**Not started.** This chat could not be set up to work on the repository. {got.get('error')}\n\n"
                           f"Open the GitHub panel to pick again, or go on in a new chat.\n")
                    await status("Not started: the repository could not be cloned", done=True)
                    return
                new, told = await self._gh_take_notes(c, chat_id)
                if new:
                    yield f"Working on **{got['repo']}** (branch {got['branch']}).\n\n"
                    note = (f"<project_note>The project folder is a fresh clone of the GitHub repository {got['repo']} "
                            f"(branch {got['branch']}). Look around before changing things.</project_note>\n\n")
                for line in told:  # the user pulled or rolled back from the panel: the files changed under the agent
                    note += f"<project_note>{line}</project_note>\n\n"
            meta = dict(info.get("metadata") or {})
            seen = int(meta.get("owui_seen") or 0)
            design = {"ui": bool(UI_RE.search(split_rag(text_of(messages[-1]))[0] if messages else "")), "choice": None}
            ask_done = False  # the ask turn's one round of questions has been answered
            asking = v.ASK_FIRST and ask_first(split_rag(text_of(messages[-1]))[0] if messages else "", seen, feature)
            if asking:  # no `ask` agent installed = no ask turn
                asking = (await c.post(f"/api/session/{sid}/agent", json={"agent": ASK_AGENT})).status_code < 300
            elif info.get("agent") == ASK_AGENT:  # an ask turn that was stopped left the session on it
                await c.post(f"/api/session/{sid}/agent", json={"agent": BUILD_AGENT})
            prompt = note + self._prompt(messages, seen)
            if not wake and not asking:  # the user is back: wake-ups held by an earlier Stop may be delivered again
                await self._hold_wakes(chat_id, False)
            woke, woke_ids = await self._take_wakes(chat_id)  # finished while no turn ran: handed over now
            if woke:
                prompt += "\n\n<background_results>\n" + woke + "\n</background_results>"
            att_json, att_notes, given = await self._attach_handover(c, messages, att or [], meta, model, user)
            for line in att_notes:  # what the message brought along, said plainly: first-time, too big, unreadable, blind model
                prompt += "\n\n" + line
            for key, fid, url in given:  # the <attached_files> citation names Open WebUI's file storage, which is
                for target in (fid, url):  # nowhere on the coding machine: point it at the attachment the agent really has
                    if target:
                        prompt = prompt.replace(f'url="{target}"', 'url="attached with this message"')
            if given:
                meta["owui_files"] = ((meta.get("owui_files") or []) + [k for k, _, _ in given])[-400:]
            preview = "" if feature else self._preview_url(chat_id)
            if preview:  # the Chat | Code script turns this into the Preview panel
                yield f"[Preview]({preview})\n\n"
            if feature:  # the Chat | Code script turns this into a chip that opens the Features panel
                yield f"[Feature #{feature['n']}]({self._feature_link(feature['n'])})\n\n"
            marked_ready = False
            form_answered = False  # the user answered a question dialog this turn
            check = TurnCheck(str(meta.get("owui_app_url") or ""))
            auto = 0             # automatic check rounds sent back to the agent this turn
            asked = split_rag(text_of(messages[-1]))[0] if messages else ""
            # The end-of-turn review: the project as it is now is what "before this turn" means to the reviewer.
            reviewing = bool(v.AUTO_REVIEW > 0 and not feature)
            snapshot = False
            if reviewing:
                try:
                    _, got = await self._vm_sh(c, SNAPSHOT_CMD, timeout=30,
                                               cwd=f"{v.PROJECTS_ROOT.rstrip('/')}/chats/{chat_id}")
                    snapshot = "SNAPSHOT_OK" in got
                except Exception:  # noqa: BLE001 - a missing snapshot only narrows the review
                    pass
            review = None        # the latest review round's result
            review_rounds = review_sent = 0
            review_due = False   # the agent was sent back with findings: its fixes get reviewed too
            answers: list = []   # question dialogs answered in this chat, for the reviewer
            for m in messages[:-1]:
                if m.get("role") == "assistant":
                    answers += QA_LINE_RE.findall(text_of(m))

            tools: dict[str, dict] = {}
            desktop_shown = False
            compact = v.COMPACT  # tool calls and thinking fold into the status timeline and one Steps block
            reasoning, reasoning_t0, held = None, 0.0, []
            last_progress, long_think_noted = 0.0, False
            segment: list = []   # compact: the text being written now
            segment_used = False # compact: that text was already shown as a step's commentary
            last_text = ""       # compact: the latest finished text - the answer, if no step follows it
            steps: list = []     # compact: one markdown line per step, shown folded at the end
            t_start = time.monotonic()
            timing = TurnTiming()
            if woke:
                timing.wake_delivered()
            wake_rounds = 0      # wake-ups handed to the agent at the end of a run, this turn
            wake_edit_mark = None  # check.edited when a wake-up was handed over after the review
            running: dict = {}   # open tool calls: id -> their status line, for the wait notes
            waiting = "prompt"   # what a quiet stream is waiting on: prompt (reading it), compacted, model
            over_steps = 0       # steps in a row that ended past the backstop size without opencode compacting
            context_armed = True
            ctx_limit = self._limits.get((model.get("providerID"), model.get("id"))) or 0
            ctx_at = min(v.CONTEXT_COMPACT_TOKENS or ctx_limit, max(ctx_limit - 32768, ctx_limit // 2)) if ctx_limit else 0
            ctx_last: dict = {}  # the latest numbers, saved with the reply as one hidden status

            async def ctx_live(**data):
                # Live only (Open WebUI saves no custom event type): the page's context ring follows the turn.
                if emit and ctx_limit:
                    with contextlib.suppress(Exception):
                        await emit({"type": "slerm:context", "data": dict(data, limit=ctx_limit, compact_at=ctx_at,
                                                                          source="code")})
            out_started = False
            finished = None
            if not feature:  # this chat's next message: its earlier review rounds are no longer worth replaying
                await self._prune_reviews(c, chat_id)
            uid = str((user or {}).get("id") or "")
            turn = object()  # this turn's token in CHAT_TURNS
            pausing = None  # the Fixer-button report this turn's run was interrupted for (0.28.0)
            if v.FIXER_PAUSE and not chat_id.startswith("temp-"):
                # The user filed a report with the Fixer button and it still runs: this turn waits for it, the way a
                # turn waits in the agent's own `report-bug` (the one model slot is the fixer's meanwhile).
                FIXER_HOLDS.pop(chat_id, None)  # a hold left by a turn that has ended: this look replaces it
                fx = await self._fixer_user_report(chat_id)
                if fx:
                    yield (f"> **Waiting for the Fixer** to finish looking into: {fx['title'] or 'this chat'}\n\n"
                           f"[Fixer]({fx['view']})\n\n")
                    rec = await self._fixer_wait(fx["id"], status, time.time())
                    yield f"> **Fixer:** {rec.get('verdict') or 'no verdict'}\n\n"
                    prompt += "\n\n" + fixer_resume_prompt(fx, rec, paused=False)
            try:
                async with Events(c, sid) as ev:
                    try:
                        r = await c.post(f"/api/session/{sid}/prompt",
                                         json={"text": prompt, **({"files": att_json} if att_json else {})})
                        r.raise_for_status()
                    except httpx.HTTPError:
                        await self._unclaim_wakes(woke_ids)  # never reached the agent: ready again for code-wake
                        raise
                    user_msg = r.json()["data"]["id"]
                    CHAT_TURNS[chat_id] = turn  # an older turn still following this session stops driving it
                    await status(f"Working with {model['id']} · reading context…")
                    live = "" if asking else self._live_url(chat_id, sid, user_msg)  # an ask turn: once it builds
                    if live and emit:  # the live window: Open WebUI shows embeds between the status line and the reply
                        await emit({"type": "embeds", "data": {"embeds": [live]}})
                        desktop_shown = True  # the window shows the desktop itself

                    async def follow(resp):
                        # The live window follows ONE prompt and folds into its bar when that prompt's run ends. The
                        # automatic check and the review send-back are new prompts in this same reply, so each gets a
                        # window of its own (the browser swaps it in for the folded one). user_msg stays the user's
                        # message: the turn's diff and review count from it. (2026-10-17: the user watched a bar stuck
                        # at 24m 15s while the agent kept working after an automatic check.)
                        url = "" if asking else self._live_url(chat_id, sid, resp.json()["data"]["id"])
                        if url and emit:
                            await emit({"type": "embeds", "data": {"embeds": [url]}})
                    async def prompt_more(text: str, wake_ids: Optional[list] = None):
                        # A new prompt in this same reply (automatic check, review send-back, wake-up): its live
                        # window, and a quiet stream until the model answers is it reading that prompt.
                        nonlocal waiting
                        try:
                            r2 = await c.post(f"/api/session/{sid}/prompt", json={"text": text})
                            r2.raise_for_status()
                        except httpx.HTTPError:
                            await self._unclaim_wakes(wake_ids)
                            raise
                        waiting = "prompt"
                        await follow(r2)

                    while finished is None:
                        if CHAT_TURNS.get(chat_id) is not turn:
                            # A later message in this chat reached the session: its turn follows the run from here
                            # and checks and reviews it. Nothing more is sent or started from this reply.
                            finished = "taken over"
                            yield ("\n\n" if out_started else "") + (
                                "*Your next message reached the agent while this was running. The work carries on in "
                                "that message's reply, which checks and reviews it.*\n")
                            out_started = False
                            break
                        hold = FIXER_HOLDS.get(chat_id) if v.FIXER_PAUSE and pausing is None else None
                        if hold and not asking and not running and time.time() - hold.get("t", 0) < FIXER_HOLD_MAX_S \
                                and time.monotonic() - hold.get("tried", -60.0) > 5:
                            # The user filed a report with the Fixer button while this run works: stop it here, between
                            # steps (no command in flight), and carry on once the fixer is done (the interrupted
                            # event below). Not running any more = it just ended: the hold waits for its next run.
                            hold["tried"] = time.monotonic()
                            try:
                                ri = await c.post(f"/api/session/{sid}/interrupt")
                                hit = ri.status_code < 300 and (ri.json() if ri.content else {}).get("interrupted", True)
                            except (httpx.HTTPError, ValueError, AttributeError):
                                hit = False
                            if hit:
                                pausing = FIXER_HOLDS.pop(chat_id, None) or hold
                                await status("Pausing for the Fixer…")
                        try:
                            left = beat["gap"] - (time.monotonic() - beat["t"]) if beat["gap"] > 0 else None
                            e = await ev.get(timeout=max(0.05, left) if left is not None else None)
                        except asyncio.TimeoutError:
                            # Nothing said for a while (a long tool, a long think, a cold prompt, a compaction): say
                            # what it waits on and for how long, so a quiet turn never looks stuck or "thinking".
                            await status(self._wait_note(timing, running, waiting), heartbeat=True)
                            continue
                        if CHAT_TURNS.get(chat_id) is not turn:
                            continue  # taken over while it waited: the newer turn handles this event
                        timing.event()
                        t, d = e.get("type", ""), e.get("data") or {}
                        if t != "session.tool.progress":
                            waiting = "model"
                        if asking and t in ("session.execution.succeeded", "session.execution.interrupted") and \
                                (form_answered or t.endswith("succeeded") and is_ready(last_text or "".join(segment))):
                            # The ask turn is over with its answers (or nothing was open): the normal agent builds.
                            asking = False
                            await c.post(f"/api/session/{sid}/agent", json={"agent": BUILD_AGENT})
                            said = (last_text or "".join(segment)).strip()
                            if compact and said:
                                steps.append(f"- *{md_line(said)}*")
                            last_text, segment = "", []
                            await status("Questions answered - starting" if form_answered else "Nothing to ask - building")
                            r = await c.post(f"/api/session/{sid}/prompt", json={"text": build_now(design["choice"])})
                            r.raise_for_status()
                            waiting = "prompt"
                            # What changed, and what the live window follows, is counted from here: opencode has
                            # no diff from the ask turn's message (nothing could change in it).
                            user_msg = r.json()["data"]["id"]
                            live = self._live_url(chat_id, sid, user_msg)
                            if live and emit:
                                await emit({"type": "embeds", "data": {"embeds": [live]}})
                                desktop_shown = True
                            continue
                        if t == "session.text.delta":
                            if compact:
                                segment.append(d.get("delta", ""))
                            elif reasoning is not None:
                                held.append(d.get("delta", ""))
                            else:
                                out_started = True
                                yield d.get("delta", "")
                        elif t == "session.text.ended":
                            if compact:
                                text = (d.get("text") or "".join(segment)).strip()
                                segment = []
                                if segment_used:  # already shown as the commentary of the step it introduced
                                    segment_used = False
                                elif text:
                                    last_text = text
                            elif reasoning is None:
                                yield "\n\n"
                        elif t == "session.reasoning.started":
                            timing.reasoning_started()
                            reasoning, reasoning_t0, long_think_noted = [], time.monotonic(), False
                            if not compact:
                                await status("Thinking…")
                        elif t == "session.reasoning.delta" and reasoning is not None:
                            reasoning.append(d.get("delta", ""))
                            if compact:
                                if not long_think_noted and time.monotonic() - reasoning_t0 > 20:
                                    long_think_noted = True  # once per long think, so the timeline stays readable
                                    await status("Thinking it through…")
                            elif time.monotonic() - last_progress > 3:  # long thinks would otherwise look stuck
                                last_progress = time.monotonic()
                                words = len("".join(reasoning).split())
                                await status(f"Thinking… {words} words, {round(last_progress - reasoning_t0)}s")
                        elif t == "session.reasoning.ended":
                            timing.reasoning_ended()
                            text = d.get("text") or "".join(reasoning or [])
                            if not compact and v.SHOW_REASONING and text.strip():
                                yield reasoning_block(text, round(time.monotonic() - reasoning_t0))
                            reasoning = None
                            if held:
                                out_started = True
                                yield "".join(held)
                                held = []
                        elif t == "session.tool.input.started":  # the only event that names the tool
                            tools[d.get("id", "")] = {"name": d.get("name") or "tool", "input": None}
                        elif t == "session.tool.called":
                            tool = tools.setdefault(d.get("id", ""), {"name": d.get("name") or "tool", "input": None})
                            tool["input"] = d.get("input")
                            timing.tool_started(d.get("id", ""), tool_phase(tool["name"], tool["input"]))
                            # What it said before this step is commentary, not the answer. opencode often ends that
                            # text only after announcing the call, so the still-open segment counts too.
                            said = "\n".join(x for x in (last_text, "".join(segment).strip()) if x)
                            if compact and said:
                                await status(said.splitlines()[0][:110])
                                steps.append(f"- *{md_line(said)}*")
                                last_text = ""
                                if segment:
                                    segment, segment_used = [], True
                            running[d.get("id", "")] = (time.monotonic(), tool_status(tool["name"], tool["input"]))
                            await status(running[d.get("id", "")][1])
                            command = (tool["input"] or {}).get("command", "") if isinstance(tool["input"], dict) else ""
                            if feature and FEATURE_READY_RE.search(command or ""):
                                marked_ready = True
                            if "report-bug" in (command or "") and "--wait" not in command and v.FIXER_TOKEN and emit:
                                asyncio.create_task(self._announce_fixer(chat_id, emit, status))
                            if not desktop_shown and v.VIEW_TOKEN and emit and DESKTOP_RE.search(command or ""):
                                # The `desktop` command drives the desktop named after this chat; show it live.
                                await emit({"type": "embeds", "data": {"embeds": [
                                    viewer_embed(v.DESKTOP_URL, v.VIEW_TOKEN, re.sub(r"[^A-Za-z0-9_-]", "", chat_id)[:64])]}})
                                desktop_shown = True
                        elif t in ("session.tool.success", "session.tool.failed"):
                            timing.tool_ended(d.get("id", ""))
                            running.pop(d.get("id", ""), None)
                            info_t = tools.get(d.get("id", ""), {"name": "tool", "input": None})
                            failed = t.endswith("failed")
                            if not failed:
                                result = "\n".join(p.get("text", "") for p in d.get("content") or [] if isinstance(p, dict))
                            else:
                                result = f"Error: {(d.get('error') or {}).get('message', 'failed')}"
                            check.tool(info_t["name"], info_t["input"], result, failed)
                            timing.tool_done(info_t["name"], info_t["input"], failed)
                            if compact:
                                steps.append(step_line(info_t["name"], info_t["input"], result, failed))
                                continue
                            if len(result) > v.TOOL_RESULT_CHARS:
                                result = result[:v.TOOL_RESULT_CHARS] + "\n... (cut)"
                            yield ("\n" if out_started else "") + tool_block(d.get("id", ""), info_t["name"], info_t["input"], result)
                            out_started = False
                        elif t == "permission.asked":
                            timing.permission_started()
                            try:
                                yield await self._permission(c, sid, d, call, status, chat_id, user)
                            finally:
                                timing.permission_ended()
                        elif t == "form.created":
                            if ask_done:  # one round is enough: anything more it asks is dropped unseen
                                await c.delete(f"/api/session/{sid}/form/{(d.get('form') or {}).get('id')}")
                                continue
                            asker = PageAsk(c, chat_id, uid, sid, str((d.get("form") or {}).get("id") or "")) \
                                if v.PAGE_ASKS else call
                            text, ok = await self._form(c, sid, d.get("form") or {}, asker, status, design if asking else None)
                            form_answered = form_answered or ok
                            answers += QA_LINE_RE.findall(text)
                            yield text
                            if asking and ok:  # answered: stop the ask agent here, the build starts on its stop event
                                ask_done = True
                                await c.post(f"/api/session/{sid}/interrupt")
                        elif t == "session.step.ended":
                            tok = d.get("tokens") or {}
                            timing.step(tok)
                            cache = tok.get("cache") or {}
                            # The prompt this step read. opencode counts the uncached input apart from the cache
                            # (82 in + 98,215 read on 10-20), so the two add up; the old max() never reached 98,304.
                            used = tok.get("input", 0) + cache.get("read", 0) + cache.get("write", 0)
                            # The session's size after this step: what the model read plus what it wrote.
                            ctx_used = tok.get("input", 0) + cache.get("read", 0) + cache.get("write", 0) + tok.get("output", 0)
                            if ctx_used:
                                ctx_last = {"used": ctx_used}
                                await ctx_live(phase="step", used=ctx_used)
                            # the model's own line (its context - 32768, the valve at most): raising the valve for
                            # a 256k model must not take the backstop away from the 128k ones (0.27.1)
                            threshold = ctx_at or v.CONTEXT_COMPACT_TOKENS
                            if threshold and used < threshold * 0.75:
                                context_armed, over_steps = True, 0
                            if threshold and used >= threshold and context_armed and not asking:
                                over_steps += 1
                            # opencode compacts by itself right after a step that reaches context - reserved (98,304
                            # here: all 98 compactions on record were its own). Two steps in a row past the line with
                            # no compaction between them mean its trigger missed: only then is one asked for, so the
                            # two can never queue a second compaction behind the first.
                            if over_steps >= 2:
                                context_armed, over_steps = False, 0
                                try:
                                    r = await c.post(f"/api/session/{sid}/compact", json={"delivery": "steer"})
                                    r.raise_for_status()
                                    await status("Keeping context manageable: requested a checkpoint before the next step…")
                                except httpx.HTTPError:
                                    await status("Early context checkpoint was unavailable; opencode's own compaction remains active.")
                            if not compact:
                                await status(f"Step done ({tok.get('input', 0)} in / {tok.get('output', 0)} out tokens)")
                        elif t == "session.compaction.started":
                            timing.compaction_started()
                            over_steps = 0
                            await ctx_live(phase="compacting", used=ctx_last.get("used"))
                            await status("Compacting the session…")
                        elif t == "session.compaction.ended":
                            timing.compaction_ended()
                            waiting = "compacted"   # the next step reads the whole new context cold
                            # The new size shows with the next step; until then the ring shows it was just emptied.
                            await ctx_live(phase="compacted", before=ctx_last.get("used"), used=None)
                            ctx_last = {"used": None, "compacted_from": ctx_last.get("used")}
                            await status("Session compacted")
                        elif t == "session.compaction.failed":
                            timing.compaction_ended()
                            await ctx_live(phase="compact_failed", used=ctx_last.get("used"))
                            await status("Compaction failed; opencode will try again")
                        elif t == "session.step.failed":
                            err = d.get("error") or {}
                            if err.get("type") != "aborted":  # aborted = a refusal or Stop, reported below
                                yield f"\n\n**Step failed:** {err.get('message') or 'unknown error'}\n"
                        elif t == "session.execution.succeeded":
                            if asking:  # it asked in plain text instead: the user's next message answers, on the normal agent
                                asking = False
                                await c.post(f"/api/session/{sid}/agent", json={"agent": BUILD_AGENT})
                            if wake_rounds < v.WAKES_PER_TURN:
                                # A job or timer it set finished while it worked (job 4684, 10-23: a 19-second test's
                                # result waited 51 minutes behind this very turn and its review). It is handed over
                                # here, at the end of a run, never in the middle of a tool call.
                                woke_now, now_ids = await self._take_wakes(chat_id)
                                if woke_now:
                                    wake_rounds += 1
                                    timing.wake_delivered()
                                    said = "\n".join(x for x in (last_text, "".join(segment).strip()) if x)
                                    if compact and said:
                                        steps.append(f"- *{md_line(said)}*")
                                        last_text, segment = "", []
                                    steps.append("- **Wake-up:** a job or timer it set finished; handed to it in this turn")
                                    await status("A background job finished - handing the result to the agent")
                                    await prompt_more(woke_now, now_ids)
                                    continue
                            why = "" if feature else check.pending()
                            if why and auto < v.AUTO_VERIFY:
                                # Not finished: the turn changed a web app and verify hasn't passed since. Send the
                                # agent back with what's missing; its premature summary folds into the steps.
                                auto += 1
                                timing.auto_checked()
                                said = "\n".join(x for x in (last_text, "".join(segment).strip()) if x)
                                if compact and said:
                                    steps.append(f"- *{md_line(said)}*")
                                    last_text, segment = "", []
                                steps.append(f"- **Automatic check {auto}:** {why}; sent back to verify and fix")
                                await status(f"Automatic check: {why} - sending it back ({auto}/{v.AUTO_VERIFY})")
                                await prompt_more(self._check_prompt(why, check, asked))
                                continue
                            if wake_edit_mark is not None and check.edited > wake_edit_mark:
                                review_due, wake_edit_mark = True, None  # it changed files after a post-review wake-up
                            if reviewing and (review_due or review_rounds == 0 and check.edited):
                                # The turn would end here. A separate session checks it against what the user asked
                                # for; findings go back to the agent once, and its fixes are reviewed again.
                                names, lines = [], 0
                                try:
                                    r = await c.get(f"/api/session/{sid}/diff", params={"from": user_msg})
                                    files = (r.json().get("data") or []) if r.status_code == 200 else []
                                    names = [str(f.get("file", "")).replace("\\", "/") for f in files]
                                    lines = sum(int(f.get("additions") or 0) + int(f.get("deletions") or 0) for f in files)
                                except (httpx.HTTPError, ValueError, TypeError):
                                    pass
                                if names and not review_due and (
                                        all(n.startswith(("ideas/", "./ideas/")) for n in names) or
                                        lines < v.REVIEW_MIN_LINES and not any(RISK_FILE_RE.search(n) for n in names)):
                                    # An ideas board is a proposal (the build turn gets reviewed), and a small tweak
                                    # is not worth the minutes a review takes.
                                    reviewing = False
                                else:
                                    said = "\n".join(x for x in (last_text, "".join(segment).strip()) if x)
                                    requests = [split_rag(text_of(m))[0][:1500] for m in messages if m.get("role") == "user"]
                                    review_rounds += 1
                                    review_due = False
                                    await status("Reviewing the work against what you asked for…")
                                    before = ""
                                    if snapshot:
                                        try:
                                            _, before = await self._vm_sh(
                                                c, BEFORE_CMD.format(ref=REVIEW_REF), timeout=30,
                                                cwd=f"{v.PROJECTS_ROOT.rstrip('/')}/chats/{chat_id}")
                                        except Exception:  # noqa: BLE001 - the reviewer then makes the list itself
                                            pass
                                    record = ""
                                    if check.edited and check.web:
                                        record = (f"`desktop verify` did NOT pass after the last change ({why})." if why
                                                  else "`desktop verify` passed for the checked page after a post-edit open/load.")
                                    record += "\nRecorded page checks (not a clean-install or backend-workflow test):\n" + (
                                        "\n".join(check.records) or "No page check recorded this turn.")
                                    review = await self._review(c, chat_id, review_model, status, review_prompt(
                                        [x for x in requests if x][-6:], answers[-12:], said, names, snapshot,
                                        review["findings"] if review and review.get("findings") else [], before,
                                        record), review_rounds, emit, lambda: CHAT_TURNS.get(chat_id) is not turn)
                                    if CHAT_TURNS.get(chat_id) is not turn:
                                        continue  # a later message came during the review: that turn reviews anew
                                    guard = await self._script_checks(c, chat_id, names)
                                    if guard:
                                        review["findings"] = list(dict.fromkeys(guard + review["findings"]))
                                        review["passed"] = False
                                    await self._review_log(c, chat_id, {
                                        "t": int(time.time()), "model": model["id"], "round": review_rounds,
                                        "passed": review["passed"], "findings": review["findings"],
                                        "seconds": review["seconds"], "error": review["error"],
                                        "request": (requests[-1] if requests else "")[:600], "files": names[:40]})
                                    found = review["findings"] if review["passed"] is False else []
                                    review["noted"] = [f for f in found if harmless_leftover(f)]
                                    if review["noted"]:  # scratch outside the deliverable: shown, never sent back
                                        found = [f for f in found if f not in review["noted"]]
                                        review["findings"] = found
                                        review["passed"] = not found
                                    timing.reviewed(review["seconds"], bool(found) and review_sent < v.AUTO_REVIEW)
                                    if found and review_sent < v.AUTO_REVIEW:
                                        review_sent += 1
                                        review_due = True
                                        if compact and said:
                                            steps.append(f"- *{md_line(said)}*")
                                            last_text, segment = "", []
                                        steps.append(f"- **Review {review_rounds}:** {len(found)} "
                                                     f"thing{'s' if len(found) != 1 else ''} to fix; sent back")
                                        steps.extend(f"- *↳ {md_line(f, 300)}*" for f in found)
                                        await status(f"Review: {len(found)} thing{'s' if len(found) != 1 else ''} to fix "
                                                     f"- sending it back ({review_sent}/{v.AUTO_REVIEW})")
                                        auto = min(auto, max(v.AUTO_VERIFY - 1, 0))  # the fixes get a verify round too
                                        await prompt_more(review_fix_prompt(found))
                                        continue
                                    steps.append(f"- **Review {review_rounds}:** " + (
                                        "nothing open" if review["passed"] else
                                        f"{len(found)} still open" if found else "no verdict"))
                            if wake_rounds < v.WAKES_PER_TURN:
                                # One that finished during the review (or since the check above): still this turn's.
                                # The review is done, so whatever it changes next gets a focused recheck.
                                woke_now, now_ids = await self._take_wakes(chat_id)
                                if woke_now:
                                    wake_rounds += 1
                                    timing.wake_delivered()
                                    steps.append("- **Wake-up:** a job or timer finished during the review; handed to it")
                                    await status("A background job finished - handing the result to the agent")
                                    wake_edit_mark = check.edited  # edits after this get a focused recheck
                                    await prompt_more(woke_now, now_ids)
                                    continue
                            finished = "ok"
                        elif t == "session.execution.interrupted" and pausing is not None:
                            fx, pausing = pausing, None
                            said = "\n".join(x for x in (last_text, "".join(segment).strip()) if x)
                            if compact and said:
                                steps.append(f"- *{md_line(said)}*")
                                last_text, segment = "", []
                            yield (("\n" if out_started else "") + "\n> **Paused for the Fixer:** "
                                   f"{fx.get('title') or 'you asked it to look into this chat'}\n\n[Fixer]({fx['view']})\n\n")
                            out_started = False
                            rec = await self._fixer_wait(fx["id"], status, fx.get("t") or time.time())
                            verdict = rec.get("verdict") or "no verdict"
                            steps.append(f"- **Fixer:** paused for the report you filed; {verdict}")
                            yield (f"> **Fixer:** {verdict}" + (f" — {md_line(str(rec['summary']), 300)}" if rec.get("summary")
                                                                else "") + "\n\n")
                            await status("Fixer done - the agent carries on")
                            await prompt_more(fixer_resume_prompt(fx, rec))
                            continue
                        elif t == "session.execution.interrupted":
                            finished = "stopped"
                            await self._hold_wakes(chat_id, True)  # stopped: nothing it set may restart it on its own
                            yield "\n\n*opencode stopped. Tell it what to do next.*\n"
                        elif t in ("session.execution.failed", "stream.error"):
                            err = d.get("error")
                            msg = err.get("message") if isinstance(err, dict) else (err or d.get("message"))
                            yield f"\n\n**opencode failed:** {msg or t}\n"
                            finished = "failed"
            except asyncio.CancelledError:  # the user pressed Stop
                try:
                    await asyncio.wait_for(c.post(f"/api/session/{sid}/interrupt"), 5)
                except Exception:
                    pass
                # A stopped turn keeps its timing (marked stopped, open phases flagged), and what it set with `later`
                # waits for the user's next message instead of restarting the task by itself.
                timing.stopped = True
                for job in (self._hold_wakes(chat_id, True),
                            emit({"type": "status", "data": {"description": "", "done": True, "hidden": True,
                                                             "action": "slerm_timing", "source": "code",
                                                             "timing": timing.completed()}}) if emit else None):
                    if job is not None:
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await asyncio.wait_for(asyncio.shield(job), 5)
                raise
            except httpx.HTTPError as e:
                yield f"\n\n**Lost contact with opencode:** {e.__class__.__name__}: {e}\n"
                finished = "failed"

            if held:
                yield "".join(held)
            elapsed = time.monotonic() - t_start
            unverified = check.pending() if finished == "ok" and not feature else ""
            if compact:
                answer = "\n\n".join(x for x in (last_text, "".join(segment).strip()) if x)
                if answer and friendly and preview and LOCAL_URL_RE.search(answer):
                    answer = user_links(answer, friendly, await self._app_ports(preview, check))
                if unverified:
                    yield unverified_note(unverified, check.problems, auto)
                if review and finished == "ok":
                    yield review_note(review, review_sent)
                if answer:
                    if unverified or review and review.get("passed") is not True:
                        yield "<details>\n<summary>Builder's summary — see check results above</summary>\n\n" + answer + "\n\n</details>\n\n"
                    else:
                        yield answer + "\n\n"
                if steps:
                    yield steps_block(steps, elapsed)
            else:
                if unverified:
                    yield "\n\n" + unverified_note(unverified, check.problems, auto)
                if review and finished == "ok":
                    yield "\n\n" + review_note(review, review_sent)
            if feature:  # the whole feature so far (its folders are separate repos, so opencode can't diff them)
                yield diff_block(await self._feature_diff(feature["n"]), v.DIFF_CHARS).replace(
                    "<summary>Changed", "<summary>Feature so far: changed", 1)
                now = await self._feature_of(chat_id, create=False) if marked_ready else None
                if now and now.get("status") == "ready":
                    yield f"\n[Ready to deploy]({self._feature_link(feature['n'], ready=True)})\n"
            else:
                try:
                    r = await c.get(f"/api/session/{sid}/diff", params={"from": user_msg})
                    if r.status_code == 200:
                        changed = r.json().get("data") or []
                        yield diff_block(changed, v.DIFF_CHARS)
                        yield ideas_link(changed, preview)
                except (httpx.HTTPError, NameError):
                    pass
            meta["owui_seen"] = len(messages) + 1  # this user message and the reply being written now
            if check.url:
                meta["owui_app_url"] = check.url    # the next turn's automatic check knows where the app lives
            try:
                await c.patch(f"/api/session/{sid}", json={"metadata": meta})
            except httpx.HTTPError:
                pass
            n = sum(1 for x in steps if not x.startswith("- *") and not x.startswith("- **Automatic"))
            verdict = " · not verified" if unverified else " · page checked" if check.edited and check.web else ""
            if review and finished == "ok":
                left = len(review["findings"]) if review["passed"] is False else 0
                verdict += f" · review: {left} open" if left else " · reviewed" if review["passed"] else " · not reviewed"
            unresolved = unverified or review and review.get("passed") is not True
            if emit and ctx_limit and ctx_last.get("used"):
                with contextlib.suppress(Exception):
                    await emit({"type": "status", "data": dict(ctx_last, action="slerm_context", description="",
                                                               hidden=True, done=True, limit=ctx_limit,
                                                               compact_at=ctx_at, source="code")})
            if emit:
                with contextlib.suppress(Exception):
                    await emit({"type": "status", "data": {"description": "", "done": True, "hidden": True,
                                                              "action": "slerm_timing", "source": "code",
                                                              "timing": timing.completed()}})
            await status((f"{'Needs attention' if unresolved else 'Done'} in {duration(elapsed)}" + (f" · {n} steps" if compact else "") + verdict)
                         if finished == "ok" else "Carried on in your next message" if finished == "taken over"
                         else "Stopped", done=True)
            if CHAT_TURNS.get(chat_id) is turn:
                CHAT_TURNS.pop(chat_id, None)

    @staticmethod
    def _wait_note(timing: "TurnTiming", running: dict, waiting: str) -> str:
        """What a quiet turn is waiting on, in plain words, with how long (a pending approval is never 'thinking')."""
        if running:
            started, line = min(running.values())
            return f"{line} — still running ({duration(time.monotonic() - started)})"
        for what, label in (("permission", "Waiting for your approval"), ("compaction", "Compacting the session"),
                            ("reasoning", "Thinking")):
            secs = timing.open_since(what)
            if secs is not None:
                return wait_note(secs, label)
        label = {"prompt": "Reading context", "compacted": "Reading the compacted context"}.get(
            waiting, "Waiting for the model")
        return wait_note(timing.quiet_for(), label)

    def _later_client(self) -> Optional[httpx.AsyncClient]:
        v = self.valves
        if not (v.LATER_URL and v.LATER_TOKEN):
            return None
        return httpx.AsyncClient(base_url=v.LATER_URL.rstrip("/"), timeout=httpx.Timeout(10, connect=3),
                                 headers={"Authorization": f"Bearer {v.LATER_TOKEN}"})

    async def _take_wakes(self, chat_id: str) -> tuple[str, list]:
        """Claims this chat's ready wake-ups from code-later (atomically: each is delivered once, by whoever claims it
        first - this turn or code-wake). Returns (their message, their ids), or ('', []) when there are none, they are
        held, or code-later is not set up or not answering (code-wake then delivers them after the turn, as before)."""
        client = self._later_client()
        if client is None:
            return "", []
        try:
            async with client as lc:
                r = await lc.post("/api/claim", json={"chat": chat_id, "note": "handed to the agent in a running turn"})
                if r.status_code != 200:
                    return "", []
                got = r.json() or {}
                return str(got.get("text") or ""), [str(i) for i in got.get("ids") or []]
        except (httpx.HTTPError, ValueError):
            return "", []

    async def _unclaim_wakes(self, ids: Optional[list]) -> None:
        """A claimed wake-up whose prompt never reached opencode: ready again, so code-wake delivers it."""
        client = self._later_client() if ids else None
        if client is None:
            return
        with contextlib.suppress(httpx.HTTPError, ValueError):
            async with client as lc:
                await lc.post("/api/unclaim", json={"ids": list(ids)})

    async def _hold_wakes(self, chat_id: str, hold: bool) -> None:
        """Stop: the chat's wake-ups are held (never delivered by themselves); the user's next message releases them."""
        client = self._later_client()
        if client is None:
            return
        with contextlib.suppress(httpx.HTTPError, ValueError):
            async with client as lc:
                await lc.post("/api/hold", json={"chat": chat_id, "hold": bool(hold)})

    async def _script_checks(self, c: httpx.AsyncClient, chat_id: str, names: list) -> list:
        paths = [n for n in names if n.endswith(".sh") and not n.startswith("/") and ".." not in n.split("/")][:20]
        if not paths:
            return []
        script = ("import json,pathlib\nroot=pathlib.Path.cwd().resolve()\nout={}\n"
                  f"for name in {paths!r}:\n"
                  " p=(root/name).resolve()\n"
                  " if p.is_relative_to(root) and p.is_file() and p.stat().st_size <= 100000:\n"
                  "  out[name]=p.read_text(errors='replace')\n"
                  "print(json.dumps(out))\n")
        try:
            rc, out = await self._vm_sh(c, "python3 -c " + shlex.quote(script), timeout=20,
                                      cwd=f"{self.valves.PROJECTS_ROOT.rstrip('/')}/chats/{chat_id}")
            if rc:
                return ["[not true] Installer checks could not run; do not claim the installer was checked."]
            return shell_quality_findings(json.loads(out))
        except (httpx.HTTPError, ValueError, TypeError):
            return ["[not true] Installer checks were unavailable; do not claim the installer was checked."]

    async def _review(self, c: httpx.AsyncClient, chat_id: str, model: dict, status, prompt: str,
                      round_no: int, emit: Optional[Callable[[dict], Awaitable[None]]] = None,
                      taken: Optional[Callable[[], bool]] = None) -> dict:
        """One review round, in its own opencode session on the `review` agent in the chat's project folder (the
        model already loaded does it, so nothing is swapped). The session says which chat and which round it reviews
        (`owui_review_of`, `owui_review_round` - never `owui_chat_id`, the pipe finds the chat's own session by that)
        and stays after the round, so its live window can be replayed until the chat's next turn (see _prune_reviews).
        The round gets a live window of its own as it starts, so the reviewer can be watched reading and diffing.
        Returns {"passed": True / False / None (no verdict), "findings": [...], "text": its reply, "seconds",
        "error"}. Only a cancelled turn raises. taken() true = a later message took the turn over: the round stops
        (checked every few seconds), so no reviewer runs on for a reply that is over."""
        v = self.valves
        out = {"passed": None, "findings": [], "text": "", "seconds": 0, "error": ""}
        t0, rid = time.monotonic(), None
        try:
            r = await c.post("/api/session", json={
                "model": {k: model[k] for k in ("id", "providerID", "variant") if model.get(k)},
                "location": {"directory": f"{v.PROJECTS_ROOT.rstrip('/')}/chats/{chat_id}"},
                "metadata": {"owui_review_of": chat_id, "owui_review_round": round_no}})
            r.raise_for_status()
            rid = r.json()["data"]["id"]
            if (await c.post(f"/api/session/{rid}/agent", json={"agent": REVIEW_AGENT})).status_code >= 300:
                out["error"] = "the review agent is not installed"
                return out
            texts, segment, names = [], [], {}
            async with Events(c, rid) as ev:
                r = await c.post(f"/api/session/{rid}/prompt", json={"text": prompt})
                r.raise_for_status()
                live = self._live_url(chat_id, rid, str(((r.json() or {}).get("data") or {}).get("id") or ""))
                if live and emit:  # watch this round from the moment it starts, like the builder's window
                    await emit({"type": "embeds", "data": {"embeds": [live]}})
                deadline = review_clock() + max(1, v.REVIEW_MINUTES) * 60
                while True:
                    try:
                        # Checked before every event: a reviewer that keeps streaming never goes quiet long enough
                        # for the get() timeout alone to end the round.
                        left = deadline - review_clock()
                        if left <= 0:
                            raise asyncio.TimeoutError
                        if taken and taken():
                            out["error"] = "a later message in this chat took the turn over"
                            await c.post(f"/api/session/{rid}/interrupt")
                            break
                        e = await ev.get(timeout=max(0.05, min(left, 3.0) if taken else left))
                    except asyncio.TimeoutError:
                        if taken and deadline - review_clock() > 0:
                            continue  # only a look at taken(): the round goes on
                        out["error"] = f"it did not finish in {v.REVIEW_MINUTES} minutes"
                        await c.post(f"/api/session/{rid}/interrupt")
                        break
                    t, d = e.get("type", ""), e.get("data") or {}
                    if t == "session.text.delta":
                        segment.append(d.get("delta", ""))
                    elif t == "session.text.ended":
                        text = (d.get("text") or "".join(segment)).strip()
                        segment = []
                        if text:
                            texts.append(text)
                    elif t == "session.tool.input.started":
                        names[d.get("id", "")] = d.get("name") or "tool"
                    elif t == "session.tool.called":
                        await status("Reviewing · " + describe_tool(names.get(d.get("id", ""), d.get("name") or "tool"),
                                                                    d.get("input")))
                    elif t == "permission.asked":  # the review changes nothing: whatever needs approval is refused
                        await c.post(f"/api/session/{rid}/permission/{d['id']}/reply", json={
                            "decision": "reject", "message": "The review changes nothing. Use read-only commands."})
                    elif t == "form.created":
                        await c.delete(f"/api/session/{rid}/form/{(d.get('form') or {}).get('id')}")
                    elif t == "session.execution.succeeded":
                        break
                    elif t in ("session.execution.failed", "session.execution.interrupted", "stream.error"):
                        err = d.get("error")
                        msg = err.get("message") if isinstance(err, dict) else (err or d.get("message"))
                        out["error"] = out["error"] or str(msg or t)
                        break
            if "".join(segment).strip():
                texts.append("".join(segment).strip())
            # Its verdict is the last text that has one (it may write a line before a tool call).
            out["text"] = next((x for x in reversed(texts) if REVIEW_VERDICT_RE.search(x)), texts[-1] if texts else "")
            out["passed"], out["findings"] = parse_review(out["text"])
            if out["error"] and out["passed"] is True:
                out["passed"] = None  # a stale PASS before an interrupted/failed review is not a completed review
            if out["passed"] is None and not out["error"]:
                gaps = [line for line in out["text"].splitlines()
                        if re.search(r"\|\s*(?:unverified|could not check|blocked)\s*\|", line, re.I)]
                out["error"] = "; ".join(gaps)[:800] if gaps else "it gave no complete verdict"
        except asyncio.CancelledError:  # the user pressed Stop: the turn's own handler stops the builder
            if rid:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(c.post(f"/api/session/{rid}/interrupt"), 5)
            raise
        except Exception as e:  # noqa: BLE001 - a review that breaks must never break the turn it reviews
            out["error"] = out["error"] or f"{e.__class__.__name__}: {e}"[:200]
        out["seconds"] = round(time.monotonic() - t0)  # the session itself stays: _prune_reviews clears it next turn
        return out

    async def _prune_reviews(self, c: httpx.AsyncClient, chat_id: str) -> None:
        """A new turn starts: clear this chat's review sessions from the earlier turns. A round's session stays after
        its round so its live window can be replayed while the chat is idle; once the user has sent the next message
        they are done. Only sessions whose own metadata says they review THIS chat: the chat's own session is found
        by owui_chat_id and is never touched, and neither is any other session in the folder. A failure is logged
        (never the valves: they hold tokens) and never breaks the turn."""
        try:
            r = await c.get("/api/session", params={
                "directory": f"{self.valves.PROJECTS_ROOT.rstrip('/')}/chats/{chat_id}"})
            r.raise_for_status()
            for s in r.json().get("data") or []:
                if (s.get("metadata") or {}).get("owui_review_of") == chat_id:
                    await c.delete(f"/api/session/{s['id']}")
        except Exception as e:  # noqa: BLE001 - an earlier turn's review sessions can wait for the next clear
            print(f"code-mode: could not clear this chat's older review sessions ({e.__class__.__name__})", flush=True)

    async def _review_log(self, c: httpx.AsyncClient, chat_id: str, entry: dict) -> None:
        """One line per review round in ~/.local/state/code-mode/review/<chat>.jsonl on the VM: what keeps going
        wrong, in the reviewer's words, for whoever tunes the agents' instructions later. Never fails a turn."""
        keep = re.sub(r"[^A-Za-z0-9_-]", "", chat_id)
        if not keep:
            return
        line = base64.b64encode((json.dumps(entry, ensure_ascii=False) + "\n").encode()).decode()
        try:
            await self._vm_sh(c, 'd="${HOME:-/home/coder}/.local/state/code-mode/review"; mkdir -p "$d" && '
                                 f"printf %s '{line}' | base64 -d >> \"$d/{keep}.jsonl\"", timeout=15)
        except Exception:  # noqa: BLE001
            pass

    async def _announce_fixer(self, chat_id: str, emit, status):
        """The coding agent filed a bug report: put a Fixer link in the reply (the Chat | Code script opens the
        Fixer window from it) and show the chat as paused."""
        v = self.valves
        base = v.FIXER_URL.rstrip("/")
        chat = re.sub(r"[^A-Za-z0-9_-]", "", chat_id)[:64]
        async with httpx.AsyncClient(timeout=10) as http:
            for _ in range(20):
                await asyncio.sleep(1)
                try:
                    r = await http.get(f"{base}/api/active", params={"chat": chat},
                                       headers={"Authorization": f"Bearer {v.FIXER_TOKEN}"})
                except httpx.HTTPError:
                    continue
                if r.status_code == 200:
                    await emit({"type": "message", "data": {"content": f"\n[Fixer]({base}{r.json()['view']})\n\n"}})
                    await status("Paused - Ornith is investigating a bug report")
                    return

    async def _permission(self, c: httpx.AsyncClient, sid: str, d: dict, call, status,
                          chat_id: str = "", user: Optional[dict] = None) -> str:
        what = f"{d.get('action', 'action')}: " + "; ".join(d.get("resources") or [])
        mode = self.valves.PERMISSIONS.lower()
        allowed, reason = mode == "allow", ""
        if mode == "ask" and self.valves.PAGE_ASKS and chat_id:
            # The page's box: shown in every tab of the chat (and the others' notice), back after a reload, no limit.
            await status(f"Waiting for your approval: {what[:80]}")
            got = await PageAsk(c, chat_id, str((user or {}).get("id") or ""), sid, str(d.get("id") or ""))(
                {"type": "permission", "data": {"action": d.get("action") or "action",
                                                "resources": [str(x) for x in (d.get("resources") or [])][:60]}})
            if got is ANSWERED_ELSEWHERE:
                return f"\n> Answered elsewhere: `{what}`\n\n"
            allowed = got.get("allow") is True
            reason = "" if allowed else str(got.get("message") or "").strip()
        elif mode == "ask" and call:
            await status(f"Waiting for your approval: {what[:80]}")
            allowed = bool(await call({"type": "confirmation", "data": {
                "title": "opencode wants to run something that needs approval",
                "message": f"{what}\n\nAllow it this once?"}}))
            if not allowed:  # with a reason opencode carries on; without one it stops and waits
                reason = await call({"type": "input", "data": {
                    "title": "Refused. Anything to tell opencode?",
                    "message": "Optional: why, or what to do instead. Leave empty to stop it here.",
                    "placeholder": "e.g. keep the build folder, just delete build/cache"}})
                reason = reason.strip() if isinstance(reason, str) else ""
        reply = {"decision": "once" if allowed else "reject"}
        if reason:
            reply["message"] = reason
        await c.post(f"/api/session/{sid}/permission/{d['id']}/reply", json=reply)
        return f"\n> {'Allowed' if allowed else 'Refused'}: `{what}`" + (f" ({reason})" if reason else "") + "\n\n"

    async def _ask_one(self, call, status, title: str, question: str, options: list):
        """One question as Open WebUI's input dialog. Returns the option picked (its dict), or the text typed, or
        None when it was cancelled or there is no page to show it on. The first option is the suggested one and
        shows as the field's placeholder: Confirm with nothing typed means that option (Open WebUI answers that with
        True, a typed answer with the text, Cancel with False). The page's box (PageAsk) answers the same way, and
        ANSWERED_ELSEWHERE when the form was answered or closed without it."""
        if not call:
            return None
        labels = [str(o.get("label", "")) for o in options]
        listed = "\n".join(f"{i}. {label}" + (f" ({o['description']})" if o.get("description") else "")
                           for i, (label, o) in enumerate(zip(labels, options), 1))
        message = question
        if listed:
            message += f"\n\n{listed}\n\nType a number, your own answer, or leave it empty for: {labels[0]}"
        await status(f"opencode is asking: {question[:80]}")
        reply = await call({"type": "input", "data": {
            "title": title or "opencode has a question", "message": message, "placeholder": labels[0] if labels else "",
            # the page's box draws the options as buttons (Open WebUI's own dialog ignores these)
            "question": question, "options": labels,
            "descriptions": [str(o.get("description") or "") for o in options]}})
        if reply is ANSWERED_ELSEWHERE:
            return ANSWERED_ELSEWHERE
        return match_option(options, reply)

    async def _form(self, c: httpx.AsyncClient, sid: str, form: dict, call, status,
                    design: Optional[dict] = None) -> tuple[str, bool]:
        """opencode's `question` tool: one dialog per question. Returns the lines for the reply and whether every
        question was answered. Cancel, or no page to show a dialog on, leaves the form unanswered: the reply then
        lists the questions, and the user's next message can answer them.
        design (the ask turn only): {"ui": the request is for something with a screen, "choice": None}. How the
        thing should look is not left to the agent's wording: a question of its own about the look is replaced by
        DESIGN_QUESTION, and if it asked none and there is a screen, DESIGN_QUESTION is asked after its own. The
        answer comes back in design["choice"]: "show", "pick", or what the user typed."""
        answers, lines = {}, []
        fields = form.get("fields") or []

        async def unanswered():
            await c.delete(f"/api/session/{sid}/form/{form['id']}")
            asked = []
            for n, g in enumerate(fields, 1):
                opts = " / ".join(str(o.get("label", "")) for o in g.get("options") or [])
                asked.append(f"> {n}. {g.get('description') or g.get('title') or '?'}" + (f" ({opts})" if opts else ""))
            return ("\n> **Not answered yet.** Answer in your next message:\n" + "\n".join(asked) + "\n\n"), False

        async def ask_design():
            got = await self._ask_one(call, status, "Design", DESIGN_QUESTION, DESIGN_OPTIONS)
            if got is ANSWERED_ELSEWHERE:
                return None
            if got is not None:
                design["choice"] = got["value"] if isinstance(got, dict) else got
                lines.append(f"> {DESIGN_QUESTION} — **{got['label'] if isinstance(got, dict) else got}**")
            return got

        for f in fields:
            options = f.get("options") or []
            question = f.get("description") or f.get("title") or "?"
            if design is not None and LOOK_RE.search(f"{f.get('title') or ''} {question}"):
                if design["choice"] is None and await ask_design() is None:
                    return await unanswered()
                answers[f["key"]] = DESIGN_ANSWERS.get(design["choice"], design["choice"])
                continue
            got = await self._ask_one(call, status, f.get("title") or "", question, options)
            if got is ANSWERED_ELSEWHERE:  # an older page answered it straight to opencode, or the run was stopped
                return "\n> *These questions were answered or closed elsewhere.*\n\n", True
            if got is None:
                return await unanswered()
            pick = (got.get("value") or got.get("label")) if isinstance(got, dict) else got
            answers[f["key"]] = pick
            lines.append(f"> {question} — **{pick}**")
        if design is not None and design["ui"] and design["choice"] is None:
            await ask_design()  # dismissed = the agent decides, like any detail nobody asked about
        await c.post(f"/api/session/{sid}/form/{form['id']}/reply", json={"answer": answers})
        return "\n" + "\n".join(lines) + "\n\n", True

    # ------------------------------------------------------------------ GitHub button (pipe 0.10.0)
    # The page script's GitHub panel calls these: POST /api/chat/completions with code_github = {"op": ..., ...} and
    # no chat id, which Open WebUI runs inline and answers with the dict returned here (an exception would become
    # `null`, so _github catches everything). Admins only.
    # The sign-in token lives in GITHUB_STATE on this machine and goes nowhere else: no op returns it, and nothing
    # here hands it to the sandbox VM, where the coding agent could read it.
    # Each op is a method _gh_op_<name>(args, state) -> dict. Add an op by adding a method (and its name to
    # GH_NEEDS_TOKEN when it needs a signed-in user). tests/fake_github.py plays GitHub in the tests.

    GH_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
    GH_CLIENT_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")
    GH_LOGIN_KEYS = ("device_code", "user_code", "verification_uri", "interval", "expires_at")  # a sign-in under way
    GH_NEEDS_TOKEN = {"repos", "create_repo", "set_visibility", "chat_status", "pull", "attach", "changes", "branches",
                      "push", "history", "rollback", "suggest_message", "diagnose"}
    GH_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
    GH_CHAT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    GH_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
    GH_KEYDIR_RE = re.compile(r"^/[\w./-]+/\.local/state/code-mode/gh/[A-Za-z0-9]+$")  # what _gh_key may remove
    GH_HOSTKEY_RE = re.compile(r"^[a-z0-9-]+ [A-Za-z0-9+/=]+$")
    GH_EXCLUDE = (".venv/", "venv/", "node_modules/", "__pycache__/", "*.pyc", ".env", ".env.*", ".pytest_cache/")

    def _gh_path(self) -> str:
        return os.path.expanduser(self.valves.GITHUB_STATE)

    def _gh_load(self) -> dict:
        try:
            with open(self._gh_path()) as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def _gh_save(self, st: dict) -> None:
        path = self._gh_path()
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from the first byte
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(st, f)
        os.replace(tmp, path)

    def _gh_forget(self, st: dict) -> None:
        """Signed out: drop the token and any sign-in under way. The Client ID stays."""
        for k in ("access_token", "login", "avatar_url") + self.GH_LOGIN_KEYS:
            st.pop(k, None)
        self._gh_save(st)

    def _gh_api(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.valves.GITHUB_API.rstrip("/"), timeout=30, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "code-mode"})

    def _gh_web(self) -> httpx.AsyncClient:
        """github.com's two sign-in endpoints; without this Accept header they answer form-encoded text."""
        return httpx.AsyncClient(base_url=self.valves.GITHUB_WEB.rstrip("/"), timeout=30,
                                 headers={"Accept": "application/json", "User-Agent": "code-mode"})

    @staticmethod
    def _gh_json(r: httpx.Response) -> dict:
        try:
            d = r.json()
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def _gh_check(self, r: httpx.Response, what: str) -> None:
        """Raise for a failed API call, in GitHub's own words when it gave any ('name already exists on this
        account'). A 401 means the token is dead."""
        if r.status_code == 401:
            raise GitHubSignedOut("GitHub no longer accepts the saved sign-in. Sign in again.")
        if r.is_success:
            return
        d = self._gh_json(r)
        errs = d.get("errors")
        detail = (errs[0].get("message") if isinstance(errs, list) and errs and isinstance(errs[0], dict) else "") \
            or d.get("message") or f"GitHub answered {r.status_code}"
        raise GitHubError(f"{what}: {detail}")

    @staticmethod
    def _gh_repo(x: dict) -> dict:
        return {"full_name": x.get("full_name") or "", "private": bool(x.get("private")),
                "description": x.get("description") or "", "default_branch": x.get("default_branch") or "main",
                "pushed_at": x.get("pushed_at") or "", "html_url": x.get("html_url") or ""}

    # --- working on a repo in a chat (pipe 0.11.0)
    # Git runs on the sandbox VM, in the chat's project folder, but the token never goes there. Each operation that
    # must talk to GitHub gets a DEPLOY KEY instead: an SSH key that works for that one repo, made on the VM, added
    # on GitHub, and removed from both a moment later (_gh_key). Every value that goes into a shell command is
    # checked against one of the patterns above and quoted; the only folder ever removed is the key folder that
    # _gh_key itself made (GH_KEYDIR_RE).

    def _vm(self) -> httpx.AsyncClient:
        """opencode on the sandbox VM, for the ops the page calls (in a chat turn _run already has this client)."""
        return httpx.AsyncClient(base_url=self.valves.OPENCODE_URL.rstrip("/"), timeout=httpx.Timeout(30, connect=5),
                                 auth=("opencode", self.valves.OPENCODE_PASSWORD))

    async def _vm_sh(self, c: httpx.AsyncClient, command: str, cwd: Optional[str] = None,
                     timeout: float = 60) -> tuple[int, str]:
        """Run one shell command on the VM and wait for it: (exit code, stdout and stderr together).
        VM_NO_FOLDER when cwd does not exist; 124 when it did not finish in `timeout` seconds."""
        body: dict = {"command": command, "timeout": int(timeout * 1000)}
        if cwd:
            body["cwd"] = cwd
        r = await c.post("/api/shell", json=body)
        if r.status_code == 500 and cwd:  # what opencode answers for a working folder that is not there
            return VM_NO_FOLDER, ""
        r.raise_for_status()
        job = r.json()["data"]
        try:
            pause, end = 0.05, time.monotonic() + timeout + 15
            while job.get("status") == "running" and time.monotonic() < end:
                await asyncio.sleep(pause)
                pause = min(pause * 1.5, 0.5)
                job = (await c.get(f"/api/shell/{job['id']}")).json()["data"]
            out = (await c.get(f"/api/shell/{job['id']}/output")).json()["data"].get("output") or ""
        finally:
            try:
                await c.delete(f"/api/shell/{job['id']}")  # opencode keeps the output until told to drop it
            except httpx.HTTPError:
                pass
        if job.get("status") != "exited":
            return 124, out
        return (job["exit"] if isinstance(job.get("exit"), int) else 1), out

    @staticmethod
    def _last_line(out: str, fallback: str = "no output") -> str:
        lines = [x.strip() for x in (out or "").splitlines() if x.strip()]
        return lines[-1][:300] if lines else fallback

    def _gh_repo_ok(self, full_name: str) -> bool:
        return bool(self.GH_REPO_RE.fullmatch(full_name or "")) and not any(p in (".", "..") for p in full_name.split("/"))

    def _gh_branch_ok(self, branch: str) -> bool:
        return bool(self.GH_BRANCH_RE.fullmatch(branch or "")) and ".." not in branch and not branch.startswith("-")

    def _gh_folder(self, chat_id: str) -> str:
        return f"{self.valves.PROJECTS_ROOT.rstrip('/')}/chats/{chat_id}"

    def _gh_remote(self, full_name: str) -> str:
        return f"{self.valves.GITHUB_GIT}{full_name}.git"

    @contextlib.asynccontextmanager
    async def _gh_key(self, c: httpx.AsyncClient, st: dict, full_name: str, read_only: bool = True):
        """A deploy key for ONE repo that exists only inside the `async with`. Yields the environment to put in
        front of a git command on the VM (`f"{env} git fetch origin"`). On the way out, whatever happened, the key
        is deleted on GitHub and its folder removed from the VM. read_only=False is for pushing."""
        code, out = await self._vm_sh(c, 'set -e; mkdir -p "$HOME/.local/state/code-mode/gh"; '
                                         'd=$(mktemp -d "$HOME/.local/state/code-mode/gh/XXXXXXXX"); '
                                         'ssh-keygen -q -t ed25519 -N "" -C code-mode -f "$d/key"; '
                                         'echo "$d"; cat "$d/key.pub"', timeout=30)
        lines = out.strip().splitlines()
        d = lines[0].strip() if lines else ""
        if not self.GH_KEYDIR_RE.fullmatch(d):  # nothing of ours to clean up, and nothing else may be touched
            raise GitHubError(f"Could not make a key on the sandbox VM: {self._last_line(out)}")
        key_id = None
        async with self._gh_api(st["access_token"]) as g:
            try:
                pub = lines[1].strip() if len(lines) > 1 else ""
                if code or not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+( [\w.@-]+)?", pub):
                    raise GitHubError(f"Could not make a key on the sandbox VM: {self._last_line(out)}")
                await self._gh_drop_stale_keys(g, st)
                hosts = [k for k in self._gh_json(await g.get("/meta")).get("ssh_keys") or []
                         if isinstance(k, str) and self.GH_HOSTKEY_RE.fullmatch(k)]
                if not hosts:
                    raise GitHubError("GitHub did not say what its SSH host keys are, so git would have nothing to "
                                      "check the connection against.")
                known = " ".join(shlex.quote(f"github.com {k}") for k in hosts)
                code, out = await self._vm_sh(c, f"printf '%s\\n' {known} > {shlex.quote(d + '/known_hosts')}", timeout=30)
                if code:
                    raise GitHubError(f"Could not prepare the key on the sandbox VM: {self._last_line(out)}")
                r = await g.post(f"/repos/{full_name}/keys", json={"title": "Code mode (temporary)", "key": pub,
                                                                   "read_only": bool(read_only)})
                if r.status_code == 404:
                    raise GitHubError(f"GitHub would not take a deploy key for {full_name}. That needs you to be an "
                                      f"admin of the repository (your own repos always are).")
                self._gh_check(r, f"Could not add a deploy key to {full_name}")
                key_id = self._gh_json(r).get("id")
                # d passed GH_KEYDIR_RE, so it has no spaces or quotes and can sit inside the ssh command as is.
                ssh = (f"ssh -i {d}/key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes "
                       f"-o UserKnownHostsFile={d}/known_hosts")
                yield f"GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND={shlex.quote(ssh)}"
            finally:
                try:
                    if key_id is not None:
                        gone = await g.delete(f"/repos/{full_name}/keys/{key_id}")
                        if gone.status_code not in (204, 404):
                            raise httpx.HTTPError(f"GitHub answered {gone.status_code}")
                except httpx.HTTPError:  # remember it and remove it next time; without its private half it opens nothing
                    st.setdefault("stale_keys", []).append({"repo": full_name, "id": key_id})
                    self._gh_save(st)
                finally:
                    await self._vm_sh(c, f"rm -rf {shlex.quote(d)}", timeout=30)

    async def _gh_drop_stale_keys(self, g: httpx.AsyncClient, st: dict) -> None:
        """Deploy keys an earlier operation could not delete (GitHub unreachable at that moment): try again."""
        left = []
        for k in st.get("stale_keys") or []:
            try:
                ok = self._gh_repo_ok(str(k.get("repo"))) and isinstance(k.get("id"), int)
                if ok and (await g.delete(f"/repos/{k['repo']}/keys/{k['id']}")).status_code not in (204, 404):
                    left.append(k)
            except httpx.HTTPError:
                left.append(k)
        if left != (st.get("stale_keys") or []):
            st["stale_keys"] = left
            if not left:
                st.pop("stale_keys")
            self._gh_save(st)

    async def _gh_git(self, c: httpx.AsyncClient, env: str, args: str, cwd: str, timeout: float = 600) -> tuple[int, str]:
        """`git <args>` on the VM with a deploy key's environment. A key GitHub has only just been given can be
        refused for a moment, so that one answer is tried once more."""
        code, out = await self._vm_sh(c, f"{env} git {args}", cwd=cwd, timeout=timeout)
        if code and "Permission denied (publickey)" in out:
            await asyncio.sleep(2)
            code, out = await self._vm_sh(c, f"{env} git {args}", cwd=cwd, timeout=timeout)
        return code, out

    async def _gh_chat_state(self, c: httpx.AsyncClient, chat_id: str) -> dict:
        """What the chat's project folder is. linked = its `origin` is one of our GitHub remotes (then `repo`);
        changed = files with uncommitted changes; ahead = commits GitHub does not have; project = it has work of its
        own and no repo; fresh = no work in it at all (a repo may be cloned into it); head = its current commit."""
        if not self.GH_CHAT_RE.fullmatch(chat_id or ""):
            return {"ok": False, "error": "Not a chat id."}
        code, out = await self._vm_sh(c, "git remote get-url origin 2>/dev/null; echo ---; git branch --show-current "
                                         "2>/dev/null; echo ---; git status --porcelain 2>/dev/null | wc -l; echo ---; "
                                         "git rev-parse -q --verify HEAD 2>/dev/null; echo ---; "
                                         "git rev-list --count HEAD --not --remotes=origin 2>/dev/null; echo ---; "
                                         "[ -d .git ] && echo git; echo ---; ls -A 2>/dev/null | grep -v '^\\.git$' "
                                         "| grep -v '^uploads$' | head -n 1; true", cwd=self._gh_folder(chat_id))
        none = {"ok": True, "linked": False, "repo": "", "branch": "", "changed": 0, "ahead": 0, "project": False}
        if code == VM_NO_FOLDER:
            return {**none, "fresh": True, "folder": False, "head": ""}
        parts = [p.strip() for p in out.split("---")] + [""] * 7
        origin, prefix = parts[0], self.valves.GITHUB_GIT
        repo = origin[len(prefix):-4] if origin.startswith(prefix) and origin.endswith(".git") else ""
        if not self._gh_repo_ok(repo):
            repo = ""
        changed = int(parts[2]) if parts[2].isdigit() else 0
        loose = parts[5] != "git" and bool(parts[6])  # files in a folder git was never started in: work all the same
        fresh = not origin and not changed and not parts[3] and not loose
        return {**none, "linked": bool(repo), "repo": repo, "branch": parts[1], "changed": changed,
                "ahead": int(parts[4]) if repo and parts[4].isdigit() else 0, "project": not repo and not fresh,
                "fresh": fresh, "folder": True, "head": parts[3]}

    @staticmethod
    def _gh_public(state: dict) -> dict:
        """A chat state as the page gets it."""
        return {k: v for k, v in state.items() if k not in ("fresh", "folder", "head")}

    async def _gh_linked(self, c: httpx.AsyncClient, chat_id: str) -> dict:
        """The chat's state, for an op that only makes sense in a chat that works on a repo."""
        state = await self._gh_chat_state(c, chat_id)
        if not state["ok"]:
            raise GitHubError(state["error"])
        if not state["linked"]:
            raise GitHubError("This chat is not working on a GitHub repository.")
        return state

    def _gh_setup_cmd(self, login: str, uid: int) -> str:
        """For a chat's repo only: commits are the user's (GitHub's no-reply address), and local junk and secrets
        stay out of them without touching the repo's own .gitignore."""
        q = shlex.quote
        return (f"git config user.name {q(login)} && git config user.email {q(f'{uid}+{login}@users.noreply.github.com')} "
                f"&& mkdir -p .git/info && printf '%s\\n' {' '.join(q(x) for x in self.GH_EXCLUDE)} >> .git/info/exclude")

    async def _gh_note(self, c: httpx.AsyncClient, chat_id: str, text: str) -> None:
        """Leave a line for the agent's next turn: something happened to the project while it was not looking."""
        await self._vm_sh(c, f"printf '%s\\n' {shlex.quote(' '.join(text.split()))} >> .git/code-mode-notes",
                          cwd=self._gh_folder(chat_id))

    async def _gh_take_notes(self, c: httpx.AsyncClient, chat_id: str) -> tuple[bool, list]:
        """(the project is new to the agent, notes left by the panel's actions), each told once. A clone may have
        happened in this very turn (a new chat's first message) or earlier, from the panel."""
        code, out = await self._vm_sh(c, "if [ -f .git/code-mode-new ]; then rm -f .git/code-mode-new; echo NEW; fi; "
                                         "echo ---; if [ -f .git/code-mode-notes ]; then cat .git/code-mode-notes; "
                                         "rm -f .git/code-mode-notes; fi", cwd=self._gh_folder(chat_id))
        if code:
            return False, []
        new, _, notes = out.partition("---")
        return "NEW" in new, [x.strip()[:300] for x in notes.splitlines() if x.strip()][:5]

    async def _gh_attach(self, c: httpx.AsyncClient, chat_id: str, full_name: str) -> dict:
        """Make the chat work on the repo. A chat with no work in it gets a clone. A chat with a project of its own
        can be given a repo that is still EMPTY: nothing is cloned or changed, the folder just gets the repo as its
        origin, and a push uploads the project. {"ok": True, "repo", "branch", "fresh"} (fresh = cloned just now) or
        {"ok": False, "error"}. Never raises."""
        st = self._gh_load()
        try:
            if not self._gh_repo_ok(full_name):
                return {"ok": False, "error": f"Not a repository name: {str(full_name)[:80]}"}
            state = await self._gh_chat_state(c, chat_id)
            if not state["ok"]:
                return state
            if not state.get("folder"):
                return {"ok": False, "error": "This chat has no project folder yet."}
            if state["linked"]:
                if state["repo"] == full_name:
                    return {"ok": True, "repo": full_name, "branch": state["branch"], "fresh": False}
                return {"ok": False, "error": f"This chat already works on {state['repo']}. Start a new chat for {full_name}."}
            if not st.get("access_token"):
                return {"ok": False, "signed_out": True, "error": "Not signed in to GitHub. Open the GitHub button, sign in, and send this again."}
            async with self._gh_api(st["access_token"]) as g:
                r = await g.get(f"/repos/{full_name}")
                self._gh_check(r, f"Could not open {full_name}")
                branch = self._gh_json(r).get("default_branch") or "main"
                u = await g.get("/user")
                self._gh_check(u, "Could not read your GitHub account")
                user = self._gh_json(u)
                empty = False
                if not state["fresh"]:
                    b = await g.get(f"/repos/{full_name}/branches", params={"per_page": 1})
                    self._gh_check(b, f"Could not look at {full_name}")
                    empty = b.json() == []
            login, uid = str(user.get("login") or ""), user.get("id")
            if not self._gh_branch_ok(branch) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login) or not isinstance(uid, int):
                return {"ok": False, "error": f"GitHub gave a branch or account name this cannot use ({branch[:40]}, {login[:40]})."}
            folder, q = self._gh_folder(chat_id), shlex.quote
            if not state["fresh"]:
                if not empty:
                    return {"ok": False, "error": (
                        f"This chat already has a project of its own, and {full_name} is not empty. A chat's own "
                        f"project can go into an empty repository: make one below, then press Work on it there.")}
                code, out = await self._vm_sh(c, f"git remote add origin {q(self._gh_remote(full_name))} && "
                                                 f"{self._gh_setup_cmd(login, uid)}", cwd=folder)
                if code:
                    return {"ok": False, "error": f"Could not set the project up: {self._last_line(out)}"}
                return {"ok": True, "repo": full_name, "branch": state["branch"] or branch, "fresh": False, "own": True}
            async with self._gh_key(c, st, full_name, read_only=True) as env:
                code, out = await self._vm_sh(c, f"git remote add origin {q(self._gh_remote(full_name))}", cwd=folder)
                if code:
                    raise GitHubError(f"Could not set the project up: {self._last_line(out)}")
                try:
                    code, out = await self._gh_git(c, env, "fetch -q origin", cwd=folder)
                    if code:
                        raise GitHubError(f"Could not fetch {full_name}: {self._last_line(out)}")
                    # An empty repo has no branch to check out: point HEAD at the default one, ready for a first commit.
                    code, out = await self._vm_sh(c, (
                        f"if git rev-parse -q --verify refs/remotes/origin/{q(branch)} >/dev/null; "
                        f"then git checkout -q -B {q(branch)} --track origin/{q(branch)}; "
                        f"else git symbolic-ref HEAD refs/heads/{q(branch)}; fi"), cwd=folder, timeout=300)
                    if code:
                        raise GitHubError(f"Could not check out {branch}: {self._last_line(out)}")
                except BaseException:
                    await self._vm_sh(c, "git remote remove origin", cwd=folder)  # so a retry starts clean
                    raise
            # .git/code-mode-new tells the chat's next turn that the project is new to the agent (_gh_take_notes).
            code, out = await self._vm_sh(c, f"{self._gh_setup_cmd(login, uid)} && : > .git/code-mode-new", cwd=folder)
            if code:
                return {"ok": False, "error": f"Cloned {full_name}, but could not finish setting it up: {self._last_line(out)}"}
            return {"ok": True, "repo": full_name, "branch": branch, "fresh": True}
        except GitHubSignedOut as e:
            self._gh_forget(st)
            return {"ok": False, "signed_out": True, "error": str(e)}
        except GitHubError as e:
            return {"ok": False, "error": str(e)}
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"GitHub or the sandbox VM could not be reached ({e.__class__.__name__})."}

    async def _github(self, body: dict, user: dict, request=None) -> dict:
        """One call from the GitHub panel. Always a dict: {"ok": True, ...} or {"ok": False, "error": plain words},
        plus "signed_out": True when there is no usable sign-in (the panel then shows Sign in)."""
        st: dict = {}
        try:
            if (user or {}).get("role") != "admin":
                return {"ok": False, "error": "The GitHub button is for admins only."}
            args = body.get("code_github")
            op = args.get("op") if isinstance(args, dict) else None
            handler = getattr(self, f"_gh_op_{op}", None) if isinstance(op, str) and op.isidentifier() else None
            if handler is None:
                return {"ok": False, "error": f"Unknown GitHub op: {op}"}
            st = self._gh_load()
            if op in self.GH_NEEDS_TOKEN and not st.get("access_token"):
                return {"ok": False, "signed_out": True, "error": "Not signed in to GitHub."}
            return await handler({**args, "__request__": request, "__user__": user}, st)
        except GitHubSignedOut as e:
            self._gh_forget(st)
            return {"ok": False, "signed_out": True, "error": str(e)}
        except GitHubError as e:
            return {"ok": False, "error": str(e)}
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"GitHub could not be reached ({e.__class__.__name__})."}
        except Exception as e:  # noqa: BLE001 - see the note above: never raise
            return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}

    async def _gh_op_status(self, a: dict, st: dict) -> dict:
        """What the panel shows when it opens. Checks the saved token with GitHub, so a revoked one is noticed."""
        out = {"ok": True, "client_id_set": bool(st.get("client_id")), "signed_in": False}
        if not st.get("access_token"):
            return out
        try:
            async with self._gh_api(st["access_token"]) as c:
                r = await c.get("/user")
        except httpx.HTTPError:  # GitHub unreachable: say who was signed in; the next real call reports the outage
            return {**out, "signed_in": True, "login": st.get("login", ""), "avatar_url": st.get("avatar_url", "")}
        if r.status_code == 401:
            self._gh_forget(st)
            return {**out, "signed_out": True}
        u = self._gh_json(r)
        return {**out, "signed_in": True, "login": u.get("login") or st.get("login", ""),
                "avatar_url": u.get("avatar_url") or st.get("avatar_url", "")}

    async def _gh_op_set_client(self, a: dict, st: dict) -> dict:
        cid = str(a.get("client_id") or "").strip()
        if not self.GH_CLIENT_RE.fullmatch(cid):
            return {"ok": False, "error": "That does not look like a Client ID (it is the short id on the OAuth App's "
                                          "page, not the client secret)."}
        if cid != st.get("client_id"):
            self._gh_forget(st)  # a token belongs to the app that issued it
        st["client_id"] = cid
        self._gh_save(st)
        return {"ok": True, "client_id_set": True}

    async def _gh_op_login_start(self, a: dict, st: dict) -> dict:
        """Device flow, step 1: a one-time code for the user to enter at verification_uri."""
        if not st.get("client_id"):
            return {"ok": False, "error": "No Client ID saved yet."}
        async with self._gh_web() as c:
            r = await c.post("/login/device/code", data={"client_id": st["client_id"], "scope": "repo"})
        d = self._gh_json(r)
        if r.status_code == 404:
            return {"ok": False, "bad_client": True, "error": "GitHub does not know this Client ID. Check it on the "
                                                               "OAuth App's page."}
        if d.get("error") == "device_flow_disabled":
            return {"ok": False, "error": "Device Flow is off for this OAuth App. Tick \"Enable Device Flow\" on its "
                                          "page on GitHub, save, and try again."}
        if not r.is_success or not d.get("device_code"):
            return {"ok": False, "error": f"GitHub would not start the sign-in ({d.get('error') or r.status_code})."}
        st.update(device_code=d["device_code"], user_code=d.get("user_code", ""),
                  verification_uri=d.get("verification_uri", ""), interval=d.get("interval") or 5,
                  expires_at=time.time() + (d.get("expires_in") or 900))
        self._gh_save(st)
        return {"ok": True, "user_code": st["user_code"], "verification_uri": st["verification_uri"],
                "interval": st["interval"], "expires_in": d.get("expires_in") or 900}

    async def _gh_op_login_poll(self, a: dict, st: dict) -> dict:
        """Device flow, step 2: one poll. The page calls again after `interval` seconds while "pending" is true;
        "restart": True means this sign-in is over and a new one must be started."""
        if not st.get("device_code"):
            return {"ok": False, "restart": True, "error": "No sign-in is under way."}
        async with self._gh_web() as c:
            r = await c.post("/login/oauth/access_token", data={
                "client_id": st.get("client_id", ""), "device_code": st["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        d = self._gh_json(r)  # GitHub answers 200 for its errors too: read the body, not the status
        token = d.get("access_token")
        if token:
            async with self._gh_api(token) as c:
                u = self._gh_json(await c.get("/user"))
            for k in self.GH_LOGIN_KEYS:
                st.pop(k, None)
            st.update(access_token=token, login=u.get("login") or "", avatar_url=u.get("avatar_url") or "")
            self._gh_save(st)
            return {"ok": True, "signed_in": True, "login": st["login"], "avatar_url": st["avatar_url"]}
        err = d.get("error")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":  # polled too fast: GitHub says how long to wait from now on
                st["interval"] = d.get("interval") or (st.get("interval") or 5) + 5
                self._gh_save(st)
            return {"ok": True, "pending": True, "interval": st.get("interval") or 5}
        for k in self.GH_LOGIN_KEYS:
            st.pop(k, None)
        self._gh_save(st)
        words = {"expired_token": "The code expired before it was used. Start again.",
                 "access_denied": "The sign-in was cancelled on GitHub."}
        return {"ok": False, "restart": True,
                "error": words.get(err) or f"GitHub refused the sign-in ({err or r.status_code})."}

    async def _gh_op_logout(self, a: dict, st: dict) -> dict:
        """Forget the token here. (It stays valid on GitHub until revoked there: Settings > Applications >
        Authorized OAuth Apps. Revoking by API needs the app's client secret, which this flow never has.)"""
        self._gh_forget(st)
        return {"ok": True}

    async def _gh_op_repos(self, a: dict, st: dict) -> dict:
        """Every repo the user can reach, latest push first (up to 1000)."""
        base = self.valves.GITHUB_API.rstrip("/")
        url, repos, more = "/user/repos?per_page=100&sort=pushed", [], False
        async with self._gh_api(st["access_token"]) as c:
            for page in range(10):
                r = await c.get(url)
                self._gh_check(r, "Could not list the repos")
                rows = r.json()
                repos += [self._gh_repo(x) for x in rows if isinstance(x, dict)] if isinstance(rows, list) else []
                m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
                if not m or not m.group(1).startswith(base + "/"):  # the token goes to the API host and nowhere else
                    break
                url, more = m.group(1), page == 9
        return {"ok": True, "repos": repos, "more": more, "login": st.get("login", ""),
                "avatar_url": st.get("avatar_url", "")}

    async def _gh_op_create_repo(self, a: dict, st: dict) -> dict:
        name = str(a.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "Give the repository a name."}
        # GitHub makes a repo PUBLIC unless told otherwise: private unless the page explicitly sent false.
        payload = {"name": name, "private": a.get("private") is not False, "auto_init": False}
        description = str(a.get("description") or "").strip()
        if description:
            payload["description"] = description
        async with self._gh_api(st["access_token"]) as c:
            r = await c.post("/user/repos", json=payload)
        self._gh_check(r, "Could not create the repository")
        return {"ok": True, "repo": self._gh_repo(self._gh_json(r))}

    async def _gh_op_set_visibility(self, a: dict, st: dict) -> dict:
        full = str(a.get("full_name") or "")
        if not self.GH_REPO_RE.fullmatch(full) or any(part in (".", "..") for part in full.split("/")):
            return {"ok": False, "error": f"Not a repository name: {full[:80]}"}
        if not isinstance(a.get("private"), bool):
            return {"ok": False, "error": "set_visibility needs private: true or false."}
        async with self._gh_api(st["access_token"]) as c:
            r = await c.patch(f"/repos/{full}", json={"private": a["private"]})
        self._gh_check(r, f"Could not make {full} {'private' if a['private'] else 'public'}")
        return {"ok": True, "repo": self._gh_repo(self._gh_json(r))}

    async def _gh_op_attach(self, a: dict, st: dict) -> dict:
        """"Work on it" in a chat that already exists: make its project folder a clone of the repo, now. A chat that
        never ran the coding agent has no folder yet, so it is made here the way _session makes it."""
        chat_id, full_name = str(a.get("chat_id") or ""), str(a.get("full_name") or "")
        if not self.GH_CHAT_RE.fullmatch(chat_id):
            return {"ok": False, "error": "Not a chat id."}
        if not self._gh_repo_ok(full_name):
            return {"ok": False, "error": f"Not a repository name: {full_name[:80]}"}
        folder = shlex.quote(self._gh_folder(chat_id))
        async with self._vm() as c:
            code, out = await self._vm_sh(c, f"mkdir -p {folder} && git -C {folder} init -q")
            if code:
                return {"ok": False, "error": f"Could not make the chat's project folder: {self._last_line(out)}"}
            got = await self._gh_attach(c, chat_id, full_name)
            if not got.get("ok"):
                return got
            state = await self._gh_chat_state(c, chat_id)
        return {**self._gh_public(state), "fresh": got["fresh"], "own": bool(got.get("own"))}

    async def _gh_op_chat_status(self, a: dict, st: dict) -> dict:
        """The repo the open chat works on, for the panel's first line: {"linked", "repo", "branch", "changed"}."""
        async with self._vm() as c:
            state = await self._gh_chat_state(c, str(a.get("chat_id") or ""))
        return self._gh_public(state)

    async def _gh_op_pull(self, a: dict, st: dict) -> dict:
        """Bring the chat's project up to date with GitHub, fast-forward only: never a merge, never losing work."""
        chat_id = str(a.get("chat_id") or "")
        async with self._vm() as c:
            state = await self._gh_chat_state(c, chat_id)
            if not state["ok"]:
                return state
            if not state["linked"]:
                return {"ok": False, "error": "This chat is not working on a GitHub repository."}
            if not self._gh_branch_ok(state["branch"]):
                return {"ok": False, "error": "This project is not on a branch, so there is nothing to pull into."}
            async with self._gh_key(c, st, state["repo"], read_only=True) as env:
                code, out = await self._gh_git(c, env, f"pull -q --ff-only origin {shlex.quote(state['branch'])}",
                                               cwd=self._gh_folder(chat_id))
            if code:
                return {"ok": False, "error": f"Could not pull: {self._last_line(out)}"}
            before, state = state["head"], await self._gh_chat_state(c, chat_id)
            if state["head"] != before:
                await self._gh_note(c, chat_id, "The user pulled new commits from GitHub into the project. Files may "
                                                "have changed: read them again before editing.")
        return self._gh_public(state)

    # --- diagnosing the chat's project against its repo (pipe 0.23.0)

    async def _gh_op_diagnose(self, a: dict, st: dict) -> dict:
        """Diagnose: compare the chat's project with its GitHub repo and say what is wrong with it.

        Read-only. The only thing it sends to GitHub through git is a fetch (it changes no file of the project and
        no branch of the repo); the rest is the repo's branch list from the API and the project's own git state.
        What it finds becomes findings, each carrying the panel action that answers it (pull / push / the chat's
        agent) or "" when the fix is the user's decision. Two sides having diverged is git work, so its finding
        points at the chat's agent: the brief Ask the agent sends lists the facts and the safe way through.
        """
        chat_id = str(a.get("chat_id") or "")
        async with self._vm() as c:
            state = await self._gh_chat_state(c, chat_id)
            if not state["ok"]:
                return state
            if not state["linked"]:
                return self._gh_diagnose_local(state)
            repo, branch, folder = state["repo"], state["branch"], self._gh_folder(chat_id)

            async with self._gh_api(st["access_token"]) as g:
                r = await g.get(f"/repos/{repo}")
                gone = r.status_code == 404
                on_github: list = []
                if not gone:
                    self._gh_check(r, f"Could not open {repo}")
                    b = await g.get(f"/repos/{repo}/branches", params={"per_page": 100})
                    self._gh_check(b, f"Could not look at {repo}")
                    on_github = [str(x.get("name") or "") for x in (b.json() or []) if isinstance(x, dict)]

            note, fetched = "", False
            if not gone and self._gh_branch_ok(branch):  # GitHub's newest refs, so the comparison is not from stale ones
                try:
                    async with self._gh_key(c, st, repo, read_only=True) as env:
                        code, out = await self._gh_git(c, env, "fetch -q --prune origin", cwd=folder, timeout=180)
                        if code:
                            note = f"Could not fetch {repo} from GitHub: {self._last_line(out)}"
                        else:
                            fetched = True
                except GitHubError as e:
                    note = str(e)
            facts = await self._gh_diagnose_facts(c, chat_id, branch) if self._gh_branch_ok(branch) else {}
            findings = self._gh_diagnose_findings(repo, branch, gone, note, on_github, facts)
            brief = self._gh_diagnose_brief(repo, branch, findings, facts)
        return {**self._gh_public(state), "fetched": fetched,
                "summary": "The project and GitHub match." if not findings else
                f"{len(findings)} thing(s) to know about.",
                "findings": findings, "brief": brief}

    def _gh_diagnose_findings(self, repo: str, branch: str, gone: bool, note: str, on_github: list, facts: dict) -> list:
        """What the comparison found, in the order the panel shows it."""
        findings = []
        if gone:
            findings.append(_gh_finding(
                "no_repo", f"GitHub no longer has the repository {repo}. The project still has its own git history "
                "here, so nothing is lost; it just has nowhere to be pushed.", ""))
        if note:
            findings.append(_gh_finding("stale", f"{note} So this comparison may be out of date.", ""))
        if not facts:
            return findings + [_gh_finding(
                "no_state", "Could not read the project's git state, or this project is not on a branch it could "
                "compare with GitHub.", "agent")]
        if not facts["has_commits"]:
            findings.append(_gh_finding("no_commits", "The project has no commits yet, so there is nothing to push.",
                                        ""))
        elif facts["remote_ref"]:
            mine, theirs = self._gh_commit_line(facts["mine"]), self._gh_commit_line(facts["theirs"])
            if facts["ahead"] and facts["behind"]:
                findings.append(_gh_finding(
                    "diverged", f"{branch} has diverged: the project has {facts['ahead']} commit(s) GitHub does not "
                    f"have ({mine}), and GitHub has {facts['behind']} commit(s) the project does not have ({theirs}). "
                    "Push is refused because that would erase what GitHub has; Pull would not work either.", "agent"))
            elif facts["behind"]:
                findings.append(_gh_finding(
                    "behind", f"GitHub has {facts['behind']} commit(s) on {branch} that this chat does not have "
                    f"({theirs}).", "pull"))
            elif facts["ahead"]:
                findings.append(_gh_finding(
                    "ahead", f"The project has {facts['ahead']} commit(s) not yet on GitHub ({branch}): {mine}.",
                    "push"))
        else:
            first = " Pushing uploads your first commits there." if not on_github else " Pushing creates it."
            findings.append(_gh_finding("no_branch", f"GitHub has no branch {branch} of {repo} yet." + first, "push"))
        if facts["changed"]:
            findings.append(_gh_finding("uncommitted", f"{facts['changed']} file(s) in the project are not committed "
                                        "yet.", ""))
        return findings

    @staticmethod
    def _gh_commit_line(commits: list) -> str:
        """The commits a finding names: `56789ab add the map panel; 3cd45ef fix the header`."""
        return "; ".join(f"{c['short']} {c['subject']}" for c in commits if c) or "none listed"

    async def _gh_diagnose_facts(self, c: httpx.AsyncClient, chat_id: str, branch: str) -> dict:
        """The project's git state in one shell command: whether it has commits, its branch, whether origin/<branch>
        is known (after the fetch), how HEAD and that ref differ in each direction, up to 3 commit subjects from each
        side, and the number of uncommitted files. Sections are separated by a line holding only ---, and every
        section starts with its own word, so an empty one cannot make the parsing shift."""
        q, ref = shlex.quote, f"refs/remotes/origin/{branch}"  # show-ref --verify needs the full name
        code, out = await self._vm_sh(c, (
            "git rev-parse -q --verify HEAD >/dev/null && echo HAVE || echo NOHEAD; echo ---; "
            "git branch --show-current 2>/dev/null; echo ---; "
            f"git show-ref -q --verify {q(ref)} 2>/dev/null && echo REF || echo NOREF; echo ---; "
            f"git rev-list --left-right --count HEAD...{q(ref)} 2>/dev/null || echo 0 0; echo ---; "
            f"echo MINE; git log -n 3 --format='%h%x1f%s' HEAD --not {q(ref)} 2>/dev/null; echo ---; "
            f"echo THEIRS; git log -n 3 --format='%h%x1f%s' {q(ref)} --not HEAD 2>/dev/null; echo ---; "
            "git status --porcelain 2>/dev/null | wc -l; true"), cwd=self._gh_folder(chat_id), timeout=60)
        if code:
            return {}
        parts = [p.strip("\n") for p in out.split("\n---\n")]
        if len(parts) < 7 or not parts[4].startswith("MINE") or not parts[5].startswith("THEIRS"):
            return {}
        counts = parts[3].split()
        pair = counts if len(counts) == 2 and all(x.isdigit() for x in counts) else ["0", "0"]

        def commits(text: str) -> list:  # after the section's own word: up to 3, newest first
            return [_gh_commit(x) for x in text.splitlines()[1:] if x.strip()][:3]

        return {"has_commits": parts[0] == "HAVE", "branch": parts[1], "remote_ref": parts[2] == "REF",
                "ahead": int(pair[0]), "behind": int(pair[1]),
                "mine": commits(parts[4]), "theirs": commits(parts[5]),
                "changed": int(parts[6]) if parts[6].isdigit() else 0}

    def _gh_diagnose_local(self, state: dict) -> dict:
        """Diagnose for a chat that is not working on a repository: what its project is, and how it would get there."""
        if not state.get("folder"):
            return {"ok": False, "error": "This chat has no project folder yet."}
        findings = []
        if state["fresh"]:
            findings.append(_gh_finding("empty", "This chat's project folder is empty: nothing has been built here "
                                        "yet.", ""))
        else:
            findings.append(_gh_finding("unlinked", "This chat's project is not on GitHub yet. To put it there: make "
                                        "an empty repository (or pick one below), press Work on it, then Push.", ""))
        brief = ("GitHub check: this chat's project is not on GitHub yet, so the GitHub Diagnose button had nothing "
                 "to compare. " + ("The project folder is empty. " if state["fresh"] else "It has work of its own. ")
                 + "If it should be on GitHub, tell the user how: make or pick an empty repository in the GitHub "
                 "panel, press Work on it, then Push.")
        return {**self._gh_public(state), "fetched": False,
                "summary": f"{len(findings)} thing(s) to know about.", "findings": findings, "brief": brief}

    def _gh_diagnose_brief(self, repo: str, branch: str, findings: list, facts: dict) -> str:
        """What Ask the agent sends to the chat's agent: the facts, and what it may do about them. Push and Pull
        themselves stay in the panel; the agent works on the project's git and tells the user what it did."""
        lines = [f"GitHub check: {repo} branch {branch} — the GitHub Diagnose button compared this chat's project "
                 "with the repository and found:"]
        lines += [f"- {f['text']}" for f in findings]
        if any(f["id"] == "diverged" for f in findings):
            lines += [f"- Your commits, not on GitHub: {self._gh_commit_line(facts.get('mine') or [])}",
                      f"- GitHub's commits, not here: {self._gh_commit_line(facts.get('theirs') or [])}"]
        lines += [
            f"origin's newest refs are already fetched, so compare with git (git log, git diff origin/{branch}) "
            "before changing anything.",
            "If a safe fix is clear, do it in the project folder — for a divergence: commit any local work, then "
            f"git merge origin/{branch}, then tell the user to press Push in the GitHub panel to publish it.",
            "Otherwise give the user their options, saying what each one keeps and what it loses, and name which "
            "you recommend.",
            "Never rewrite history, force-push or delete commits unless the user says so in this chat. Pushing and "
            "pulling themselves happen through the GitHub panel.",
        ]
        return "\n".join(lines)

    # --- pushing, history, rolling back (pipe 0.12.0)

    async def _gh_op_changes(self, a: dict, st: dict) -> dict:
        """What a push would commit: the files with uncommitted changes (the first 40 of them)."""
        chat_id = str(a.get("chat_id") or "")
        async with self._vm() as c:
            state = await self._gh_linked(c, chat_id)
            code, out = await self._vm_sh(c, "git status --porcelain | head -n 400", cwd=self._gh_folder(chat_id))
        rows = [x for x in out.splitlines() if len(x) > 3][:40]
        return {**self._gh_public(state), "files": [{"status": x[:2].strip() or "?", "path": x[3:][:300]} for x in rows],
                "more": max(0, state["changed"] - len(rows))}

    async def _gh_op_branches(self, a: dict, st: dict) -> dict:
        """Where a push can go: the repo's branches on GitHub, its default branch, and the chat's current one."""
        async with self._vm() as c:
            state = await self._gh_linked(c, str(a.get("chat_id") or ""))
        async with self._gh_api(st["access_token"]) as g:
            r = await g.get(f"/repos/{state['repo']}")
            self._gh_check(r, f"Could not open {state['repo']}")
            b = await g.get(f"/repos/{state['repo']}/branches", params={"per_page": 100})
            self._gh_check(b, f"Could not list the branches of {state['repo']}")
        rows = b.json() if isinstance(b.json(), list) else []
        names = [x["name"] for x in rows if isinstance(x, dict) and self._gh_branch_ok(str(x.get("name") or ""))]
        return {"ok": True, "branches": names, "default": self._gh_json(r).get("default_branch") or "main",
                "current": state["branch"]}

    async def _gh_op_push(self, a: dict, st: dict) -> dict:
        """Commit what is uncommitted (with the user's message) and push the project to `branch`, new or existing.
        Never forces: when GitHub has commits this chat lacks, it says so and changes nothing there."""
        chat_id, branch = str(a.get("chat_id") or ""), str(a.get("branch") or "").strip()
        message = " ".join(str(a.get("message") or "").split())[:300] or "Update from Code mode"
        if not self._gh_branch_ok(branch):
            return {"ok": False, "error": f"Not a usable branch name: {branch[:80]}"}
        folder, q = self._gh_folder(chat_id), shlex.quote
        async with self._vm() as c:
            state = await self._gh_linked(c, chat_id)
            # Commit, then be on the target branch: made here when new, moved up when it is behind. A branch of
            # that name with other work on it is left alone.
            code, out = await self._vm_sh(c, (
                f"set -e; git add -A; if ! git diff --cached --quiet; then git commit -q -m {q(message)}; fi; "
                f"git rev-parse -q --verify HEAD >/dev/null || {{ echo EMPTY; exit 0; }}; "
                f"cur=$(git branch --show-current); "
                f"if [ \"$cur\" != {q(branch)} ]; then "
                f"if git show-ref -q --verify refs/heads/{q(branch)}; then "
                f"git merge-base --is-ancestor {q(branch)} HEAD || {{ echo DIVERGED; exit 0; }}; "
                f"git switch -q -C {q(branch)}; "
                f"else git switch -q -c {q(branch)}; echo \"CREATED $cur\"; fi; fi; echo READY"), cwd=folder, timeout=120)
            words = out.split()
            if code or "READY" not in words:
                if "EMPTY" in words:
                    return {"ok": False, "error": "Nothing to push yet: the project has no files."}
                if "DIVERGED" in words:
                    return {"ok": False, "error": f"This project already has a branch called {branch} with other work "
                                                  f"on it. Pick another name."}
                return {"ok": False, "error": f"Could not prepare the push: {self._last_line(out)}"}
            made = re.search(r"^CREATED (\S*)$", out, re.M)  # the branch was made for this push, from that one
            async with self._gh_key(c, st, state["repo"], read_only=False) as env:
                code, out = await self._gh_git(c, env, f"push -q -u origin {q(branch)}", cwd=folder)
            if code:
                if made and self._gh_branch_ok(made.group(1)):  # put the project back on the branch it was on
                    await self._vm_sh(c, f"git switch -q {q(made.group(1))} && git branch -D {q(branch)}", cwd=folder)
                if any(w in out for w in ("non-fast-forward", "fetch first", "[rejected]")):
                    return {"ok": False, "error": f"GitHub has commits on {branch} that this chat does not have. Pull "
                                                  f"first, or push to a new branch."}
                return {"ok": False, "error": f"Could not push: {self._last_line(out)}"}
            state = await self._gh_chat_state(c, chat_id)
        web = self.valves.GITHUB_WEB.rstrip("/")
        return {**self._gh_public(state), "pushed": branch,
                "url": f"{web}/{state['repo']}/tree/{urllib.parse.quote(branch)}"}

    async def _gh_op_history(self, a: dict, st: dict) -> dict:
        """The last 40 versions (commits) of the chat's branch, newest first; pushed = GitHub has it."""
        chat_id = str(a.get("chat_id") or "")
        async with self._vm() as c:
            state = await self._gh_linked(c, chat_id)
            code, out = await self._vm_sh(c, "git log -n 40 --format='%H%x1f%s%x1f%ct%x1f%an%x1e' 2>/dev/null; echo; "
                                             "echo ---; git rev-list -n 400 HEAD --not --remotes=origin 2>/dev/null; "
                                             "true", cwd=self._gh_folder(chat_id))
        log, _, rest = out.partition("\n---")
        local, commits = set(rest.split()), []
        for rec in log.split("\x1e"):
            f = rec.strip("\n").split("\x1f")
            if len(f) == 4 and self.GH_SHA_RE.fullmatch(f[0]):
                commits.append({"sha": f[0], "short": f[0][:7], "subject": f[1][:200], "author": f[3][:80],
                                "time": int(f[2]) if f[2].isdigit() else 0, "pushed": f[0] not in local})
        return {**self._gh_public(state), "commits": commits}

    async def _gh_op_rollback(self, a: dict, st: dict) -> dict:
        """Make the project's files what they were at an earlier version, as a NEW commit on top: history is never
        rewritten, so nothing is lost and no force-push is ever needed. Uncommitted work is committed first."""
        chat_id, sha = str(a.get("chat_id") or ""), str(a.get("sha") or "")
        if not self.GH_SHA_RE.fullmatch(sha):
            return {"ok": False, "error": "Not a version id."}
        async with self._vm() as c:
            await self._gh_linked(c, chat_id)
            code, out = await self._vm_sh(c, (
                f"set -e; full=$(git rev-parse -q --verify {shlex.quote(sha + '^{commit}')}) || {{ echo UNKNOWN; exit 0; }}; "
                f"git merge-base --is-ancestor \"$full\" HEAD || {{ echo NOTMINE; exit 0; }}; "
                f"git add -A; if ! git diff --cached --quiet; then "
                f"git commit -q -m 'Work in progress, saved before rolling back'; echo SAVED; fi; "
                f"if git diff --quiet \"$full\" HEAD; then echo SAME; exit 0; fi; "
                f"git read-tree -u --reset \"$full\"; "
                f"git log -1 --format='Roll back to %h: %s' \"$full\" | git commit -q -F -; echo DONE"),
                cwd=self._gh_folder(chat_id), timeout=120)
            words = out.split()
            if "UNKNOWN" in words:
                return {"ok": False, "error": "This project has no such version."}
            if "NOTMINE" in words:
                return {"ok": False, "error": "That version is not part of this branch's history."}
            if code or not ("DONE" in words or "SAME" in words):
                return {"ok": False, "error": f"Could not roll back: {self._last_line(out)}"}
            if "DONE" in words:
                await self._gh_note(c, chat_id, f"The user rolled the project back to an earlier version ({sha[:7]}) "
                                                f"with the GitHub button. The files changed: read them again before "
                                                f"editing.")
            state = await self._gh_chat_state(c, chat_id)
        return {**self._gh_public(state), "rolled": sha[:7], "same": "SAME" in words, "saved": "SAVED" in words}

    # --- a name for the version, from the small model (pipe 0.12.1)

    GH_MESSAGE_SYSTEM = ("You name a saved version of a software project, like a git commit message. Reply with ONE "
                         "short line, at most 70 characters, that says what changed, starting with a verb (Add, Fix, "
                         "Update, Remove...). No quotes, no explanation, nothing else.")

    async def _task_thinking(self, model_id: str) -> list:
        """The thinking levels the router says that model has: ["none", "on"], ["low", "medium", "high"], or [] when
        it does not think or nothing is known (the gateway's /v1/models, slerm.thinking)."""
        url = self.valves.TASK_ROUTER_MODELS
        if not url:
            return []
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(url)
            for m in r.json().get("data") or []:
                if m.get("id") == model_id.split(".", 1)[-1]:
                    levels = ((m.get("slerm") or {}).get("thinking") or {}).get("levels") or []
                    return [x for x in levels if isinstance(x, str)]
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        return []

    @staticmethod
    def _gh_model_form(model: str, levels: list, system: str, prompt: str) -> dict:
        """The request for a one-line answer. A model that thinks first spends a short answer's whole allowance on
        the thinking and returns nothing (measured 2026-10-12 on Qwen3.6: 300 tokens, empty answer), so thinking is
        turned off where the model allows it; where it does not, the least it offers, with room for it."""
        form = {"model": model, "stream": False, "temperature": 0.2, "max_tokens": 120,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if "none" in levels:
            form["reasoning_effort"] = "none"
        elif levels:
            form["reasoning_effort"] = next((x for x in ("minimal", "low") if x in levels), levels[0])
            form["max_tokens"] = 900
        return form

    async def _gh_ask_model(self, request, user: dict, system: str, prompt: str) -> str:
        """One short answer from the model that writes chat titles and suggestions: whatever the router has loaded,
        else the small CPU model beside it (_task_model), so asking never loads or swaps a model."""
        from open_webui.models.users import Users
        from open_webui.utils.chat import generate_chat_completion

        model = await self._task_model()
        form = self._gh_model_form(model, await self._task_thinking(model), system, prompt)
        res = await generate_chat_completion(request, form, Users.get_user_by_id(user["id"]), bypass_filter=True)
        if hasattr(res, "body"):
            res = json.loads(res.body.decode() or "{}")
        return res["choices"][0]["message"].get("content") or ""

    @staticmethod
    def _gh_clean_message(text: str) -> str:
        """The model's answer as one usable line: no thinking, quotes, markdown, "Commit message:" or chatter."""
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
        for line in text.splitlines():
            line = line.strip().strip("`*\"'# ").strip()
            line = re.sub(r"^(commit message|message|subject|title|version name)\s*[:\-]\s*", "", line, flags=re.I)
            line = line.strip("`*\"' ").strip()
            if len(line) >= 3 and not re.match(r"(here|sure|okay|ok|certainly|of course)\b", line, re.I):
                return line[:100].rstrip(" .")
        return ""

    @staticmethod
    def _gh_plain_summary(status: str) -> str:
        """A name put together from `git status --porcelain` alone, for when no model answers."""
        groups: dict = {"Add": [], "Update": [], "Remove": []}
        for row in status.splitlines():
            if len(row) < 4:
                continue
            code, name = row[:2], row[3:].split(" -> ")[-1].strip().strip('"').rstrip("/").rsplit("/", 1)[-1]
            groups["Add" if "?" in code or "A" in code else "Remove" if "D" in code else "Update"].append(name)
        parts = [f"{verb} {', '.join(names[:3])}" + (f" and {len(names) - 3} more" if len(names) > 3 else "")
                 for verb, names in groups.items() if names]
        return "; ".join(parts)[:100] or "Update the project"

    async def _gh_op_suggest_message(self, a: dict, st: dict) -> dict:
        """A one-line name for the version a push would save, written by the small model from the changed files and
        a short piece of the diff; put together from the file names when the model has nothing. The user edits it."""
        chat_id = str(a.get("chat_id") or "")
        if not self.GH_CHAT_RE.fullmatch(chat_id):
            return {"ok": False, "error": "Not a chat id."}
        async with self._vm() as c:
            code, out = await self._vm_sh(c, "git status --porcelain | head -n 40; echo =====SPLIT=====; "
                                             "git diff --stat HEAD 2>/dev/null | tail -n 12; echo =====SPLIT=====; "
                                             "git diff HEAD 2>/dev/null | head -c 6000; true", cwd=self._gh_folder(chat_id))
        if code == VM_NO_FOLDER:
            return {"ok": False, "error": "This chat has no project yet."}
        status, stat, diff = (out.split("=====SPLIT=====") + ["", ""])[:3]
        if not status.strip():
            return {"ok": False, "error": "There are no changes to describe."}
        cut = diff.strip()[:1800]
        prompt = (f"Changed files (M = changed, ?? or A = new, D = removed):\n{status.strip()[:900]}\n\n"
                  + (f"Size of the changes:\n{stat.strip()[:500]}\n\n" if stat.strip() else "")
                  + (f"Part of the changes:\n{cut[:cut.rfind(chr(10))] if chr(10) in cut else cut}\n\n" if cut else "")
                  + "Name this version in one short line.")
        try:
            text = await asyncio.wait_for(self._gh_ask_model(a.get("__request__"), a.get("__user__") or {},
                                                             self.GH_MESSAGE_SYSTEM, prompt), 60)
        except Exception:  # noqa: BLE001 - no model, a busy or slow one, no Open WebUI (tests): the plain summary will do
            text = ""
        message = self._gh_clean_message(text)
        return {"ok": True, "message": message or self._gh_plain_summary(status), "from_model": bool(message)}
