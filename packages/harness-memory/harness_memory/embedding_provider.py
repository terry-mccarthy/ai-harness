"""Pluggable embedding providers for PostgresMemoryStore.

Mirrors the LLMProvider/build_llm_from_env pattern in
`harness_agents/llm.py`, but scoped to embeddings and living here in
harness_memory — this package already owns httpx/asyncpg, and only the
memory store needs embeddings, so this avoids a new cross-package
dependency on harness_agents.

Only 'ollama' is implemented today. Gemini/OpenRouter embedding backends
are deferred until a hosted deployment actually needs a second backend
(see issue 08 — pluggable embedding provider).
"""
import os
from typing import Protocol

import httpx
import numpy as np


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str

    async def embed(self, text: str) -> np.ndarray:
        ...


class OllamaEmbeddingProvider:
    """Embedding provider backed by Ollama's `/api/embed` endpoint.

    Pure extraction of PostgresMemoryStore's original hardcoded `_embed()`
    body — same request shape, same return type, no behavior change.
    """

    provider_name = "ollama"

    def __init__(self, host: str, model: str):
        self._host = host.rstrip("/")
        self.model_name = model

    async def embed(self, text: str) -> np.ndarray:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._host}/api/embed",
                json={"model": self.model_name, "input": text},
            )
            resp.raise_for_status()
        data = resp.json()
        return np.array(data["embeddings"][0], dtype=np.float32)


def _pick(override, config_val, *env_vars_and_default):
    """Return the first non-empty value: override > config_val > env vars > default."""
    if override is not None and override != "":
        return override
    if config_val is not None and config_val != "":
        return config_val
    *env_vars, default = env_vars_and_default
    for var in env_vars:
        val = os.environ.get(var, "")
        if val:
            return val
    return default


def _build_ollama(overrides: dict, cfg: dict) -> "OllamaEmbeddingProvider":
    return OllamaEmbeddingProvider(
        host=_pick(overrides.get("host"), cfg.get("host"), "OLLAMA_HOST", "http://localhost:11434"),
        model=_pick(overrides.get("model"), cfg.get("model"), "EMBED_MODEL", "nomic-embed-text"),
    )


_PROVIDER_BUILDERS = {
    "ollama": _build_ollama,
}


def build_embedding_provider_from_env(
    provider: str | None = None,
    config: dict | None = None,
    **overrides,
) -> "EmbeddingProvider":
    """Build an embedding provider. Resolution order: kwarg > config dict > env var > default.

    config mirrors the server_config JSONB schema:
      {"embedding_provider": "ollama", "ollama": {"model": "...", "host": "..."}, ...}

    Supported providers: 'ollama' (default). Unknown provider names raise
    ValueError listing only what's actually implemented today.
    """
    cfg = config or {}
    provider_name = _pick(
        provider, cfg.get("embedding_provider"), "EMBEDDING_PROVIDER", "ollama"
    ).lower()
    builder = _PROVIDER_BUILDERS.get(provider_name)
    if not builder:
        supported = ", ".join(sorted(_PROVIDER_BUILDERS))
        raise ValueError(
            f"Unknown embedding provider: {provider_name!r}. Supported: {supported}"
        )
    return builder(overrides, cfg.get(provider_name, {}))
