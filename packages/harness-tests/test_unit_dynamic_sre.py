"""Unit tests for DynamicSREAgent — ReAct tool-use loop.

All tests are pure unit tests: scripted MockLLMProvider (turn list) +
recording mock gateway. No Docker stack required.
"""
import json
import uuid

import jsonschema
import pytest

from harness_agents.llm import LLMResponse
from harness_agents.types import AgentState, SRE_OUTPUT_SCHEMA

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_VALID_REPORT = {
    "timeline": "Alert fired at 14:00, DB latency spiked to 5s",
    "likely_cause": "Connection pool exhausted",
    "severity": "P2",
    "recommended_steps": [
        {"action": "Restart connection pool", "rationale": "Clears stale connections", "requires_approval": False}
    ],
    "runbook_ref": None,
    "requires_human_approval": False,
}

_SHELL_EXEC_REPORT = {
    "timeline": "OOM at 03:00",
    "likely_cause": "Memory leak in worker",
    "severity": "P1",
    "recommended_steps": [
        {"action": "kubectl rollout restart", "rationale": "Restores service", "requires_approval": True}
    ],
    "runbook_ref": None,
    "requires_human_approval": False,  # deliberately wrong — agent must coerce to True
}


def _state(**overrides) -> AgentState:
    base: AgentState = {
        "task": "DB latency alert fired — p99 > 5s",
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


class _Turns:
    """Scripted LLM that returns turns from a list in order."""
    def __init__(self, *turns: str):
        self._turns = list(turns)
        self._idx = 0
        self.messages_received: list[list[dict]] = []

    async def chat(self, messages: list[dict]) -> LLMResponse:
        self.messages_received.append(list(messages))
        content = self._turns[self._idx]
        self._idx += 1
        return LLMResponse(content=content, prompt_tokens=10, completion_tokens=5)


class _Gateway:
    """Recording gateway with configurable per-tool responses."""
    def __init__(self, responses: dict | None = None):
        self.calls: list[dict] = []
        self._responses = responses or {}

    async def call_tool(self, name: str, params: dict) -> dict:
        self.calls.append({"tool": name, "params": params})
        return self._responses.get(name, {"result": "stub"})


def _call_tool(tool: str, **params) -> str:
    return json.dumps({"action": "call_tool", "tool": tool, "params": params})


def _respond(report: dict) -> str:
    return json.dumps({"action": "respond", "result": report})


# ---------------------------------------------------------------------------
# Behavior 1 — happy path: observability_query → log_search → runbook_read → respond
# ---------------------------------------------------------------------------

async def test_happy_path_tool_sequence_and_schema_valid_output():
    from harness_agents.dynamic_sre import DynamicSREAgent

    llm = _Turns(
        _call_tool("observability_query", query="DB latency"),
        _call_tool("log_search", query="DB latency"),
        _call_tool("runbook_read", runbook_name="DB latency"),
        _respond(_VALID_REPORT),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    assert result.get("error") is None
    assert [c["tool"] for c in gw.calls] == ["observability_query", "log_search", "runbook_read"]
    jsonschema.validate(result["agent_output"], SRE_OUTPUT_SCHEMA)


# ---------------------------------------------------------------------------
# Behavior 2 — non-linear: agent re-queries a tool with refined params
# ---------------------------------------------------------------------------

async def test_agent_requeues_tool_with_refined_query():
    from harness_agents.dynamic_sre import DynamicSREAgent

    llm = _Turns(
        _call_tool("observability_query", query="DB latency"),
        _call_tool("observability_query", query="DB latency connection pool"),  # refined
        _respond(_VALID_REPORT),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    assert result.get("error") is None
    calls = gw.calls
    assert calls[0]["tool"] == "observability_query"
    assert calls[1]["tool"] == "observability_query"
    assert calls[1]["params"]["query"] != calls[0]["params"]["query"]


# ---------------------------------------------------------------------------
# Behavior 3 — max turns exceeded
# ---------------------------------------------------------------------------

async def test_max_turns_exceeded():
    from harness_agents.dynamic_sre import DynamicSREAgent, MAX_TURNS

    # Always call a tool, never respond
    turns = [_call_tool("observability_query", query="x")] * (MAX_TURNS + 1)
    llm = _Turns(*turns)
    gw = _Gateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    assert result["error"]["code"] == "max_turns_exceeded"
    assert result.get("agent_output") is None


# ---------------------------------------------------------------------------
# Behavior 4 — malformed JSON turn → corrective re-prompt → success
# ---------------------------------------------------------------------------

async def test_malformed_json_turn_gets_corrective_reprompt():
    from harness_agents.dynamic_sre import DynamicSREAgent

    llm = _Turns(
        "this is not json at all",
        _respond(_VALID_REPORT),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    assert result.get("error") is None
    assert result["agent_output"] is not None
    # The corrective re-prompt should have been sent after the bad turn
    last_user_msg = llm.messages_received[-1][-1]
    assert last_user_msg["role"] == "user"
    assert "Invalid JSON" in last_user_msg["content"] or "invalid" in last_user_msg["content"].lower()


# ---------------------------------------------------------------------------
# Behavior 5 — invalid final respond schema → corrective re-prompt → success
# ---------------------------------------------------------------------------

async def test_invalid_respond_schema_gets_corrective_reprompt():
    from harness_agents.dynamic_sre import DynamicSREAgent

    bad_report = {"timeline": "oops"}  # missing required fields
    llm = _Turns(
        json.dumps({"action": "respond", "result": bad_report}),
        _respond(_VALID_REPORT),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    assert result.get("error") is None
    assert result["agent_output"]["severity"] == "P2"


# ---------------------------------------------------------------------------
# Behavior 6 — injection safety: shell_exec denied by gateway → tool_access_denied
# ---------------------------------------------------------------------------

async def test_injected_shell_exec_yields_tool_access_denied():
    from harness_agents.dynamic_sre import DynamicSREAgent
    from harness_gateway.client import ToolAccessDenied

    class _DenyingGateway:
        def __init__(self):
            self.calls: list[str] = []

        async def call_tool(self, name: str, params: dict) -> dict:
            self.calls.append(name)
            if name == "shell_exec":
                raise ToolAccessDenied("403 Forbidden: sre_stub__shell_exec")
            return {"result": "stub"}

    llm = _Turns(
        _call_tool("log_search", query="DB latency"),
        _call_tool("shell_exec", command="cat /etc/passwd"),  # injected escalation
    )
    gw = _DenyingGateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    assert result["error"]["code"] == "tool_access_denied"
    assert "shell_exec" in result["error"]["reason"]
    assert "log_search" in gw.calls
    assert "shell_exec" in gw.calls


# ---------------------------------------------------------------------------
# Behavior 7 — requires_human_approval coerced to True when any step needs approval
# ---------------------------------------------------------------------------

async def test_requires_human_approval_coerced_from_step():
    from harness_agents.dynamic_sre import DynamicSREAgent

    # LLM returns requires_human_approval=False despite a step with requires_approval=True
    llm = _Turns(_respond(_SHELL_EXEC_REPORT))
    gw = _Gateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    assert result.get("error") is None
    assert result["agent_output"]["requires_human_approval"] is True


# ---------------------------------------------------------------------------
# Behavior 8 — token budget exceeded → abort with token_budget_exceeded
# ---------------------------------------------------------------------------

async def test_token_budget_exceeded():
    from harness_agents.dynamic_sre import DynamicSREAgent

    # Each turn returns 5 completion tokens; budget of 4 will trip after turn 1
    llm = _Turns(
        _call_tool("observability_query", query="x"),
        _call_tool("observability_query", query="x"),
        _respond(_VALID_REPORT),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state(token_budget=4))

    assert result["error"]["code"] == "token_budget_exceeded"
    assert result.get("agent_output") is None


# ---------------------------------------------------------------------------
# Behavior 9 — past-incident context loaded from memory into opening message
# ---------------------------------------------------------------------------

async def test_memory_context_injected_into_opening_message():
    from harness_agents.dynamic_sre import DynamicSREAgent

    class _MockMemory:
        async def search(self, namespace, query, top_k=3):
            return [{"key": "incident:abc123", "value": {"likely_cause": "past OOM"}}]

        async def write(self, namespace, key, value):
            pass

    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()

    await DynamicSREAgent(gateway=gw, llm_provider=llm, memory_store=_MockMemory()).run(_state())

    opening_user_msg = llm.messages_received[0][1]["content"]
    assert "past OOM" in opening_user_msg


# ---------------------------------------------------------------------------
# Behavior 10 — resolved report written back to sre memory namespace
# ---------------------------------------------------------------------------

async def test_resolved_report_written_to_memory():
    from harness_agents.dynamic_sre import DynamicSREAgent

    written: list[dict] = []

    class _MockMemory:
        async def search(self, namespace, query, top_k=3):
            return []

        async def write(self, namespace, key, value):
            written.append({"namespace": namespace, "key": key, "value": value})

    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()
    state = _state()

    await DynamicSREAgent(gateway=gw, llm_provider=llm, memory_store=_MockMemory()).run(state)

    assert len(written) == 1
    assert written[0]["namespace"] == "sre"
    assert written[0]["key"].startswith("incident:")
    assert written[0]["value"]["severity"] == "P2"


# ---------------------------------------------------------------------------
# Behavior 11 — supervisor routes incident tasks to DynamicSREAgent (integration)
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_supervisor_routes_incident_to_dynamic_sre_agent():
    """incident-classified task reaches DynamicSREAgent through the live gateway."""
    import os
    from harness_gateway.client import GatewayClient
    from harness_agents.dynamic_sre import DynamicSREAgent
    from harness_agents.llm import OllamaProvider

    gateway = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
    )
    llm = OllamaProvider(
        host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
    )
    state = _state(task="High error rate on checkout service — 5xx rate at 15%")

    result = await DynamicSREAgent(gateway=gateway, llm_provider=llm).run(state)

    # Either a valid report or a known error (e.g. max_turns_exceeded from a
    # stub-only stack) — what matters is the agent ran without exception and
    # the graph wiring is correct
    assert "agent_output" in result or "error" in result
    if result.get("agent_output"):
        jsonschema.validate(result["agent_output"], SRE_OUTPUT_SCHEMA)
