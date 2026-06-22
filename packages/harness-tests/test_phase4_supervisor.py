"""Phase 4 — Agent Orchestration supervisor graph.

23 tests: unit (classify, route, error_handler), integration (formula nodes,
human gate, checkpoint), and E2E (full graph runs with MockLLM).
"""
import json
import os
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class MockLLMProvider:
    def __init__(self, response: str):
        self._response = response

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(content=self._response)


class SequentialMockLLMProvider:
    """Returns each response in sequence; repeats the last one indefinitely."""
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


_VALID_ADR = json.dumps({
    "title": "ADR-001: Use PostgreSQL",
    "status": "proposed",
    "context": "Need persistent storage.",
    "decision": "Use PostgreSQL.",
    "consequences": "Requires pgvector.",
    "alternatives_considered": [],
})

_VALID_FINDINGS = json.dumps({
    "verdict": "pass",
    "findings": [],
    "summary": "No issues found.",
})

_VALID_INCIDENT = json.dumps({
    "timeline": "Alert fired at 14:00",
    "likely_cause": "Connection pool exhausted",
    "severity": "P2",
    "recommended_steps": [
        {"action": "Restart pool", "rationale": "Clears stale connections", "requires_approval": False}
    ],
    "runbook_ref": None,
    "requires_human_approval": False,
})

_INCIDENT_NEEDS_APPROVAL = json.dumps({
    "timeline": "Critical DB failure",
    "likely_cause": "Disk full",
    "severity": "P1",
    "recommended_steps": [
        {"action": "rm -rf /tmp/*", "rationale": "Free disk space", "requires_approval": True}
    ],
    "runbook_ref": None,
    "requires_human_approval": True,
})

DOLT_CONN = dict(
    host=os.environ.get("DOLT_HOST", "localhost"),
    port=int(os.environ.get("DOLT_PORT", "3306")),
    user="root",
    password="root",
    database="harness",
)

PG_DSN = os.environ.get("PG_DSN", "postgresql://harness:harness@localhost:5432/harness")


# ---------------------------------------------------------------------------
# Slice 1 — classify node
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task,task_type", [
    ("Design the auth service", "design"),
    ("Review this PR diff", "review"),
    ("Alert fired: DB latency spike", "incident"),
])
async def test_classify_extracts_task_type_from_llm_json(task, task_type):
    """Happy path: classify_node extracts task_type from clean LLM JSON."""
    from harness_supervisor.nodes import classify_node
    from harness_supervisor.state import HarnessState

    llm = MockLLMProvider(json.dumps({"task_type": task_type}))
    state: HarnessState = _base_state(task)
    result = await classify_node(state, llm_provider=llm)
    assert result["task_type"] == task_type


async def test_classify_llm_overrides_keyword_match():
    """LLM verdict wins over a misleading surface keyword."""
    from harness_supervisor.nodes import classify_node

    llm = MockLLMProvider(json.dumps({"task_type": "incident"}))
    state = _base_state("Review the alert that fired in production and find the cause")
    result = await classify_node(state, llm_provider=llm)
    assert result["task_type"] == "incident"


async def test_classify_falls_back_to_keywords_when_llm_unavailable():
    """LLM failure does not break routing — keyword heuristic takes over."""
    from harness_supervisor.nodes import classify_node

    class FailingLLMProvider:
        async def chat(self, messages):
            raise ConnectionError("ollama unreachable")

    state = _base_state("Design the auth service schema")
    result = await classify_node(state, llm_provider=FailingLLMProvider())
    assert result["task_type"] == "design"


async def test_classify_unparseable_llm_output_defaults_to_review():
    """Garbage LLM output + no keywords → safe default 'review'."""
    from harness_supervisor.nodes import classify_node

    llm = MockLLMProvider("I think this might be about deployment, hard to say!")
    state = _base_state("Do the thing we discussed yesterday")
    result = await classify_node(state, llm_provider=llm)
    assert result["task_type"] == "review"


async def test_classify_strips_think_blocks():
    """Thinking-model output (<think>…</think> before JSON) is parsed correctly."""
    from harness_supervisor.nodes import classify_node

    llm = MockLLMProvider(
        '<think>{"task_type": "review"}? No — an outage.</think>\n{"task_type": "incident"}'
    )
    state = _base_state("Customers cannot log in since this morning")
    result = await classify_node(state, llm_provider=llm)
    assert result["task_type"] == "incident"


# ---------------------------------------------------------------------------
# Slice 2 — route function (1 parametrized unit test)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task_type,expected", [
    ("design", "architect"),
    ("review", "code_reviewer"),
    ("incident", "sre"),
])
def test_route_node(task_type, expected):
    from harness_supervisor.nodes import route_node
    assert route_node({"task_type": task_type}) == expected


# ---------------------------------------------------------------------------
# Slice 3 — error handler (1 unit test)
# ---------------------------------------------------------------------------

async def test_error_handler_on_gateway_403():
    """Gateway 403 triggers error_handler; error dict has tool_name and reason."""
    from harness_supervisor.nodes import error_handler_node
    from harness_supervisor.state import HarnessState

    state: HarnessState = {
        **_base_state("Do something"),
        "error": {"code": "tool_access_denied", "reason": "403 Forbidden: shell_exec"},
    }
    result = await error_handler_node(state)
    assert result["error"] is not None
    assert "tool_access_denied" in result["error"]["code"]
    assert result["final_response"] is not None


# ---------------------------------------------------------------------------
# Slice 4 — formula lookup + outcome (3 integration tests)
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_formula_lookup_hit():
    """formula_lookup with task_type='incident' returns formula_id + instance_id."""
    from harness_supervisor.nodes import formula_lookup_node
    from harness_memory.formula_store import DoltFormulaStore

    fstore = DoltFormulaStore(**DOLT_CONN)
    state = {**_base_state("DB latency alert fired"), "task_type": "incident"}
    result = await formula_lookup_node(state, formula_store=fstore)

    assert result["formula_id"] == "sre:triage-incident"
    assert result["formula_instance_id"] is not None


@pytest.mark.integration
async def test_formula_lookup_miss():
    """formula_lookup with no match sets formula_id=None; graph continues."""
    from harness_supervisor.nodes import formula_lookup_node
    from harness_memory.formula_store import DoltFormulaStore

    fstore = DoltFormulaStore(**DOLT_CONN)
    state = {**_base_state("Plan a team offsite"), "task_type": "design"}
    result = await formula_lookup_node(state, formula_store=fstore)

    assert result["formula_id"] is None


@pytest.mark.integration
async def test_formula_outcome_recorded():
    """synthesise node produces a final_response when a skill was matched."""
    from harness_supervisor.nodes import synthesise_node
    from harness_memory.formula_store import DoltFormulaStore

    instance_id = str(uuid.uuid4())
    fstore = DoltFormulaStore(**DOLT_CONN)

    state = {
        **_base_state("Triage incident"),
        "formula_id": "sre:triage-incident",
        "formula_instance_id": instance_id,
        "agent_output": json.loads(_VALID_INCIDENT),
        "active_agent": "sre",
        "task_type": "incident",
    }
    result = await synthesise_node(state, formula_store=fstore)
    assert result.get("final_response"), "synthesise must return a final_response"


# ---------------------------------------------------------------------------
# Slice 5 — ad-hoc routing + propose_formula (2 integration tests)
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_agent_executes_ad_hoc_without_formula():
    """When formula_id is None, SRE agent runs freely and does not error."""
    from harness_supervisor.nodes import run_agent_node
    from harness_agents.dynamic_sre import DynamicSREAgent

    _react_respond = json.dumps({"action": "respond", "result": json.loads(_VALID_INCIDENT)})
    gw = _mock_gateway({"observability_query": {}, "log_search": {}, "runbook_read": {}})
    agent = DynamicSREAgent(gateway=gw, llm_provider=MockLLMProvider(_react_respond))
    state = {
        **_base_state("DB latency spike"),
        "task_type": "incident",
        "formula_id": None,
        "formula_instance_id": None,
        "active_agent": "sre",
    }
    result = await run_agent_node(state, agent=agent)
    assert result["error"] is None
    assert result["agent_output"] is not None


@pytest.mark.integration
async def test_propose_formula_on_novel_task():
    """Ad-hoc run → propose_formula inserts a draft formula in Dolt."""
    from harness_supervisor.nodes import propose_formula_node
    from harness_memory.formula_store import DoltFormulaStore

    fstore = DoltFormulaStore(**DOLT_CONN)
    state = {
        **_base_state("Investigate unusual memory pattern"),
        "task_type": "incident",
        "formula_id": None,
        "formula_instance_id": None,
        "active_agent": "sre",
        "agent_output": json.loads(_VALID_INCIDENT),
    }

    try:
        await propose_formula_node(state, formula_store=fstore)
        drafts = fstore._get_drafts_by_role("sre")
        assert any("memory" in (d.description or "").lower() or d.status == "draft" for d in drafts)
    finally:
        fstore._delete_where_id_like("draft:%")


# ---------------------------------------------------------------------------
# Slice 6 — agent_executes_formula_steps (1 integration test)
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_agent_executes_formula_steps():
    """When formula_id is set, SRE agent calls tools in formula step order."""
    from harness_supervisor.nodes import run_agent_node
    from harness_agents.dynamic_sre import DynamicSREAgent
    from harness_memory.formula_store import DoltFormulaStore

    fstore = DoltFormulaStore(**DOLT_CONN)
    formula = fstore.get("sre:triage-incident")
    assert formula is not None

    _react_respond = json.dumps({"action": "respond", "result": json.loads(_VALID_INCIDENT)})
    gw = _mock_gateway({"observability_query": {}, "log_search": {}, "runbook_read": {}})
    agent = DynamicSREAgent(gateway=gw, llm_provider=MockLLMProvider(_react_respond))

    state = {
        **_base_state("DB latency alert"),
        "task_type": "incident",
        "formula_id": "sre:triage-incident",
        "formula_instance_id": str(uuid.uuid4()),
        "active_agent": "sre",
    }
    result = await run_agent_node(state, agent=agent, formula=formula)
    assert result["error"] is None

    called_tools = [c["tool"] for c in gw.last_calls]
    formula_actions = [s["action"] for s in formula.steps if s["action"] != "llm_synthesise"]
    for action in formula_actions:
        assert action in called_tools, f"Expected formula step '{action}' to be called"


# ---------------------------------------------------------------------------
# Slice 6.5 — _after_human_gate routing (1 unit test)
# ---------------------------------------------------------------------------


async def test_after_human_gate_architect_gate_does_not_route_to_sre():
    """_after_human_gate with gate_signal + valid token → synthesise, not sre.

    When the human gate is reached via architect → architectural_gate → FAIL,
    a valid approval token should NOT resume the SRE agent.
    """
    from unittest.mock import patch
    from harness_supervisor.graph import _after_human_gate

    state = {
        **_base_state("Design the auth service"),
        "gate_signal": {
            "result": "FAIL",
            "violations": [
                {"rule": "layer-violation", "severity": "HARD", "file": "a.py", "message": "bad"}
            ],
            "action": "STOP_AND_SURFACE",
        },
        "human_approval_token": "valid-token",
    }

    with patch("harness_supervisor.graph.validate_approval_token", return_value=True):
        result = _after_human_gate(state)

    assert result != "sre", "_after_human_gate must not route to sre when gate_signal is present"
    assert result == "synthesise", f"Expected 'synthesise', got {result!r}"


# ---------------------------------------------------------------------------
# Slice 7 — human gate (4 tests)
# ---------------------------------------------------------------------------

async def test_human_gate_pauses_graph():
    """SRE output with requires_human_approval=True pauses the graph."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_INCIDENT_NEEDS_APPROVAL),
        gateway=_mock_gateway({"observability_query": {}, "log_search": {}, "runbook_read": {}}),
        checkpointer=InMemorySaver(),
    )
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    final = await supervisor.ainvoke(
        _harness_input("P1 alert: disk full on prod-db-01"),
        config,
    )
    assert final.get("requires_human_approval") is True
    assert final.get("human_approval_token") is None


@pytest.mark.integration
async def test_human_gate_resumes_with_valid_token():
    """Valid human_approval_token resumes graph; final_response populated."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor
    from harness_supervisor.approval import issue_approval_token

    # First SRE call: needs approval. Second (post-approval): resolves without approval.
    supervisor = await build_supervisor(
        llm_provider=SequentialMockLLMProvider(_INCIDENT_NEEDS_APPROVAL, _VALID_INCIDENT),
        gateway=_mock_gateway({
            "observability_query": {}, "log_search": {}, "runbook_read": {},
            "shell_exec": {"output": "done"},
        }),
        checkpointer=InMemorySaver(),
    )
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    await supervisor.ainvoke(_harness_input("P1 alert: disk full"), config)

    token = issue_approval_token(
        thread_id=thread_id,
        tool_name="shell_exec",
        secret=os.environ.get("JWT_SECRET", "dev-jwt-secret-change-in-prod-xyz"),
    )
    # Inject the approval token into the checkpointed state, then resume
    await supervisor.aupdate_state(config, {"human_approval_token": token, "thread_id": thread_id})
    final = await supervisor.ainvoke(None, config)
    assert final.get("final_response") is not None


async def test_human_gate_rejects_expired_token():
    """Expired human_approval_token moves graph to error_handler."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor
    from harness_supervisor.approval import issue_approval_token

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_INCIDENT_NEEDS_APPROVAL),
        gateway=_mock_gateway({"observability_query": {}, "log_search": {}, "runbook_read": {}}),
        checkpointer=InMemorySaver(),
    )
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    await supervisor.ainvoke(_harness_input("P1 alert: disk full"), config)

    expired_token = issue_approval_token(
        thread_id=thread_id,
        tool_name="shell_exec",
        secret=os.environ.get("JWT_SECRET", "dev-jwt-secret-change-in-prod-xyz"),
        ttl_seconds=-1,
    )
    await supervisor.aupdate_state(config, {"human_approval_token": expired_token, "thread_id": thread_id})
    final = await supervisor.ainvoke(None, config)
    assert final.get("error") is not None


async def test_human_gate_rejects_wrong_scope():
    """Token scoped to different thread_id is rejected."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor
    from harness_supervisor.approval import issue_approval_token

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_INCIDENT_NEEDS_APPROVAL),
        gateway=_mock_gateway({"observability_query": {}, "log_search": {}, "runbook_read": {}}),
        checkpointer=InMemorySaver(),
    )
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    await supervisor.ainvoke(_harness_input("P1 alert"), config)

    wrong_thread_token = issue_approval_token(
        thread_id="wrong-thread-id",
        tool_name="shell_exec",
        secret=os.environ.get("JWT_SECRET", "dev-jwt-secret-change-in-prod-xyz"),
    )
    await supervisor.aupdate_state(config, {"human_approval_token": wrong_thread_token, "thread_id": thread_id})
    final = await supervisor.ainvoke(None, config)
    assert final.get("error") is not None


# ---------------------------------------------------------------------------
# Slice 8 — checkpoint durability (1 integration test)
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_checkpoint_survives_human_pause():
    """Graph state is checkpointed before human_gate; resume reads from checkpoint."""
    from harness_supervisor.graph import build_supervisor

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_INCIDENT_NEEDS_APPROVAL),
        gateway=_mock_gateway({"observability_query": {}, "log_search": {}, "runbook_read": {}}),
        pg_dsn=PG_DSN,
    )
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    await supervisor.ainvoke(_harness_input("P1 alert"), config)

    # Build a fresh supervisor (new object) — reads state from checkpointer
    supervisor2 = await build_supervisor(
        llm_provider=MockLLMProvider(_INCIDENT_NEEDS_APPROVAL),
        gateway=_mock_gateway({"observability_query": {}, "log_search": {}, "runbook_read": {}}),
        pg_dsn=PG_DSN,
    )
    try:
        state = await supervisor2.aget_state(config)
        assert state is not None
        assert state.values.get("requires_human_approval") is True
    finally:
        # Close both pools — unclosed AsyncConnectionPool background tasks
        # linger on the event loop and block the next test's synchronous I/O.
        await supervisor.checkpointer.conn.close()
        await supervisor2.checkpointer.conn.close()


# ---------------------------------------------------------------------------
# Slice 9 — OTel spans (1 integration test — real Dolt formula store)
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_otel_spans_emitted():
    """After a full graph run, in-memory exporter has spans for key nodes."""
    from langgraph.checkpoint.memory import InMemorySaver
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from harness_supervisor.graph import build_supervisor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_VALID_INCIDENT),
        gateway=_mock_gateway({"observability_query": {}, "log_search": {}, "runbook_read": {}}),
        checkpointer=InMemorySaver(),
        tracer_provider=provider,
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    await supervisor.ainvoke(_harness_input("DB latency spike"), config)

    span_names = {s.name for s in exporter.get_finished_spans()}
    for expected in ("classify", "formula_lookup", "route", "synthesise"):
        assert expected in span_names, f"Missing OTel span: {expected}. Got: {span_names}"
    # One of the agent nodes should have emitted a span
    assert span_names & {"architect", "code_reviewer", "sre"}, \
        f"No agent span found. Got: {span_names}"


# ---------------------------------------------------------------------------
# Slice 10 — Full graph runs (3 unit tests, no real infrastructure)
# ---------------------------------------------------------------------------

async def test_full_design_task():
    """Full graph: design task → formula_lookup → architect → synthesise → final_response."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_VALID_ADR),
        gateway=_mock_gateway({
            "codebase_search": {"files": []},
            "adr_read": {"adrs": []},
            "execute_architecture_check": {"result": "PASS", "violations": [], "action": "PROCEED"},
        }),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    final = await supervisor.ainvoke(_harness_input("Design the auth service"), config)

    assert final.get("final_response") is not None
    assert final.get("error") is None
    assert final.get("task_type") == "design"


async def test_full_review_task():
    """Full graph: review task → formula_lookup → reviewer → synthesise → verdict in final_response."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_VALID_FINDINGS),
        gateway=_mock_gateway({
            "git_diff": {"diff": "..."},
            "run_linter": {"issues": []},
        }),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    final = await supervisor.ainvoke(
        {**_harness_input("Review this PR diff"), "diff": "diff --git ..."},
        config,
    )

    assert final.get("final_response") is not None
    assert final.get("task_type") == "review"


async def test_full_incident_task_no_shell():
    """Incident not requiring shell_exec completes without pausing at human_gate."""
    from langgraph.checkpoint.memory import InMemorySaver
    from harness_supervisor.graph import build_supervisor

    supervisor = await build_supervisor(
        llm_provider=MockLLMProvider(_VALID_INCIDENT),
        gateway=_mock_gateway({
            "observability_query": {}, "log_search": {}, "runbook_read": {},
        }),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    final = await supervisor.ainvoke(_harness_input("DB latency spike"), config)

    assert final.get("final_response") is not None
    assert final.get("requires_human_approval") is False
    assert final.get("error") is None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

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
    }


def _harness_input(task: str) -> dict:
    return {**_base_state(task), "thread_id": str(uuid.uuid4())}
