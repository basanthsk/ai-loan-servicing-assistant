"""Chat with the assistant in the terminal (needs `poetry run poe up --services` running).

Run: poetry run poe chat                     # interactive, as customer C1001
     poetry run poe chat -- -q "What's my next EMI?" -q "Why did it change?"

Type 'yes' or 'no' to answer a confirmation; type the 6-digit code shown, or just 'otp'.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.assistant import build_assistant  # noqa: E402
from core.session import Session  # noqa: E402


async def run(customer: str, questions: list[str], show_trace: bool, channel: str = "text") -> None:
    a = build_assistant()
    session = Session(customer_id=customer)
    await a.orchestrator.prefetch(session)
    print(f"Logged in as {session.customer_name} ({customer}). Ctrl+C to quit.\n")

    last_otp = {"code": ""}

    async def emit(e: dict) -> None:
        t = e["type"]
        if t == "otp_required":
            last_otp["code"] = e["demo_code"]
        if t == "token":
            print(e["text"], end="", flush=True)
        elif t == "replace":
            print(f"\n[answer rewritten after number check]\n{e['text']}", end="")
        elif t == "status":
            print(f"  ... {e['text']}", flush=True)
        elif t == "action_required":
            print(f"\n  [confirm] {e['action']['summary']}")
        elif t == "otp_required":
            print(f"\n  [demo OTP sent to phone ending {e['phone_last4']}: {e['demo_code']}]")
        elif t == "done":
            tr = e["trace"]
            tools = ", ".join(f"{x['name']} {x['ms']}ms" for x in e["explain"]["tools"]) or "none"
            print(
                f"\n  -- intent {e['intent']}, {tr['total_ms']:.0f} ms total, first token "
                f"{tr['marks'].get('first_token', '-')} ms, tools: {tools}, "
                f"numbers verified: {e['verified']}\n"
            )
            if show_trace:
                for s in tr["spans"]:
                    print(
                        f"     {s['start_ms']:7.0f} +{s['duration_ms']:6.0f} ms  {s['kind']:8} {s['name']}"
                    )

    async def ask(q: str) -> None:
        if q.lower() == "otp":  # demo convenience: type 'otp' to enter the code just "sent"
            q = last_otp["code"]
        print(f"You: {q}\nAssistant: ", end="")
        await a.orchestrator.handle(session, q, emit, channel=channel)

    try:
        if questions:
            for q in questions:
                await ask(q)
        else:
            loop = asyncio.get_running_loop()
            while True:
                q = (await loop.run_in_executor(None, input, "You: ")).strip()
                if q:
                    if q.lower() == "otp":
                        q = last_otp["code"]
                    print("Assistant: ", end="")
                    await a.orchestrator.handle(session, q, emit, channel=channel)
    finally:
        await a.hub.aclose()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("-c", "--customer", default="C1001")
    p.add_argument("-q", "--question", action="append", default=[])
    p.add_argument("--trace", action="store_true", help="print every span of each turn")
    p.add_argument("--voice", action="store_true", help="answer in the spoken (voice) style")
    args = p.parse_args()
    try:
        asyncio.run(
            run(args.customer, args.question, args.trace, "voice" if args.voice else "text")
        )
    except (KeyboardInterrupt, EOFError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
