"""Ollama on your own machine (development model).

Needs Ollama running locally with the models pulled:
    ollama pull qwen3:8b
    ollama pull qwen3:4b

Run: poetry run poe smoke-ollama
"""

from common import run_llm_checks
from core.settings import get_settings


def main() -> int:
    s = get_settings()
    ok = True
    for role, model in (("dev", s.ollama_model), ("dev-fast", s.ollama_fast_model)):
        r = run_llm_checks(
            provider="Ollama (local)",
            base_url=s.ollama_base_url,
            api_key="ollama",
            model=model,
            runs=3,
        )
        r.save(f"ollama-{role}")
        ok &= r.ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
