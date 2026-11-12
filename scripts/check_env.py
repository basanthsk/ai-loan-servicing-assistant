"""Show which settings are configured, without printing any secret values.

Run: poetry run poe check-env
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.settings import get_settings  # noqa: E402

KEYS = {
    "CEREBRAS_API_KEY": "Cerebras (primary LLM: Qwen 3.8 27B; also gpt-oss-120b fallback)",
    "GROQ_API_KEY": "Groq (fallback LLM)",
    "OPENROUTER_API_KEY": "OpenRouter (fallback LLM)",
    "DEEPGRAM_API_KEY": "Deepgram (speech-to-text, text-to-speech)",
    "OPENAI_API_KEY": "OpenAI (optional, paid provider)",
    "TYPESAFE_API_KEY": "TypeSafe JEV (optional Decider backend)",
    "HF_TOKEN": "Hugging Face (Svarah dataset)",
}


def main() -> int:
    s = get_settings()
    print(f".env found: {Path('.env').exists()}")
    print(f"Provider chain: {', '.join(s.provider_chain)}\n")
    for key, label in KEYS.items():
        status = "set" if getattr(s, key.lower()) else "MISSING"
        print(f"  {status:8} {key:20} {label}")
    if len(s.jwt_secret) < 32:
        print("\n  note     JWT_SECRET not set (or short): the gateway uses a random one per run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
