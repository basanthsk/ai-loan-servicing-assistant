"""OpenAI-compatible LLM stub with fixed latency, for load tests.

Load tests should measure our system (gateway, orchestrator, connectors, mock
systems), not a free tier's rate limits. This stub answers like the real models:
- Decider calls (model stub-fast): a JSON intent after DECIDER_MS
- planner calls (model stub-planner): first a tool call chosen from keywords,
  then, once a tool result is present, a streamed answer: first token after
  PLANNER_FIRST_TOKEN_MS, then tokens every TOKEN_MS

Run: poetry run python scripts/llm_stub.py   (port 8199; use LLM_PROVIDER_CHAIN=stub)
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

DECIDER_MS = 300
PLANNER_FIRST_TOKEN_MS = 450
TOKEN_MS = 15

app = FastAPI()

ROUTES = [  # (keyword, intent, tool, args)
    ("went up", "loan_emi_change", "explain_emi_change", {"loan_id": "L-1042"}),
    ("prepay", "loan_prepayment", "simulate_prepayment", {"loan_id": "L-1042", "amount": 50000}),
    ("fail", "loan_payments", "explain_failed_payments", {"months": 6}),
    ("foreclos", "loan_foreclosure", "check_foreclosure", {"loan_id": "L-1042"}),
    ("next emi", "loan_next_emi", None, None),
]


def route(text: str):
    t = text.lower()
    for kw, intent, tool, args in ROUTES:
        if kw in t:
            return intent, tool, args
    return "loan_general", "get_loan_details", {"loan_id": "L-1042"}


def last_user(messages: list[dict]) -> str:
    return next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")


def chunk(delta: dict, finish=None) -> str:
    body = {
        "id": "stub",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "stub",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n"


@app.get("/v1/models")
def models():
    return {
        "object": "list",
        "data": [{"id": "stub-planner", "object": "model"}, {"id": "stub-fast", "object": "model"}],
    }


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    messages = body["messages"]
    if body["model"] == "stub-fast":  # Decider
        await asyncio.sleep(DECIDER_MS / 1000)
        text = messages[-1]["content"]
        msg = text.split('Customer message: "')[-1]
        intent, _, _ = route(msg)
        content = json.dumps({intent: 0.95})
        return JSONResponse(
            {
                "id": "stub",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "stub",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    has_tool_result = any(m.get("role") == "tool" for m in messages)
    _, tool, args = route(last_user(messages))

    async def stream():
        await asyncio.sleep(PLANNER_FIRST_TOKEN_MS / 1000)
        if not has_tool_result and tool and body.get("tool_choice") != "none":
            yield chunk(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{uuid.uuid4().hex[:6]}",
                            "type": "function",
                            "function": {"name": tool, "arguments": json.dumps(args)},
                        }
                    ],
                }
            )
            yield chunk({}, "tool_calls")
        else:
            for word in "Here is what I found in your account, based on the details above.".split():
                yield chunk({"content": word + " "})
                await asyncio.sleep(TOKEN_MS / 1000)
            yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    if body.get("stream"):
        return StreamingResponse(stream(), media_type="text/event-stream")
    await asyncio.sleep(PLANNER_FIRST_TOKEN_MS / 1000)
    return JSONResponse(
        {
            "id": "stub",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "stub",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Here is what I found."},
                    "finish_reason": "stop",
                }
            ],
        }
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8199, log_level="warning")
