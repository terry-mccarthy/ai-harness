"""Sync Ollama chat client for the architect server.

The embedder lives in :mod:`architect_server.embeddings`; this module is the
LLM counterpart, talking to ``POST /api/chat`` with ``stream=false`` so the
caller gets a single string back.
"""
from __future__ import annotations

import os

import httpx


_DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
_DEFAULT_LLM_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
_DEFAULT_TEMPERATURE = float(os.environ.get("OLLAMA_TEMPERATURE", "0.1"))
_DEFAULT_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
_DEFAULT_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "300"))


class OllamaLLM:
    """Synchronous chat client. One system + one user message per call."""

    def __init__(
        self,
        host: str = _DEFAULT_OLLAMA_HOST,
        model: str = _DEFAULT_LLM_MODEL,
        temperature: float = _DEFAULT_TEMPERATURE,
        num_ctx: int = _DEFAULT_NUM_CTX,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout = timeout

    def chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.host}/api/chat", json=payload)
            resp.raise_for_status()
        body = resp.json()
        return body["message"]["content"]
