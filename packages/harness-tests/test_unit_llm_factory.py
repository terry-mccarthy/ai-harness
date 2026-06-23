"""Unit tests for the centralised build_llm_from_env() factory.

No real LLM calls — providers are constructed but not invoked.
"""
import os
import pytest


def test_defaults_to_ollama_provider(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    from harness_agents.llm import OllamaProvider, build_llm_from_env
    llm = build_llm_from_env()
    assert isinstance(llm, OllamaProvider)


def test_ollama_reads_env_vars(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://myhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:32b")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    from harness_agents.llm import OllamaProvider, build_llm_from_env
    llm = build_llm_from_env()
    assert isinstance(llm, OllamaProvider)
    assert llm.model == "qwen2.5-coder:32b"


def test_ollama_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    from harness_agents.llm import OllamaProvider, build_llm_from_env
    llm = build_llm_from_env(provider="ollama", model="qwen2.5-coder:32b")
    assert isinstance(llm, OllamaProvider)
    assert llm.model == "qwen2.5-coder:32b"


def test_openrouter_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
    from harness_agents.llm import OpenRouterProvider, build_llm_from_env
    llm = build_llm_from_env()
    assert isinstance(llm, OpenRouterProvider)
    assert llm.model == "anthropic/claude-3.5-sonnet"


def test_openrouter_max_tokens_kwarg(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    from harness_agents.llm import OpenRouterProvider, build_llm_from_env
    llm = build_llm_from_env(max_tokens=4096)
    assert isinstance(llm, OpenRouterProvider)
    assert llm.max_tokens == 4096


def test_openrouter_raises_without_api_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from harness_agents.llm import build_llm_from_env
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build_llm_from_env()


def test_gemini_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    from harness_agents.llm import GeminiProvider, build_llm_from_env
    llm = build_llm_from_env()
    assert isinstance(llm, GeminiProvider)
    assert llm.model == "gemini-2.5-flash"


def test_gemini_raises_without_api_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from harness_agents.llm import build_llm_from_env
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        build_llm_from_env()


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "cohere")
    from harness_agents.llm import build_llm_from_env
    with pytest.raises(ValueError, match="cohere"):
        build_llm_from_env()


def test_provider_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    from harness_agents.llm import OpenRouterProvider, build_llm_from_env
    llm = build_llm_from_env(provider="openrouter")
    assert isinstance(llm, OpenRouterProvider)


# ---------------------------------------------------------------------------
# DB config dict layer — config= kwarg mirrors the server_config JSONB schema
# ---------------------------------------------------------------------------

def test_config_dict_selects_provider(monkeypatch):
    """config['llm_provider'] overrides LLM_PROVIDER env var."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    from harness_agents.llm import GeminiProvider, build_llm_from_env
    llm = build_llm_from_env(config={"llm_provider": "gemini", "gemini": {"model": "gemini-2.5-flash"}})
    assert isinstance(llm, GeminiProvider)


def test_config_dict_model_overrides_env(monkeypatch):
    """config[provider][model] overrides OLLAMA_MODEL env var."""
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    from harness_agents.llm import OllamaProvider, build_llm_from_env
    llm = build_llm_from_env(config={"ollama": {"model": "qwen2.5-coder:32b"}})
    assert isinstance(llm, OllamaProvider)
    assert llm.model == "qwen2.5-coder:32b"


def test_kwarg_overrides_config_dict(monkeypatch):
    """Direct kwarg takes precedence over config dict value."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    from harness_agents.llm import OllamaProvider, build_llm_from_env
    llm = build_llm_from_env(
        config={"ollama": {"model": "qwen2.5-coder:7b"}},
        model="qwen2.5-coder:32b",
    )
    assert isinstance(llm, OllamaProvider)
    assert llm.model == "qwen2.5-coder:32b"


def test_config_dict_provider_kwarg_still_wins(monkeypatch):
    """Explicit provider= kwarg beats config['llm_provider']."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    from harness_agents.llm import OpenRouterProvider, build_llm_from_env
    llm = build_llm_from_env(
        provider="openrouter",
        config={"llm_provider": "gemini", "gemini": {"api_key": "fake"}},
    )
    assert isinstance(llm, OpenRouterProvider)


def test_empty_config_dict_falls_through_to_env(monkeypatch):
    """Empty config dict is equivalent to no config."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    from harness_agents.llm import OllamaProvider, build_llm_from_env
    llm = build_llm_from_env(config={})
    assert isinstance(llm, OllamaProvider)
    assert llm.model == "qwen2.5-coder:7b"


# ---------------------------------------------------------------------------
# build_role_llm — per-role model selection
# ---------------------------------------------------------------------------

def test_build_role_llm_no_role_models_uses_global_provider(monkeypatch):
    """Without role_models in config, build_role_llm behaves like build_llm_from_env."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    from harness_agents.llm import OllamaProvider, build_role_llm
    llm = build_role_llm("architect", config={})
    assert isinstance(llm, OllamaProvider)
    assert llm.model == "qwen2.5-coder:7b"


def test_build_role_llm_role_model_overrides_global_model(monkeypatch):
    """role_models[role].model takes precedence over the global provider model."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
    from harness_agents.llm import OllamaProvider, build_role_llm
    config = {
        "llm_provider": "ollama",
        "ollama": {"model": "qwen2.5-coder:7b"},
        "role_models": {
            "architect": {"model": "qwen2.5-coder:32b"},
        },
    }
    llm = build_role_llm("architect", config=config)
    assert isinstance(llm, OllamaProvider)
    assert llm.model == "qwen2.5-coder:32b"


def test_build_role_llm_other_role_unaffected(monkeypatch):
    """A role not in role_models still gets the global model."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    from harness_agents.llm import OllamaProvider, build_role_llm
    config = {
        "role_models": {"architect": {"model": "qwen2.5-coder:32b"}},
    }
    llm = build_role_llm("sre", config=config)
    assert isinstance(llm, OllamaProvider)
    assert llm.model == "qwen2.5-coder:7b"  # default


def test_build_role_llm_role_can_switch_provider(monkeypatch):
    """role_models[role].provider switches to a different LLM provider."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    from harness_agents.llm import OpenRouterProvider, build_role_llm
    config = {
        "llm_provider": "ollama",
        "ollama": {"model": "qwen2.5-coder:7b"},
        "openrouter": {"model": "claude-opus-4-8"},
        "role_models": {
            "architect": {"provider": "openrouter", "model": "claude-opus-4-8"},
        },
    }
    llm = build_role_llm("architect", config=config)
    assert isinstance(llm, OpenRouterProvider)
    assert llm.model == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# list_available_models — provider model discovery
# ---------------------------------------------------------------------------

import pytest

@pytest.mark.asyncio
async def test_list_available_models_ollama(monkeypatch):
    """list_available_models('ollama') returns model names from GET /api/tags."""
    from unittest.mock import AsyncMock, MagicMock, patch
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "models": [
            {"name": "qwen2.5-coder:7b"},
            {"name": "qwen2.5-coder:32b"},
            {"name": "nomic-embed-text:latest"},
        ]
    }
    mock_get = AsyncMock(return_value=fake_response)
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=MagicMock(get=mock_get))
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("harness_agents.llm.httpx.AsyncClient", return_value=mock_client):
        from harness_agents.llm import list_available_models
        models = await list_available_models("ollama")

    assert models == ["qwen2.5-coder:7b", "qwen2.5-coder:32b", "nomic-embed-text:latest"]


@pytest.mark.asyncio
async def test_list_available_models_openrouter(monkeypatch):
    """list_available_models('openrouter') returns model ids from GET /api/v1/models."""
    from unittest.mock import AsyncMock, MagicMock, patch
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "data": [
            {"id": "anthropic/claude-sonnet-4-6"},
            {"id": "anthropic/claude-opus-4-8"},
            {"id": "google/gemini-2.5-flash"},
        ]
    }
    mock_get = AsyncMock(return_value=fake_response)
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=MagicMock(get=mock_get))
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("harness_agents.llm.httpx.AsyncClient", return_value=mock_client):
        from harness_agents.llm import list_available_models
        models = await list_available_models("openrouter")

    assert models == [
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-8",
        "google/gemini-2.5-flash",
    ]


@pytest.mark.asyncio
async def test_list_available_models_unknown_provider_raises():
    """list_available_models raises ValueError for unsupported providers."""
    from harness_agents.llm import list_available_models
    with pytest.raises(ValueError, match="cohere"):
        await list_available_models("cohere")
