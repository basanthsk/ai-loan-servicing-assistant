import pytest

from core.settings import MissingSettingError, Settings, require


def test_provider_chain_is_parsed_in_order():
    s = Settings(_env_file=None, llm_provider_chain="groq, cerebras ,openrouter")
    assert s.provider_chain == ["groq", "cerebras", "openrouter"]


def test_require_names_every_missing_key():
    s = Settings(_env_file=None, groq_api_key="", deepgram_api_key="")
    with pytest.raises(MissingSettingError) as err:
        require("groq_api_key", "deepgram_api_key", settings=s)
    assert "GROQ_API_KEY" in str(err.value)
    assert "DEEPGRAM_API_KEY" in str(err.value)


def test_require_passes_when_key_present():
    s = Settings(_env_file=None, groq_api_key="test-key")
    require("groq_api_key", settings=s)
