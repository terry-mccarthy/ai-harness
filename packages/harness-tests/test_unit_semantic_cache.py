"""Unit tests for DynamicSREAgent semantic cache.

All tests use a mock memory store — no Docker stack required.
"""
import uuid

import pytest

from harness_agents.types import AgentState

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_CACHED_OUTPUT = {
    "timeline": "Alert fired at 14:00",
    "likely_cause": "Connection pool exhausted",
    "severity": "P2",
    "recommended_steps": [
        {"action": "Restart pool", "rationale": "Clears stale connections", "requires_approval": False}
    ],
    "runbook_ref": None,
    "requires_human_approval": False,
}

_CACHED_STATE: AgentState = {
    "task": "DB latency alert fired",
    "thread_id": "aabbccdd-0000-0000-0000-000000000000",
    "agent_output": _CACHED_OUTPUT,
    "error": None,
    "requires_human_approval": False,
}


def _state(**overrides) -> AgentState:
    base: AgentState = {
        "task": "DB latency alert fired",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    base.update(overrides)
    return base


class _MockMemory:
    """Mock PostgresMemoryStore. search() returns a configurable result list."""

    def __init__(self, search_results: list[dict] | None = None):
        self._search_results = search_results or []
        self.written: list[dict] = []

    async def read(self, namespace: str, key: str) -> dict | None:
        return None

    async def search(self, namespace: str, query: str, top_k: int = 5) -> list[dict]:
        return self._search_results

    async def write(self, namespace: str, key: str, value: dict, ttl_hours: float | None = None, **_) -> None:
        self.written.append({"namespace": namespace, "key": key, "value": value, "ttl_hours": ttl_hours})


class _NeverLLM:
    """LLM that raises if called — verifies the loop was never entered."""
    async def chat(self, messages):
        raise AssertionError("LLM should not have been called on a cache hit")


class _OneTurnLLM:
    """LLM that returns a valid respond action in one turn."""
    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        import json
        return LLMResponse(
            content=json.dumps({"action": "respond", "result": _CACHED_OUTPUT}),
            prompt_tokens=10,
            completion_tokens=5,
        )


class _Gateway:
    async def call_tool(self, name, params):
        return {"result": "stub"}


# ---------------------------------------------------------------------------
# Behavior 1 — high-score hit → cache_hit: True returned, no LLM call
# ---------------------------------------------------------------------------

async def test_high_score_hit_returns_cached_result():
    from harness_agents.dynamic_sre import DynamicSREAgent

    memory = _MockMemory(search_results=[{"key": "cache:x", "value": _CACHED_STATE, "score": 0.95}])
    agent = DynamicSREAgent(gateway=_Gateway(), llm_provider=_NeverLLM(), memory_store=memory)

    result = await agent.run(_state())

    assert result.get("cache_hit") is True
    assert result["agent_output"] == _CACHED_OUTPUT


# ---------------------------------------------------------------------------
# Behavior 2 — low-score hit (below threshold) → loop runs, no cache_hit
# ---------------------------------------------------------------------------

async def test_low_score_hit_runs_loop():
    from harness_agents.dynamic_sre import DynamicSREAgent

    memory = _MockMemory(search_results=[{"key": "cache:x", "value": _CACHED_STATE, "score": 0.80}])
    agent = DynamicSREAgent(gateway=_Gateway(), llm_provider=_OneTurnLLM(), memory_store=memory)

    result = await agent.run(_state())

    assert result.get("cache_hit") is None
    assert result.get("agent_output") is not None


# ---------------------------------------------------------------------------
# Behavior 9 — force_refresh=True skips lookup even on high-score hit
# ---------------------------------------------------------------------------

async def test_force_refresh_skips_cache_lookup():
    from harness_agents.dynamic_sre import DynamicSREAgent

    memory = _MockMemory(search_results=[{"key": "cache:x", "value": _CACHED_STATE, "score": 0.99}])
    agent = DynamicSREAgent(gateway=_Gateway(), llm_provider=_OneTurnLLM(), memory_store=memory)

    result = await agent.run(_state(force_refresh=True))

    assert result.get("cache_hit") is None
    assert result.get("agent_output") is not None


# ---------------------------------------------------------------------------
# Behavior 4 — no memory store → agent runs unchanged (backward compat)
# ---------------------------------------------------------------------------

async def test_no_memory_store_agent_runs_unchanged():
    from harness_agents.dynamic_sre import DynamicSREAgent

    agent = DynamicSREAgent(gateway=_Gateway(), llm_provider=_OneTurnLLM(), memory_store=None)

    result = await agent.run(_state())

    assert result.get("cache_hit") is None
    assert result.get("agent_output") is not None


# ---------------------------------------------------------------------------
# Behavior 5 — _report_llm_usage not called on cache hit
# ---------------------------------------------------------------------------

async def test_llm_usage_not_reported_on_cache_hit():
    from harness_agents.dynamic_sre import DynamicSREAgent

    usage_reported = []

    class _TrackingGateway:
        async def call_tool(self, name, params):
            return {"result": "stub"}

        async def report_llm_usage(self, provider, model, prompt_tokens, completion_tokens):
            usage_reported.append((prompt_tokens, completion_tokens))

    class _TrackingLLM:
        provider_name = "test"
        model_name = "test-model"

        async def chat(self, messages):
            raise AssertionError("should not reach LLM")

    memory = _MockMemory(search_results=[{"key": "cache:x", "value": _CACHED_STATE, "score": 0.95}])
    agent = DynamicSREAgent(gateway=_TrackingGateway(), llm_provider=_TrackingLLM(), memory_store=memory)

    await agent.run(_state())

    assert usage_reported == []


# ---------------------------------------------------------------------------
# Behavior 7 — cache_threshold configurable (1.0 makes a 0.95-score miss)
# ---------------------------------------------------------------------------

async def test_configurable_threshold_respected():
    from harness_agents.dynamic_sre import DynamicSREAgent

    memory = _MockMemory(search_results=[{"key": "cache:x", "value": _CACHED_STATE, "score": 0.95}])
    agent = DynamicSREAgent(
        gateway=_Gateway(), llm_provider=_OneTurnLLM(), memory_store=memory, cache_threshold=1.0
    )

    result = await agent.run(_state())

    assert result.get("cache_hit") is None
    assert result.get("agent_output") is not None


# ---------------------------------------------------------------------------
# Behavior 8 — cache_ttl_seconds written as ttl_hours on successful completion
# ---------------------------------------------------------------------------

async def test_successful_run_writes_to_cache_with_ttl():
    from harness_agents.dynamic_sre import DynamicSREAgent

    memory = _MockMemory(search_results=[])
    agent = DynamicSREAgent(
        gateway=_Gateway(), llm_provider=_OneTurnLLM(), memory_store=memory, cache_ttl_seconds=3600
    )

    result = await agent.run(_state(thread_id="aabbccdd-1234-0000-0000-000000000000"))

    assert result.get("error") is None
    cache_writes = [w for w in memory.written if w["namespace"] == "cache"]
    assert len(cache_writes) == 1
    assert cache_writes[0]["ttl_hours"] == pytest.approx(1.0)
    assert cache_writes[0]["value"] == {"task": _state()["task"], "agent_output": _CACHED_OUTPUT}


# ---------------------------------------------------------------------------
# Behavior 6 — failed run (error in result) → no write to "cache" namespace
# ---------------------------------------------------------------------------

async def test_failed_run_does_not_write_to_cache():
    from harness_agents.dynamic_sre import DynamicSREAgent, MAX_TURNS

    memory = _MockMemory(search_results=[])

    # Always call a tool, never respond → max_turns_exceeded
    class _LoopForeverLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            import json
            return LLMResponse(
                content=json.dumps({"action": "call_tool", "tool": "observability_query", "params": {"query": "x"}}),
                prompt_tokens=1,
                completion_tokens=1,
            )

    agent = DynamicSREAgent(gateway=_Gateway(), llm_provider=_LoopForeverLLM(), memory_store=memory)
    result = await agent.run(_state())

    assert result["error"]["code"] == "max_turns_exceeded"
    cache_writes = [w for w in memory.written if w["namespace"] == "cache"]
    assert cache_writes == []


# ---------------------------------------------------------------------------
# Behavior 3 — empty search result → loop runs normally
# ---------------------------------------------------------------------------

async def test_empty_search_result_runs_loop():
    from harness_agents.dynamic_sre import DynamicSREAgent

    memory = _MockMemory(search_results=[])
    agent = DynamicSREAgent(gateway=_Gateway(), llm_provider=_OneTurnLLM(), memory_store=memory)

    result = await agent.run(_state())

    assert result.get("cache_hit") is None
    assert result.get("agent_output") is not None


# ---------------------------------------------------------------------------
# Behavior 10 — force_refresh=True suppresses cache write on successful run
# ---------------------------------------------------------------------------

async def test_force_refresh_does_not_write_to_cache():
    from harness_agents.dynamic_sre import DynamicSREAgent

    memory = _MockMemory(search_results=[])
    agent = DynamicSREAgent(gateway=_Gateway(), llm_provider=_OneTurnLLM(), memory_store=memory)

    result = await agent.run(_state(force_refresh=True))

    assert result.get("error") is None
    assert result.get("agent_output") is not None
    cache_writes = [w for w in memory.written if w["namespace"] == "cache"]
    assert cache_writes == []
