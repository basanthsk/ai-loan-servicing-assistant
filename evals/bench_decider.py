"""Benchmark Decider backends on evals/decisions.jsonl.

Measures, per backend: accuracy, latency (p50/p95), and symmetry (does the top
choice stay the same when the options are listed in reverse order).

Backends:
  keyword         instant rules for clear replies (answers only when sure)
  jev             TypeSafe JEV decision model (TYPESAFE_API_KEY)
  llm:<provider>  LLM JSON backend on one provider from the chain
                  (e.g. llm:groq-fast, llm:cerebras, llm:openrouter, llm:ollama)
  keyword+llm     the production Decider: keyword first, then the LLM

Dedicated decision models (Decider, Celeris, Elephant...) plug in as another
backend once an API key or local weights are available.

Run: poetry run python evals/bench_decider.py --backends keyword,keyword+llm:groq-fast
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.decider import JevDecider, KeywordDecider, LLMJsonDecider  # noqa: E402
from core.llm import ProviderChain, build_chain  # noqa: E402
from core.registry import load_packs  # noqa: E402
from core.settings import get_settings  # noqa: E402

QUESTIONS = {
    "intent": "Which intent does the customer's message express?",
    "confirmation": "The assistant asked the customer to confirm a request. How did the customer reply?",
    "barge_in": "The assistant is speaking. Is the customer interrupting or just acknowledging?",
}


def options_for(kind: str) -> dict[str, str]:
    if kind == "intent":
        packs = load_packs()
        opts = {i: s.description for p in packs.values() for i, s in p.decisions.intents.items()}
        opts.update(
            {
                "greeting": "greets, thanks or says goodbye, with no question",
                "out_of_scope": "asks about something unrelated to their accounts or our products",
            }
        )
        return opts
    if kind == "confirmation":
        return {
            "yes": "agrees to go ahead",
            "no": "declines or cancels",
            "changed_mind": "asks something else or wants to change the request",
            "unclear": "reply is unclear",
        }
    return {
        "interruption": "the customer wants to say something or change the topic",
        "backchannel": "a short acknowledgement like 'okay' or 'mm-hmm'",
    }


def pct(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))] if v else None


async def run_backend(name: str, rows: list[dict], symmetry_every: int, pause: float) -> dict:
    keyword = KeywordDecider()
    llm = None
    if name.endswith("jev"):
        st = get_settings()
        llm = JevDecider(
            st.typesafe_api_key or "proxy-injected", st.typesafe_base_url, st.typesafe_model
        )
    elif "llm" in name:
        provider = name.split(":", 1)[1] if ":" in name else None
        chain = build_chain(get_settings())
        if provider:
            chain = ProviderChain(
                [p for p in chain.providers if p.name == provider] or chain.providers
            )
        llm = LLMJsonDecider(chain)

    correct = answered = 0
    times, sym_same, sym_total, errors = [], 0, 0, 0
    per_kind: dict[str, list[int]] = {}
    wrong = []
    for i, r in enumerate(rows):
        opts = options_for(r["kind"])
        t0 = time.perf_counter()
        top = None
        if name.startswith("keyword"):
            probs = keyword.decide(r["kind"], r["text"])
            if probs is not None and set(probs) == set(opts):
                top = max(probs, key=probs.get)
        ms = None
        if top is not None:
            ms = (time.perf_counter() - t0) * 1000
        if top is None and llm is not None:
            try:
                d = await llm.adecide(QUESTIONS[r["kind"]], opts, r["text"], r.get("context", ""))
                top = d.top
                ms = (time.perf_counter() - t0) * 1000  # decision time only, not pauses or re-asks
            except Exception:  # noqa: BLE001
                errors += 1
            await asyncio.sleep(pause)
            if top and symmetry_every and i % symmetry_every == 0:
                try:
                    rev = dict(reversed(list(opts.items())))
                    d2 = await llm.adecide(
                        QUESTIONS[r["kind"]], rev, r["text"], r.get("context", "")
                    )
                    sym_total += 1
                    sym_same += d2.top == top
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(pause)
        if top is not None:
            answered += 1
            times.append(ms)
        ok = top == r["label"]
        correct += ok
        per_kind.setdefault(r["kind"], [0, 0])
        per_kind[r["kind"]][0] += ok
        per_kind[r["kind"]][1] += 1
        if top is not None and not ok:
            wrong.append({"text": r["text"], "expected": r["label"], "got": top})
        print(
            f"  {name:22} {i + 1:3}/{len(rows)} {'ok ' if ok else ('-- ' if top is None else 'BAD')} "
            f"{r['kind']:12} {r['text'][:40]!r} -> {top}",
            flush=True,
        )
    return {
        "backend": name,
        "cases": len(rows),
        "answered": answered,
        "accuracy_all": round(correct / len(rows), 3),
        "accuracy_when_answered": round(correct / answered, 3) if answered else None,
        "per_kind": {k: f"{v[0]}/{v[1]}" for k, v in per_kind.items()},
        "latency_ms_p50": round(statistics.median(times)) if times else None,
        "latency_ms_p95": round(pct(times, 95)) if times else None,
        "symmetry": f"{sym_same}/{sym_total}" if sym_total else None,
        "errors": errors,
        "wrong": wrong[:15],
    }


async def main_async(backends: list[str], limit: int, symmetry_every: int, pause: float) -> None:
    rows = [
        json.loads(line)
        for line in (ROOT / "evals" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if limit:
        rows = rows[:: max(1, len(rows) // limit)][:limit]
    results = [await run_backend(b, rows, symmetry_every, pause) for b in backends]
    out = ROOT / "evals" / "results" / "decider-benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(out.read_text()) if out.exists() else {"results": []}
    merged = {r["backend"]: r for r in previous["results"]}
    merged.update({r["backend"]: r for r in results})
    out.write_text(
        json.dumps(
            {"run_at": time.strftime("%Y-%m-%d %H:%M %Z"), "results": list(merged.values())},
            indent=1,
            ensure_ascii=False,
        )
    )
    for r in results:
        print(json.dumps({k: v for k, v in r.items() if k != "wrong"}))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backends", default="keyword,keyword+llm:groq-fast")
    p.add_argument("--limit", type=int, default=0, help="spread-sample this many cases")
    p.add_argument(
        "--symmetry-every", type=int, default=3, help="re-ask every Nth case with options reversed"
    )
    p.add_argument("--pause", type=float, default=0.3)
    args = p.parse_args()
    asyncio.run(main_async(args.backends.split(","), args.limit, args.symmetry_every, args.pause))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
