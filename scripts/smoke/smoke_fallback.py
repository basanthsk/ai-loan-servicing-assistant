"""Provider fallback against real providers.

Puts a fake provider that always answers 429 (rate limited) at the front of the
real chain from LLM_PROVIDER_CHAIN, then checks the next real provider answers.

Run: poetry run poe smoke-fallback
"""

import httpx
from openai import OpenAI

from common import CheckResult, SmokeResult
from core.llm import Provider, ProviderChain, build_chain
from core.settings import get_settings


def always_rate_limited() -> Provider:
    transport = httpx.MockTransport(lambda req: httpx.Response(429, json={"error": "rate limited"}))
    client = OpenAI(
        base_url="https://rate-limited.invalid/v1",
        api_key="x",
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )
    return Provider("fake-429", client, "any", "any")


def main() -> int:
    real = build_chain(get_settings())
    chain = ProviderChain([always_rate_limited(), *real.providers])
    result = SmokeResult(
        provider="Fallback chain", model=" -> ".join(p.name for p in chain.providers)
    )
    print(f"\n== Fallback chain: {result.model}")

    try:
        r = chain.chat(
            role="fast",
            messages=[{"role": "user", "content": "Reply with exactly the word OK."}],
            max_tokens=200,
        )
        steps = ", ".join(
            f"{a.provider} {'ok' if a.ok else a.error} ({a.ms:.0f} ms)" for a in r.attempts
        )
        first_failed = not r.attempts[0].ok and r.attempts[0].error == "RateLimitError"
        result.add(
            CheckResult(
                "fails over on 429",
                first_failed and r.provider != "fake-429",
                f"answered by {r.provider}; attempts: {steps}",
            )
        )
    except Exception as e:  # noqa: BLE001
        result.add(CheckResult("fails over on 429", False, f"{type(e).__name__}: {e}"))
    result.save("fallback")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
