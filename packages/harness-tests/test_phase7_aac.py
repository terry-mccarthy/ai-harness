"""Phase 7 — Architecture as Code (AaC) Engine.

Tests for the architectural gate that runs static analysis checks and
records failures in Dolt. Follows TDD: these tests define the contract
before implementation.
"""
import json
import os
import uuid
from unittest.mock import MagicMock

import pytest

class MockLLMProvider:
    def __init__(self, response: str):
        self._response = response

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(content=self._response)


class SequentialMockLLMProvider:
    def __init__(self, *responses: str):
        self._responses = list(responses)
        self._idx = 0

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        response = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return LLMResponse(content=response)


def _mock_gateway(tool_responses: dict | None = None):
    gw = MagicMock()
    gw.last_calls = []
    responses = tool_responses or {}

    async def call_tool(name, params=None):
        gw.last_calls.append({"tool": name, "params": params or {}})
        return responses.get(name, {"result": "ok"})

    gw.call_tool = call_tool
    return gw


_GATE_PASS = {
    "result": "PASS",
    "violations": [],
    "action": "PROCEED",
}

_GATE_FAIL_LAYER = {
    "result": "FAIL",
    "violations": [
        {
            "rule": "layer-violation",
            "severity": "HARD",
            "file": "src/domain/user.py",
            "message": "Domain layer must not import infrastructure. Found: from infra.db import session",
        }
    ],
    "action": "STOP_AND_SURFACE",
}

_GATE_FAIL_COMPLEXITY = {
    "result": "FAIL",
    "violations": [
        {
            "rule": "complexity-limit",
            "severity": "SOFT",
            "file": "src/core/processor.py",
            "message": "Cyclomatic complexity 22 exceeds recommended limit of 15",
        }
    ],
    "action": "PROCEED",
}

_VALID_ADR = json.dumps({
    "title": "ADR-001: Use PostgreSQL",
    "status": "proposed",
    "summary": "PostgreSQL is the recommended storage layer for this service.",
    "findings": [],
    "recommendations": [],
    "context": "Need persistent storage.",
    "decision": "Use PostgreSQL.",
    "consequences": "Requires pgvector.",
    "alternatives_considered": [],
})


def _base_state(task: str) -> dict:
    return {
        "task": task,
        "diff": "",
        "thread_id": str(uuid.uuid4()),
        "task_type": None,
        "formula_id": None,
        "formula_instance_id": None,
        "active_agent": None,
        "agent_output": None,
        "final_response": None,
        "human_approval_token": None,
        "requires_human_approval": False,
        "error": None,
        "memory_context": None,
        "tokens_used": 0,
        "token_budget": None,
        "target_language": "python",
        "repo_path": "/tmp/test-repo",
        "gate_signal": None,
        "human_justification": None,
    }


# ---------------------------------------------------------------------------
# Slice 1 — architectural_gate_node unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_passes_clean_code():
    """Clean code with no violations → gate_signal.result == 'PASS'."""
    from harness_supervisor.nodes import architectural_gate_node

    gw = _mock_gateway({"execute_architecture_check": _GATE_PASS})
    state = _base_state("Design auth service")
    result = await architectural_gate_node(state, gateway=gw)

    assert result["gate_signal"] is not None
    assert result["gate_signal"]["result"] == "PASS"
    assert len(result["gate_signal"]["violations"]) == 0


@pytest.mark.asyncio
async def test_gate_fails_layer_violation():
    """Layer violation → gate_signal.result == 'FAIL' with HARD severity."""
    from harness_supervisor.nodes import architectural_gate_node

    gw = _mock_gateway({"execute_architecture_check": _GATE_FAIL_LAYER})
    state = _base_state("Design auth service")
    result = await architectural_gate_node(state, gateway=gw)

    assert result["gate_signal"]["result"] == "FAIL"
    assert any(v["severity"] == "HARD" for v in result["gate_signal"]["violations"])
    assert any("layer" in v["rule"] for v in result["gate_signal"]["violations"])


@pytest.mark.asyncio
async def test_gate_enforces_complexity_limit():
    """Complexity violation → gate_signal.result == 'FAIL' with SOFT severity."""
    from harness_supervisor.nodes import architectural_gate_node

    gw = _mock_gateway({"execute_architecture_check": _GATE_FAIL_COMPLEXITY})
    state = _base_state("Design auth service")
    result = await architectural_gate_node(state, gateway=gw)

    assert result["gate_signal"]["result"] == "FAIL"
    assert any(v["severity"] == "SOFT" for v in result["gate_signal"]["violations"])


@pytest.mark.asyncio
async def test_gate_passes_params_to_tool():
    """Passes target_language and repo_path from state to execute_architecture_check."""
    from harness_supervisor.nodes import architectural_gate_node

    gw = _mock_gateway({"execute_architecture_check": _GATE_PASS})
    state = _base_state("Design auth service")
    await architectural_gate_node(state, gateway=gw)

    assert any(c["tool"] == "execute_architecture_check" for c in gw.last_calls)
    call_params = next(
        c["params"] for c in gw.last_calls if c["tool"] == "execute_architecture_check"
    )
    assert call_params["repo_path"] == "/tmp/test-repo"
    assert call_params["target_language"] == "python"


@pytest.mark.asyncio
async def test_gate_handles_tool_denied():
    """When execute_architecture_check is denied, gate_signal is FAIL with error."""
    from harness_gateway.client import ToolAccessDenied

    gw = MagicMock()
    gw.last_calls = []

    async def call_tool(name, params=None):
        gw.last_calls.append({"tool": name, "params": params or {}})
        raise ToolAccessDenied("403 Forbidden: execute_architecture_check")

    gw.call_tool = call_tool

    from harness_supervisor.nodes import architectural_gate_node

    state = _base_state("Design auth service")
    result = await architectural_gate_node(state, gateway=gw)

    assert result["gate_signal"]["result"] == "FAIL"
    assert result["error"] is not None
    assert "tool_access_denied" in result["error"]["code"]


# ---------------------------------------------------------------------------
# Slice 2 — route_after_gate unit tests
# ---------------------------------------------------------------------------


def test_route_after_gate_pass():
    """PASS result → routes to synthesise."""
    from harness_supervisor.graph import route_after_gate

    state = {**_base_state("task"), "gate_signal": dict(_GATE_PASS)}
    assert route_after_gate(state) == "synthesise"


def test_route_after_gate_hard_fail():
    """FAIL with HARD violation → routes to human_gate."""
    from harness_supervisor.graph import route_after_gate

    state = {**_base_state("task"), "gate_signal": dict(_GATE_FAIL_LAYER)}
    assert route_after_gate(state) == "human_gate"


def test_route_after_gate_soft_fail_no_justification():
    """FAIL with SOFT violation & no human_justification → routes to human_gate."""
    from harness_supervisor.graph import route_after_gate

    state = {
        **_base_state("task"),
        "gate_signal": dict(_GATE_FAIL_COMPLEXITY),
        "human_justification": None,
    }
    assert route_after_gate(state) == "human_gate"


def test_route_after_gate_soft_fail_with_justification():
    """FAIL with SOFT violation & human_justification → routes to synthesise."""
    from harness_supervisor.graph import route_after_gate

    state = {
        **_base_state("task"),
        "gate_signal": dict(_GATE_FAIL_COMPLEXITY),
        "human_justification": "Accepted complexity — will refactor in next sprint.",
    }
    assert route_after_gate(state) == "synthesise"


def test_route_after_gate_no_signal():
    """No gate_signal → routes to error_handler."""
    from harness_supervisor.graph import route_after_gate

    state = _base_state("task")
    assert route_after_gate(state) == "error_handler"


# ---------------------------------------------------------------------------
# Slice 3 — architect → gate E2E graph tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_architect_halts_on_hard_constraint():
    """architect → architectural_gate → human_gate on HARD violation."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_VALID_ADR),
        gateway=_mock_gateway({
            "codebase_search": {"files": []},
            "adr_read": {"adrs": []},
            "execute_architecture_check": _GATE_FAIL_LAYER,
        }),
        checkpointer=InMemorySaver(),
    )
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = {
        **_base_state("Design the auth service"),
        "task_type": "design",
        "thread_id": thread_id,
    }
    final = await supervisor.ainvoke(state, config)

    assert final.get("gate_signal") is not None
    assert final["gate_signal"]["result"] == "FAIL"
    assert any(v["severity"] == "HARD" for v in final["gate_signal"]["violations"])


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_architect_passes_on_clean_code():
    """architect → architectural_gate → synthesise on PASS."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_VALID_ADR),
        gateway=_mock_gateway({
            "codebase_search": {"files": []},
            "adr_read": {"adrs": []},
            "execute_architecture_check": _GATE_PASS,
        }),
        checkpointer=InMemorySaver(),
    )
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    state = {
        **_base_state("Design the auth service"),
        "task_type": "design",
        "thread_id": thread_id,
    }
    final = await supervisor.ainvoke(state, config)

    assert final.get("final_response") is not None
    assert final.get("error") is None
    assert final.get("gate_signal") is not None
    assert final["gate_signal"]["result"] == "PASS"


# ---------------------------------------------------------------------------
# Slice 4 — Dolt gate failures recording (integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dolt_records_gate_failures():
    """Dolt write_gate_failure inserts a row in architectural_gate_failures table."""
    import pymysql
    import time
    import json

    DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
    DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))

    thread_id = str(uuid.uuid4())
    timestamp_ms = int(time.time() * 1000)

    conn = pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT, user="root",
        password="root", database="harness", autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO architectural_gate_failures
                   (thread_id, rule, severity, file, message, task, repo_path,
                    target_language, gate_signal, timestamp_ms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    thread_id,
                    "layer-violation",
                    "HARD",
                    "src/domain/user.py",
                    "Domain layer import of infrastructure",
                    "Design auth service",
                    "/tmp/test-repo",
                    "python",
                    json.dumps(_GATE_FAIL_LAYER),
                    timestamp_ms,
                ),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"test: gate_failure {thread_id}",),
            )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT thread_id, rule, severity FROM architectural_gate_failures WHERE thread_id=%s",
                (thread_id,),
            )
            row = cur.fetchone()
        assert row is not None, f"No row found for thread_id={thread_id}"
        assert row[0] == thread_id
        assert row[1] == "layer-violation"
        assert row[2] == "HARD"
    finally:
        conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_audit_architectural_gate_endpoint():
    """POST /audit/architectural-gate records a failure via the HTTP endpoint."""
    import httpx

    GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"{GOVERNANCE_URL}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "architect",
                "client_secret": os.environ.get("ARCHITECT_SECRET", "architect-secret"),
            },
        )
    token_resp.raise_for_status()
    token = token_resp.json()["access_token"]

    thread_id = str(uuid.uuid4())
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GOVERNANCE_URL}/audit/architectural-gate",
            json={
                "thread_id": thread_id,
                "rule": "complexity-limit",
                "severity": "SOFT",
                "file": "src/core/processor.py",
                "message": "Cyclomatic complexity 22 exceeds limit of 15",
                "task": "Design auth service",
                "repo_path": "/tmp/test-repo",
                "target_language": "python",
                "gate_signal": _GATE_FAIL_COMPLEXITY,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code in (200, 202)
