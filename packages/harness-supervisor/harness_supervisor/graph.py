"""Build the LangGraph supervisor graph."""
import os
import logging
from functools import partial

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # used in build_supervisor

from harness_agents.architect import ArchitectAgent
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.sre import SREAgent
from harness_memory.formula_store import DoltFormulaStore

from .state import HarnessState
from .nodes import (
    classify_node,
    route_node,
    route_span_node,
    formula_lookup_node,
    run_agent_node,
    synthesise_node,
    propose_formula_node,
    architectural_gate_node,
    error_handler_node,
)
from .approval import validate_approval_token

logger = logging.getLogger(__name__)

_JWT_SECRET = os.environ.get("JWT_SECRET", "dev-jwt-secret-change-in-prod-xyz")

DOLT_CONN = dict(
    host=os.environ.get("DOLT_HOST", "localhost"),
    port=int(os.environ.get("DOLT_PORT", "3306")),
    user="root",
    password="root",
    database="harness",
)


def _should_propose_formula(state: HarnessState) -> str:
    """After agent run: if no formula was used and no error, offer to propose one."""
    if state.get("error"):
        return "error_handler"
    if state.get("requires_human_approval"):
        return "human_gate"
    if not state.get("formula_id"):
        return "propose_formula"
    return "synthesise"


def _has_violation(violations: list, severity: str) -> bool:
    for v in violations:
        if v.get("severity") == severity:
            return True
    return False


def route_after_gate(state: HarnessState) -> str:
    signal = state.get("gate_signal")
    if not signal:
        return "error_handler"
    result = signal.get("result")
    if result == "PASS":
        return "synthesise"
    if result != "FAIL":
        return "error_handler"

    violations = signal.get("violations", [])
    if _has_violation(violations, "HARD"):
        return "human_gate"
    if _has_violation(violations, "SOFT") and not state.get("human_justification"):
        return "human_gate"
    return "synthesise"


def _after_human_gate(state: HarnessState) -> str:
    # Gate soft-fail justification — resume to synthesise
    if state.get("human_justification"):
        return "synthesise"
    token = state.get("human_approval_token")
    thread_id = state.get("thread_id", "")
    if not token:
        return END  # still paused; caller must resume with token
    if validate_approval_token(token, thread_id=thread_id, tool_name="shell_exec", secret=_JWT_SECRET):
        return "sre"  # resume the SRE agent with the approval token
    return "error_handler"


async def build_supervisor(
    llm_provider,
    gateway,
    pg_dsn: str | None = None,
    formula_store=None,
    memory_store=None,
    tracer_provider=None,
    checkpointer=None,
):
    if tracer_provider is not None:
        from opentelemetry import trace as otel_trace
        otel_trace.set_tracer_provider(tracer_provider)

    fstore = formula_store or DoltFormulaStore(**DOLT_CONN)

    architect = ArchitectAgent(gateway=gateway, llm_provider=llm_provider, memory_store=memory_store)
    reviewer  = CodeReviewerAgent(gateway=gateway, llm_provider=llm_provider, memory_store=memory_store)
    sre       = SREAgent(gateway=gateway, llm_provider=llm_provider, memory_store=memory_store)

    builder = StateGraph(HarnessState)

    # Node wiring with partial application for injected dependencies
    builder.add_node("classify",        partial(classify_node,       llm_provider=llm_provider))
    builder.add_node("formula_lookup",  partial(formula_lookup_node, formula_store=fstore))
    builder.add_node("route",           route_span_node)
    builder.add_node("architect",       partial(run_agent_node,      agent=architect))
    builder.add_node("code_reviewer",   partial(run_agent_node,      agent=reviewer))
    builder.add_node("sre",             partial(run_agent_node,      agent=sre))
    builder.add_node("synthesise",      partial(synthesise_node,     formula_store=fstore, llm_provider=llm_provider))
    builder.add_node("propose_formula", partial(propose_formula_node, formula_store=fstore))
    builder.add_node("architectural_gate", partial(architectural_gate_node, gateway=gateway))
    builder.add_node("human_gate",         _human_gate_node)
    builder.add_node("error_handler",      error_handler_node)

    # Edges
    builder.set_entry_point("classify")
    builder.add_edge("classify", "formula_lookup")
    builder.add_edge("formula_lookup", "route")
    builder.add_conditional_edges("route", route_node, {
        "architect":     "architect",
        "code_reviewer": "code_reviewer",
        "sre":           "sre",
    })

    # Architect goes through architectural gate; other agents use formula routing
    builder.add_edge("architect", "architectural_gate")
    builder.add_conditional_edges("architectural_gate", route_after_gate, {
        "synthesise":    "synthesise",
        "human_gate":    "human_gate",
        "error_handler": "error_handler",
    })

    for agent_node in ("code_reviewer", "sre"):
        builder.add_conditional_edges(agent_node, _should_propose_formula, {
            "synthesise":      "synthesise",
            "propose_formula": "propose_formula",
            "human_gate":      "human_gate",
            "error_handler":   "error_handler",
        })

    builder.add_edge("propose_formula", "synthesise")
    builder.add_conditional_edges("human_gate", _after_human_gate, {
        "sre":           "sre",
        "error_handler": "error_handler",
        END:             END,
    })
    builder.add_edge("synthesise",    END)
    builder.add_edge("error_handler", END)

    if checkpointer is None:
        # Default: real PostgreSQL pool (used in production and durability tests)
        from psycopg_pool import AsyncConnectionPool
        pool = AsyncConnectionPool(
            conninfo=pg_dsn,
            max_size=5,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await pool.open()
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_gate"],
    )


async def _human_gate_node(state: HarnessState) -> dict:
    """Interrupt node — execution pauses here when requires_human_approval=True."""
    logger.info("human_gate: paused, waiting for approval on thread %s", state.get("thread_id"))
    return {}
