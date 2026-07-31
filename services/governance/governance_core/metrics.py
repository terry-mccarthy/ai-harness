"""Prometheus metric definitions."""
from prometheus_client import CollectorRegistry, Counter, Histogram

# Each service owns its own explicit registry instead of relying on
# prometheus_client's implicit global default (`prometheus_client.REGISTRY`).
# In production each service is a separate process, so the implicit global
# default is harmless there — but sharing it means importing this module
# alongside another service's metrics module (e.g. in a combined pytest
# session) raises `ValueError: Duplicated timeseries in CollectorRegistry`
# for any same-named metric. See issue #03 / packages/harness-tests/
# test_no_prometheus_registry_collision.py.
REGISTRY = CollectorRegistry()

tool_calls_total = Counter(
    "harness_tool_calls_total",
    "Total tool invocations by agent role and decision",
    ["agent_role", "decision"],
    registry=REGISTRY,
)

tool_call_latency = Histogram(
    "harness_tool_call_latency_ms",
    "Tool call latency in milliseconds",
    ["agent_role"],
    buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
    registry=REGISTRY,
)

llm_calls_total = Counter(
    "harness_llm_calls_total",
    "Total LLM invocations reported via audit, by agent role, provider, and model",
    ["agent_role", "provider", "model"],
    registry=REGISTRY,
)

llm_tokens_total = Counter(
    "harness_llm_tokens_total",
    "Total LLM tokens consumed, by agent role, provider, model, and token type",
    ["agent_role", "provider", "model", "token_type"],
    registry=REGISTRY,
)


def record_llm_usage(agent_role: str, body: dict) -> None:
    """Increment LLM counters if the audit body contains llm_tokens."""
    llm_data = body.get("llm_tokens")
    if not llm_data:
        return
    provider = body.get("llm_provider", "unknown")
    model = body.get("llm_model", "unknown")
    llm_calls_total.labels(agent_role=agent_role, provider=provider, model=model).inc()
    prompt = int(llm_data.get("prompt", 0))
    completion = int(llm_data.get("completion", 0))
    if prompt:
        llm_tokens_total.labels(
            agent_role=agent_role, provider=provider, model=model, token_type="prompt"
        ).inc(prompt)
    if completion:
        llm_tokens_total.labels(
            agent_role=agent_role, provider=provider, model=model, token_type="completion"
        ).inc(completion)
