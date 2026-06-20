"""Prometheus metric definitions for the review server."""
import time
from prometheus_client import Counter, Histogram

from harness_agents.llm import LLMProvider, LLMResponse


llm_calls_total = Counter(
    "harness_llm_calls_total",
    "Total LLM invocations by provider, model, and agent role",
    ["provider", "model", "agent_role"],
)

llm_tokens_total = Counter(
    "harness_llm_tokens_total",
    "Total LLM tokens consumed by provider, model, and token type",
    ["provider", "model", "token_type"],
)

llm_latency_seconds = Histogram(
    "harness_llm_latency_seconds",
    "LLM call latency in seconds",
    ["provider", "model"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
)


class MonitoredLLMProvider:
    """Wraps an LLMProvider and records Prometheus metrics around each call."""

    def __init__(self, provider: LLMProvider, agent_role: str = "code_reviewer"):
        self._inner = provider
        self._agent_role = agent_role

    @property
    def provider_name(self) -> str:
        return self._inner.provider_name

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    async def chat(self, messages: list) -> LLMResponse:
        start = time.monotonic()
        response = await self._inner.chat(messages)
        elapsed = time.monotonic() - start

        llm_calls_total.labels(
            provider=self._inner.provider_name,
            model=self._inner.model_name,
            agent_role=self._agent_role,
        ).inc()
        llm_tokens_total.labels(
            provider=self._inner.provider_name,
            model=self._inner.model_name,
            token_type="prompt",
        ).inc(response.prompt_tokens)
        llm_tokens_total.labels(
            provider=self._inner.provider_name,
            model=self._inner.model_name,
            token_type="completion",
        ).inc(response.completion_tokens)
        llm_latency_seconds.labels(
            provider=self._inner.provider_name,
            model=self._inner.model_name,
        ).observe(elapsed)

        return response
