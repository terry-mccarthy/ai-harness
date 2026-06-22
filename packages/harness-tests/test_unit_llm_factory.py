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
