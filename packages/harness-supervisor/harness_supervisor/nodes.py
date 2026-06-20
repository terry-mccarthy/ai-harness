"""Supervisor graph node functions."""
import json
import logging
import os
import re
import uuid
from pathlib import Path

from opentelemetry import trace

from harness_agents.llm import LLMProvider
from harness_gateway.client import ToolAccessDenied
from .state import HarnessState

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[3] / "prompts"))
_CLASSIFY_SYSTEM = (_PROMPTS_DIR / "classify.md").read_text()
_SYNTHESISE_SYSTEM = (_PROMPTS_DIR / "synthesise.md").read_text()

_TASK_TYPES = ("design", "review", "incident")

_KEYWORDS: dict[str, tuple] = {
    "design":   ("design", "architect", "adr", "schema", "blueprint"),
    "review":   ("review", "diff", "pr", "pull request", "lint"),
    "incident": ("alert", "incident", "spike", "latency", "error", "p1", "p2", "p3", "p4", "fired"),
}


def _classify_by_keywords(task: str) -> str | None:
    t = task.lower()
    for task_type, keywords in _KEYWORDS.items():
        if any(re.search(r"\b" + re.escape(kw) + r"\b", t) for kw in keywords):
            return task_type
    return None


def _parse_task_type(raw: str) -> str | None:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    match = re.search(r"\{.*?\}", raw, flags=re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0)).get("task_type")
    except json.JSONDecodeError:
        return None
    return value if value in _TASK_TYPES else None


async def classify_node(state: HarnessState, llm_provider: LLMProvider) -> dict:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("classify"):
        tokens_used = state.get("tokens_used", 0)
        try:
            response = await llm_provider.chat([
                {"role": "system", "content": _CLASSIFY_SYSTEM},
                {"role": "user", "content": state["task"]},
            ])
            tokens_used += response.prompt_tokens + response.completion_tokens
            task_type = _parse_task_type(response.content)
        except Exception:
            logger.warning("classify: LLM call failed, falling back to keywords", exc_info=True)
            task_type = None
        if task_type is None:
            task_type = _classify_by_keywords(state["task"]) or "review"
        logger.info("classify: %s → %s", state["task"][:40], task_type)
        return {"task_type": task_type, "tokens_used": tokens_used}


def route_node(state: dict) -> str:
    """Conditional edge selector — returns the next node name based on task_type."""
    mapping = {"design": "architect", "review": "code_reviewer", "incident": "sre"}
    return mapping.get(state.get("task_type", "review"), "code_reviewer")


async def route_span_node(state: HarnessState) -> dict:
    """No-op node that emits a 'route' OTel span before the conditional dispatch."""
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("route"):
        logger.info("route: task_type=%s", state.get("task_type"))
    return {}


async def formula_lookup_node(state: HarnessState, formula_store) -> dict:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("formula_lookup"):
        task_type_to_role = {"design": "architect", "review": "code_reviewer", "incident": "sre"}
        role = task_type_to_role.get(state.get("task_type", ""), "")
        formula = formula_store.lookup(role, state["task"]) if role else None
        if formula:
            instance_id = str(uuid.uuid4())
            logger.info("formula_lookup: matched %s → instance %s", formula.id, instance_id)
            return {"formula_id": formula.id, "formula_instance_id": instance_id}
        logger.info("formula_lookup: no match for task_type=%s", state.get("task_type"))
        return {"formula_id": None, "formula_instance_id": None}


def _check_token_budget(state: HarnessState, thread_id: str) -> dict | None:
    token_budget = state.get("token_budget")
    tokens_used = state.get("tokens_used", 0)
    if token_budget is not None and tokens_used >= token_budget:
        logger.warning("token_budget exceeded: used=%d budget=%d thread=%s", tokens_used, token_budget, thread_id)
        return {"error": {"code": "budget_exceeded", "reason": f"token budget exhausted (used={tokens_used}, budget={token_budget})"}}
    return None


def _needs_approval(result: dict, agent_output: dict) -> bool:
    if result.get("requires_human_approval", False):
        return True
    if isinstance(agent_output, dict) and agent_output.get("requires_human_approval", False):
        return True
    return False


def _sum_tokens(state: HarnessState, result: dict) -> int:
    agent_tokens = result.get("token_usage") or {}
    return state.get("tokens_used", 0) + agent_tokens.get("prompt_tokens", 0) + agent_tokens.get("completion_tokens", 0)


async def run_agent_node(state: HarnessState, agent, formula=None) -> dict:
    """Run a specialist agent node, optionally guided by formula steps."""
    tracer = trace.get_tracer(__name__)
    span_name = getattr(agent, "name", "agent")
    with tracer.start_as_current_span(span_name) as span:
        agent_role = getattr(agent, "name", "unknown")
        thread_id = state.get("thread_id", "")
        span.set_attribute("agent_role", agent_role)
        span.set_attribute("thread_id", thread_id)

        budget_error = _check_token_budget(state, thread_id)
        if budget_error:
            return budget_error

        from harness_agents.types import AgentState

        if formula:
            logger.info("run_agent: executing formula %s (%d steps)", formula.id, len(formula.steps))

        agent_state: AgentState = {
            "task": state["task"],
            "diff": state.get("diff", ""),
            "thread_id": state["thread_id"],
            "agent_output": None,
            "requires_human_approval": False,
            "error": None,
            "human_approval_token": state.get("human_approval_token"),
            "memory_context": state.get("memory_context"),
        }
        result = await agent.run(agent_state)
        agent_output = result.get("agent_output") or {}
        requires_approval = _needs_approval(result, agent_output)
        tokens_used = _sum_tokens(state, result)

        return {
            "agent_output": agent_output,
            "requires_human_approval": requires_approval,
            "error": result.get("error"),
            "active_agent": getattr(agent, "name", "unknown"),
            "tokens_used": tokens_used,
        }


async def synthesise_node(state: HarnessState, formula_store=None, llm_provider=None) -> dict:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("synthesise"):
        output = state.get("agent_output") or {}
        tokens_used = state.get("tokens_used", 0)

        if llm_provider:
            user_msg = (
                f"Agent: {state.get('active_agent', 'agent')}\n"
                f"Task: {state['task']}\n\n"
                f"Structured output:\n{json.dumps(output, indent=2)}"
            )
            try:
                resp = await llm_provider.chat([
                    {"role": "system", "content": _SYNTHESISE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ])
                tokens_used += resp.prompt_tokens + resp.completion_tokens
                final_response = resp.content.strip()
            except Exception:
                logger.warning("synthesise: LLM call failed, falling back to summary field", exc_info=True)
                final_response = _fallback_summary(state, output)
        else:
            final_response = _fallback_summary(state, output)

        return {"final_response": final_response, "tokens_used": tokens_used}


def _fallback_summary(state: HarnessState, output: dict) -> str:
    summary = output.get("summary") or output.get("likely_cause") or output.get("decision") or str(output)
    return f"[{state.get('active_agent', 'agent').upper()}] {summary}"


async def propose_formula_node(state: HarnessState, formula_store) -> dict:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("propose_formula"):
        from harness_memory.models import Formula
        task_type_to_role = {"design": "architect", "review": "code_reviewer", "incident": "sre"}
        role = task_type_to_role.get(state.get("task_type", ""), "sre")
        task_slug = state["task"][:30].lower().replace(" ", "-").replace(":", "")
        draft_id = f"draft:{role}:{task_slug}"

        draft = Formula(
            id=draft_id,
            name=f"Draft: {state['task'][:40]}",
            agent_role=role,
            version=1,
            status="draft",
            description=state["task"],
            input_schema={"type": "object"},
            steps=[{"action": "llm_synthesise"}],
            output_contract={"type": "object"},
            promoted_by="supervisor:propose",
        )
        formula_store.propose(draft)
        logger.info("propose_formula: created draft %s", draft_id)
        return {}


async def architectural_gate_node(state: HarnessState, gateway) -> dict:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("architectural_gate"):
        repo_path = state.get("repo_path", ".")
        target_language = state.get("target_language", "python")

        try:
            result = await gateway.call_tool("execute_architecture_check", {
                "repo_path": repo_path,
                "target_language": target_language,
            })
        except ToolAccessDenied as e:
            logger.error("gate tool denied: %s", e)
            return {
                "gate_signal": {
                    "result": "FAIL",
                    "violations": [],
                    "action": "STOP_AND_SURFACE",
                },
                "error": {"code": "tool_access_denied", "reason": str(e)},
            }

        signal = result if isinstance(result, dict) else {}
        logger.info("architectural_gate: result=%s violations=%d",
                     signal.get("result"), len(signal.get("violations", [])))
        return {"gate_signal": signal}


async def error_handler_node(state: HarnessState) -> dict:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("error_handler"):
        error = state.get("error") or {"code": "unknown", "reason": "unspecified error"}
        logger.error("error_handler: %s", error)
        return {
            "error": error,
            "final_response": f"Error: {error.get('reason', 'unspecified')}",
        }
