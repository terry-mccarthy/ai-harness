import json
import logging
import os
import re
from pathlib import Path

import jsonschema
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_gateway.skill_runner import SkillRunner
from harness_agents.types import AgentState, SRE_OUTPUT_SCHEMA
from harness_agents.llm import LLMProvider

logger = logging.getLogger(__name__)

MAX_TURNS = 16

_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[3] / "prompts"))
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _clean(raw: str) -> str:
    raw = _THINK_RE.sub("", raw).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw[: raw.rfind("```")].strip() if "```" in raw else raw
    return raw


def _coerce_approval(report: dict) -> dict:
    """Enforce: requires_human_approval must be True if any step requires_approval."""
    if any(s.get("requires_approval") for s in report.get("recommended_steps", [])):
        report["requires_human_approval"] = True
    return report


class DynamicSREAgent:
    name = "sre"
    allowed_tools = ["observability_query", "log_search", "runbook_read", "shell_exec", "skill_search", "run_skill"]
    memory_namespace = "sre"

    def __init__(
        self,
        gateway: GatewayClient,
        llm_provider: LLMProvider,
        memory_store=None,
        formula_store=None,
        cache_threshold: float = 0.92,
        cache_ttl_seconds: int = 86400,
    ):
        self.gateway = gateway
        self.llm = llm_provider
        self.memory = memory_store
        self.formula_store = formula_store
        self.cache_threshold = cache_threshold
        self.cache_ttl_seconds = cache_ttl_seconds

    def _init_token_usage(self, state: AgentState) -> dict:
        current = state.get("token_usage")
        return dict(current) if current else {"prompt_tokens": 0, "completion_tokens": 0}

    async def _llm_chat(self, messages: list[dict], token_usage: dict):
        try:
            response = await self.llm.chat(messages=messages)
            token_usage["prompt_tokens"] += response.prompt_tokens
            token_usage["completion_tokens"] += response.completion_tokens
            return response, None
        except Exception as e:
            return None, str(e)

    def _handle_respond(self, result: dict, raw: str, messages: list[dict]):
        result = _coerce_approval(result)
        try:
            jsonschema.validate(result, SRE_OUTPUT_SCHEMA)
            return result
        except jsonschema.ValidationError as e:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Invalid schema: {e.message}. Try again."})
            return None

    async def _run_skill(self, params: dict, state: AgentState) -> dict:
        """Native dispatch for run_skill — NOT a gateway/MCP tool.

        run_skill has no TOOL_NAME_MAP entry and no MCP server hosts it, so
        it must never reach self.gateway.call_tool(). Instead it drives
        SkillRunner directly against the agent's own (already-authenticated)
        GatewayClient, so every step inside the skill still gets its own
        per-step OPA check under the agent's real credentials — running a
        skill grants no extra authority over calling its steps by hand.
        """
        skill_id = params.get("skill_id")
        result = await SkillRunner(self.gateway).execute(skill_id, params.get("inputs"))
        formula = self._resolve_run_formula(skill_id, state)
        if formula is not None:
            result = {**result, "runbook_ref": getattr(formula, "runbook_ref", None)}
        return result

    def _resolve_run_formula(self, skill_id, state: AgentState):
        """Find the Formula for the skill actually run, for runbook_ref linkage.

        The preloaded formula in state (from _load_formula, if any) only
        matches when the LLM ran that exact preloaded skill. When it instead
        discovers and runs a different skill via skill_search (the
        cold-start/no-preload path this feature is largely for), the
        preloaded formula is absent or stale — fetch the real one by id so
        report linkage doesn't silently drop for that path.
        """
        formula = state.get("formula")
        if formula is not None and getattr(formula, "id", None) == skill_id:
            return formula
        if self.formula_store is not None and skill_id:
            return self.formula_store.get(skill_id)
        return None

    async def _handle_tool_call(
        self, tool: str, params: dict, raw: str, messages: list[dict],
        state: AgentState, token_usage: dict,
    ):
        try:
            if tool == "run_skill":
                result = await self._run_skill(params, state)
            else:
                result = await self.gateway.call_tool(tool, params)
        except ToolAccessDenied:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": (
                f"Access denied for tool '{tool}'. "
                "If this is a remediation action, propose it in recommended_steps "
                "with requires_approval=true instead of calling it directly."
            )})
            return None
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"Tool result:\n{json.dumps(result, indent=2)}"})
        return None

    async def _dispatch(self, action: dict, raw: str, messages: list[dict], state: AgentState, token_usage: dict):
        action_type = action.get("action")
        if action_type == "respond":
            report = self._handle_respond(action.get("result", {}), raw, messages)
            if report is not None:
                return {**state, "agent_output": report, "token_usage": token_usage}
            return None
        if action_type == "call_tool":
            return await self._handle_tool_call(
                action.get("tool", ""), action.get("params", {}), raw, messages, state, token_usage,
            )
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": "Unrecognised action. Use 'call_tool' or 'respond'."})
        return None

    async def _cache_lookup(self, task: str, force_refresh: bool = False):
        if not self.memory or force_refresh:
            return None
        key = f"cache:{hash(task)}"
        # Fast path: exact key match (Redis-accelerated for repeated identical tasks)
        hit = await self.memory.read("cache", key)
        if hit is not None:
            return hit
        # Semantic path: pgvector similarity for near-identical tasks
        results = await self.memory.search("cache", task, top_k=1)
        if results and results[0].get("score", 0) >= self.cache_threshold:
            return results[0]["value"]
        return None

    def _load_formula(self, task: str):
        if not self.formula_store:
            return None
        return self.formula_store.lookup(self.name, task)

    async def _load_memory(self, task: str) -> list:
        if not self.memory:
            return []
        return await self.memory.search(self.memory_namespace, task, top_k=3)

    async def _save_memory(self, state: AgentState, report: dict) -> None:
        if not self.memory:
            return
        key = f"incident:{state['thread_id'][:8]}"
        await self.memory.write(self.memory_namespace, key, report)

    async def _cache_write(self, state: AgentState, agent_output: dict) -> None:
        if not self.memory or state.get("force_refresh"):
            return
        task = state.get("task", "")
        key = f"cache:{hash(task)}"
        ttl_hours = self.cache_ttl_seconds / 3600 if self.cache_ttl_seconds else None
        # Embed only the task string so pgvector similarity searches compare task ↔ task
        await self.memory.write(
            "cache", key, {"task": task, "agent_output": agent_output},
            ttl_hours=ttl_hours, _embedding_text=task,
        )

    async def _report_llm_usage(self, token_usage: dict) -> None:
        """Best-effort: send accumulated LLM token counts to governance for Prometheus."""
        if not hasattr(self.gateway, "report_llm_usage"):
            return
        if not (hasattr(self.llm, "provider_name") and hasattr(self.llm, "model_name")):
            return
        try:
            await self.gateway.report_llm_usage(
                provider=self.llm.provider_name,
                model=self.llm.model_name,
                prompt_tokens=token_usage.get("prompt_tokens", 0),
                completion_tokens=token_usage.get("completion_tokens", 0),
            )
        except Exception:
            pass

    def _build_prompt_blocks(self, task: str, formula, memory_context) -> tuple[str, str]:
        formula_block = ""
        if formula:
            formula_block = (
                f"\nA proven skill exists for this incident type: '{formula.name}' "
                f"(id: {formula.id}).\n"
                f"Description: {formula.description}\n"
                f"Prefer calling run_skill with this id over improvising, unless its "
                f"description is clearly inapplicable.\n"
            )
        context_block = (
            f"\nPast incidents from memory:\n{json.dumps(memory_context, indent=2)}\n"
            if memory_context else ""
        )
        return formula_block, context_block

    async def _react_loop(
        self, messages: list, state: AgentState, token_usage: dict, token_budget: int | None
    ) -> AgentState:
        for turn in range(MAX_TURNS):
            response, error = await self._llm_chat(messages, token_usage)
            if error:
                return {**state, "token_usage": token_usage, "error": {"code": "provider_error", "reason": error}}
            if response is None or response.content is None:
                return {**state, "token_usage": token_usage, "error": {"code": "provider_error", "reason": "empty response from LLM"}}
            if token_budget is not None and token_usage["completion_tokens"] >= token_budget:
                return {**state, "token_usage": token_usage, "error": {
                    "code": "token_budget_exceeded",
                    "reason": f"completion tokens {token_usage['completion_tokens']} exceeded budget {token_budget}",
                }}

            raw = _clean(response.content)
            logger.debug("turn %d: %s", turn + 1, raw)

            try:
                action = json.loads(raw)
            except json.JSONDecodeError:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "Invalid JSON. Respond with exactly one JSON object."})
                continue

            handled = await self._dispatch(action, raw, messages, state, token_usage)
            if handled is not None:
                if handled.get("agent_output"):
                    await self._save_memory(state, handled["agent_output"])
                    await self._cache_write(state, handled["agent_output"])
                return handled

        return {**state, "token_usage": token_usage, "error": {
            "code": "max_turns_exceeded",
            "reason": f"exceeded {MAX_TURNS} turns without final response",
        }}

    def _sync_gateway_approval_context(self, state: AgentState) -> None:
        """Propagate thread_id/human_approval_token from graph state onto the
        shared gateway so a resumed shell_exec call can be verified by
        governance's /check (ADR 0012, issue #01). GatewayClient is a single
        instance shared across agents/threads for the life of the process, so
        this mutation is only safe because requests are processed one at a
        time per resumed thread; it is not concurrency-safe across
        simultaneously in-flight threads (a pre-existing property of the
        shared-gateway design, not introduced here).
        """
        self.gateway.thread_id = state.get("thread_id")
        self.gateway.human_approval_token = state.get("human_approval_token")

    async def run(self, state: AgentState) -> AgentState:
        task = state.get("task", "")
        self._sync_gateway_approval_context(state)

        force_refresh = state.get("force_refresh", False)
        cached = await self._cache_lookup(task, force_refresh=force_refresh)
        if cached is not None:
            return {**cached, "cache_hit": True}

        token_usage = self._init_token_usage(state)
        token_budget = state.get("token_budget")

        formula = state.get("formula") or self._load_formula(task)
        memory_context = await self._load_memory(task)
        formula_block, context_block = self._build_prompt_blocks(task, formula, memory_context)

        system_prompt = (_PROMPTS_DIR / "react_sre.md").read_text()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Incident: {task}{formula_block}{context_block}\n\nBegin your investigation."},
        ]

        # Make the resolved formula visible to _handle_tool_call (e.g. so a
        # run_skill dispatch can attach the matched formula's runbook_ref to
        # its tool result) — state may not have carried it in if it was
        # loaded here rather than pre-loaded by the supervisor.
        if formula is not None:
            state = {**state, "formula": formula}

        try:
            return await self._react_loop(messages, state, token_usage, token_budget)
        finally:
            await self._report_llm_usage(token_usage)
