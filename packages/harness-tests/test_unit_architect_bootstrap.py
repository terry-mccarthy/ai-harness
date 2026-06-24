"""Unit tests for architect bootstrap task type (ARCHITECTURE.md generation)."""
import json
import uuid
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

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
    "technical_debt_hotspots": [],
    "nfr_risks": [],
    "recommendations": [{"priority": 1, "action": "refactor gateway", "rationale": "reduce coupling", "roi": "high"}],
    "alternatives_considered": [],
})

_BOOTSTRAP_DOC = "# Architecture: AI Harness\n\n## Overview\nMicroservices system.\n"


class _SequentialMock:
    """Returns each response in sequence; repeats the last indefinitely."""
    def __init__(self, *responses: str):
        self._responses = list(responses)
        self._idx = 0

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return LLMResponse(content=resp)


def _mock_gateway():
    gw = MagicMock()
    gw.gateway_url = "http://fake-gateway"

    async def call_tool(name, params=None):
        return {"result": "ok"}

    gw.call_tool = call_tool
    return gw


def _architect_state(task: str, task_type: str = "") -> dict:
    return {
        "task": task,
        "task_type": task_type,
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }


# ---------------------------------------------------------------------------
# Bootstrap phase: architecture_md in output
# ---------------------------------------------------------------------------

async def test_bootstrap_adds_architecture_md():
    """When task_type='bootstrap', agent_output contains 'architecture_md' key."""
    from harness_agents.architect import ArchitectAgent

    llm = _SequentialMock(
        _PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS, _BOOTSTRAP_DOC
    )
    agent = ArchitectAgent(gateway=_mock_gateway(), llm_provider=llm)
    state = _architect_state("Bootstrap ARCHITECTURE.md for this repo", task_type="bootstrap")

    result = await agent.run(state)

    assert result["error"] is None
    assert "architecture_md" in result["agent_output"]
    doc = result["agent_output"]["architecture_md"]
    assert "# Architecture" in doc


async def test_non_bootstrap_omits_architecture_md():
    """When task_type is not 'bootstrap', agent_output has no 'architecture_md' key."""
    from harness_agents.architect import ArchitectAgent

    llm = _SequentialMock(
        _PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS
    )
    agent = ArchitectAgent(gateway=_mock_gateway(), llm_provider=llm)
    state = _architect_state("Design the persistent memory layer", task_type="design")

    result = await agent.run(state)

    assert result["error"] is None
    assert "architecture_md" not in result["agent_output"]


async def test_bootstrap_still_produces_synthesis_output():
    """Bootstrap run still includes the standard synthesis fields."""
    from harness_agents.architect import ArchitectAgent

    llm = _SequentialMock(
        _PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS, _BOOTSTRAP_DOC
    )
    agent = ArchitectAgent(gateway=_mock_gateway(), llm_provider=llm)
    state = _architect_state("Bootstrap ARCHITECTURE.md for this repo", task_type="bootstrap")

    result = await agent.run(state)

    output = result["agent_output"]
    assert output["title"] == "Architecture Review: AI Harness"
    assert "findings" in output
    assert "recommendations" in output
    assert "_phases" in output


async def test_bootstrap_continues_when_doc_phase_fails():
    """If the bootstrap LLM call fails, agent still returns synthesis output (no error)."""
    from harness_agents.architect import ArchitectAgent

    class _FailAfterFour:
        def __init__(self):
            self._calls = 0

        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            self._calls += 1
            if self._calls <= 4:
                responses = [_PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS]
                return LLMResponse(content=responses[self._calls - 1])
            raise RuntimeError("LLM unavailable")

    agent = ArchitectAgent(gateway=_mock_gateway(), llm_provider=_FailAfterFour())
    state = _architect_state("Bootstrap ARCHITECTURE.md", task_type="bootstrap")

    result = await agent.run(state)

    assert result["error"] is None
    assert "title" in result["agent_output"]
    # architecture_md should not be present since the call failed
    assert "architecture_md" not in result["agent_output"]


# ---------------------------------------------------------------------------
# Classifier: 'bootstrap' task type
# ---------------------------------------------------------------------------

async def test_classify_node_bootstrap_from_llm():
    """classify_node returns 'bootstrap' when LLM emits that task_type."""
    from harness_supervisor.nodes import classify_node
    from harness_agents.llm import LLMResponse

    class _LLM:
        async def chat(self, messages):
            return LLMResponse(content='{"task_type": "bootstrap", "confidence": 0.9, "reasoning": "generate doc"}')

    state = {"task": "Bootstrap the ARCHITECTURE.md for this repo", "tokens_used": 0, "thread_id": "t1"}
    result = await classify_node(state, llm_provider=_LLM())
    assert result["task_type"] == "bootstrap"


async def test_classify_node_bootstrap_keyword_fallback():
    """Keyword fallback classifies 'generate architecture.md' as bootstrap."""
    from harness_supervisor.nodes import classify_node
    from harness_agents.llm import LLMResponse

    class _FailLLM:
        async def chat(self, messages):
            raise RuntimeError("LLM unavailable")

    state = {"task": "generate architecture.md for the project", "tokens_used": 0, "thread_id": "t1"}
    result = await classify_node(state, llm_provider=_FailLLM())
    assert result["task_type"] == "bootstrap"


# ---------------------------------------------------------------------------
# Route: bootstrap → architect
# ---------------------------------------------------------------------------

def test_route_node_bootstrap_goes_to_architect():
    """route_node maps 'bootstrap' task_type to the architect node."""
    from harness_supervisor.nodes import route_node
    assert route_node({"task_type": "bootstrap"}) == "architect"


# ---------------------------------------------------------------------------
# Graph: bootstrap skips architectural gate
# ---------------------------------------------------------------------------

def test_route_after_architect_bootstrap_skips_gate():
    """Bootstrap tasks go straight to synthesise, bypassing the architectural gate."""
    from harness_supervisor.graph import _route_after_architect

    state = {"task_type": "bootstrap", "agent_output": {"title": "x"}, "error": None}
    assert _route_after_architect(state) == "synthesise"


def test_route_after_architect_design_goes_to_gate():
    """Non-bootstrap design tasks still go through the architectural gate."""
    from harness_supervisor.graph import _route_after_architect

    state = {"task_type": "design", "agent_output": {"title": "x"}, "error": None}
    assert _route_after_architect(state) == "architectural_gate"


def test_route_after_architect_error_goes_to_error_handler():
    """Error state always routes to error_handler regardless of task_type."""
    from harness_supervisor.graph import _route_after_architect

    state = {"task_type": "bootstrap", "error": {"code": "invalid_output"}}
    assert _route_after_architect(state) == "error_handler"
