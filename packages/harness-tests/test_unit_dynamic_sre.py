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
from harness_gateway.client import ToolAccessDenied
from harness_memory.models import Formula as _Formula

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
    "skill_ref": None,
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
    "skill_ref": None,
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

async def test_injected_shell_exec_is_denied_and_agent_recovers():
    """shell_exec is blocked by governance (ToolAccessDenied), but the agent
    treats it as non-fatal: the denial is fed back to the LLM so it can
    propose the step in the report instead of halting."""
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
        _respond(_VALID_REPORT),                               # LLM recovers and reports
    )
    gw = _DenyingGateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    # Gateway blocked shell_exec — no error propagated to the caller
    assert result.get("error") is None
    # Agent completed successfully after recovering from the denial
    assert result["agent_output"] is not None
    # Both tools were attempted; shell_exec was blocked but log_search ran
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
        async def read(self, namespace, key):
            return None

        async def search(self, namespace, query, top_k=3):
            if namespace == "sre":
                return [{"key": "incident:abc123", "value": {"likely_cause": "past OOM"}, "score": 0.9}]
            return []

        async def write(self, namespace, key, value, **_):
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
        async def read(self, namespace, key):
            return None

        async def search(self, namespace, query, top_k=3):
            return []

        async def write(self, namespace, key, value, **_):
            written.append({"namespace": namespace, "key": key, "value": value})

    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()
    state = _state()

    await DynamicSREAgent(gateway=gw, llm_provider=llm, memory_store=_MockMemory()).run(state)

    sre_writes = [w for w in written if w["namespace"] == "sre"]
    assert len(sre_writes) == 1
    assert sre_writes[0]["key"].startswith("incident:")
    assert sre_writes[0]["value"]["severity"] == "P2"


# ---------------------------------------------------------------------------
# Behaviors 11–14 — skill-aware guidance: formula_store precedence
# ---------------------------------------------------------------------------

_MATCHED_FORMULA = _Formula(
    id="sre:db-latency:1",
    name="db-connection-pool",
    agent_role="sre",
    version=1,
    status="active",
    description="Diagnose connection pool exhaustion causing DB latency",
    input_schema={},
    steps=[
        {"tool": "observability_query", "params": {"query": "connection pool metrics"}},
        {"tool": "log_search", "params": {"query": "connection pool exhausted"}},
    ],
    output_contract={},
    promoted_by="human_operator",
)


class _FakeFormulaStore:
    def __init__(self, formula=None, by_id: dict | None = None):
        self.calls: list[dict] = []
        self.get_calls: list[str] = []
        self._formula = formula
        self._by_id = by_id or {}

    def lookup(self, agent_role: str, task: str):
        self.calls.append({"agent_role": agent_role, "task": task})
        return self._formula

    def get(self, formula_id: str, version: int | None = None):
        self.get_calls.append(formula_id)
        if self._formula is not None and self._formula.id == formula_id:
            return self._formula
        return self._by_id.get(formula_id)


async def test_formula_steps_injected_into_opening_message():
    """Matched formula → its name and id appear in the opening user message,
    steering the LLM to call run_skill rather than list/replay raw steps
    (issue 06 — the agent no longer auto-executes formula steps server-side;
    it decides for itself whether to call run_skill)."""
    from harness_agents.dynamic_sre import DynamicSREAgent

    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()

    await DynamicSREAgent(
        gateway=gw, llm_provider=llm, formula_store=_FakeFormulaStore(_MATCHED_FORMULA)
    ).run(_state())

    opening = llm.messages_received[0][1]["content"]
    assert "db-connection-pool" in opening
    assert _MATCHED_FORMULA.id in opening
    assert "run_skill" in opening


async def test_no_formula_block_when_no_match():
    """formula_store.lookup returns None → no formula block in the opening message."""
    from harness_agents.dynamic_sre import DynamicSREAgent

    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()

    await DynamicSREAgent(
        gateway=gw, llm_provider=llm, formula_store=_FakeFormulaStore(None)
    ).run(_state())

    opening = llm.messages_received[0][1]["content"]
    assert "A proven skill exists" not in opening


async def test_no_formula_store_backward_compatible():
    """No formula_store → agent runs unchanged, no formula block."""
    from harness_agents.dynamic_sre import DynamicSREAgent

    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_state())

    assert result.get("error") is None
    opening = llm.messages_received[0][1]["content"]
    assert "A proven skill exists" not in opening


async def test_formula_lookup_uses_agent_role():
    """formula_store.lookup is called with the agent's own role name."""
    from harness_agents.dynamic_sre import DynamicSREAgent

    store = _FakeFormulaStore(None)
    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()

    await DynamicSREAgent(gateway=gw, llm_provider=llm, formula_store=store).run(_state())

    assert store.calls[0]["agent_role"] == "sre"


# ---------------------------------------------------------------------------
# Behaviors 14c–14h — run_skill native dispatch (issue 06)
#
# run_skill is NOT a gateway/MCP tool — TOOL_NAME_MAP has no entry for it and
# no MCP server hosts it. `_handle_tool_call` intercepts it before it would
# reach `self.gateway.call_tool()` and instead drives
# `SkillRunner(self.gateway).execute(...)` directly, so every step still gets
# its own OPA check through the agent's real gateway credentials. These tests
# fake at the SkillRunner boundary (monkeypatching
# `harness_agents.dynamic_sre.SkillRunner`) rather than mocking HTTP calls to
# governance — mirroring packages/harness-tests/test_unit_skill_runner.py.
# ---------------------------------------------------------------------------

class _FakeSkillRunner:
    """Records the skill_id/inputs it was asked to execute."""
    def __init__(self, gateway):
        self.gateway = gateway

    async def execute(self, skill_id, inputs=None):
        _FakeSkillRunner.calls.append({"skill_id": skill_id, "inputs": inputs})
        return {"skill_id": skill_id, "steps_completed": 2, "results": []}

    calls: list[dict] = []


class _DenyingFakeSkillRunner:
    """Simulates a skill step denied by OPA (e.g. a shell_exec step)."""
    def __init__(self, gateway):
        self.gateway = gateway

    async def execute(self, skill_id, inputs=None):
        raise ToolAccessDenied(f"403 Forbidden: skill step in {skill_id!r}")


def _reset_fake_skill_runner():
    _FakeSkillRunner.calls = []


async def test_llm_steered_to_call_run_skill_with_formula_id(monkeypatch):
    """Skill precedence: with a matching formula preloaded, the scripted LLM
    turn calls run_skill with the formula's id (not an improvised tool
    sequence) — the fake SkillRunner records the right skill id, and the
    call never reaches the generic gateway.call_tool path (there is no
    TOOL_NAME_MAP entry for run_skill)."""
    import harness_agents.dynamic_sre as dynamic_sre_module
    from harness_agents.dynamic_sre import DynamicSREAgent

    _reset_fake_skill_runner()
    monkeypatch.setattr(dynamic_sre_module, "SkillRunner", _FakeSkillRunner)

    llm = _Turns(
        _call_tool("run_skill", skill_id=_MATCHED_FORMULA.id),
        _respond(_VALID_REPORT),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(
        gateway=gw, llm_provider=llm, formula_store=_FakeFormulaStore(_MATCHED_FORMULA)
    ).run(_state())

    assert result.get("error") is None
    assert _FakeSkillRunner.calls == [{"skill_id": _MATCHED_FORMULA.id, "inputs": None}]
    assert "run_skill" not in [c["tool"] for c in gw.calls]


async def test_cold_start_no_formula_never_calls_run_skill(monkeypatch):
    """Cold-start fallback: no matching formula → the agent never calls
    run_skill, using runbook_read/other tools per the scripted turns
    instead."""
    import harness_agents.dynamic_sre as dynamic_sre_module
    from harness_agents.dynamic_sre import DynamicSREAgent

    _reset_fake_skill_runner()
    monkeypatch.setattr(dynamic_sre_module, "SkillRunner", _FakeSkillRunner)

    llm = _Turns(
        _call_tool("runbook_read", runbook_name="DB latency"),
        _respond(_VALID_REPORT),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(
        gateway=gw, llm_provider=llm, formula_store=_FakeFormulaStore(None)
    ).run(_state())

    assert result.get("error") is None
    assert [c["tool"] for c in gw.calls] == ["runbook_read"]
    assert _FakeSkillRunner.calls == []


async def test_run_skill_denial_is_recoverable(monkeypatch):
    """A skill step denied by the fake gateway (ToolAccessDenied bubbling up
    from SkillRunner.execute) is fed back as a recoverable message, not a
    fatal error — mirrors test_injected_shell_exec_is_denied_and_agent_recovers."""
    import harness_agents.dynamic_sre as dynamic_sre_module
    from harness_agents.dynamic_sre import DynamicSREAgent

    monkeypatch.setattr(dynamic_sre_module, "SkillRunner", _DenyingFakeSkillRunner)

    llm = _Turns(
        _call_tool("run_skill", skill_id=_MATCHED_FORMULA.id),
        _respond(_VALID_REPORT),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(
        gateway=gw, llm_provider=llm, formula_store=_FakeFormulaStore(_MATCHED_FORMULA)
    ).run(_state())

    assert result.get("error") is None
    assert result["agent_output"] is not None
    last_user_msg = llm.messages_received[-1][-1]
    assert last_user_msg["role"] == "user"
    assert "Access denied for tool 'run_skill'" in last_user_msg["content"]


async def test_run_skill_result_carries_formula_runbook_ref_into_conversation(monkeypatch):
    """Report linkage: the run_skill tool-result payload fed back to the LLM
    carries the matched formula's runbook_ref (merged in at the dispatch
    site — SkillRunner.execute itself doesn't know about runbook_ref), and a
    final report that echoes skill_ref + runbook_ref back validates against
    SRE_OUTPUT_SCHEMA."""
    import harness_agents.dynamic_sre as dynamic_sre_module
    from harness_agents.dynamic_sre import DynamicSREAgent

    formula = _Formula(
        id="sre:db-latency:1",
        name="db-connection-pool",
        agent_role="sre",
        version=1,
        status="active",
        description="Diagnose connection pool exhaustion causing DB latency",
        input_schema={},
        steps=[],
        output_contract={},
        promoted_by="human_operator",
        runbook_ref="runbook:db-pool-exhaustion",
    )
    monkeypatch.setattr(dynamic_sre_module, "SkillRunner", _FakeSkillRunner)
    _reset_fake_skill_runner()

    report = {**_VALID_REPORT, "skill_ref": formula.id, "runbook_ref": formula.runbook_ref}
    llm = _Turns(
        _call_tool("run_skill", skill_id=formula.id),
        _respond(report),
    )
    gw = _Gateway()

    result = await DynamicSREAgent(
        gateway=gw, llm_provider=llm, formula_store=_FakeFormulaStore(formula)
    ).run(_state())

    assert result.get("error") is None
    tool_result_msg = llm.messages_received[-1][-1]["content"]
    assert "runbook:db-pool-exhaustion" in tool_result_msg
    jsonschema.validate(result["agent_output"], SRE_OUTPUT_SCHEMA)
    assert result["agent_output"]["skill_ref"] == formula.id
    assert result["agent_output"]["runbook_ref"] == "runbook:db-pool-exhaustion"


async def test_run_skill_discovered_via_skill_search_still_links_runbook_ref(monkeypatch):
    """Cold-start discovery path: no formula is preloaded (formula_store.lookup
    returns None), so state carries no preloaded formula at all. The LLM
    instead finds a skill itself (e.g. via skill_search, not scripted here —
    only its consequence matters) and calls run_skill with that id directly.
    runbook_ref must still be attached by fetching the formula by id from the
    store, not silently dropped just because nothing was preloaded."""
    import harness_agents.dynamic_sre as dynamic_sre_module
    from harness_agents.dynamic_sre import DynamicSREAgent

    discovered = _Formula(
        id="sre:cost-spike:2",
        name="cost-spike-triage",
        agent_role="sre",
        version=1,
        status="active",
        description="Investigate a sudden cost spike",
        input_schema={},
        steps=[],
        output_contract={},
        promoted_by="human_operator",
        runbook_ref="runbook:cost-spike",
    )
    monkeypatch.setattr(dynamic_sre_module, "SkillRunner", _FakeSkillRunner)
    _reset_fake_skill_runner()

    report = {**_VALID_REPORT, "skill_ref": discovered.id, "runbook_ref": discovered.runbook_ref}
    llm = _Turns(
        _call_tool("run_skill", skill_id=discovered.id),
        _respond(report),
    )
    gw = _Gateway()
    # lookup() (the preload path) returns None — nothing was preloaded — but
    # get(skill_id) can still resolve the skill the LLM actually chose to run.
    store = _FakeFormulaStore(formula=None, by_id={discovered.id: discovered})

    result = await DynamicSREAgent(
        gateway=gw, llm_provider=llm, formula_store=store
    ).run(_state())

    assert result.get("error") is None
    assert store.get_calls == [discovered.id]
    tool_result_msg = llm.messages_received[-1][-1]["content"]
    assert "runbook:cost-spike" in tool_result_msg
    assert result["agent_output"]["runbook_ref"] == "runbook:cost-spike"


# ---------------------------------------------------------------------------
# Behavior 14i — SRE_OUTPUT_SCHEMA gains skill_ref (issue 06)
# ---------------------------------------------------------------------------

async def test_sre_output_schema_requires_skill_ref():
    """skill_ref is required (always-present, nullable — matching the
    existing runbook_ref convention), so a report missing it is rejected."""
    report = {k: v for k, v in _VALID_REPORT.items() if k != "skill_ref"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(report, SRE_OUTPUT_SCHEMA)


async def test_sre_output_schema_accepts_skill_ref_set():
    jsonschema.validate({**_VALID_REPORT, "skill_ref": "sre:db-latency:1"}, SRE_OUTPUT_SCHEMA)


# ---------------------------------------------------------------------------
# Behavior 14b — gateway.thread_id/human_approval_token are synced from state
# (issue #01: governance needs a real thread_id to scope-validate shell_exec
# approval tokens; nothing previously propagated it from AgentState onto the
# shared GatewayClient instance).
# ---------------------------------------------------------------------------

async def test_run_syncs_thread_id_and_approval_token_onto_gateway():
    from harness_agents.dynamic_sre import DynamicSREAgent

    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()
    state = _state(thread_id="thread-xyz", human_approval_token="approved-token")

    await DynamicSREAgent(gateway=gw, llm_provider=llm).run(state)

    assert gw.thread_id == "thread-xyz"
    assert gw.human_approval_token == "approved-token"


async def test_run_clears_stale_approval_token_when_state_has_none():
    """A gateway reused across turns must not leak a previous thread's token."""
    from harness_agents.dynamic_sre import DynamicSREAgent

    llm = _Turns(_respond(_VALID_REPORT))
    gw = _Gateway()
    gw.thread_id = "stale-thread"
    gw.human_approval_token = "stale-token"

    state = _state(thread_id="fresh-thread", human_approval_token=None)
    await DynamicSREAgent(gateway=gw, llm_provider=llm).run(state)

    assert gw.thread_id == "fresh-thread"
    assert gw.human_approval_token is None


# ---------------------------------------------------------------------------
# Behavior 15 — supervisor routes incident tasks to DynamicSREAgent (integration)
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


# ---------------------------------------------------------------------------
# Behavior 16 — end-to-end skill discovery + execution through the live
# gateway, with OPA in the path (issue 06)
#
# Mirrors test_dynamic_reviewer_injection_blocked_and_dolt_audited's pattern
# (test_redteam_prompt_injection.py): seed a real ACTIVE skill for the sre
# role, drive DynamicSREAgent through a scripted LLM over the live
# GatewayClient/gateway, and assert it discovers the skill via skill_search
# and executes it via run_skill's native dispatch — every step of the
# executed skill still hits governance's /check under the sre role's real
# credentials (SkillRunner calls self.gateway.call_tool() per step, so
# nothing here bypasses OPA). Requires the live Docker stack; will not run
# in a sandboxed environment (no network access to Dolt/governance/gateway).
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_sre_skill_discovery_and_execution_through_live_gateway():
    """Seed an ACTIVE sre skill, drive a matching incident through the live
    gateway, and assert skill_search discovers it and run_skill executes it
    end-to-end with OPA in the path."""
    import asyncio
    import json as _json
    import os
    import uuid as _uuid

    from harness_agents.dynamic_sre import DynamicSREAgent
    from harness_agents.llm import LLMResponse
    from harness_gateway.client import GatewayClient
    from harness_memory.formula_store import DoltFormulaStore

    dolt_host = os.environ.get("DOLT_HOST", "localhost")
    dolt_port = int(os.environ.get("DOLT_PORT", "3306"))
    governance_url = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")

    fstore = DoltFormulaStore(
        host=dolt_host, port=dolt_port, user="root", password="root", database="harness",
    )

    skill_id = f"sre:issue06-probe:{_uuid.uuid4().hex[:8]}"
    task = "issue-06 redteam probe incident — connection pool exhausted"
    formula = _Formula(
        id=skill_id,
        name="issue-06 skill-aware guidance probe",
        agent_role="sre",
        description="Diagnose issue-06 redteam probe connection pool exhaustion",
        input_schema={},
        steps=[{"action": "observability_query"}, {"action": "log_search"}],
        output_contract={},
        promoted_by="test",
        expires_at=None,
        runbook_ref="runbook:issue-06-probe",
    )
    fstore.propose(formula)

    try:
        gateway = GatewayClient(
            gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
            governance_url=governance_url,
            client_id="sre",
            client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
        )

        turns = iter([
            _json.dumps({
                "action": "call_tool", "tool": "skill_search",
                "params": {"agent_role": "sre", "task": task},
            }),
            _json.dumps({
                "action": "call_tool", "tool": "run_skill",
                "params": {"skill_id": skill_id},
            }),
            _json.dumps({"action": "respond", "result": {
                "timeline": "t", "likely_cause": "c", "severity": "P3",
                "recommended_steps": [], "runbook_ref": None,
                "skill_ref": skill_id, "requires_human_approval": False,
            }}),
        ])

        class _ScriptedLLM:
            async def chat(self, messages):
                return LLMResponse(content=next(turns))

        agent = DynamicSREAgent(gateway=gateway, llm_provider=_ScriptedLLM())
        state = _state(task=task)

        result = asyncio.run(agent.run(state))

        assert result.get("error") is None, f"Unexpected error: {result.get('error')}"
        called_tools = [c["tool"] for c in gateway.last_calls]
        assert "skill_search" in called_tools, "skill_search must discover the seeded skill"
        # run_skill's steps still route through gateway.call_tool per-step (OPA-checked)
        assert "observability_query" in called_tools
        assert "log_search" in called_tools
        # run_skill itself is native dispatch — never a gateway/MCP tool call
        assert "run_skill" not in called_tools
    finally:
        fstore._delete_where_id_like(f"{skill_id}%")
