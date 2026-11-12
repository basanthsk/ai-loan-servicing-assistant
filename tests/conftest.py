"""Shared fixtures: mock systems in-process and a scripted LLM, so orchestrator tests
run offline and deterministically."""

from __future__ import annotations

import os

# Tests use their own database so they never change the demo's data/bank.db.
os.environ["DB_PATH"] = "data/test.db"
os.environ["KB_EMBEDDER"] = "hash"
os.environ["VOICE_LOG"] = "evals/cache/test-voice-calls.jsonl"
os.environ["LIVE_METRICS"] = "evals/cache/test-live-metrics.json"
os.environ["KB_CHROMA_DIR"] = "data/chroma-test"
os.environ["KB_SCOPE_SCORE"] = "0.2"  # hash-embedder scores run lower

import json  # noqa: E402
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    """Fresh database for the test run (tests create service requests)."""
    subprocess.run(
        [sys.executable, str(ROOT / "data" / "seed.py")], check=True, capture_output=True
    )
    yield


class RouterTransport(httpx.AsyncBaseTransport):
    """Send each request to the in-process FastAPI app that owns its port."""

    def __init__(self, apps_by_port: dict[int, object]):
        self.transports = {p: httpx.ASGITransport(app=a) for p, a in apps_by_port.items()}

    async def handle_async_request(self, request):
        return await self.transports[request.url.port].handle_async_request(request)


@pytest.fixture
def system_apps():
    from services import crm, loans, payments, policy, serviceops
    from services.common import Faults

    apps = {
        "crm": crm.app,
        "loans": loans.app,
        "payments": payments.app,
        "policy": policy.app,
        "serviceops": serviceops.app,
    }
    try:
        from services import cards

        apps["cards"] = cards.app
    except ImportError:
        pass
    saved = {k: a.state.faults for k, a in apps.items()}
    for a in apps.values():
        a.state.faults = Faults(latency_ms=0, jitter_ms=0, failure_rate=0)
    yield apps
    for k, a in apps.items():
        a.state.faults = saved[k]


def wire_hub(hub, settings, apps):
    by_port = {urlparse(settings.system_url(name)).port: app for name, app in apps.items()}
    client = httpx.AsyncClient(transport=RouterTransport(by_port))
    hub.client = client
    for c in hub.connectors.values():
        c.client = client


# --- Scripted LLM ---------------------------------------------------------------------


def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def text_reply(text: str):
    return {"text": text}


def tool_reply(*calls: tuple[str, dict], text: str = ""):
    return {"text": text, "calls": calls}


class FakeStream:
    def __init__(self, reply):
        self.provider, self.model, self.attempts = "fake", "fake-planner", []
        chunks = []
        if reply.get("text"):
            chunks += [_chunk(content=w + " ") for w in reply["text"].split(" ")]
        for i, (name, args) in enumerate(reply.get("calls", ())):
            fn = SimpleNamespace(name=name, arguments=json.dumps(args))
            chunks.append(
                _chunk(tool_calls=[SimpleNamespace(index=i, id=f"call_{i}", function=fn)])
            )
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c


class FakeChain:
    """Planner replies come from a script; Decider calls are answered by `decisions`."""

    def __init__(
        self,
        planner_script: list[dict],
        decisions: dict[str, str] | None = None,
        rewrite: str | None = None,
    ):
        self.planner_script = list(planner_script)
        self.decisions = decisions or {}
        self.rewrite = rewrite
        self.planner_calls = []

    async def astream(self, *, role, messages, **kw):
        self.planner_calls.append(messages)
        reply = self.planner_script.pop(0) if self.planner_script else text_reply("OK.")
        return FakeStream(reply)

    async def achat(self, *, role, messages, **kw):
        content = messages[-1]["content"]
        if role == "fast":  # Decider
            for needle, option in self.decisions.items():
                if needle in content:
                    body = json.dumps({option: 0.97})
                    break
            else:
                body = json.dumps({"loan_general": 0.6})
        else:
            body = self.rewrite or "OK."
        msg = SimpleNamespace(content=body)
        return SimpleNamespace(
            response=SimpleNamespace(choices=[SimpleNamespace(message=msg)]),
            provider="fake",
            model="fake",
        )


@pytest.fixture
def make_orchestrator(system_apps):
    from core.connectors import ConnectorHub
    from core.decider import Decider, LLMJsonDecider
    from core.orchestrator import Orchestrator
    from core.registry import load_packs
    from core.settings import Settings

    hubs = []

    def make(chain: FakeChain):
        settings = Settings(_env_file=None)
        hub = ConnectorHub(settings)
        wire_hub(hub, settings, system_apps)
        hubs.append(hub)
        return Orchestrator(settings, load_packs(), chain, Decider(LLMJsonDecider(chain)), hub)

    yield make


class Recorder:
    def __init__(self):
        self.events = []

    async def __call__(self, e):
        self.events.append(e)

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]

    @property
    def done(self):
        return self.of("done")[-1]
