import json
import logging
import os
import re
from pathlib import Path

import jsonschema
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_agents.types import AgentState, REVIEWER_OUTPUT_SCHEMA
from harness_agents.llm import LLMProvider

logger = logging.getLogger(__name__)

MAX_TURNS = 8

_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[3] / "prompts"))
SYSTEM_PROMPT = (_PROMPTS_DIR / "react_code_reviewer.md").read_text()

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _clean_raw(raw: str) -> str:
    raw = _THINK_RE.sub("", raw).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw[: raw.rfind("```")].strip() if "```" in raw else raw
    return raw


class DynamicCodeReviewerAgent:
    """Code reviewer with a ReAct tool-use loop.

    The LLM directs tool selection on each turn, enabling the agent to reason
    across multiple tool results before producing a review. Unlike the static
    CodeReviewerAgent, this makes the LLM's tool-calling behaviour observable
    and testable — including injection attempts.
    """

    name = "dynamic_code_reviewer"
    allowed_tools = ["git_diff", "run_linter"]
    memory_namespace = "code_reviewer"

    def __init__(self, gateway: GatewayClient, llm_provider: LLMProvider, memory_store=None):
        self.gateway = gateway
        self.llm = llm_provider
        self.memory = memory_store

    def _init_token_usage(self, state: AgentState) -> dict:
        current = state.get("token_usage")
        if current:
            return dict(current)
        return {"prompt_tokens": 0, "completion_tokens": 0}

    async def _llm_chat(self, messages: list[dict], token_usage: dict) -> tuple | None:
        try:
            response = await self.llm.chat(messages=messages)
            return response, None
        except Exception as e:
            return None, str(e)

    def _handle_respond_action(self, result: dict, raw: str, messages: list[dict]) -> dict | None:
        try:
            jsonschema.validate(result, REVIEWER_OUTPUT_SCHEMA)
            return result
        except jsonschema.ValidationError as e:
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Invalid result schema: {e.message}. Try again."})
            return None

    async def _handle_tool_call(
        self, tool: str, params: dict, raw: str, messages: list[dict], state: AgentState, token_usage: dict
    ) -> AgentState | None:
        try:
            result = await self.gateway.call_tool(tool, params)
        except ToolAccessDenied as e:
            return {**state, "token_usage": token_usage, "error": {"code": "tool_access_denied", "reason": str(e)}}
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"Tool result:\n{json.dumps(result, indent=2)}"})
        return None

    async def _dispatch_action(
        self, action_type: str, action: dict, raw: str, messages: list[dict], state: AgentState, token_usage: dict
    ) -> AgentState | None:
        if action_type == "respond":
            result = self._handle_respond_action(action.get("result", {}), raw, messages)
            if result is not None:
                return {**state, "agent_output": result, "token_usage": token_usage}
            return None
        if action_type == "call_tool":
            return await self._handle_tool_call(action.get("tool", ""), action.get("params", {}), raw, messages, state, token_usage)
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": "Unrecognised action. Use 'call_tool' or 'respond'."})
        return None

    async def run(self, state: AgentState) -> AgentState:
        diff_text = state.get("diff", "")
        task = state.get("task", "Security review")
        token_usage = self._init_token_usage(state)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Task: {task}\n\nDiff:\n{diff_text}"},
        ]

        for turn in range(MAX_TURNS):
            response, error = await self._llm_chat(messages, token_usage)
            if error:
                return {**state, "token_usage": token_usage, "error": {"code": "provider_error", "reason": error}}

            raw = _clean_raw(response.content)
            token_usage["prompt_tokens"] += response.prompt_tokens
            token_usage["completion_tokens"] += response.completion_tokens
            logger.debug("turn %d raw: %s", turn + 1, raw)

            try:
                action = json.loads(raw)
            except json.JSONDecodeError:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "Invalid JSON. Respond with exactly one JSON object."})
                continue

            handled = await self._dispatch_action(action.get("action"), action, raw, messages, state, token_usage)
            if handled is not None:
                return handled

        return {**state, "token_usage": token_usage, "error": {
            "code": "max_turns_exceeded",
            "reason": f"exceeded {MAX_TURNS} turns without final response",
        }}
