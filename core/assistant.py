"""Build the assistant from settings: packs, LLM chain, Decider, connectors, orchestrator."""

from __future__ import annotations

from dataclasses import dataclass

from core.connectors import ConnectorHub
from core.decider import Decider, JevDecider, LLMJsonDecider
from core.llm import build_chain
from core.orchestrator import Orchestrator
from core.registry import Pack, load_packs
from core.session import SessionStore
from core.settings import Settings, get_settings


@dataclass
class Assistant:
    settings: Settings
    packs: dict[str, Pack]
    hub: ConnectorHub
    orchestrator: Orchestrator
    sessions: SessionStore


def build_assistant(settings: Settings | None = None, packs: list[str] | None = None) -> Assistant:
    s = settings or get_settings()
    loaded = load_packs(packs)
    chain = build_chain(s)
    hub = ConnectorHub(s)
    llm_decider = LLMJsonDecider(chain)
    if s.decider_backend == "jev" and s.typesafe_api_key:
        jev = JevDecider(s.typesafe_api_key, s.typesafe_base_url, s.typesafe_model)
        decider = Decider(jev, fallback=llm_decider)
    else:
        decider = Decider(llm_decider)
    orch = Orchestrator(s, loaded, chain, decider, hub)
    return Assistant(s, loaded, hub, orch, SessionStore(s.redis_url))
