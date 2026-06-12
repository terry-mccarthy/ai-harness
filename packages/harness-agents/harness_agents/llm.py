import re
from typing import Protocol, List, Dict
from dataclasses import dataclass

# OpenAI o-series reasoning models reject the temperature parameter outright.
_O_SERIES_RE = re.compile(r"openai/o\d", re.IGNORECASE)

@dataclass
class LLMResponse:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

class LLMProvider(Protocol):
    async def chat(self, messages: List[Dict[str, str]]) -> LLMResponse:
        ...

class OllamaProvider:
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
    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 1024,
    ):
        from google import genai
        self.client = genai.Client(api_key=api_key)
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


class OpenRouterProvider:
    """LLM provider backed by OpenRouter (https://openrouter.ai).

    OpenRouter exposes an OpenAI-compatible API so any model on their
    catalogue — including high-context variants (200K–1M tokens) — is
    available by setting OPENROUTER_MODEL.
    """

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
