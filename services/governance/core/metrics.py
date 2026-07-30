"""Prometheus metric definitions."""
from prometheus_client import Counter, Histogram


tool_calls_total = Counter(
    "harness_tool_calls_total",
    "Total tool invocations by agent role and decision",
    ["agent_role", "decision"],
)

tool_call_latency = Histogram(
    "harness_tool_call_latency_ms",
    "Tool call latency in milliseconds",
    ["agent_role"],
    buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)

llm_calls_total = Counter(
    "harness_llm_calls_total",
    "Total LLM invocations reported via audit, by agent role, provider, and model",
    ["agent_role", "provider", "model"],
)

llm_tokens_total = Counter(
    "harness_llm_tokens_total",
    "Total LLM tokens consumed, by agent role, provider, model, and token type",
    ["agent_role", "provider", "model", "token_type"],
)

# Issue #04: write_audit swallows Dolt failures (see core/dolt.py) so a tool
# call's response is never blocked by an audit outage. This counter is the
# durable, observable failure signal that replaces "just a log line" —
# scrape via /metrics and alert on rate() > 0. It does not recover the lost
# audit row; it makes the loss visible instead of silent.
audit_write_failures_total = Counter(
    "harness_audit_write_failures_total",
    "Total failed Dolt audit_log writes (write_audit exceptions)",
    [],
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
