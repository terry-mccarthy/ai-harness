"""Phase 3 — Specialised Agent Nodes.

14 tests across three agents. Unit tests use MockLLMProvider + mocked gateway.
Integration tests (marked integration) run against the live Docker stack.
"""
import inspect
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockLLMProvider:
    def __init__(self, response: str):
        self._response = response

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(content=self._response)


def _mock_gateway(tool_responses: dict | None = None):
    """Return a GatewayClient-shaped mock with configurable per-tool responses."""
    gw = MagicMock()
    gw.last_calls = []
    responses = tool_responses or {}

    async def call_tool(name, params=None):
        gw.last_calls.append({"tool": name, "params": params or {}})
        return responses.get(name, {"result": "ok"})

    gw.call_tool = call_tool
    return gw


_VALID_ADR = json.dumps({
    "title": "ADR-001: Use PostgreSQL for memory store",
    "status": "proposed",
    "context": "We need persistent cross-session memory.",
    "decision": "Use PostgreSQL with pgvector.",
    "consequences": "Requires pgvector extension.",
    "alternatives_considered": [
        {"option": "Redis only", "reason_rejected": "No persistence"}
    ],
})

_VALID_INCIDENT = json.dumps({
    "timeline": "Alert fired at 14:00, DB latency spiked to 5s",
    "likely_cause": "Connection pool exhausted",
    "severity": "P2",
    "recommended_steps": [
        {"action": "Restart connection pool", "rationale": "Clears stale connections", "requires_approval": False}
    ],
    "runbook_ref": None,
    "requires_human_approval": False,
})

_VALID_FINDINGS = json.dumps({
    "verdict": "fail",
    "findings": [
        {"severity": "CRITICAL", "file": "auth.py", "line": 14,
         "message": "Password printed to stdout", "suggestion": "Remove print"}
    ],
    "summary": "Critical credential leak found.",
})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_memory_store():
    store = MagicMock()
    store.read = AsyncMock(return_value=None)
    store.write = AsyncMock()
    store.search = AsyncMock(return_value=[])
    return store


# ---------------------------------------------------------------------------
# Slice 1 — AgentNode Protocol compliance (all three agents)
# ---------------------------------------------------------------------------

def test_agent_node_contract_compliance():
    """All three agent classes satisfy the AgentNode Protocol."""
    from harness_agents.protocols import AgentNode
    from harness_agents.architect import ArchitectAgent
    from harness_agents.reviewer import CodeReviewerAgent
    from harness_agents.sre import SREAgent

    for cls in (ArchitectAgent, CodeReviewerAgent, SREAgent):
        for attr in ("name", "allowed_tools", "memory_namespace"):
            assert hasattr(cls, attr), f"{cls.__name__} missing Protocol attr: {attr}"
        assert "run" in {m for m, _ in inspect.getmembers(cls, predicate=inspect.isfunction)}, \
            f"{cls.__name__} missing run()"


# ---------------------------------------------------------------------------
# Slice 2 — ArchitectAgent
# ---------------------------------------------------------------------------

async def test_architect_produces_adr():
    """Given a feature request, architect returns a dict matching the ADR contract."""
    from harness_agents.architect import ArchitectAgent
    from harness_agents.types import AgentState, ARCHITECT_OUTPUT_SCHEMA
    import jsonschema

    agent = ArchitectAgent(gateway=_mock_gateway(), llm_provider=MockLLMProvider(_VALID_ADR))
    state: AgentState = {
        "task": "Design the persistent memory layer",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    result = await agent.run(state)

    assert result["error"] is None
    jsonschema.validate(result["agent_output"], ARCHITECT_OUTPUT_SCHEMA)


async def test_architect_reads_past_adrs(mock_memory_store):
    """Architect searches memory for past ADRs before generating output."""
    from harness_agents.architect import ArchitectAgent
    from harness_agents.types import AgentState

    agent = ArchitectAgent(
        gateway=_mock_gateway(),
        llm_provider=MockLLMProvider(_VALID_ADR),
        memory_store=mock_memory_store,
    )
    state: AgentState = {
        "task": "Design auth layer",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    await agent.run(state)

    mock_memory_store.search.assert_awaited()


async def test_architect_writes_adr_to_memory(mock_memory_store):
    """After run(), memory store contains a new entry under architect/ namespace."""
    from harness_agents.architect import ArchitectAgent
    from harness_agents.types import AgentState

    agent = ArchitectAgent(
        gateway=_mock_gateway(),
        llm_provider=MockLLMProvider(_VALID_ADR),
        memory_store=mock_memory_store,
    )
    state: AgentState = {
        "task": "Design auth layer",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    await agent.run(state)

    mock_memory_store.write.assert_awaited_once()
    call_args = mock_memory_store.write.call_args
    assert call_args.args[0] == "architect"  # namespace


@pytest.mark.integration
async def test_architect_tool_calls_go_via_gateway():
    """Architect's codebase_search call is visible in gateway audit log."""
    import os
    from harness_agents.architect import ArchitectAgent
    from harness_agents.types import AgentState
    from harness_gateway.client import GatewayClient

    gw = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="architect",
        client_secret=os.environ.get("ARCHITECT_SECRET", "architect-secret"),
    )
    agent = ArchitectAgent(gateway=gw, llm_provider=MockLLMProvider(_VALID_ADR))
    state: AgentState = {
        "task": "Check current ADRs",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    await agent.run(state)

    assert any(c["tool"] == "codebase_search" for c in gw.last_calls)


@pytest.mark.integration
async def test_architect_codebase_search_returns_real_chunks():
    """codebase_search returns real BM25-ranked chunks from the friday repo, not stub echo.

    Proves ADR-0036 slice 1: the host-side architect server is registered with MCPJungle
    and the architect role can reach it through the governance + gateway path.
    """
    import os
    from harness_gateway.client import GatewayClient

    gw = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="architect",
        client_secret=os.environ.get("ARCHITECT_SECRET", "architect-secret"),
    )
    result = await gw.call_tool("codebase_search", {"query": "audit log dolt commit", "top_k": 3})

    assert "chunks" in result, f"expected real response shape, got {result!r}"
    chunks = result["chunks"]
    assert chunks, "expected at least one chunk for a query that matches real repo content"
    first = chunks[0]
    for field in ("file", "start_line", "end_line", "text", "score"):
        assert field in first, f"chunk missing field {field!r}: {first!r}"
    assert first["score"] > 0


@pytest.mark.integration
async def test_architect_denied_shell_exec():
    """Architect node raises ToolAccessDenied if it attempts shell_exec."""
    import os
    from harness_agents.architect import ArchitectAgent
    from harness_agents.types import AgentState
    from harness_gateway.client import GatewayClient, ToolAccessDenied

    gw = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="architect",
        client_secret=os.environ.get("ARCHITECT_SECRET", "architect-secret"),
    )
    with pytest.raises(ToolAccessDenied):
        await gw.call_tool("shell_exec", {"command": "ls"})


# ---------------------------------------------------------------------------
# Slice 3 — CodeReviewer Phase 3 additions
# ---------------------------------------------------------------------------

async def test_reviewer_produces_structured_findings():
    """Given a diff, reviewer returns findings with severity/file/line/message."""
    from harness_agents.reviewer import CodeReviewerAgent
    from harness_agents.types import AgentState, REVIEWER_OUTPUT_SCHEMA
    import jsonschema

    agent = CodeReviewerAgent(
        gateway=_mock_gateway({"git_diff": {"diff": "..."}, "run_linter": {"issues": []}}),
        llm_provider=MockLLMProvider(_VALID_FINDINGS),
    )
    state: AgentState = {
        "task": "Review this diff",
        "diff": "diff --git a/auth.py ...",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    result = await agent.run(state)

    assert result["error"] is None
    jsonschema.validate(result["agent_output"], REVIEWER_OUTPUT_SCHEMA)
    assert len(result["agent_output"]["findings"]) > 0


async def test_reviewer_verdict_fail_on_critical():
    """If any finding is CRITICAL, verdict is 'fail'."""
    from harness_agents.reviewer import CodeReviewerAgent
    from harness_agents.types import AgentState

    agent = CodeReviewerAgent(
        gateway=_mock_gateway({"git_diff": {}, "run_linter": {}}),
        llm_provider=MockLLMProvider(_VALID_FINDINGS),
    )
    state: AgentState = {
        "task": "Review",
        "diff": "some diff",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    result = await agent.run(state)

    assert result["agent_output"]["verdict"] == "fail"


async def test_reviewer_loop_max_iterations():
    """Reviewer gives up and returns error after 3 failed parse attempts."""
    from harness_agents.reviewer import CodeReviewerAgent
    from harness_agents.types import AgentState

    agent = CodeReviewerAgent(
        gateway=_mock_gateway({"git_diff": {}, "run_linter": {}}),
        llm_provider=MockLLMProvider("this is not json at all"),
    )
    state: AgentState = {
        "task": "Review",
        "diff": "diff",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    result = await agent.run(state)

    assert result["error"] is not None
    assert result["error"]["code"] == "invalid_output"


async def test_reviewer_reads_conventions(mock_memory_store):
    """Reviewer searches memory for repo conventions before the first LLM call."""
    from harness_agents.reviewer import CodeReviewerAgent
    from harness_agents.types import AgentState

    agent = CodeReviewerAgent(
        gateway=_mock_gateway({"git_diff": {}, "run_linter": {}}),
        llm_provider=MockLLMProvider(_VALID_FINDINGS),
        memory_store=mock_memory_store,
    )
    state: AgentState = {
        "task": "Review",
        "diff": "diff",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    await agent.run(state)

    mock_memory_store.search.assert_awaited()


# ---------------------------------------------------------------------------
# Slice 4 — SREAgent
# ---------------------------------------------------------------------------

async def test_sre_produces_incident_report():
    """Given an alert input, SRE returns a dict matching the incident output contract."""
    from harness_agents.sre import SREAgent
    from harness_agents.types import AgentState, SRE_OUTPUT_SCHEMA
    import jsonschema

    agent = SREAgent(
        gateway=_mock_gateway({
            "observability_query": {"metrics": []},
            "log_search": {"logs": []},
            "runbook_read": {"runbook": None},
        }),
        llm_provider=MockLLMProvider(_VALID_INCIDENT),
    )
    state: AgentState = {
        "task": "DB latency alert fired — p99 > 5s",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    result = await agent.run(state)

    assert result["error"] is None
    jsonschema.validate(result["agent_output"], SRE_OUTPUT_SCHEMA)


@pytest.mark.integration
async def test_sre_shell_exec_blocked_without_approval():
    """SRE node attempting shell_exec without human_approval_token receives 403."""
    import os
    from harness_gateway.client import GatewayClient, ToolAccessDenied

    gw = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
    )
    with pytest.raises(ToolAccessDenied, match="403"):
        await gw.call_tool("shell_exec", {"command": "ls"})


@pytest.mark.integration
async def test_sre_shell_exec_allowed_with_approval():
    """SRE with a valid human_approval_token in headers can call shell_exec."""
    import os
    from harness_gateway.client import GatewayClient

    gw = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
        human_approval_token="approved-test-token",
    )
    result = await gw.call_tool("shell_exec", {"command": "echo ok"})
    assert result is not None


async def test_sre_writes_incident_to_memory(mock_memory_store):
    """After resolving an incident, memory store has entry under sre/ namespace."""
    from harness_agents.sre import SREAgent
    from harness_agents.types import AgentState

    agent = SREAgent(
        gateway=_mock_gateway({
            "observability_query": {},
            "log_search": {},
            "runbook_read": {},
        }),
        llm_provider=MockLLMProvider(_VALID_INCIDENT),
        memory_store=mock_memory_store,
    )
    state: AgentState = {
        "task": "DB latency alert",
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    await agent.run(state)

    mock_memory_store.write.assert_awaited_once()
    assert mock_memory_store.write.call_args.args[0] == "sre"
