import json
import logging
import os
import re
from pathlib import Path

import jsonschema
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_agents.types import AgentState, SRE_OUTPUT_SCHEMA
from harness_agents.llm import LLMProvider

logger = logging.getLogger(__name__)

MAX_TURNS = 8

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
    allowed_tools = ["observability_query", "log_search", "runbook_read", "shell_exec"]
    memory_namespace = "sre"

    def __init__(self, gateway: GatewayClient, llm_provider: LLMProvider, memory_store=None):
        self.gateway = gateway
        self.llm = llm_provider
        self.memory = memory_store

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

    async def _handle_tool_call(
        self, tool: str, params: dict, raw: str, messages: list[dict],
        state: AgentState, token_usage: dict,
    ):
        try:
            result = await self.gateway.call_tool(tool, params)
        except ToolAccessDenied as e:
            return {**state, "token_usage": token_usage, "error": {"code": "tool_access_denied", "reason": str(e)}}
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

    async def _load_memory(self, task: str) -> list:
        if not self.memory:
            return []
        return await self.memory.search(self.memory_namespace, task, top_k=3)

    async def _save_memory(self, state: AgentState, report: dict) -> None:
        if not self.memory:
            return
        key = f"incident:{state['thread_id'][:8]}"
        await self.memory.write(self.memory_namespace, key, report)

    async def run(self, state: AgentState) -> AgentState:
        task = state.get("task", "")
        token_usage = self._init_token_usage(state)
        token_budget = state.get("token_budget")

        memory_context = await self._load_memory(task)
        context_block = (
            f"\nPast incidents from memory:\n{json.dumps(memory_context, indent=2)}\n"
            if memory_context else ""
        )

        system_prompt = (_PROMPTS_DIR / "react_sre.md").read_text()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Incident: {task}{context_block}\n\nBegin your investigation."},
        ]

        for turn in range(MAX_TURNS):
            response, error = await self._llm_chat(messages, token_usage)
            if error:
                return {**state, "token_usage": token_usage, "error": {"code": "provider_error", "reason": error}}

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
                return handled

        return {**state, "token_usage": token_usage, "error": {
            "code": "max_turns_exceeded",
            "reason": f"exceeded {MAX_TURNS} turns without final response",
        }}
