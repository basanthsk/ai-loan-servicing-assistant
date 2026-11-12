"""OpenRouter free models (second fallback).

Lists the free models that support tool calling, tries them in preference order,
and runs the full checks on the first one whose tool call works. Free accounts
have a small daily request limit, so this uses few requests.

Run: poetry run poe smoke-openrouter
"""

import json

from common import GET_LOAN_TOOL, make_client, run_llm_checks
from core.settings import get_settings, require

# Larger general-purpose models first; small or code-only models last.
PREFERENCE = ["gpt-oss", "gemma-4-31b", "nemotron-3-ultra", "nemotron-3.5-lightning", "gemma-4-26b"]


def rank(model_id: str) -> int:
    for i, key in enumerate(PREFERENCE):
        if key in model_id:
            return i
    return len(PREFERENCE)


def tool_call_works(client, model: str) -> bool:
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "What is the outstanding balance on my loan L-1042?"}
            ],
            tools=[GET_LOAN_TOOL],
            max_tokens=300,
        )
        calls = r.choices[0].message.tool_calls or []
        return (
            bool(calls)
            and json.loads(calls[0].function.arguments or "{}").get("loan_id") == "L-1042"
        )
    except Exception as e:  # noqa: BLE001
        print(f"  skip {model}: {type(e).__name__}: {str(e)[:120]}")
        return False


def main() -> int:
    s = get_settings()
    require("openrouter_api_key", settings=s)
    client = make_client(s.openrouter_base_url, s.openrouter_api_key)

    if s.openrouter_model:
        candidates = [s.openrouter_model]
    else:
        models = client.models.list().data
        free = [
            m.id
            for m in models
            if m.id.endswith(":free")
            and "tools" in (getattr(m, "supported_parameters", None) or [])
        ]
        candidates = sorted(free, key=rank)
        print(f"{len(free)} free models with tool support: {', '.join(candidates)}")

    for model in candidates[:4]:
        print(f"\nTrying {model} ...")
        if tool_call_works(client, model):
            r = run_llm_checks(
                provider="OpenRouter (free)",
                base_url=s.openrouter_base_url,
                api_key=s.openrouter_api_key,
                model=model,
                runs=3,
            )
            r.notes.append(
                f"Chosen from {len(candidates)} free tool-capable models; set OPENROUTER_MODEL={model}"
            )
            r.save("openrouter")
            print(f"\nSet this in .env: OPENROUTER_MODEL={model}")
            return 0 if r.ok else 1
    print("No free model passed the tool-call check.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
