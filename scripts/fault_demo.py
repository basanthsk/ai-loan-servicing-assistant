"""Fault-injection run: the Payments system goes down mid-conversation, then recovers.

Records, per turn: the Payments call time and outcome, the circuit-breaker state,
and whether the assistant still answered. Shows partial answers, the breaker
opening and failing fast, and recovery once Payments is back.

Uses whichever LLM chain is configured; with LLM_PROVIDER_CHAIN=stub it measures
our system only. Writes evals/results/fault-injection.json.

Run: poetry run python scripts/fault_demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core.assistant import build_assistant  # noqa: E402
from core.session import Session  # noqa: E402

Q = "Which of my payments failed?"


async def set_payments(admin: httpx.AsyncClient, url: str, down: bool) -> None:
    f = (await admin.get(f"{url}/admin/faults")).json()
    f["down"] = down
    await admin.put(f"{url}/admin/faults", json=f)


async def main() -> int:
    a = build_assistant()
    url = a.settings.payments_url
    session = Session(customer_id="C1001")
    await a.orchestrator.prefetch(session)
    rows = []
    async with httpx.AsyncClient(trust_env=False) as admin:
        plan = [("normal", False)] * 2 + [("payments down", True)] * 6 + [("recovered", False)] * 2
        for i, (phase, down) in enumerate(plan):
            await set_payments(admin, url, down)
            if phase == "recovered" and rows[-1]["phase"] == "payments down":
                await asyncio.sleep(16)  # breaker cool-down before it half-opens
            a.hub.cache.clear_customer("C1001")  # make every turn really call Payments
            events = []

            async def rec(e, events=events):
                events.append(e)

            t0 = time.perf_counter()
            answer = await a.orchestrator.handle(session, Q, rec)
            done = events[-1]
            spans = [
                s
                for s in done["trace"]["spans"]
                if s["kind"] == "backend" and s["name"].startswith("payments")
            ]
            rows.append(
                {
                    "turn": i + 1,
                    "phase": phase,
                    "payments_call_ms": round(sum(s["duration_ms"] for s in spans)),
                    "payments_ok": all(s["ok"] for s in spans) if spans else None,
                    "circuit": a.hub["payments"].breaker.state,
                    "failed_fast": any(s["attrs"].get("circuit") == "open" for s in spans),
                    "turn_ms": round((time.perf_counter() - t0) * 1000),
                    "answered": bool(answer),
                    "answer": answer[:160],
                }
            )
            print(json.dumps(rows[-1]))
        await set_payments(admin, url, False)
    await a.hub.aclose()
    out = ROOT / "evals" / "results" / "fault-injection.json"
    out.write_text(
        json.dumps(
            {
                "run_at": time.strftime("%Y-%m-%d %H:%M %Z"),
                "llm_chain": a.settings.llm_provider_chain,
                "turns": rows,
            },
            indent=1,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
