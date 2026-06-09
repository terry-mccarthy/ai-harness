from typing import Protocol, List, Dict
from dataclasses import dataclass

@dataclass
class LLMResponse:
    content: str

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
        return LLMResponse(content=response.message.content)

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

    async def chat(self, messages: List[Dict[str, str]]) -> LLMResponse:
        from google.genai import types

        contents = []
        system_instruction = None

        for msg in messages:
            role = msg["role"]
            text = msg["content"]
            
            if role == "system":
                # System instructions are passed separately in the config
                system_instruction = text
            elif role == "user":
                contents.append(
                    types.Content(role="user", parts=[types.Part.from_text(text=text)])
                )
            elif role == "assistant":
                contents.append(
                    types.Content(role="model", parts=[types.Part.from_text(text=text)])
                )

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
        return LLMResponse(content=response.text)
