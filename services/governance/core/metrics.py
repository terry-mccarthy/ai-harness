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
