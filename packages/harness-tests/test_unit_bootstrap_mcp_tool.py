"""Unit tests for the bootstrap_architecture MCP tool on review-server.

Imports the tool function directly by absolute path (avoids collision with
github_mcp/server.py which is also named 'server'). Patches are done via
patch.object on the loaded module so the target is unambiguous.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Load review_server/server.py under a unique module name
# ---------------------------------------------------------------------------

_REVIEW_SERVER_PATH = Path(__file__).resolve().parents[2] / "services" / "review_server" / "server.py"
_REVIEW_SERVER_DIR = str(_REVIEW_SERVER_PATH.parent)
if _REVIEW_SERVER_DIR not in sys.path:
    sys.path.insert(0, _REVIEW_SERVER_DIR)

# Load once; subsequent imports from other test files won't interfere.
_spec = importlib.util.spec_from_file_location("_rv_server", _REVIEW_SERVER_PATH)
_srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_srv)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_PHASE_RECON = json.dumps({
    "phase": "reconnaissance",
    "domain": "AI code review system",
    "architectural_style": "Microservices",
    "dependencies": [],
    "red_flags": [],
    "critical_path_suggestion": "review flow",
    "interfaces_to_examine": [],
})

_PHASE_FLOW = json.dumps({
    "phase": "flow_trace",
    "critical_path": "review flow",
    "flow_summary": "Request via MCP",
    "structural_violations": [],
    "coupling_issues": [],
    "layering_assessment": "isolated",
    "domain_isolation_score": 9,
})

_PHASE_ABSTRACTION = json.dumps({
    "phase": "abstraction_analysis",
    "interface_findings": [],
    "leaky_abstractions": [],
    "isp_violations": [],
    "swap_difficulty": "low",
    "abstraction_score": 9,
})

_PHASE_SYNTHESIS = json.dumps({
    "title": "Architecture Review: AI Harness",
    "status": "completed",
    "summary": "System is well-architected",
    "current_state_assessment": "Layered microservices",
    "findings": [{"severity": "LOW", "category": "modularity", "title": "minor", "message": "", "location": "", "phase_origin": "reconnaissance"}],
    "technical_debt_hotspots": [],
    "nfr_risks": [],
    "recommendations": [{"priority": 1, "action": "refactor", "rationale": "improve", "roi": "high"}],
    "alternatives_considered": [],
})

_BOOTSTRAP_DOC = "# Architecture: AI Harness\n\n## Overview\nMicroservices system.\n"


def _make_sequential_llm(*responses):
    from harness_agents.llm import LLMResponse
    idx = {"i": 0}

    async def chat(messages):
        r = responses[min(idx["i"], len(responses) - 1)]
        idx["i"] += 1
        return LLMResponse(content=r)

    m = MagicMock()
    m.chat = chat
    return m


def _make_mock_gateway():
    gw = MagicMock()
    gw.gateway_url = "http://mcpjungle:8080"
    gw.governance_url = None
    gw.client_id = "architect"

    async def call_tool(name, params=None):
        return {"result": "ok"}

    gw.call_tool = call_tool
    return gw


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_bootstrap_architecture_returns_md():
    """Happy path: mock LLM returns valid phases + markdown, tool returns architecture_md."""
    llm = _make_sequential_llm(
        _PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS, _BOOTSTRAP_DOC
    )
    mock_gw = _make_mock_gateway()

    with patch.object(_srv, "_build_llm_provider", return_value=llm), \
         patch.object(_srv, "MonitoredLLMProvider", side_effect=lambda p, **kw: p), \
         patch.object(_srv, "GatewayClient", return_value=mock_gw):

        result = await _srv.bootstrap_architecture(repo="https://github.com/owner/repo")

    assert "architecture_md" in result
    assert "# Architecture" in result["architecture_md"]
    assert "summary" in result
    assert isinstance(result["findings"], list)


async def test_bootstrap_architecture_raises_on_agent_error():
    """When synthesis fails, tool raises RuntimeError."""
    llm = _make_sequential_llm("not json", "not json", "not json", "not json")
    mock_gw = _make_mock_gateway()

    with patch.object(_srv, "_build_llm_provider", return_value=llm), \
         patch.object(_srv, "MonitoredLLMProvider", side_effect=lambda p, **kw: p), \
         patch.object(_srv, "GatewayClient", return_value=mock_gw):

        with pytest.raises(RuntimeError):
            await _srv.bootstrap_architecture(repo="https://github.com/owner/repo")


async def test_bootstrap_architecture_uses_architect_credentials():
    """GatewayClient is constructed with client_id='architect'."""
    llm = _make_sequential_llm(
        _PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS, _BOOTSTRAP_DOC
    )
    mock_gw = _make_mock_gateway()
    captured = {}

    def capture_gw(**kwargs):
        captured.update(kwargs)
        return mock_gw

    with patch.object(_srv, "_build_llm_provider", return_value=llm), \
         patch.object(_srv, "MonitoredLLMProvider", side_effect=lambda p, **kw: p), \
         patch.object(_srv, "GatewayClient", side_effect=capture_gw):

        await _srv.bootstrap_architecture(repo="https://github.com/owner/repo")

    assert captured.get("client_id") == "architect"


async def test_bootstrap_architecture_default_task_includes_repo():
    """Default task string passed to ArchitectAgent contains the repo URL."""
    llm = _make_sequential_llm(
        _PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS, _BOOTSTRAP_DOC
    )
    mock_gw = _make_mock_gateway()
    captured_state = {}

    from harness_agents.architect import ArchitectAgent
    orig = ArchitectAgent.run

    async def spy_run(self, state):
        captured_state.update(state)
        return await orig(self, state)

    with patch.object(_srv, "_build_llm_provider", return_value=llm), \
         patch.object(_srv, "MonitoredLLMProvider", side_effect=lambda p, **kw: p), \
         patch.object(_srv, "GatewayClient", return_value=mock_gw), \
         patch.object(ArchitectAgent, "run", spy_run):

        await _srv.bootstrap_architecture(repo="https://github.com/owner/repo")

    assert "https://github.com/owner/repo" in captured_state.get("task", "")


async def test_bootstrap_architecture_repo_passed_to_agent():
    """ArchitectAgent is constructed with the repo URL, not the gateway URL."""
    llm = _make_sequential_llm(
        _PHASE_RECON, _PHASE_FLOW, _PHASE_ABSTRACTION, _PHASE_SYNTHESIS, _BOOTSTRAP_DOC
    )
    mock_gw = _make_mock_gateway()
    captured_repo = {}

    from harness_agents.architect import ArchitectAgent
    orig_init = ArchitectAgent.__init__

    def spy_init(self, gateway, llm_provider, repo="", **kwargs):
        captured_repo["repo"] = repo
        orig_init(self, gateway=gateway, llm_provider=llm_provider, repo=repo, **kwargs)

    with patch.object(_srv, "_build_llm_provider", return_value=llm), \
         patch.object(_srv, "MonitoredLLMProvider", side_effect=lambda p, **kw: p), \
         patch.object(_srv, "GatewayClient", return_value=mock_gw), \
         patch.object(ArchitectAgent, "__init__", spy_init):

        await _srv.bootstrap_architecture(repo="https://github.com/owner/repo")

    assert captured_repo.get("repo") == "https://github.com/owner/repo"
