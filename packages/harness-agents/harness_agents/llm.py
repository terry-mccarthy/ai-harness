import os
import re
from typing import Protocol, List, Dict
from dataclasses import dataclass
import httpx

# OpenAI o-series reasoning models reject the temperature parameter outright.
_O_SERIES_RE = re.compile(r"openai/o\d", re.IGNORECASE)

@dataclass
class LLMResponse:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

class LLMProvider(Protocol):
    provider_name: str
    model_name: str

    async def chat(self, messages: List[Dict[str, str]]) -> LLMResponse:
        ...

class OllamaProvider:
    provider_name = "ollama"

    def __init__(
        self,
        host: str,
        model: str,
        num_ctx: int = 8192,
        temperature: float = 0.1,
        num_predict: int = 1024,
        timeout: float = 120.0,
    ):
        from ollama import AsyncClient
        self.client = AsyncClient(host=host, timeout=timeout)
        self.model_name = model
        self.model = model
        self._options = {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        }

    async def chat(self, messages: List[Dict[str, str]]) -> LLMResponse:
        response = await self.client.chat(
            model=self.model,
            messages=messages,
            options=self._options,
        )
        return LLMResponse(
            content=response.message.content,
            prompt_tokens=response.prompt_eval_count or 0,
            completion_tokens=response.eval_count or 0,
        )

class GeminiProvider:
    provider_name = "gemini"

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 1024,
    ):
        from google import genai
        self.client = genai.Client(api_key=api_key)
        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    def _build_contents(self, messages: List[Dict[str, str]], types):
        """Split a message list into Gemini contents and an optional system instruction."""
        contents = []
        system_instruction = None
        for msg in messages:
            role, text = msg["role"], msg["content"]
            if role == "system":
                system_instruction = text
            elif role == "user":
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
            elif role == "assistant":
                contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
        return contents, system_instruction

    async def chat(self, messages: List[Dict[str, str]]) -> LLMResponse:
        from google.genai import types

        contents, system_instruction = self._build_contents(messages, types)
        config = types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
        )
        if system_instruction:
            config.system_instruction = system_instruction

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        usage = response.usage_metadata
        return LLMResponse(
            content=response.text,
            prompt_tokens=(usage.prompt_token_count or 0) if usage else 0,
            completion_tokens=(usage.candidates_token_count or 0) if usage else 0,
        )


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


def _build_ollama(overrides: dict, cfg: dict) -> "OllamaProvider":
    return OllamaProvider(
        host=_pick(overrides.get("host"), cfg.get("host"), "OLLAMA_HOST", "http://localhost:11434"),
        model=_pick(overrides.get("model"), cfg.get("model"), "OLLAMA_MODEL", "qwen2.5-coder:7b"),
        num_ctx=int(_pick(overrides.get("num_ctx"), cfg.get("num_ctx"), "OLLAMA_NUM_CTX", "8192")),
        temperature=float(_pick(overrides.get("temperature"), cfg.get("temperature"), "LLM_TEMPERATURE", "OLLAMA_TEMPERATURE", "0.1")),
        num_predict=int(_pick(overrides.get("num_predict") or overrides.get("max_tokens"), cfg.get("num_predict"), "OLLAMA_NUM_PREDICT", "LLM_MAX_TOKENS", "1024")),
    )


def _build_gemini(overrides: dict, cfg: dict) -> "GeminiProvider":
    api_key = _pick(None, cfg.get("api_key"), "GEMINI_API_KEY", "")
    if not str(api_key or "").strip():
        raise ValueError("GEMINI_API_KEY is required for the gemini provider")
    return GeminiProvider(
        model=_pick(overrides.get("model"), cfg.get("model"), "GEMINI_MODEL", "gemini-2.5-flash"),
        api_key=str(api_key).strip(),
        temperature=float(_pick(overrides.get("temperature"), cfg.get("temperature"), "LLM_TEMPERATURE", "0.1")),
        max_output_tokens=int(_pick(overrides.get("max_tokens"), cfg.get("max_output_tokens"), "LLM_MAX_TOKENS", "1024")),
    )


def _build_openrouter(overrides: dict, cfg: dict) -> "OpenRouterProvider":
    api_key = _pick(None, cfg.get("api_key"), "OPENROUTER_API_KEY", "")
    if not str(api_key or "").strip():
        raise ValueError("OPENROUTER_API_KEY is required for the openrouter provider")
    return OpenRouterProvider(
        api_key=str(api_key).strip(),
        model=_pick(overrides.get("model"), cfg.get("model"), "OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
        temperature=float(_pick(overrides.get("temperature"), cfg.get("temperature"), "LLM_TEMPERATURE", "0.1")),
        max_tokens=int(_pick(overrides.get("max_tokens"), cfg.get("max_tokens"), "LLM_MAX_TOKENS", "1024")),
    )


_PROVIDER_BUILDERS = {
    "ollama": _build_ollama,
    "gemini": _build_gemini,
    "openrouter": _build_openrouter,
}


def build_llm_from_env(
    provider: str | None = None,
    config: dict | None = None,
    **overrides,
) -> "LLMProvider":
    """Build an LLM provider. Resolution order: kwarg > config dict > env var > default.

    config mirrors the server_config JSONB schema:
      {"llm_provider": "gemini", "gemini": {"model": "...", "api_key": "..."}, ...}

    Supported providers: 'ollama' (default), 'gemini', 'openrouter'.
    """
    cfg = config or {}
    provider_name = _pick(provider, cfg.get("llm_provider"), "LLM_PROVIDER", "ollama").lower()
    builder = _PROVIDER_BUILDERS.get(provider_name)
    if not builder:
        raise ValueError(
            f"Unknown LLM provider: {provider_name!r}. Supported: ollama, gemini, openrouter"
        )
    return builder(overrides, cfg.get(provider_name, {}))


async def list_available_models(provider: str, config: dict | None = None) -> list[str]:
    """Return model names/ids available from the given provider.

    Does not require a running LLM — queries the provider's catalogue API.
    Raises ValueError for unsupported providers.
    """
    cfg = (config or {}).get(provider, {})
    if provider == "ollama":
        host = cfg.get("host") or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{host}/api/tags")
            return [m["name"] for m in r.json().get("models", [])]
    if provider == "openrouter":
        key = cfg.get("api_key") or os.environ.get("OPENROUTER_API_KEY", "")
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            return [m["id"] for m in r.json().get("data", [])]
    raise ValueError(f"list_available_models: unsupported provider {provider!r}")


def build_role_llm(role: str, config: dict | None = None, **overrides) -> "LLMProvider":
    """Build an LLM provider for a specific agent role.

    Checks config['role_models'][role] for a provider/model override, then
    falls back to the global config and env vars via build_llm_from_env.

    role_models entry schema:
      {"provider": "openrouter", "model": "claude-opus-4-8"}
    Both keys are optional — omitting provider keeps the global provider.
    """
    cfg = config or {}
    role_cfg = cfg.get("role_models", {}).get(role, {})
    provider = role_cfg.get("provider") or cfg.get("llm_provider") or None
    # Merge role model into provider sub-dict so _pick resolution works
    if role_cfg.get("model"):
        provider_name = provider or overrides.get("provider") or \
            __import__("os").environ.get("LLM_PROVIDER", "ollama")
        merged_cfg = {**cfg, provider_name: {**cfg.get(provider_name, {}), "model": role_cfg["model"]}}
    else:
        merged_cfg = cfg
    return build_llm_from_env(provider=provider, config=merged_cfg, **overrides)


class OpenRouterProvider:
    """LLM provider backed by OpenRouter (https://openrouter.ai).

    OpenRouter exposes an OpenAI-compatible API so any model on their
    catalogue — including high-context variants (200K–1M tokens) — is
    available by setting OPENROUTER_MODEL.
    """

    provider_name = "openrouter"
    _BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str,
        model: str = "anthropic/claude-3.5-sonnet",
        temperature: float = 0.1,
        max_tokens: int = 1024,
        timeout: float = 120.0,
    ):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self._BASE_URL,
            timeout=timeout,
        )
        self.model_name = model
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def chat(self, messages: List[Dict[str, str]]) -> LLMResponse:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,  # type: ignore[arg-type]
            "max_tokens": self.max_tokens,
        }
        if not _O_SERIES_RE.search(self.model):
            kwargs["temperature"] = self.temperature
        response = await self.client.chat.completions.create(**kwargs)
        if not response.choices:
            raise ValueError(
                f"OpenRouter returned empty choices for model {self.model!r} "
                "(content filter or upstream error)"
            )
        content = response.choices[0].message.content or ""
        usage = response.usage
        return LLMResponse(
            content=content,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )
