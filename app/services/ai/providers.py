"""AI provider chain with circuit breakers.

Order: OpenAI-compatible gateway (primary, Gemma 3 on llama.cpp) → vLLM → Gemma 3 failsafe → Claude → ChatGPT.
Every provider is optional except the gateway; unhealthy providers are skipped with exponential back-off.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

log = logging.getLogger("kisansetu.ai")


class AIUnavailable(Exception):
    pass


@dataclass
class AIResult:
    text: str
    provider: str
    model: str
    ms: int


@dataclass
class Provider:
    name: str
    kind: str                      # openai | anthropic
    model: str
    api_key: str
    base_url: str | None = None
    token_param: str = "max_tokens"
    send_temperature: bool = True
    vision: bool = True
    client: Any = None
    failures: int = 0
    open_until: float = 0.0
    last_error: str | None = None
    last_ok: float | None = None
    latency_ms: int | None = None
    calls: int = 0
    stats: dict[str, int] = field(default_factory=lambda: {"ok": 0, "fail": 0})

    def available(self) -> bool:
        return time.monotonic() >= self.open_until

    def record_ok(self, ms: int) -> None:
        self.failures, self.open_until, self.last_ok, self.latency_ms = 0, 0.0, time.time(), ms
        self.stats["ok"] += 1

    def record_fail(self, exc: BaseException) -> None:
        self.stats["fail"] += 1
        self.last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500 and status not in (401, 403, 408, 429):
            return  # a bad request is about that request, not provider health — keep the circuit closed
        self.failures += 1
        if self.failures >= 2:
            self.open_until = time.monotonic() + min(300.0, 10.0 * 2 ** (self.failures - 2))

    def get_client(self) -> Any:
        if self.client is None:
            if self.kind == "anthropic":
                import anthropic

                self.client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=settings.AI_TIMEOUT_SECONDS, max_retries=0)
            else:
                import openai

                self.client = openai.AsyncOpenAI(api_key=self.api_key or "none", base_url=self.base_url,
                                                 timeout=settings.AI_TIMEOUT_SECONDS, max_retries=0)
        return self.client


Image = tuple[bytes, str]  # (data, mime)


def normalize_turns(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Strict user/assistant alternation starting and ending with a user turn (Gemma/Claude templates require it)."""
    out: list[dict[str, str]] = []
    for m in messages:
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": out[-1]["content"] + "\n\n" + content}
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    while out and out[-1]["role"] != "user":
        out.pop()
    return out


def _data_url(img: Image) -> str:
    return f"data:{img[1]};base64,{base64.b64encode(img[0]).decode()}"


class AIRouter:
    def __init__(self) -> None:
        s = settings
        self.providers: list[Provider] = []
        if s.AI_GATEWAY_BASE_URL:
            self.providers.append(Provider("gateway", "openai", s.AI_GATEWAY_MODEL, s.AI_GATEWAY_API_KEY, s.AI_GATEWAY_BASE_URL))
        if s.VLLM_BASE_URL:
            self.providers.append(Provider("vllm", "openai", s.VLLM_MODEL, s.VLLM_API_KEY, s.VLLM_BASE_URL))
        if s.GEMMA3_BASE_URL:
            self.providers.append(Provider("gemma3", "openai", s.GEMMA3_MODEL, s.GEMMA3_API_KEY, s.GEMMA3_BASE_URL))
        if s.ANTHROPIC_API_KEY:
            self.providers.append(Provider("claude", "anthropic", s.CLAUDE_MODEL, s.ANTHROPIC_API_KEY))
        if s.OPENAI_API_KEY:
            self.providers.append(Provider("openai", "openai", s.OPENAI_MODEL, s.OPENAI_API_KEY, None,
                                           token_param="max_completion_tokens", send_temperature=False))

    # ------------------------------------------------------------------ public API
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system: str | None = None,
        images: list[Image] | None = None,
        max_tokens: int = 700,
        temperature: float = 0.3,
        timeout: float | None = None,
    ) -> AIResult:
        errors = []
        messages = normalize_turns(messages)
        if not messages:
            raise AIUnavailable("empty conversation")
        for p in self.providers:
            if not p.available():
                continue
            if images and not p.vision:
                continue
            t0 = time.monotonic()
            limit = timeout or settings.AI_TIMEOUT_SECONDS
            try:
                coro = (self._anthropic(p, messages, system, images, max_tokens, limit) if p.kind == "anthropic"
                        else self._openai(p, messages, system, images, max_tokens, temperature, limit))
                text = await asyncio.wait_for(coro, limit + 5)
                if not text or not text.strip():
                    raise AIUnavailable("empty completion")
                ms = int((time.monotonic() - t0) * 1000)
                p.record_ok(ms)
                return AIResult(text.strip(), p.name, p.model, ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                p.record_fail(exc)
                errors.append(f"{p.name}: {p.last_error}")
                log.warning("AI provider %s failed: %s", p.name, p.last_error)
        raise AIUnavailable("; ".join(errors) or "no AI provider available")

    def status(self) -> list[dict[str, Any]]:
        return [{
            "name": p.name, "model": p.model, "available": p.available(), "failures": p.failures,
            "last_error": p.last_error, "last_ok": p.last_ok, "latency_ms": p.latency_ms, **p.stats,
        } for p in self.providers]

    def warm(self) -> None:
        """Build provider clients (TLS context + connection pool) at startup — doing it lazily blocked the event loop
        for ~0.4 s on the first AI request (found by the load test's loop-lag monitor)."""
        for p in self.providers:
            try:
                p.get_client()
            except Exception as exc:  # a misconfigured optional provider must not stop the app
                log.warning("AI provider %s not initialised: %s", p.name, exc)

    async def ping_gateway(self) -> bool:
        from app.services.http import http

        p = next((x for x in self.providers if x.name == "gateway"), None)
        if not p:
            return False
        try:
            r = await http().get(f"{p.base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {p.api_key}"}, timeout=6)
            ok = r.status_code == 200
        except Exception as exc:
            p.record_fail(exc)
            return False
        if ok and not p.available():
            p.open_until = 0.0
        return ok

    # ------------------------------------------------------------------ providers
    async def _openai(self, p: Provider, messages, system, images, max_tokens, temperature, timeout: float) -> str:
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        for i, m in enumerate(messages):
            if images and i == len(messages) - 1 and m["role"] == "user":
                parts: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}]
                parts += [{"type": "image_url", "image_url": {"url": _data_url(img)}} for img in images]
                msgs.append({"role": "user", "content": parts})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        kwargs: dict[str, Any] = {"model": p.model, "messages": msgs, p.token_param: max_tokens, "timeout": timeout}
        if p.send_temperature:
            kwargs["temperature"] = temperature
        resp = await p.get_client().chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "") if resp.choices else ""

    async def _anthropic(self, p: Provider, messages, system, images, max_tokens, timeout: float) -> str:
        conv: list[dict[str, Any]] = []
        for m in messages:
            role = "assistant" if m["role"] == "assistant" else "user"
            if conv and conv[-1]["role"] == role:
                conv[-1]["content"][0]["text"] += "\n\n" + m["content"]
            else:
                conv.append({"role": role, "content": [{"type": "text", "text": m["content"]}]})
        while conv and conv[0]["role"] != "user":
            conv.pop(0)
        if images and conv:
            for img in images:
                conv[-1]["content"].append({"type": "image", "source": {
                    "type": "base64", "media_type": img[1], "data": base64.b64encode(img[0]).decode()}})
        kwargs: dict[str, Any] = {"model": p.model, "max_tokens": max_tokens, "messages": conv, "timeout": timeout}
        if system:
            kwargs["system"] = system
        resp = await p.get_client().messages.create(**kwargs)
        return "".join(getattr(b, "text", "") for b in resp.content)


router = AIRouter()
