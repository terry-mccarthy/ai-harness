"""Ollama embedder for the architect server.

The ``Embedder`` protocol lives in :mod:`architect_server.search`. This module
provides one concrete implementation that talks to a local Ollama server using
the same ``POST /api/embed`` convention used by ``harness_memory``.
"""
from __future__ import annotations

import os

import httpx


_DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
_DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
_DEFAULT_TIMEOUT = float(os.environ.get("EMBED_TIMEOUT", "120"))


class OllamaEmbedder:
    """Synchronous Ollama embedder. Embeds one text per call or many in batch."""

    def __init__(
        self,
        host: str = _DEFAULT_OLLAMA_HOST,
        model: str = _DEFAULT_EMBED_MODEL,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, payload: dict) -> list[list[float]]:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.host}/api/embed", json=payload)
            resp.raise_for_status()
        return resp.json()["embeddings"]

    def embed_one(self, text: str) -> list[float]:
        return self._post({"model": self.model, "input": text})[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._post({"model": self.model, "input": texts})
