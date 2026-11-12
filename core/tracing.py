"""Per-turn traces and rolling latency metrics.

Every turn gets a Trace: a list of timed spans (Decider, each backend call,
each LLM call, speech). The UI shows the last turn as a waterfall; Metrics keeps
recent durations per step so the latency panel can show p50 and p95.

Spans are attached through a context variable, so code deep inside the
platform (connectors, the LLM chain) records into the current turn without
passing the trace around.
"""

from __future__ import annotations

import asyncio
import contextvars
import statistics
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

_current: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("trace", default=None)


@dataclass
class Span:
    name: str
    kind: str  # decider | llm | tool | backend | rules | action | speech | turn
    start_ms: float
    duration_ms: float = 0.0
    ok: bool = True
    attrs: dict = field(default_factory=dict)


@dataclass
class Trace:
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    channel: str = "text"
    text: str = ""
    t0: float = field(default_factory=time.perf_counter)
    spans: list[Span] = field(default_factory=list)
    marks: dict[str, float] = field(default_factory=dict)

    def now_ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000

    def mark(self, name: str) -> None:
        """Record a moment once, e.g. first_token, first_audio."""
        self.marks.setdefault(name, round(self.now_ms(), 1))

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("t0")
        d["total_ms"] = round(self.now_ms(), 1)
        for s in d["spans"]:
            s["start_ms"] = round(s["start_ms"], 1)
            s["duration_ms"] = round(s["duration_ms"], 1)
        return d


def current() -> Trace | None:
    return _current.get()


@contextmanager
def use_trace(trace: Trace):
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


@contextmanager
def span(name: str, kind: str, **attrs):
    """Time a block into the current trace (no-op when there is none)."""
    trace = _current.get()
    t0 = time.perf_counter()
    s = Span(name, kind, trace.now_ms() if trace else 0.0, attrs=attrs)
    try:
        yield s
    except asyncio.CancelledError:
        s.ok = False
        s.attrs["cancelled"] = True  # e.g. discarded speculative planner, barge-in
        raise
    except BaseException:
        s.ok = False
        raise
    finally:
        s.duration_ms = (time.perf_counter() - t0) * 1000
        if trace is not None:
            trace.spans.append(s)
        # Cache hits and cancelled work would flatter or skew the latency figures.
        if s.attrs.get("cached"):
            METRICS.record("cache hit", s.duration_ms)
        elif not s.attrs.get("cancelled"):
            key = kind if kind in ("turn", "rules") else f"{kind}:{name}"
            METRICS.record(key, s.duration_ms)


class Metrics:
    """Rolling window of recent durations per step."""

    def __init__(self, window: int = 500):
        self.samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=window))

    def record(self, key: str, ms: float) -> None:
        self.samples[key].append(ms)

    def save(self, path) -> None:
        """Write the summary and the raw samples, so a run's numbers can be shared."""
        import json
        from datetime import datetime
        from pathlib import Path

        data = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "summary": self.summary(),
            "samples_ms": {k: [round(x) for x in v] for k, v in sorted(self.samples.items())},
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=1), encoding="utf-8")

    def summary(self) -> dict:
        out = {}
        for key, values in sorted(self.samples.items()):
            v = sorted(values)
            if not v:
                continue
            p95 = v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]
            out[key] = {"n": len(v), "p50": round(statistics.median(v)), "p95": round(p95)}
        return out


METRICS = Metrics()
