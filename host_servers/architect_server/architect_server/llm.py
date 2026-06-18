"""Sync LLM clients for the architect server.

Supports Ollama, Google Gemini, and OpenRouter, selected at runtime by the
``LLM_PROVIDER`` env var (default: ``ollama``). Each client implements the
``chat(system: str, user: str) -> str`` protocol expected by
:mod:`architect_server.architecture_review` and :mod:`architect_server.diagram`.
"""
from __future__ import annotations

import os
import re

import httpx


# OpenAI o-series reasoning models reject the temperature parameter outright.
_O_SERIES_RE = re.compile(r"openai/o\d", re.IGNORECASE)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


_DEFAULT_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.1)
_DEFAULT_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 1024)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

class OllamaLLM:
    """Synchronous chat client using Ollama's ``/api/chat`` endpoint."""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        num_ctx: int | None = None,
        timeout: float = 300.0,
    ):
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
        self.temperature = temperature if temperature is not None else _env_float("OLLAMA_TEMPERATURE", _DEFAULT_TEMPERATURE)
        self.num_ctx = num_ctx if num_ctx is not None else _env_int("OLLAMA_NUM_CTX", 8192)
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


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

class GeminiLLM:
    """Synchronous chat client using Google Gemini."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ):
        from google import genai

        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not resolved_key:
            raise ValueError("GEMINI_API_KEY is required for the gemini provider")
        self.client = genai.Client(api_key=resolved_key)
        self.model = (
            model
            or os.environ.get("GEMINI_MODEL")
            or os.environ.get("GEMINI_LLM_MODEL")
            or "gemini-2.5-flash"
        )
        self.temperature = temperature if temperature is not None else _DEFAULT_TEMPERATURE
        self.max_output_tokens = max_output_tokens if max_output_tokens is not None else _DEFAULT_MAX_TOKENS

    def chat(self, system: str, user: str) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            system_instruction=system,
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=user,
            config=config,
        )
        return response.text


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

class OpenRouterLLM:
    """Synchronous chat client using OpenRouter (OpenAI-compatible API)."""

    _BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float = 120.0,
    ):
        from openai import OpenAI

        resolved_key = api_key or os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not resolved_key:
            raise ValueError("OPENROUTER_API_KEY is required for the openrouter provider")
        self.client = OpenAI(api_key=resolved_key, base_url=self._BASE_URL, timeout=timeout)
        self.model = model or os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")
        self.temperature = temperature if temperature is not None else _DEFAULT_TEMPERATURE
        self.max_tokens = max_tokens if max_tokens is not None else _DEFAULT_MAX_TOKENS

    def chat(self, system: str, user: str) -> str:
        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
        }
        if not _O_SERIES_RE.search(self.model):
            kwargs["temperature"] = self.temperature
        response = self.client.chat.completions.create(**kwargs)
        if not response.choices:
            raise ValueError(
                f"OpenRouter returned empty choices for model {self.model!r} "
                "(content filter or upstream error)"
            )
        return response.choices[0].message.content or ""
