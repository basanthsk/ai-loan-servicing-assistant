"""Shared helpers for the provider smoke tests.

Every LLM smoke test runs the same four checks against an OpenAI-compatible API:
  1. reachable   - the models endpoint answers
  2. chat        - a short prompt gets a sensible reply
  3. tool call   - the model calls get_loan with valid JSON arguments
  4. latency     - streamed replies, time to first token and total, over N runs

Results are written to scripts/smoke/results/<name>.json and summarised by
smoke_report.py into scripts/smoke/results/summary.md.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from openai import BadRequestError, OpenAI, RateLimitError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

RESULTS_DIR = ROOT / "scripts" / "smoke" / "results"
# This cloud sandbox has an agent proxy at /root/.ccr; any other machine is yours.
RUN_WHERE = "cloud session" if Path("/root/.ccr").exists() else "your machine"

GET_LOAN_TOOL = {
    "type": "function",
    "function": {
        "name": "get_loan",
        "description": "Get the details of one of the customer's loans, including outstanding balance.",
        "parameters": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string", "description": "Loan identifier, e.g. L-1042"}
            },
            "required": ["loan_id"],
        },
    },
}

LATENCY_PROMPT = "In one sentence, what does EMI stand for in personal loans?"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    ms: float | None = None
    skipped: bool = False  # could not run in this environment; run it on your machine


@dataclass
class SmokeResult:
    provider: str
    model: str
    run_where: str = RUN_WHERE
    checks: list[CheckResult] = field(default_factory=list)
    latency: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        ran = [c for c in self.checks if not c.skipped]
        return bool(ran) and all(c.ok for c in ran)

    def add(self, check: CheckResult) -> None:
        self.checks.append(check)
        mark = "SKIP" if check.skipped else ("PASS" if check.ok else "FAIL")
        timing = f" ({check.ms:.0f} ms)" if check.ms is not None else ""
        print(f"  [{mark}] {check.name}{timing}: {check.detail}")

    def save(self, name: str) -> Path:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RESULTS_DIR / f"{name}.json"
        data = asdict(self)
        data["ok"] = self.ok
        data["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        path.write_text(json.dumps(data, indent=2))
        print(
            f"\n  {'ALL PASSED' if self.ok else 'SOME CHECKS FAILED'} -> {path.relative_to(ROOT)}"
        )
        return path


def make_client(base_url: str, api_key: str, timeout: float = 60.0) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key or "missing", timeout=timeout, max_retries=0)


def _extra(model: str) -> dict:
    """Ask gpt-oss models for low reasoning effort, which keeps latency down."""
    return {"reasoning_effort": "low"} if "gpt-oss" in model else {}


def _create(client: OpenAI, model: str, **kwargs):
    """Call chat.completions.create, dropping reasoning_effort if the provider rejects it."""
    extra = _extra(model)
    try:
        return client.chat.completions.create(model=model, **extra, **kwargs)
    except BadRequestError:
        if not extra:
            raise
        return client.chat.completions.create(model=model, **kwargs)


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile; with few samples p95 is close to the max."""
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * len(ordered) + 0.5) - 1))
    return ordered[k]


def run_llm_checks(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    runs: int = 5,
    pause_s: float = 0.0,
    name: str | None = None,
) -> SmokeResult:
    result = SmokeResult(provider=provider, model=model)
    client = make_client(base_url, api_key)
    print(f"\n== {provider} / {model}")

    # 1. Reachable
    t0 = time.perf_counter()
    try:
        ids = [m.id for m in client.models.list().data]
        found = model in ids
        result.add(
            CheckResult(
                "reachable",
                True,
                f"{len(ids)} models listed; target {'found' if found else 'NOT in list'}",
                (time.perf_counter() - t0) * 1000,
            )
        )
        if not found:
            result.notes.append(f"{model} was not in the models list")
    except Exception as e:  # noqa: BLE001 - smoke test reports any failure
        result.add(CheckResult("reachable", False, f"{type(e).__name__}: {e}"))
        return result

    # 2. Chat
    t0 = time.perf_counter()
    try:
        r = _create(
            client,
            model,
            messages=[{"role": "user", "content": "Reply with exactly the word OK."}],
            max_tokens=200,
        )
        text = (r.choices[0].message.content or "").strip()
        result.add(
            CheckResult(
                "chat",
                "ok" in text.lower(),
                f"reply={text[:60]!r}",
                (time.perf_counter() - t0) * 1000,
            )
        )
    except Exception as e:  # noqa: BLE001
        result.add(CheckResult("chat", False, f"{type(e).__name__}: {e}"))

    # 3. Tool call
    t0 = time.perf_counter()
    try:
        r = _create(
            client,
            model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a loan servicing assistant. Use tools to look up data.",
                },
                {"role": "user", "content": "What is the outstanding balance on my loan L-1042?"},
            ],
            tools=[GET_LOAN_TOOL],
            tool_choice="auto",
            max_tokens=300,
        )
        calls = r.choices[0].message.tool_calls or []
        ok, detail = False, "no tool call returned"
        if calls:
            fn = calls[0].function
            try:
                args = json.loads(fn.arguments or "{}")
                ok = fn.name == "get_loan" and args.get("loan_id") == "L-1042"
                detail = f"{fn.name}({fn.arguments})"
            except json.JSONDecodeError:
                detail = f"invalid JSON arguments: {fn.arguments!r}"
        result.add(CheckResult("tool call", ok, detail, (time.perf_counter() - t0) * 1000))
    except Exception as e:  # noqa: BLE001
        result.add(CheckResult("tool call", False, f"{type(e).__name__}: {e}"))

    # 4. Latency (streaming)
    ttft, total, errors, error_kinds = [], [], 0, []
    rate_limit_waits = 0
    for _ in range(runs):
        if pause_s:
            time.sleep(pause_s)
        for attempt in range(2):
            t0 = time.perf_counter()
            first = None
            try:
                stream = _create(
                    client,
                    model,
                    messages=[{"role": "user", "content": LATENCY_PROMPT}],
                    max_tokens=200,
                    stream=True,
                )
                for chunk in stream:
                    if first is None and chunk.choices and chunk.choices[0].delta.content:
                        first = time.perf_counter()
                end = time.perf_counter()
                if first is not None:
                    ttft.append((first - t0) * 1000)
                    total.append((end - t0) * 1000)
                else:
                    errors += 1
                    error_kinds.append("stream returned no text")
                break
            except RateLimitError as e:
                # Free tiers allow few requests per minute: wait once as the provider asks.
                if attempt == 0:
                    rate_limit_waits += 1
                    wait = min(float(e.response.headers.get("retry-after", 30)), 70.0)
                    print(f"  rate limited, waiting {wait:.0f} s as the provider asks")
                    time.sleep(wait)
                    continue
                errors += 1
                error_kinds.append(f"RateLimitError: {str(e)[:100]}")
            except Exception as e:  # noqa: BLE001
                errors += 1
                error_kinds.append(f"{type(e).__name__}: {str(e)[:100]}")
                break
    if rate_limit_waits:
        result.notes.append(
            f"rate limited {rate_limit_waits} time(s) during latency runs (free-tier limit)"
        )
    if error_kinds:
        result.notes.append("latency run errors: " + " | ".join(sorted(set(error_kinds))))
    if ttft:
        result.latency = {
            "runs": runs,
            "errors": errors,
            "ttft_ms_p50": round(statistics.median(ttft)),
            "ttft_ms_p95": round(pct(ttft, 95)),
            "total_ms_p50": round(statistics.median(total)),
            "total_ms_p95": round(pct(total, 95)),
        }
        result.add(
            CheckResult(
                "latency",
                errors == 0,
                f"time to first token p50 {result.latency['ttft_ms_p50']} ms, "
                f"p95 {result.latency['ttft_ms_p95']} ms; total p50 {result.latency['total_ms_p50']} ms "
                f"({runs - errors}/{runs} runs ok)",
            )
        )
    else:
        result.add(CheckResult("latency", False, f"0/{runs} streamed runs returned text"))

    if name:
        result.save(name)
    return result
