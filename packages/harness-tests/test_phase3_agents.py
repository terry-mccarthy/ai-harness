"""Phase 3 — Specialised Agent Nodes.

10 tests across three agents. Unit tests use MockLLMProvider + mocked gateway.
Integration tests (marked integration) run against the live Docker stack.
"""
import inspect
import json
import os
import uuid
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockLLMProvider:
    def __init__(self, response: str | list[str]):
        if isinstance(response, list):
            self._responses = list(reversed(response))
        else:
            self._responses = [response]

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(content=self._responses[-1] if len(self._responses) == 1 else self._responses.pop())


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

_PHASE_RECON = json.dumps({
    "phase": "reconnaissance",
    "domain": "AI code review system",
    "architectural_style": "Microservices",
    "dependencies": [{"name": "PostgreSQL", "role": "database"}],
    "red_flags": [],
    "critical_path_suggestion": "code review submission flow",
    "interfaces_to_examine": ["gateway/client.py"],
})

_PHASE_FLOW = json.dumps({
    "phase": "flow_trace",
    "critical_path": "code review submission flow",
    "flow_summary": "Request enters via MCP tool",
    "structural_violations": [],
    "coupling_issues": [],
    "layering_assessment": "isolated",
    "domain_isolation_score": 8,
})

_PHASE_ABSTRACTION = json.dumps({
    "phase": "abstraction_analysis",
    "interface_findings": [],
    "leaky_abstractions": [],
    "isp_violations": [],
    "swap_difficulty": "moderate",
    "abstraction_score": 8,
})

_PHASE_SYNTHESIS = json.dumps({
    "title": "Architecture Review: AI Harness",
    "status": "completed",
    "summary": "System is well-architected",
    "current_state_assessment": "Layered microservices with clear boundaries",
    "findings": [{"severity": "LOW", "category": "modularity", "title": "minor concern", "message": "", "location": "", "phase_origin": "reconnaissance"}],
    "technical_debt_hotspots": [{"rank": 1, "area": "gateway", "description": "coupling", "impact": "medium"}],
    "nfr_risks": [{"concern": "scalability", "risk": "potential bottleneck", "severity": "LOW"}],
    "recommendations": [{"priority": 1, "action": "refactor gateway", "rationale": "reduce coupling", "roi": "high"}],
    "alternatives_considered": [],
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

async def test_architect_produces_review():
    """Given a feature request, architect returns a multi-phase architecture review report."""
    from harness_agents.architect import ArchitectAgent
    from harness_agents.types import AgentState

    agent = ArchitectAgent(
        gateway=_mock_gateway(),
        llm_provider=MockLLMProvider([_PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS]),
    )
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
    output = result["agent_output"]
    assert output["title"] == "Architecture Review: AI Harness"
    assert output["status"] == "completed"
    assert len(output["findings"]) > 0
    assert len(output["recommendations"]) > 0
    assert "_phases" in output
    assert "reconnaissance" in output["_phases"]
    assert "flow_trace" in output["_phases"]
    assert "abstraction_analysis" in output["_phases"]


def _architect_state(task: str) -> "AgentState":
    return {
        "task": task,
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }


# Schema-invalid synthesis: parses as JSON but omits required findings/recommendations.
_INVALID_SYNTHESIS = json.dumps({
    "title": "Architecture Review: AI Harness",
    "status": "completed",
    "summary": "missing the required findings and recommendations arrays",
})


async def test_architect_synthesis_retries_on_schema_violation():
    """A synthesis output that violates ARCHITECT_OUTPUT_SCHEMA is rejected and retried;
    a subsequent schema-valid response is accepted."""
    from harness_agents.architect import ArchitectAgent

    agent = ArchitectAgent(
        gateway=_mock_gateway(),
        llm_provider=MockLLMProvider(
            [_PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _INVALID_SYNTHESIS, _PHASE_SYNTHESIS]
        ),
    )
    result = await agent.run(_architect_state("Design the persistent memory layer"))

    assert result["error"] is None
    output = result["agent_output"]
    assert len(output["findings"]) > 0
    assert len(output["recommendations"]) > 0


async def test_architect_errors_when_synthesis_never_schema_valid():
    """If synthesis never produces schema-valid output across all retries, run() errors."""
    from harness_agents.architect import ArchitectAgent

    agent = ArchitectAgent(
        gateway=_mock_gateway(),
        llm_provider=MockLLMProvider(
            [_PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION,
             _INVALID_SYNTHESIS, _INVALID_SYNTHESIS, _INVALID_SYNTHESIS]
        ),
    )
    result = await agent.run(_architect_state("Design the persistent memory layer"))

    assert result["agent_output"] is None
    assert result["error"] is not None
    assert result["error"]["code"] == "invalid_output"


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
    """codebase_search returns real GitHub code search results from the friday repo.

    Proves ADR-0036 slice 1: the host-side architect server is registered with MCPJungle
    and the architect role can reach it through the governance + gateway path.
    """
    from harness_gateway.client import GatewayClient

    gw = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="architect",
        client_secret=os.environ.get("ARCHITECT_SECRET", "architect-secret"),
    )
    result = await gw.call_tool("codebase_search", {
        "query": "import ast",
        "repo": "https://github.com/psf/black",
        "top_k": 3,
    })

    assert "results" in result, f"expected real response shape, got {result!r}"
    results = result["results"]
    assert results, "expected at least one result for a query that matches real repo content"
    first = results[0]
    for field in ("path", "repo", "html_url"):
        assert field in first, f"result missing field {field!r}: {first!r}"


@pytest.mark.integration
async def test_architect_adr_read_returns_real_records():
    """adr_read returns real ADR records from <repo>/docs/adr/, not stub echo.

    Proves ADR-0036 slice 2: the host-side architect server can read ADRs from
    a passed-in repo path through the governance + gateway path.
    """
    from harness_gateway.client import GatewayClient

    gw = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="architect",
        client_secret=os.environ.get("ARCHITECT_SECRET", "architect-secret"),
    )
    result = await gw.call_tool(
        "adr_read",
        {"query": "architect", "repo": "https://github.com/terry-mccarthy/ai-harness"},
    )

    assert "adrs" in result, f"expected real response shape, got {result!r}"
    adrs = result["adrs"]
    assert adrs, "expected at least one ADR for a query matching ADR-0036"
    first = adrs[0]
    for field in ("id", "title", "status", "path", "content"):
        assert field in first, f"adr missing field {field!r}: {first!r}"
    assert first["id"] == "0036"
    assert "architect" in first["title"].lower()


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
