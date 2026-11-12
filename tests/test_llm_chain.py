import httpx
import pytest
from openai import OpenAI

from core.llm import AllProvidersFailed, Provider, ProviderChain, build_chain
from core.settings import Settings

COMPLETION = {
    "id": "x",
    "object": "chat.completion",
    "created": 0,
    "model": "m",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
    ],
}


def fake_client(status: int, body: dict | None = None) -> OpenAI:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json=body or {"error": "x"})
    )
    return OpenAI(
        base_url="https://example.invalid/v1",
        api_key="test",
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )


def provider(name: str, status: int) -> Provider:
    return Provider(
        name, fake_client(status, COMPLETION if status == 200 else None), "big", "small"
    )


def test_falls_back_on_rate_limit():
    chain = ProviderChain([provider("first", 429), provider("second", 200)])
    result = chain.chat(messages=[{"role": "user", "content": "hi"}])
    assert result.provider == "second"
    assert [(a.provider, a.ok) for a in result.attempts] == [("first", False), ("second", True)]
    assert result.attempts[0].error == "RateLimitError"


def test_falls_back_on_server_error():
    chain = ProviderChain([provider("first", 503), provider("second", 200)])
    assert chain.chat(messages=[]).provider == "second"


def test_bad_request_is_not_retried_elsewhere():
    chain = ProviderChain([provider("first", 400), provider("second", 200)])
    with pytest.raises(Exception) as err:
        chain.chat(messages=[])
    assert not isinstance(err.value, AllProvidersFailed)


def test_all_failed_lists_every_attempt():
    chain = ProviderChain([provider("first", 429), provider("second", 503)])
    with pytest.raises(AllProvidersFailed) as err:
        chain.chat(messages=[])
    assert len(err.value.attempts) == 2


def test_role_selects_model():
    chain = ProviderChain([provider("only", 200)])
    assert chain.chat(role="fast", messages=[]).model == "small"
    assert chain.chat(role="planner", messages=[]).model == "big"


def test_build_chain_skips_providers_without_keys():
    s = Settings(
        _env_file=None,
        llm_provider_chain="groq,cerebras,openrouter",
        groq_api_key="k",
        cerebras_api_key="",
        openrouter_api_key="k",
        openrouter_model="",
    )
    assert [p.name for p in build_chain(s).providers] == ["groq"]


async def test_waits_for_recovery_when_all_providers_are_rate_limited(monkeypatch):
    import time as _t

    from core import llm

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)
    chain = ProviderChain([provider("only", 200)])
    chain._cool_until[("only", "big")] = _t.monotonic() + 5
    p = chain.providers[0]
    from openai import AsyncOpenAI

    p.aclient = AsyncOpenAI(
        base_url="https://example.invalid/v1",
        api_key="x",
        max_retries=0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=COMPLETION))
        ),
    )
    r = await chain.achat(messages=[])
    assert r.provider == "only" and slept and 4 < slept[0] <= 5


async def test_retries_once_when_a_cooling_provider_recovers(monkeypatch):
    """The only ready provider fails, but another is cooling down and recovers soon:
    wait for it instead of failing the turn."""
    import time as _t

    from openai import AsyncOpenAI

    from core import llm

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)

    def aclient(status):
        body = COMPLETION if status == 200 else {"error": "x"}
        return AsyncOpenAI(
            base_url="https://example.invalid/v1",
            api_key="x",
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))
            ),
        )

    cooling, failing = provider("cooling", 200), provider("failing", 429)
    cooling.aclient, failing.aclient = aclient(200), aclient(429)
    chain = ProviderChain([cooling, failing], max_wait_s=30)
    chain._cool_until[("cooling", "big")] = _t.monotonic() + 10
    r = await chain.achat(messages=[])
    assert r.provider == "cooling" and any(8 < s <= 10 for s in slept)


async def test_gives_up_when_recovery_is_too_far_away(monkeypatch):
    import time as _t

    from core import llm

    async def fake_sleep(s):
        pass

    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)
    chain = ProviderChain([provider("cooling", 200), provider("failing", 429)], max_wait_s=5)
    chain._cool_until[("cooling", "big")] = _t.monotonic() + 60
    from openai import AsyncOpenAI

    chain.providers[1].aclient = AsyncOpenAI(
        base_url="https://example.invalid/v1",
        api_key="x",
        max_retries=0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": "x"}))
        ),
    )
    with pytest.raises(AllProvidersFailed):
        await chain.achat(messages=[])


def test_provider_renames_and_drops_fields_it_does_not_accept():
    p = Provider(
        "openai",
        None,
        "big",
        "small",
        {"reasoning_effort": "low"},
        rename={"max_tokens": "max_completion_tokens"},
        drop=("temperature",),
    )
    assert p.args({"max_tokens": 600, "temperature": 0, "messages": []}) == {
        "reasoning_effort": "low",
        "max_completion_tokens": 600,
        "messages": [],
    }
