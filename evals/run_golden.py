"""Run the golden questions against the real assistant and score them.

Needs the mock systems running (`poetry run poe up --services`) and LLM keys.
Each question runs in a fresh session. Results go to evals/results/golden-<pack>.json
and are summarised in docs/eval-report.md by `poetry run poe eval-report`.

Run: poetry run poe eval                  # all packs
     poetry run poe eval -- --pack cards   # one pack
     poetry run poe eval -- --only L04,C12 # some questions
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.assistant import build_assistant  # noqa: E402
from core.session import Session  # noqa: E402
from core.verify import numbers_in_text  # noqa: E402

RESULTS = ROOT / "evals" / "results"


def norm(text: str) -> str:
    text = re.sub("[\u2010\u2011\u2013]", "-", text)
    text = re.sub("[\u00a0\u202f\u2009\u2007]", " ", text)  # non-breaking and thin spaces
    text = text.replace("\u2019", "'").replace("\u2018", "'")  # curly apostrophes
    return text.replace("**", "").lower()


def check(case: dict, answer: str, events: list[dict]) -> list[str]:
    problems = []
    nums = numbers_in_text(answer)
    for n in case.get("numbers", []):
        if not any(abs(x - n) <= (1 if n >= 100 else 0.01) for x in nums):
            problems.append(f"missing number {n:g}")
    text = norm(answer)
    for w in case.get("words", []):
        if not any(alt.lower() in text for alt in w.split("|")):
            problems.append(f"missing word '{w}'")
    for w in case.get("not", []):
        if w.lower() in text:
            problems.append(f"contains forbidden '{w}'")
    if case.get("action"):
        proposed = [e["action"]["title"] for e in events if e["type"] == "action_required"]
        names = [e.get("_name") for e in events if e["type"] == "action_required"]
        if not proposed:
            problems.append(f"no action proposed (expected {case['action']})")
        elif case["action"] not in names:
            problems.append(f"proposed {names}, expected {case['action']}")
    return problems


async def run_case(a, case: dict) -> dict:
    session = Session(customer_id=case["customer"])
    await a.orchestrator.prefetch(session)
    for t in case.get("turns", []):

        async def ignore(e):
            pass

        await a.orchestrator.handle(session, t, ignore)
    events: list[dict] = []

    async def record(e):
        if e["type"] == "action_required" and session.pending_action:
            e = {**e, "_name": session.pending_action.name}
        events.append(e)

    t0 = time.perf_counter()
    answer = await a.orchestrator.handle(session, case["q"], record)
    ms = (time.perf_counter() - t0) * 1000
    done = next(e for e in reversed(events) if e["type"] == "done")
    problems = check(case, answer, events)
    marks = done["trace"]["marks"] if done.get("trace") else {}
    return {
        "id": case["id"],
        "customer": case["customer"],
        "question": case["q"],
        "answer": answer,
        "passed": not problems,
        "problems": problems,
        "intent": done.get("intent"),
        "numbers_verified": done.get("verified"),
        "tools": [t["name"] for t in done["explain"]["tools"]],
        "total_ms": round(ms),
        "first_words_ms": marks.get("first_token"),
        "first_status_ms": marks.get("first_status"),
        "providers": sorted(
            {
                s["attrs"].get("provider")
                for s in done["trace"]["spans"]
                if s["kind"] == "llm" and s["attrs"].get("provider")
            }
        )
        if done.get("trace")
        else [],
    }


async def main_async(packs: list[str], only: set[str], pause: float) -> int:
    golden = yaml.safe_load((ROOT / "evals" / "golden.yaml").read_text(encoding="utf-8"))
    a = build_assistant()
    RESULTS.mkdir(parents=True, exist_ok=True)
    failures = 0
    try:
        for pack in packs:
            cases = [c for c in golden[pack] if not only or c["id"] in only]
            out_file = RESULTS / f"golden-{pack}.json"
            previous = json.loads(out_file.read_text()) if out_file.exists() and only else {}
            results = {r["id"]: r for r in previous.get("results", [])}
            for case in cases:
                try:
                    r = await run_case(a, case)
                except Exception as e:  # noqa: BLE001 - record and carry on
                    r = {
                        "id": case["id"],
                        "question": case["q"],
                        "passed": False,
                        "problems": [f"error: {type(e).__name__}: {e}"],
                        "answer": "",
                    }
                results[case["id"]] = r
                failures += not r["passed"]
                mark = "PASS" if r["passed"] else "FAIL"
                print(
                    f"[{mark}] {case['id']} {r.get('total_ms', '-')} ms  {case['q'][:60]}"
                    + (
                        ""
                        if r["passed"]
                        else f"\n       {r['problems']}\n       answer: {r['answer'][:200]}"
                    )
                )
                await asyncio.sleep(pause)  # stay under free-tier rate limits
            ordered = [results[c["id"]] for c in golden[pack] if c["id"] in results]
            out_file.write_text(
                json.dumps(
                    {
                        "pack": pack,
                        "run_at": time.strftime("%Y-%m-%d %H:%M %Z"),
                        "passed": sum(r["passed"] for r in ordered),
                        "total": len(ordered),
                        "results": ordered,
                    },
                    indent=1,
                    ensure_ascii=False,
                )
            )
    finally:
        await a.hub.aclose()
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pack", choices=["loans", "cards"], action="append")
    p.add_argument("--only", default="")
    p.add_argument("--pause", type=float, default=3.0, help="seconds between questions")
    args = p.parse_args()
    packs = args.pack or ["loans", "cards"]
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    return asyncio.run(main_async(packs, only, args.pause))


if __name__ == "__main__":
    raise SystemExit(main())
