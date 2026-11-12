import httpx
import pytest

from core.connectors import CircuitBreaker, Connector, NotFound, SystemUnavailable, TTLCache


def connector(handler, retries=2):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Connector(
        "payments", "http://payments", client, TTLCache(), timeout_s=0.5, retries=retries
    )


async def test_retry_recovers_transient_failure():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, json={"ok": True})

    assert await connector(handler).get("/x", customer_id="C1") == {"ok": True}
    assert calls["n"] == 2


async def test_gives_up_after_retries():
    c = connector(lambda r: httpx.Response(503))
    with pytest.raises(SystemUnavailable):
        await c.get("/x", customer_id="C1")


async def test_breaker_opens_after_repeated_failures_and_fails_fast():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    c = connector(handler, retries=0)
    for _ in range(5):
        with pytest.raises(SystemUnavailable):
            await c.get("/x", customer_id="C1")
    assert c.breaker.state == "open"
    before = calls["n"]
    with pytest.raises(SystemUnavailable, match="circuit breaker"):
        await c.get("/x", customer_id="C1")
    assert calls["n"] == before  # no request sent while open


def test_breaker_half_opens_after_cooldown():
    b = CircuitBreaker(threshold=1, cooldown_s=0)
    b.failure()
    assert b.state == "half-open" and b.allow()


async def test_cache_is_per_customer():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    c = connector(handler)
    a1 = await c.get("/x", customer_id="C1", cache_ttl_s=60)
    a2 = await c.get("/x", customer_id="C1", cache_ttl_s=60)
    b1 = await c.get("/x", customer_id="C2", cache_ttl_s=60)
    assert a1 == a2 and b1 != a1 and calls["n"] == 2


async def test_not_found_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"detail": "loan not found"})

    with pytest.raises(NotFound):
        await connector(handler).get("/x", customer_id="C1")
    assert calls["n"] == 1


async def test_post_without_idempotency_key_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(SystemUnavailable):
        await connector(handler).post("/requests", json={})
    assert calls["n"] == 1
