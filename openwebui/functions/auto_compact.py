"""
title: Auto Compact
author: slerm
version: 2.0
description: When a slerm chat nears its model's context, the chat's own model summarises the older turns. The summary is kept per chat and reused, and the page's context ring is told how full the chat is.
"""

import asyncio
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

# The summary request goes to the same model as the chat, after the conversation itself, so the router can reuse
# the conversation it already holds instead of reading it again.
SUMMARY_PROMPT = (
    "Pause here. Write a summary of the whole conversation above (including any earlier summary in it). It will "
    "replace those messages, and the conversation will continue from your summary alone. Keep every concrete "
    "fact, decision, name, number, file name, code identifier, error and open question. Keep code that is still "
    "needed word for word. Dense notes, no commentary, no greeting. Answer with the summary only."
)
CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _text_tokens(content) -> int:
    # ~4 characters per token is close enough to decide when to compact. The ring shows the reply's exact count.
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        n = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                n += len(part.get("text") or "") // 4
            elif part.get("type") in ("image_url", "input_image"):
                n += 800
        return n
    return 0


def _tokens(messages: list) -> int:
    return sum(_text_tokens(m.get("content")) + 4 for m in messages)


def _fingerprint(messages: list) -> str:
    h = hashlib.sha1()
    for m in messages:
        h.update(json.dumps([m.get("role"), m.get("content")], sort_keys=True, default=str).encode())
    return h.hexdigest()


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(default=True, description="Turn auto-compaction on or off.")
        trigger_pct: int = Field(
            default=75,
            description="Compact once the chat reaches this percent of its model's context (from the slerm gateway).",
        )
        keep_recent: int = Field(default=6, description="How many of the most recent messages are never summarised.")
        gateway_url: str = Field(
            default="http://slerm-head:8090/v1",
            description="The slerm gateway: model contexts come from its /models, and summaries are written there by the chat's own model.",
        )
        model_prefixes: str = Field(
            default="slerm-turing.",
            description="Comma-separated Open WebUI model id prefixes that are slerm models (the prefix is dropped for the gateway). Other models are left alone.",
        )
        skip_model_prefixes: str = Field(
            default="code_mode.,computer_agent.",
            description="Comma-separated model id prefixes this filter never touches (pipes that keep their own context).",
        )
        summary_max_tokens: int = Field(default=2048, description="Longest summary the model may write.")
        timeout: int = Field(
            default=900,
            description="Longest a summary may take, seconds. The reply waits for it; Open WebUI itself keeps running.",
        )
        notify: bool = Field(default=True, description="Show a status line while and after compacting.")

    def __init__(self):
        self.valves = self.Valves()
        self._models = {"at": 0.0, "map": {}}

    # ------------------------------------------------------------------ helpers
    def _state_dir(self) -> Path:
        base = Path(os.environ.get("DATA_DIR") or Path.home() / ".open-webui")
        d = base / "auto_compact"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load(self, chat_id: str) -> Optional[dict]:
        if not chat_id or not CHAT_ID_RE.match(chat_id):
            return None
        try:
            return json.loads((self._state_dir() / f"{chat_id}.json").read_text())
        except (OSError, ValueError):
            return None

    def _save(self, chat_id: str, state: dict):
        if not chat_id or not CHAT_ID_RE.match(chat_id):
            return
        p = self._state_dir() / f"{chat_id}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(p)

    def _gateway_models(self) -> dict:
        """{gateway model id: its "slerm" block}, kept for 5 minutes. Only the gateway's model list is read: asking
        the router about one model (/props) makes it load that model."""
        if time.time() - self._models["at"] < 300 and self._models["map"]:
            return self._models["map"]
        url = self.valves.gateway_url.rstrip("/") + "/models"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read()).get("data") or []
        self._models = {"at": time.time(), "map": {m.get("id"): m.get("slerm") or {} for m in data if m.get("id")}}
        return self._models["map"]

    def _summarise(self, gw_model: str, levels: list, messages: list) -> Optional[str]:
        payload = {
            "model": gw_model,
            "messages": messages + [{"role": "user", "content": SUMMARY_PROMPT}],
            "max_tokens": self.valves.summary_max_tokens,
            "temperature": 0.3,
            "stream": False,
        }
        if "none" in levels:  # write the summary straight away, no thinking
            payload["reasoning_effort"] = "none"
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif "low" in levels:
            payload["reasoning_effort"] = "low"
        req = urllib.request.Request(
            self.valves.gateway_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.valves.timeout) as r:
            d = json.loads(r.read())
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        return (msg.get("content") or "").strip() or None

    async def _emit(self, emitter, event: dict):
        if emitter:
            try:
                await emitter(event)
            except Exception:
                pass

    async def _status(self, emitter, text: str, done: bool):
        if self.valves.notify:
            await self._emit(emitter, {"type": "status", "data": {"action": "slerm_compact", "description": text, "done": done}})

    async def _context(self, emitter, info: dict):
        # Live (not saved) for the page's ring, then one hidden status saved with the reply so the ring is right
        # after a reload. A hidden status hides the status line when it is the last one, so visible ones go after.
        await self._emit(emitter, {"type": "slerm:context", "data": info})
        await self._emit(emitter, {"type": "status", "data": dict(info, action="slerm_context", description="",
                                                                    hidden=True, done=True)})

    # -------------------------------------------------------------------- inlet
    async def inlet(self, body: dict, __event_emitter__=None, __user__: Optional[dict] = None,
                    __metadata__: Optional[dict] = None) -> dict:
        # Anything unexpected leaves the conversation untouched: a filter that throws breaks the chat.
        try:
            if not self.valves.enabled:
                return body
            model = str(body.get("model") or "")
            if any(model.startswith(p.strip()) for p in self.valves.skip_model_prefixes.split(",") if p.strip()):
                return body
            prefix = next((p.strip() for p in self.valves.model_prefixes.split(",")
                           if p.strip() and model.startswith(p.strip())), None)
            if not prefix:
                return body
            gw_model = model[len(prefix):]
            messages = body.get("messages") or []
            if not messages:
                return body
            try:
                info = (await asyncio.to_thread(self._gateway_models)).get(gw_model) or {}
            except Exception:
                info = {}
            ctx = int(info.get("ctx") or 0)
            levels = list((info.get("thinking") or {}).get("levels") or [])
            chat_id = str((__metadata__ or {}).get("chat_id") or body.get("chat_id") or "")
            trigger = ctx * self.valves.trigger_pct // 100 if ctx else 0

            head, rest = [], messages
            if messages[0].get("role") == "system":
                head, rest = [messages[0]], messages[1:]

            # A summary kept from an earlier turn still applies while the messages it covers are unchanged.
            saved = self._load(chat_id)
            covered, summary = 0, None
            if saved and 0 < saved.get("n", 0) <= len(rest) and saved.get("fp") == _fingerprint(rest[: saved["n"]]):
                covered, summary = saved["n"], saved.get("summary")

            def view(n: int, s: Optional[str]) -> list:
                note = [{"role": "system", "content": "Summary of the earlier conversation (auto-compacted):\n" + s}] if s else []
                return head + note + rest[n:]

            current = view(covered, summary)
            used = _tokens(current)
            if not trigger or used < trigger or len(rest) - covered <= self.valves.keep_recent:
                body["messages"] = current
                if ctx:
                    await self._context(__event_emitter__, {"used": used, "limit": ctx, "compact_at": trigger,
                                                            "source": "chat", "estimate": True})
                return body

            # Compact: the model summarises everything before the last keep_recent messages.
            n = len(rest) - self.valves.keep_recent
            older = view(covered, summary)[: len(head) + (1 if summary else 0) + (n - covered)]
            await self._status(__event_emitter__, "Compacting %d earlier messages…" % (n - covered), False)
            await self._emit(__event_emitter__, {"type": "slerm:context", "data": {
                "phase": "compacting", "used": used, "limit": ctx, "compact_at": trigger, "source": "chat"}})
            t0 = time.monotonic()
            try:
                new_summary = await asyncio.to_thread(self._summarise, gw_model, levels, older)
            except Exception:
                new_summary = None
            if not new_summary:
                body["messages"] = current
                await self._emit(__event_emitter__, {"type": "slerm:context", "data": {
                    "phase": "compact_failed", "used": used, "limit": ctx, "compact_at": trigger, "source": "chat"}})
                await self._context(__event_emitter__, {"used": used, "limit": ctx, "compact_at": trigger,
                                                        "source": "chat", "estimate": True})
                await self._status(__event_emitter__, "Compaction skipped: the model did not write a summary.", True)
                return body

            self._save(chat_id, {"n": n, "fp": _fingerprint(rest[:n]), "summary": new_summary, "t": int(time.time())})
            body["messages"] = view(n, new_summary)
            after = _tokens(body["messages"])
            await self._emit(__event_emitter__, {"type": "slerm:context", "data": {
                "phase": "compacted", "before": used, "used": after, "limit": ctx, "compact_at": trigger, "source": "chat"}})
            await self._context(__event_emitter__, {"used": after, "limit": ctx, "compact_at": trigger,
                                                    "source": "chat", "estimate": True, "compacted_from": used})
            await self._status(__event_emitter__, "Compacted %d messages: ~%dk → ~%dk tokens (%ds)"
                               % (n - covered, used // 1000, after // 1000, time.monotonic() - t0), True)
        except Exception:
            return body
        return body
