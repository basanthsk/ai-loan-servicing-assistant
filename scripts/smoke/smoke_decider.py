"""Decider returns sensible option probabilities.

Three decisions from our journeys, including a negated confirmation, which is
the case decision models most often get wrong.

Run: poetry run poe smoke-decider
"""

from common import CheckResult, SmokeResult
from core.decider import LLMJsonDecider
from core.llm import build_chain
from core.settings import get_settings

CASES = [
    (
        "intent",
        "Which intent does the message express?",
        ["next_emi", "payment_history", "prepayment_quote", "foreclosure", "out_of_scope"],
        "What's my next EMI?",
        "next_emi",
    ),
    (
        "barge-in",
        "The assistant is speaking. Is the customer interrupting or just acknowledging?",
        ["interruption", "backchannel"],
        "mm-hmm",
        "backchannel",
    ),
    (
        "confirmation",
        "The assistant asked to confirm a prepayment request of 50,000 rupees. What is the reply?",
        ["yes", "no", "changed_mind", "unclear"],
        "No wait, don't do that yet.",
        "no",
    ),
]


def main() -> int:
    s = get_settings()
    decider = LLMJsonDecider(build_chain(s))
    result = SmokeResult(
        provider="Decider", model=f"llm_json via {s.llm_provider_chain} (fast role)"
    )
    print("\n== Decider (llm_json backend)")
    times = []
    for name, question, options, text, expected in CASES:
        try:
            d = decider.decide(question, options, text)
            times.append(d.ms)
            total = sum(d.probabilities.values())
            probs = ", ".join(
                f"{k} {v:.2f}" for k, v in sorted(d.probabilities.items(), key=lambda kv: -kv[1])
            )
            result.add(
                CheckResult(
                    name,
                    d.top == expected and abs(total - 1) < 0.01,
                    f"{text!r} -> {d.top} ({probs}) via {d.provider}",
                    d.ms,
                )
            )
        except Exception as e:  # noqa: BLE001
            result.add(CheckResult(name, False, f"{type(e).__name__}: {e}"))
    if times:
        result.latency = {
            "decisions": len(times),
            "ms_min": round(min(times)),
            "ms_max": round(max(times)),
        }
    result.notes.append(
        "Baseline only: the LLM backend is a fallback. Decision models are benchmarked against it in evals/bench_decider.py."
    )
    result.save("decider")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
