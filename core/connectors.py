"""Connector SDK: the only way the assistant talks to enterprise systems.

Every call gets, from one place:
- a timeout per system
- retries with exponential backoff and jitter, for reads only
- a circuit breaker per system: after repeated failures calls fail fast for a
  while instead of waiting on a system that is down
- a short per-customer cache for reads
- a trace span (system, path, latency, outcome)

Failures surface as SystemUnavailable, which the orchestrator turns into a
partial answer that says what could not be reached.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

from core.settings import Settings
from core.tracing import span


class SystemUnavailable(RuntimeError):
    def __init__(self, system: str, reason: str):
        self.system, self.reason = system, reason
        super().__init__(f"{system} unavailable: {reason}")


class NotFound(RuntimeError):
    pass


@dataclass
class CircuitBreaker:
    """Opens after `threshold` failures within `window_s`; half-opens after `cooldown_s`."""

    threshold: int = 5
    window_s: float = 30.0
    cooldown_s: float = 15.0
    failures: list[float] = field(default_factory=list)
    opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if time.monotonic() - self.opened_at >= self.cooldown_s else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        self.failures.clear()
        self.opened_at = None

    def failure(self) -> None:
        now = time.monotonic()
        if self.state == "half-open":
            self.opened_at = now  # probe failed: stay open
            return
        self.failures = [t for t in self.failures if now - t < self.window_s] + [now]
        if len(self.failures) >= self.threshold:
            self.opened_at = now


class TTLCache:
    def __init__(self, max_items: int = 5000):
        self._data: dict[tuple, tuple[float, object]] = {}
        self.max_items = max_items

    def get(self, key: tuple):
        hit = self._data.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        self._data.pop(key, None)
        return None

    def put(self, key: tuple, value, ttl_s: float) -> None:
        if len(self._data) >= self.max_items:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + ttl_s, value)

    def clear_customer(self, customer_id: str) -> None:
        for k in [k for k in self._data if k[0] == customer_id]:
            self._data.pop(k, None)


DEFAULT_TIMEOUTS_S = {
    "crm": 1.0,
    "loans": 1.5,
    "payments": 2.0,
    "policy": 1.0,
    "serviceops": 3.0,
    "cards": 1.5,
}


class Connector:
    def __init__(
        self,
        system: str,
        base_url: str,
        client: httpx.AsyncClient,
        cache: TTLCache,
        timeout_s: float = 2.0,
        retries: int = 2,
    ):
        self.system, self.base_url = system, base_url.rstrip("/")
        self.client, self.cache = client, cache
        self.timeout_s, self.retries = timeout_s, retries
        self.breaker = CircuitBreaker()

    async def get(
        self, path: str, *, customer_id: str, params: dict | None = None, cache_ttl_s: float = 0
    ) -> object:
        key = (customer_id, self.system, path, tuple(sorted((params or {}).items())))
        if cache_ttl_s:
            hit = self.cache.get(key)
            if hit is not None:
                with span(f"{self.system} {path}", "backend", system=self.system, cached=True):
                    return hit
        value = await self._request("GET", path, params=params, retries=self.retries)
        if cache_ttl_s:
            self.cache.put(key, value, cache_ttl_s)
        return value

    async def post(
        self,
        path: str,
        *,
        json: dict,
        idempotency_key: str | None = None,
        params: dict | None = None,
    ) -> object:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        # Writes are retried only when an idempotency key makes the retry safe.
        retries = self.retries if idempotency_key else 0
        return await self._request(
            "POST", path, params=params, json=json, headers=headers, retries=retries
        )

    async def _request(self, method: str, path: str, retries: int, **kw) -> object:
        with span(f"{self.system} {path}", "backend", system=self.system, method=method) as s:
            if not self.breaker.allow():
                s.attrs["circuit"] = "open"
                raise SystemUnavailable(self.system, "circuit breaker open after repeated failures")
            last_error = "unknown error"
            for attempt in range(retries + 1):
                if attempt:
                    await asyncio.sleep(
                        min(0.1 * 2 ** (attempt - 1), 0.8) * (0.5 + random.random())
                    )
                try:
                    r = await self.client.request(
                        method, self.base_url + path, timeout=self.timeout_s, **kw
                    )
                except httpx.TimeoutException:
                    last_error = f"timed out after {self.timeout_s:.1f}s"
                    self.breaker.failure()
                    continue
                except httpx.TransportError as e:
                    last_error = f"connection failed ({type(e).__name__})"
                    self.breaker.failure()
                    continue
                if r.status_code == 404:
                    self.breaker.success()
                    raise NotFound(r.json().get("detail", "not found"))
                if r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                    self.breaker.failure()
                    continue
                if r.status_code >= 400:
                    raise ValueError(f"{self.system} rejected the request: {r.text[:200]}")
                self.breaker.success()
                s.attrs["attempts"] = attempt + 1
                return r.json()
            s.attrs["attempts"] = retries + 1
            raise SystemUnavailable(self.system, last_error)


class ConnectorHub:
    """One connector per system, shared by all conversations."""

    def __init__(self, settings: Settings, systems: list[str] | None = None):
        self.cache = TTLCache()
        self.client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50), trust_env=False
        )
        names = systems or list(DEFAULT_TIMEOUTS_S)
        self.connectors = {
            n: Connector(
                n, settings.system_url(n), self.client, self.cache, DEFAULT_TIMEOUTS_S.get(n, 2.0)
            )
            for n in names
        }

    def __getitem__(self, system: str) -> Connector:
        return self.connectors[system]

    def breaker_states(self) -> dict[str, str]:
        return {n: c.breaker.state for n, c in self.connectors.items()}

    async def aclose(self) -> None:
        await self.client.aclose()
