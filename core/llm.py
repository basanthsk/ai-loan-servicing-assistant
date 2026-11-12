"""LLM provider chain over OpenAI-compatible APIs.

Providers are tried in order. A provider is skipped for one call when it is rate
limited, times out, cannot be reached, or returns a server error; the next one
is tried. Errors in the request itself (bad arguments) are raised at once,
because another provider would reject them too.

After a 429 the provider/model pair cools down for the time the provider asks
(Retry-After), so later calls go straight to the next provider instead of
waiting on a request that will fail.

Sync calls (`chat`) are used by scripts; the assistant uses the async calls
(`achat`, `astream`). Streams fail over only before the first chunk arrives.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from core.settings import Settings

log = logging.getLogger(__name__)

MAX_WAIT_S = 20.0  # default longest wait for a rate-limited provider to recover (LLM_MAX_WAIT_S)

FAILOVER_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


@dataclass
class Provider:
    name: str
    client: OpenAI | None
    planner_model: str
    fast_model: str
    # Extra request fields this provider always gets, e.g. low reasoning effort for gpt-oss.
    defaults: dict = field(default_factory=dict)
    aclient: AsyncOpenAI | None = None
    # Request fields this provider names differently (old name -> new name), and fields
    # it rejects. OpenAI's GPT-5 models take max_completion_tokens and only the default
    # temperature.
    rename: dict = field(default_factory=dict)
    drop: tuple = ()

    def model(self, role: str) -> str:
        return self.planner_model if role == "planner" else self.fast_model

    def args(self, kwargs: dict) -> dict:
        merged = {**self.defaults, **kwargs}
        for old, new in self.rename.items():
            if old in merged:
                merged[new] = merged.pop(old)
        for name in self.drop:
            merged.pop(name, None)
        return merged


@dataclass
class Attempt:
    provider: str
    model: str
    ok: bool
    ms: float
    error: str = ""


@dataclass
class ChainResult:
    response: object
    provider: str
    model: str
    attempts: list[Attempt] = field(default_factory=list)


class AllProvidersFailed(RuntimeError):
    def __init__(self, attempts: list[Attempt]):
        self.attempts = attempts
        summary = "; ".join(f"{a.provider}: {a.error}" for a in attempts) or "none available"
        super().__init__(f"All LLM providers failed: {summary}")


@dataclass
class StreamResult:
    """An open stream from whichever provider answered first."""

    provider: str
    model: str
    attempts: list[Attempt]
    _first: object | None
    _rest: AsyncIterator

    async def __aiter__(self):
        if self._first is not None:
            yield self._first
        async for chunk in self._rest:
            yield chunk


def _retry_after(e: Exception) -> float:
    try:
        return min(float(e.response.headers.get("retry-after", 20)), 120.0)
    except Exception:  # noqa: BLE001
        return 20.0


class ProviderChain:
    def __init__(self, providers: list[Provider], max_wait_s: float = MAX_WAIT_S):
        if not providers:
            raise ValueError("ProviderChain needs at least one provider")
        self.providers = providers
        self.max_wait_s = max_wait_s
        self._cool_until: dict[tuple[str, str], float] = {}

    def _cooling(self, p: Provider, model: str) -> bool:
        return self._cool_until.get((p.name, model), 0) > time.monotonic()

    def _fail(self, p: Provider, model: str, e: Exception, t0: float, attempts: list[Attempt]):
        ms = (time.perf_counter() - t0) * 1000
        attempts.append(Attempt(p.name, model, False, ms, type(e).__name__))
        if isinstance(e, RateLimitError):
            self._cool_until[(p.name, model)] = time.monotonic() + _retry_after(e)
        log.warning(
            "LLM provider %s/%s failed with %s; trying next", p.name, model, type(e).__name__
        )

    def _candidates(self, role: str):
        ready = [(p, p.model(role)) for p in self.providers if not self._cooling(p, p.model(role))]
        # If every provider is cooling down, try them anyway rather than fail immediately.
        return ready or [(p, p.model(role)) for p in self.providers]

    def chat(self, *, role: str = "planner", **kwargs) -> ChainResult:
        """Sync call: role "planner" for multi-step reasoning, "fast" for simple turns."""
        attempts: list[Attempt] = []
        for p, model in self._candidates(role):
            t0 = time.perf_counter()
            try:
                response = p.client.chat.completions.create(model=model, **p.args(kwargs))
            except FAILOVER_ERRORS as e:
                self._fail(p, model, e, t0, attempts)
                continue
            attempts.append(Attempt(p.name, model, True, (time.perf_counter() - t0) * 1000))
            return ChainResult(response, p.name, model, attempts)
        raise AllProvidersFailed(attempts)

    async def _wait_if_all_cooling(self, role: str) -> None:
        """When every provider is rate limited, wait for the first to recover (at most
        MAX_WAIT_S) instead of failing at once and cascading into more failures."""
        waits = [
            self._cool_until.get((p.name, p.model(role)), 0) - time.monotonic()
            for p in self.providers
        ]
        if waits and min(waits) > 0:
            await asyncio.sleep(min(min(waits), self.max_wait_s))

    async def _wait_for_recovery(self, role: str) -> bool:
        """Every provider tried has just failed. If one of them is cooling down and
        recovers within max_wait_s, wait for it and report that a retry is worth it."""
        now = time.monotonic()
        waits = [
            w
            for p in self.providers
            if 0 < (w := self._cool_until.get((p.name, p.model(role)), 0) - now) <= self.max_wait_s
        ]
        if not waits:
            return False
        log.warning("All LLM providers failed; retrying in %.0f s when one recovers", min(waits))
        await asyncio.sleep(min(waits))
        return True

    async def achat(self, *, role: str = "planner", **kwargs) -> ChainResult:
        attempts: list[Attempt] = []
        for round_ in range(2):
            await self._wait_if_all_cooling(role)
            for p, model in self._candidates(role):
                t0 = time.perf_counter()
                try:
                    response = await p.aclient.chat.completions.create(
                        model=model, **p.args(kwargs)
                    )
                except FAILOVER_ERRORS as e:
                    self._fail(p, model, e, t0, attempts)
                    continue
                attempts.append(Attempt(p.name, model, True, (time.perf_counter() - t0) * 1000))
                return ChainResult(response, p.name, model, attempts)
            if round_ == 0 and not await self._wait_for_recovery(role):
                break
        raise AllProvidersFailed(attempts)

    async def astream(self, *, role: str = "planner", **kwargs) -> StreamResult:
        attempts: list[Attempt] = []
        for round_ in range(2):
            result = await self._astream_once(role, attempts, kwargs)
            if result is not None:
                return result
            if round_ == 0 and not await self._wait_for_recovery(role):
                break
        raise AllProvidersFailed(attempts)

    async def _astream_once(self, role: str, attempts: list[Attempt], kwargs: dict):
        await self._wait_if_all_cooling(role)
        for p, model in self._candidates(role):
            t0 = time.perf_counter()
            try:
                stream = await p.aclient.chat.completions.create(
                    model=model, stream=True, **p.args(kwargs)
                )
                it = stream.__aiter__()
                try:
                    first = await it.__anext__()
                except StopAsyncIteration:
                    first = None
            except FAILOVER_ERRORS as e:
                self._fail(p, model, e, t0, attempts)
                continue
            attempts.append(Attempt(p.name, model, True, (time.perf_counter() - t0) * 1000))
            return StreamResult(p.name, model, attempts, first, it)
        return None


def build_chain(settings: Settings, timeout: float = 30.0) -> ProviderChain:
    """Build the chain from LLM_PROVIDER_CHAIN, skipping providers without a key."""

    def clients(base_url: str, key: str):
        return (
            OpenAI(base_url=base_url, api_key=key, timeout=timeout, max_retries=0),
            AsyncOpenAI(base_url=base_url, api_key=key, timeout=timeout, max_retries=0),
        )

    # gpt-oss reasons before answering; low effort keeps latency and token use down.
    gpt_oss = {"reasoning_effort": "low"}

    def make(name, base_url, key, planner, fast, defaults=None, **extra):
        sync, async_ = clients(base_url, key)
        return Provider(name, sync, planner, fast, defaults or {}, async_, **extra)

    s = settings
    available = {
        "groq": lambda: (
            make(
                "groq",
                s.groq_base_url,
                s.groq_api_key,
                s.groq_planner_model,
                s.groq_fast_model,
                gpt_oss,
            )
            if s.groq_api_key
            else None
        ),
        # Same Groq key, planning on the fast model: Groq rate limits are per model,
        # so this is a separate token budget when the large model is rate limited.
        "groq-fast": lambda: (
            make(
                "groq-fast",
                s.groq_base_url,
                s.groq_api_key,
                s.groq_fast_model,
                s.groq_fast_model,
                gpt_oss,
            )
            if s.groq_api_key
            else None
        ),
        "cerebras": lambda: (
            make(
                "cerebras",
                s.cerebras_base_url,
                s.cerebras_api_key,
                s.cerebras_planner_model,
                s.cerebras_planner_model,
                gpt_oss,
            )
            if s.cerebras_api_key
            else None
        ),
        # Same Cerebras key, open-weight Qwen: separate (and much higher) free limits.
        "cerebras-qwen": lambda: (
            make(
                "cerebras-qwen",
                s.cerebras_base_url,
                s.cerebras_api_key,
                s.cerebras_qwen_model,
                s.cerebras_qwen_model,
            )
            if s.cerebras_api_key
            else None
        ),
        "openrouter": lambda: (
            make(
                "openrouter",
                s.openrouter_base_url,
                s.openrouter_api_key,
                s.openrouter_model,
                s.openrouter_model,
            )
            if s.openrouter_api_key and s.openrouter_model
            else None
        ),
        # Paid, closed models; used only when listed in LLM_PROVIDER_CHAIN.
        "openai": lambda: (
            make(
                "openai",
                s.openai_base_url,
                s.openai_api_key,
                s.openai_planner_model,
                s.openai_fast_model,
                {"reasoning_effort": "low"},
                rename={"max_tokens": "max_completion_tokens"},
                drop=("temperature",),
            )
            if s.openai_api_key
            else None
        ),
        "ollama": lambda: make(
            "ollama", s.ollama_base_url, "ollama", s.ollama_model, s.ollama_fast_model
        ),
        "stub": lambda: make("stub", s.stub_base_url, "stub", "stub-planner", "stub-fast"),
    }
    providers = []
    for name in s.provider_chain:
        factory = available.get(name)
        if factory is None:
            raise ValueError(f"Unknown provider {name!r} in LLM_PROVIDER_CHAIN")
        provider = factory()
        if provider is None:
            log.warning("Skipping provider %s: its key or model is not configured", name)
            continue
        providers.append(provider)
    if not providers:
        log.warning("No LLM provider has a key configured; falling back to local Ollama")
        providers.append(available["ollama"]())
    return ProviderChain(providers, settings.llm_max_wait_s)
