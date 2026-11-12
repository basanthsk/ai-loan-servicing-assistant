"""Application settings loaded from environment variables and the .env file.

Keys are optional at load time, because not every command needs every provider.
Code that needs a key calls `require(...)`, which fails with a clear message
naming each missing key.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM providers
    llm_provider_chain: str = "cerebras-qwen,groq,cerebras,groq-fast,openrouter"
    # Longest a turn waits for a rate-limited provider to recover before giving up
    llm_max_wait_s: float = 20.0

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_planner_model: str = "openai/gpt-oss-120b"
    groq_fast_model: str = "openai/gpt-oss-20b"

    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    cerebras_planner_model: str = "gpt-oss-120b"
    # Decider model backend: "llm" (the fast role of the LLM chain) or "jev"
    # (TypeSafe's JEV decision model, with the LLM as fallback when it is unavailable)
    decider_backend: str = "llm"
    typesafe_api_key: str = ""
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_model: str = "jev-latest"

    # Provider "openai" (paid; only used when listed in LLM_PROVIDER_CHAIN)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_planner_model: str = "gpt-5-mini"
    openai_fast_model: str = "gpt-5-nano"

    # Provider "cerebras-qwen": same Cerebras key, open-weight Qwen for both roles.
    # Its free limits are separate from gpt-oss-120b's and much higher.
    cerebras_qwen_model: str = "qwen-3.8-27b"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = ""

    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "qwen3:8b"
    ollama_fast_model: str = "qwen3:4b"

    # Voice
    deepgram_api_key: str = ""
    # deepgram = streaming (lowest latency); groq = Whisper over HTTP, segmented on pauses
    voice_stt: str = "deepgram"
    # deepgram = streaming WebSocket; deepgram-http = one HTTP request per sentence
    voice_tts: str = "deepgram"

    # Datasets
    hf_token: str = ""

    # LLM stub for load tests (scripts/llm_stub.py); use provider name "stub"
    stub_base_url: str = "http://127.0.0.1:8199/v1"

    # App
    jwt_secret: str = ""
    # Empty means in-memory session state (fine for one gateway process)
    redis_url: str = ""
    # Fixed "today" so the seeded data and every answer are reproducible
    demo_today: str = "2026-11-11"
    db_path: str = "data/bank.db"

    # Policy knowledge base (RAG): Hugging Face embedding model run locally with
    # fastembed (ONNX, no key needed), vectors stored in a local Chroma database.
    # "hash" is an offline keyword embedder used by the tests.
    kb_embedder: str = "fastembed"
    kb_embed_model: str = "BAAI/bge-small-en-v1.5"
    kb_model_dir: str = "data/models"
    kb_chroma_dir: str = "data/chroma"
    kb_top_k: int = 4
    # Hits less similar than this (cosine, 0-1) are dropped as not relevant
    kb_min_score: float = 0.6
    # A message routed "out of scope" is answered as a policy question instead when a
    # policy passage matches it at least this well (off-topic chatter scores below 0.53)
    kb_scope_score: float = 0.62

    # Mock enterprise systems
    crm_url: str = "http://127.0.0.1:8101"
    loans_url: str = "http://127.0.0.1:8102"
    payments_url: str = "http://127.0.0.1:8103"
    policy_url: str = "http://127.0.0.1:8104"
    serviceops_url: str = "http://127.0.0.1:8105"
    cards_url: str = "http://127.0.0.1:8106"
    gateway_port: int = 8000

    def system_url(self, system: str) -> str:
        return getattr(self, f"{system}_url")

    @property
    def provider_chain(self) -> list[str]:
        return [p.strip() for p in self.llm_provider_chain.split(",") if p.strip()]


class MissingSettingError(RuntimeError):
    pass


@lru_cache
def get_settings() -> Settings:
    return Settings()


def require(*names: str, settings: Settings | None = None) -> None:
    """Raise MissingSettingError listing every named setting that is empty."""
    s = settings or get_settings()
    missing = [n.upper() for n in names if not getattr(s, n.lower(), "")]
    if missing:
        raise MissingSettingError(
            "Missing required setting(s): "
            + ", ".join(missing)
            + ". Add them to your .env file (see .env.example)."
        )
