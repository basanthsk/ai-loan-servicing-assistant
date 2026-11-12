"""Load test: concurrent customer conversations over the gateway's WebSocket.

Each simulated customer logs in, opens the chat WebSocket and asks a sequence of
questions with think time between them. Measured per turn, from the client:
time to first status, time to first words, total turn time, errors.

Point it at a gateway running with LLM_PROVIDER_CHAIN=stub (scripts/llm_stub.py)
to measure our system rather than an LLM provider's rate limits.

Run: poetry run python scripts/loadtest.py --url http://127.0.0.1:8010 --levels 10,50,100,200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = [
    "What's my next EMI?",
    "My EMI went up this month, why?",
    "What if I prepay 50,000?",
    "Which payments failed?",
    "Can I foreclose my loan?",
]
CUSTOMERS = ["C1001", "C1002", "C1003"] + [f"C{2000 + i}" for i in range(47)]


def pct(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]) if v else None


async def conversation(
    url: str, n_turns: int, think: tuple[float, float], results: list, errors: list, stop_at: float
):
    ws_url = url.replace("http", "ws", 1)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.post(
                f"{url}/api/login", json={"customer_id": random.choice(CUSTOMERS[:20])}
            )
            r.raise_for_status()
            token = r.json()["token"]
        async with websockets.connect(
            f"{ws_url}/ws?token={token}", proxy=None, open_timeout=30
        ) as ws:
            json.loads(await ws.recv())  # ready
            for _ in range(n_turns):
                if time.monotonic() > stop_at:
                    break
                await asyncio.sleep(random.uniform(*think))
                t0 = time.perf_counter()
                first_status = first_token = None
                await ws.send(json.dumps({"type": "message", "text": random.choice(QUESTIONS)}))
                while True:
                    e = json.loads(await asyncio.wait_for(ws.recv(), 60))
                    now = (time.perf_counter() - t0) * 1000
                    if e["type"] == "status" and first_status is None:
                        first_status = now
                    if e["type"] in ("token", "replace") and first_token is None:
                        first_token = now
                    if e["type"] == "error":
                        errors.append(e.get("text"))
                    if e["type"] == "done":
                        results.append(
                            {"first_status": first_status, "first_token": first_token, "total": now}
                        )
                        break
    except Exception as e:  # noqa: BLE001
        errors.append(f"{type(e).__name__}: {e}")


async def level(
    urls: list[str], users: int, turns: int, think: tuple[float, float], ramp_s: float, max_s: float
) -> dict:
    results, errors = [], []
    stop_at = time.monotonic() + max_s
    tasks = []
    t0 = time.perf_counter()
    for i in range(users):
        url = urls[i % len(urls)]  # each conversation stays on one gateway (sessions are local)
        tasks.append(asyncio.create_task(conversation(url, turns, think, results, errors, stop_at)))
        await asyncio.sleep(ramp_s / users)
    await asyncio.gather(*tasks)
    wall = time.perf_counter() - t0
    totals = [r["total"] for r in results]
    firsts = [r["first_token"] for r in results if r["first_token"] is not None]
    status = [r["first_status"] for r in results if r["first_status"] is not None]
    return {
        "concurrent_conversations": users,
        "turns_completed": len(results),
        "errors": len(errors),
        "error_samples": list(dict.fromkeys(errors))[:3],
        "turns_per_s": round(len(results) / wall, 1),
        "first_status_ms_p50": pct(status, 50),
        "first_status_ms_p95": pct(status, 95),
        "first_words_ms_p50": pct(firsts, 50),
        "first_words_ms_p95": pct(firsts, 95),
        "turn_ms_p50": pct(totals, 50),
        "turn_ms_p95": pct(totals, 95),
        "turn_ms_mean": round(statistics.mean(totals)) if totals else None,
    }


async def main_async(args) -> None:
    out = []
    for users in [int(x) for x in args.levels.split(",")]:
        r = await level(
            args.url.split(","),
            users,
            args.turns,
            (args.think_min, args.think_max),
            args.ramp,
            args.max_seconds,
        )
        r["gateways"] = len(args.url.split(","))
        print(json.dumps(r), flush=True)
        out.append(r)
    path = ROOT / "evals" / "results" / "loadtest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(path.read_text()) if path.exists() else {"levels": []}
    path.write_text(
        json.dumps(
            {
                "run_at": time.strftime("%Y-%m-%d %H:%M %Z"),
                "llm": args.llm_note,
                "levels": previous["levels"] + out,
            },
            indent=1,
        )
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--url", default="http://127.0.0.1:8010", help="one or more gateway URLs, comma-separated"
    )
    p.add_argument("--levels", default="10,50,100,200")
    p.add_argument("--turns", type=int, default=3)
    p.add_argument("--think-min", type=float, default=1.0)
    p.add_argument("--think-max", type=float, default=4.0)
    p.add_argument("--ramp", type=float, default=5.0, help="seconds to start all conversations")
    p.add_argument("--max-seconds", type=float, default=90)
    p.add_argument("--llm-note", default="stub: decider 300 ms, planner first token 450 ms")
    args = p.parse_args()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
