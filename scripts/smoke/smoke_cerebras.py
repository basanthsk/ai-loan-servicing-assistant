"""Cerebras, planner model fallback (gpt-oss-120B).

Run: poetry run poe smoke-cerebras
"""

from common import run_llm_checks
from core.settings import get_settings, require


def main() -> int:
    s = get_settings()
    require("cerebras_api_key", settings=s)
    r = run_llm_checks(
        provider="Cerebras",
        base_url=s.cerebras_base_url,
        api_key=s.cerebras_api_key,
        model=s.cerebras_planner_model,
        pause_s=5,  # the free tier allows only a few requests per minute
        name="cerebras-planner",
    )
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
