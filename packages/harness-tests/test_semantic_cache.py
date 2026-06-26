"""Integration tests for DynamicSREAgent semantic cache.

Requires the live Docker stack (PG + Redis + Ollama for embeddings).
The agent's ReAct loop uses a scripted mock LLM — no chat model needed.
"""
import json
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get("PG_DSN", "postgresql://harness:harness@localhost:5432/harness")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")

_VALID_OUTPUT = {
    "timeline": "Alert fired at 14:00, DB latency spiked to 5s",
    "likely_cause": "Connection pool exhausted",
    "severity": "P2",
    "recommended_steps": [
        {"action": "Restart connection pool", "rationale": "Clears stale connections", "requires_approval": False}
    ],
    "runbook_ref": None,
    "requires_human_approval": False,
}


class _ScriptedLLM:
    """Returns a valid respond action on the first (and only) call."""
    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(
            content=json.dumps({"action": "respond", "result": _VALID_OUTPUT}),
            prompt_tokens=10,
            completion_tokens=5,
        )


class _Gateway:
    async def call_tool(self, name, params):
        return {"result": "stub"}


def _state(task: str, **overrides):
    from harness_agents.types import AgentState
    base: AgentState = {
        "task": task,
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


@pytest.fixture
async def memory_store():
    from harness_memory.memory_store import PostgresMemoryStore
    store = PostgresMemoryStore(PG_DSN, REDIS_URL, EMBED_MODEL, OLLAMA_HOST)
    await store.setup()
    yield store
    await store._truncate()
    await store.close()


# ---------------------------------------------------------------------------
# Behavior 10 — same task submitted twice → second call returns cache_hit: True
# ---------------------------------------------------------------------------

async def test_same_task_twice_returns_cache_hit(memory_store):
    from harness_agents.dynamic_sre import DynamicSREAgent

    task = f"DB latency alert — p99 > 5s [{uuid.uuid4()}]"
    agent = DynamicSREAgent(
        gateway=_Gateway(), llm_provider=_ScriptedLLM(), memory_store=memory_store
    )

    first = await agent.run(_state(task))
    assert first.get("error") is None
    assert first.get("cache_hit") is None

    second = await agent.run(_state(task))
    assert second.get("cache_hit") is True
    assert second["agent_output"] == _VALID_OUTPUT


# ---------------------------------------------------------------------------
# Behavior 11 — semantically equivalent task → cache hit (live embedding)
# ---------------------------------------------------------------------------

async def test_semantically_equivalent_task_returns_cache_hit(memory_store):
    from harness_agents.dynamic_sre import DynamicSREAgent

    tag = str(uuid.uuid4())
    task_a = f"DB latency alert fired — p99 over 5 seconds [{tag}]"
    task_b = f"Database latency alert triggered — p99 latency exceeds 5s [{tag}]"

    agent = DynamicSREAgent(
        gateway=_Gateway(),
        llm_provider=_ScriptedLLM(),
        memory_store=memory_store,
        cache_threshold=0.88,
    )

    first = await agent.run(_state(task_a))
    assert first.get("error") is None

    second = await agent.run(_state(task_b))
    assert second.get("cache_hit") is True
    assert second["agent_output"] == _VALID_OUTPUT


# ---------------------------------------------------------------------------
# Behavior 12 — expired cache entry treated as miss
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Behavior 13 — force_refresh=True on a cached task runs full loop, no cache_hit
# ---------------------------------------------------------------------------

async def test_force_refresh_bypasses_cached_result_and_runs_loop(memory_store):
    from harness_agents.dynamic_sre import DynamicSREAgent

    task = f"DB latency alert — force refresh [{uuid.uuid4()}]"
    agent = DynamicSREAgent(
        gateway=_Gateway(), llm_provider=_ScriptedLLM(), memory_store=memory_store
    )

    # Populate cache
    first = await agent.run(_state(task))
    assert first.get("error") is None
    assert first.get("cache_hit") is None

    # Confirm it's cached
    second = await agent.run(_state(task))
    assert second.get("cache_hit") is True

    # force_refresh=True must bypass the cache and run the loop
    call_count = {"n": 0}

    class _CountingScriptedLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            call_count["n"] += 1
            return LLMResponse(
                content=json.dumps({"action": "respond", "result": _VALID_OUTPUT}),
                prompt_tokens=10,
                completion_tokens=5,
            )

    fresh_agent = DynamicSREAgent(
        gateway=_Gateway(), llm_provider=_CountingScriptedLLM(), memory_store=memory_store
    )
    third = await fresh_agent.run(_state(task, force_refresh=True))

    assert third.get("cache_hit") is None
    assert third.get("agent_output") is not None
    assert call_count["n"] >= 1


# ---------------------------------------------------------------------------
# Behavior 12 — expired cache entry treated as miss
# ---------------------------------------------------------------------------

async def test_expired_cache_entry_is_a_miss(memory_store):
    from harness_agents.dynamic_sre import DynamicSREAgent

    task = f"DB latency alert — expired [{uuid.uuid4()}]"

    # Manually write a cache entry that is already expired
    key = f"cache:{hash(task)}"
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await memory_store.write(
        "cache", key, {"task": task, "agent_output": _VALID_OUTPUT, "error": None},
        _expires_at=past,
    )

    call_count = {"n": 0}

    class _CountingLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            call_count["n"] += 1
            return LLMResponse(
                content=json.dumps({"action": "respond", "result": _VALID_OUTPUT}),
                prompt_tokens=10,
                completion_tokens=5,
            )

    agent = DynamicSREAgent(
        gateway=_Gateway(), llm_provider=_CountingLLM(), memory_store=memory_store
    )

    result = await agent.run(_state(task))

    assert result.get("cache_hit") is None
    assert call_count["n"] >= 1
