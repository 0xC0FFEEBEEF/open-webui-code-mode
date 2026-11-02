"""
title: Desktop and Code
author: 0xC0FFEEBEEF
description: Gives any chat model two abilities - use the sandbox desktop (models that can't see hand the job to the computer-agent model, which reports back), and switch the chat to Code mode to build what was discussed.
version: 0.2.2
"""
import json
import re
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

# Router models with an image projector (mmproj in slerm's models.ini, or the STRATA job's --mmproj): they
# can see screenshots. Keep in step with VISION_MODELS in /etc/code-mode/desktop.env (Code mode's list).
VISION = ('bonsai-27b,gemma-3-4b-it-q4km,gemma4-26b-a4b,holo-3.1-9b,holo4-35b-a3b,'
          'nemotron-3.5-lightning-30b-a3b,ornith-1.5-35b-a3b,ornith-1.5-9b,qwen3.5-9b,qwen3.6-35b-a3b-ud-q4kxl,'
          'qwen3.6-35b-a3b-ud-q5kxl,qwen3.8-27b,qwen3.8-27b-turbo,qwen3.8-flash-next-coder,'
          'STRATA-qwen3.8-flash-next-coder,STRATA-qwen3.8-flash-next-iq2xs,STRATA-qwen3.8-flash-next-iq3xxs,'
          'ui-venus-2-9b')


def chat_model_id(model: Optional[dict], metadata: Optional[dict]) -> str:
    """The chat's own model. Open WebUI hands tools its task model as __model__ (llama3.2:1b here), so a
    chat on Ornith looked blind and holo4 took over the desktop; the request metadata has the real one."""
    m = (metadata or {}).get("model")
    if isinstance(m, dict) and m.get("id"):
        return m["id"]
    if isinstance(m, str) and m:
        return m
    return (model or {}).get("id", "")


class Tools:
    class Valves(BaseModel):
        COMPUTER_AGENT_MODEL: str = Field("slerm-turing.holo4-35b-a3b",
                                          description="Drives the desktop for chat models that can't see")
        VISION_MODELS: str = Field(VISION, description="Models (without the connection prefix) that can see; "
                                                       "they drive the desktop themselves")
        FIXER_URL: str = Field("http://ai-sandbox:4098", description="code-fixer on the AI sandbox VM (API + live viewer)")
        FIXER_TOKEN: str = Field("", description="FIXER_TOKEN from /etc/code-mode/fixer.env on the VM")

    def __init__(self):
        self.valves = self.Valves()

    async def use_computer(self, task: str, __user__: dict = None, __request__=None, __model__: dict = None,
                           __messages__: list = None, __metadata__: dict = None,
                           __event_emitter__: Callable[[dict], Awaitable[Any]] = None) -> str:
        """
        Use a real computer - a desktop with Google Chrome that the user watches live in this chat - to do a
        task on the web or to look at and test a web page: open pages, click, type, read what is on screen.
        Returns a report of what was done and seen. Describe the whole task in plain words, for example
        "open http://localhost:8000 and check that every button works".
        :param task: What to do on the computer, in plain words.
        """
        from open_webui.functions import get_function_module_by_id

        current = chat_model_id(__model__, __metadata__)
        name = current.split(".", 1)[-1]
        vision = {m.strip() for m in self.valves.VISION_MODELS.split(",") if m.strip()}
        driver = current if name in vision else self.valves.COMPUTER_AGENT_MODEL
        if __event_emitter__ and driver != current:
            await __event_emitter__({"type": "status", "data": {
                "description": f"{name or 'This model'} can't see, so {driver.split('.', 1)[-1]} is using the computer",
                "done": False}})
        agent = get_function_module_by_id(__request__, "computer_agent")
        context = [{"role": m["role"], "content": m.get("content", "")} for m in (__messages__ or [])
                   if m.get("role") in ("user", "assistant")][-6:]
        result = await agent.pipe({"model": f"computer_agent.{driver}",
                                   "messages": context + [{"role": "user", "content": task}]},
                                  __user__=__user__, __request__=__request__, __event_emitter__=__event_emitter__,
                                  __metadata__=__metadata__)
        report = re.sub(r"<details.*?</details>", "", str(result or ""), flags=re.S).strip()
        who = "you" if driver == current else driver.split(".", 1)[-1]
        return f"The computer task was done by {who}. Its report:\n\n{report or '(no report)'}"

    async def report_bug(self, title: str, details: str, __model__: dict = None, __messages__: list = None,
                         __metadata__: dict = None, __event_emitter__: Callable[[dict], Awaitable[Any]] = None,
                         __event_call__: Callable[[dict], Awaitable[Any]] = None) -> str:
        """
        Report a broken tool to the fixer: the desktop, the coding agent, the preview panel or Open WebUI itself
        erroring or not doing what it should. Never for what an app or website shows - wrong, old or demo data,
        design, missing features: deal with that in the chat or tell the user. The chat pauses while the fixer
        investigates with this chat's context and the service logs, fixes the tool if it can, and reports back;
        the user watches it live. Then carry on with your task, retrying what failed. Not for questions.
        :param title: One line: which tool is broken.
        :param details: What you did, what you expected, what happened instead (exact errors), and anything you tried.
        """
        import httpx

        v = self.valves
        if not (v.FIXER_URL and v.FIXER_TOKEN):
            return "The fixer isn't configured (FIXER_URL / FIXER_TOKEN). Tell the user what is broken instead."

        async def status(text: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": text, "done": done}})

        chat_id = (__metadata__ or {}).get("chat_id") or "unknown"
        context = [{"role": m["role"], "content": m["content"] if isinstance(m.get("content"), str) else str(m.get("content"))}
                   for m in (__messages__ or []) if m.get("role") in ("user", "assistant")][-12:]
        base = v.FIXER_URL.rstrip("/")
        headers = {"Authorization": f"Bearer {v.FIXER_TOKEN}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=90)) as http:
            r = await http.post(f"{base}/api/report", headers=headers, json={
                "chat_id": re.sub(r"[^A-Za-z0-9_-]", "", chat_id)[:64] or "unknown", "source": "chat",
                "reporter_model": chat_model_id(__model__, __metadata__), "title": title[:200], "details": details[:20000],
                "context": context})
            if r.status_code != 201:
                return f"The fixer refused the report (HTTP {r.status_code}): {r.text[:300]}"
            incident = r.json()
            view = base + incident["view"]
            if __event_call__:  # open the Fixer window next to the chat
                js = ("try { return (window.__codeToggle && window.__codeToggle.openFixer) ? "
                      f"window.__codeToggle.openFixer({json.dumps(view)}) : 'missing'; }} catch (e) {{ return 'error'; }}")
                try:
                    await __event_call__({"type": "execute", "data": {"code": js}})
                except Exception:
                    pass
            record, waited = {}, 0
            while True:  # the chat stays paused until the fixer is done
                await status(f"Paused - Ornith is investigating: {title[:60]} ({waited // 60}m {waited % 60:02d}s)")
                w = await http.get(f"{base}/api/incidents/{incident['id']}/wait", headers=headers,
                                   params={"timeout": 60})
                record = w.json() if w.status_code == 200 else {}
                if record.get("status") == "done" or waited > 3600:
                    break
                waited += 60
        result = record  # the fixer's incident record is flat: verdict, summary, root_cause, changed, for_the_chat
        verdict = result.get("verdict") or "unknown"
        await status(f"Fixer: {verdict.replace('_', ' ')} - resuming", done=True)
        changed = "; ".join(f"{c.get('path')}: {c.get('why')}" for c in result.get("changed") or []) or "nothing"
        return (f"The fixer's verdict: {verdict}.\nSummary: {result.get('summary', '')}\nRoot cause: "
                f"{result.get('root_cause', '')}\nChanged: {changed}\nNext: {result.get('for_the_chat', '')}\n"
                "Continue the user's task now, retrying what failed with the fix in place.")

    async def start_coding(self, task: str, __event_call__: Callable[[dict], Awaitable[Any]] = None) -> str:
        """
        Switch this chat to Code mode and hand the task to the coding agent, which writes the files, runs and
        tests them on its own machine, with a live preview the user can watch. Use it when the user wants
        something built or changed (a website, a script, an app), not just explained. Put everything the coder
        needs to know - what was agreed in this conversation - into the task.
        :param task: What to build, with the details agreed in the conversation.
        """
        if not __event_call__:
            return "This chat has no browser attached, so it couldn't switch. Ask the user to flip the Chat | Code toggle."
        js = ("try { return (window.__codeToggle && window.__codeToggle.startCoding) ? "
              f"window.__codeToggle.startCoding({json.dumps(task)}) : 'missing'; }} "
              "catch (e) { return 'error: ' + e; }")
        answer = await __event_call__({"type": "execute", "data": {"code": js}})
        if answer != "ok":
            return (f"Couldn't switch to Code mode ({answer}). Ask the user to flip the Chat | Code toggle at the "
                    "top to Code and send the task again.")
        return ("Switched this chat to Code mode. The coding agent starts on the task as soon as this reply ends. "
                "Tell the user that in one short sentence and stop.")
