"""Unit tests for the pluggable embedding provider seam in harness_memory.

Mirrors packages/harness-tests/test_unit_llm_factory.py's structure for the
chat-provider factory (build_llm_from_env), but scoped to embeddings.

No real Ollama calls — provider construction / dispatch only. Only the
'ollama' backend is implemented today (see issue 08); Gemini/OpenRouter
embedding providers are deferred.
"""
import numpy as np
import pytest


# ---------------------------------------------------------------------------
# build_embedding_provider_from_env — resolution order: kwarg > config > env > default
# ---------------------------------------------------------------------------

def test_defaults_to_ollama_provider(monkeypatch):
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    from harness_memory.embedding_provider import OllamaEmbeddingProvider, build_embedding_provider_from_env
    provider = build_embedding_provider_from_env()
    assert isinstance(provider, OllamaEmbeddingProvider)


def test_ollama_reads_env_vars(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://myhost:11434")
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    from harness_memory.embedding_provider import OllamaEmbeddingProvider, build_embedding_provider_from_env
    provider = build_embedding_provider_from_env()
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.model_name == "nomic-embed-text"
    assert provider._host == "http://myhost:11434"


def test_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    from harness_memory.embedding_provider import OllamaEmbeddingProvider, build_embedding_provider_from_env
    provider = build_embedding_provider_from_env(provider="ollama", model="mxbai-embed-large")
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.model_name == "mxbai-embed-large"


def test_config_dict_selects_provider(monkeypatch):
    """config['embedding_provider'] overrides EMBEDDING_PROVIDER env var."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    from harness_memory.embedding_provider import OllamaEmbeddingProvider, build_embedding_provider_from_env
    provider = build_embedding_provider_from_env(
        config={"embedding_provider": "ollama", "ollama": {"model": "nomic-embed-text"}}
    )
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.model_name == "nomic-embed-text"


def test_config_dict_model_overrides_env(monkeypatch):
    """config[provider][model] overrides EMBED_MODEL env var."""
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    from harness_memory.embedding_provider import OllamaEmbeddingProvider, build_embedding_provider_from_env
    provider = build_embedding_provider_from_env(config={"ollama": {"model": "mxbai-embed-large"}})
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.model_name == "mxbai-embed-large"


def test_kwarg_overrides_config_dict(monkeypatch):
    """Direct kwarg takes precedence over config dict value."""
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    from harness_memory.embedding_provider import OllamaEmbeddingProvider, build_embedding_provider_from_env
    provider = build_embedding_provider_from_env(
        config={"ollama": {"model": "nomic-embed-text"}},
        model="mxbai-embed-large",
    )
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.model_name == "mxbai-embed-large"


def test_config_dict_provider_kwarg_still_wins(monkeypatch):
    """Explicit provider= kwarg beats config['embedding_provider']."""
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    from harness_memory.embedding_provider import OllamaEmbeddingProvider, build_embedding_provider_from_env
    provider = build_embedding_provider_from_env(
        provider="ollama",
        config={"embedding_provider": "gemini"},
    )
    assert isinstance(provider, OllamaEmbeddingProvider)


def test_empty_config_dict_falls_through_to_env(monkeypatch):
    """Empty config dict is equivalent to no config."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("EMBED_MODEL", "nomic-embed-text")
    from harness_memory.embedding_provider import OllamaEmbeddingProvider, build_embedding_provider_from_env
    provider = build_embedding_provider_from_env(config={})
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.model_name == "nomic-embed-text"


def test_unknown_provider_raises_listing_only_implemented(monkeypatch):
    """Unsupported provider names raise ValueError listing only what's implemented today."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    from harness_memory.embedding_provider import build_embedding_provider_from_env
    with pytest.raises(ValueError, match="gemini") as exc_info:
        build_embedding_provider_from_env()
    # Must not pretend gemini/openrouter are supported — only ollama is real today.
    assert "openrouter" not in str(exc_info.value)
    assert "ollama" in str(exc_info.value)


def test_unknown_provider_openrouter_raises(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openrouter")
    from harness_memory.embedding_provider import build_embedding_provider_from_env
    with pytest.raises(ValueError, match="openrouter"):
        build_embedding_provider_from_env()


# ---------------------------------------------------------------------------
# EmbeddingProvider protocol conformance — fake provider, no real Ollama call
# ---------------------------------------------------------------------------

class _FakeEmbeddingProvider:
    """Minimal EmbeddingProvider Protocol implementation for isolated unit tests."""

    provider_name = "fake"

    def __init__(self, model_name: str = "fake-model", vector: np.ndarray | None = None):
        self.model_name = model_name
        self._vector = vector if vector is not None else np.zeros(4, dtype=np.float32)
        self.calls: list[str] = []

    async def embed(self, text: str) -> np.ndarray:
        self.calls.append(text)
        return self._vector


@pytest.mark.asyncio
async def test_memory_store_embed_delegates_to_provider():
    """PostgresMemoryStore._embed() is a thin delegation to the injected provider."""
    from harness_memory.memory_store import PostgresMemoryStore

    provider = _FakeEmbeddingProvider(vector=np.array([1.0, 2.0, 3.0], dtype=np.float32))
    store = PostgresMemoryStore("postgresql://unused", "redis://unused", provider)

    result = await store._embed("hello world")

    assert provider.calls == ["hello world"]
    assert np.array_equal(result, provider._vector)


@pytest.mark.asyncio
async def test_memory_store_dim_cache_keys_off_provider_model_name():
    """_embed_dim_cache keys off provider.model_name, not a raw string param."""
    from harness_memory.memory_store import PostgresMemoryStore

    unique_model = "fake-dim-test-model"
    PostgresMemoryStore._embed_dim_cache.pop(unique_model, None)
    provider = _FakeEmbeddingProvider(model_name=unique_model, vector=np.zeros(7, dtype=np.float32))
    store = PostgresMemoryStore("postgresql://unused", "redis://unused", provider)

    dim = await store._resolve_embedding_dim()

    assert dim == 7
    assert PostgresMemoryStore._embed_dim_cache[unique_model] == 7
