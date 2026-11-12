"""Groq, planner model (gpt-oss-120B) and fast model (gpt-oss-20B).

Run: poetry run poe smoke-groq
"""

from common import run_llm_checks
from core.settings import get_settings, require


def main() -> int:
    s = get_settings()
    require("groq_api_key", settings=s)
    ok = True
    for role, model in (("planner", s.groq_planner_model), ("fast", s.groq_fast_model)):
        r = run_llm_checks(
            provider="Groq",
            base_url=s.groq_base_url,
            api_key=s.groq_api_key,
            model=model,
            name=f"groq-{role}",
        )
        ok &= r.ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
