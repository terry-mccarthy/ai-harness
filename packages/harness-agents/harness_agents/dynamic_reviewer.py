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

    async def run(self, state: AgentState) -> AgentState:
        diff_text = state.get("diff", "")
        task = state.get("task", "Security review")
        token_usage = dict(state.get("token_usage") or {"prompt_tokens": 0, "completion_tokens": 0})

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Task: {task}\n\nDiff:\n{diff_text}"},
        ]

        for turn in range(MAX_TURNS):
            try:
                response = await self.llm.chat(messages=messages)
            except Exception as e:
                return {**state, "token_usage": token_usage, "error": {"code": "provider_error", "reason": str(e)}}

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

            if action.get("action") == "respond":
                result = action.get("result", {})
                try:
                    jsonschema.validate(result, REVIEWER_OUTPUT_SCHEMA)
                    return {**state, "agent_output": result, "token_usage": token_usage}
                except jsonschema.ValidationError as e:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content": f"Invalid result schema: {e.message}. Try again."})
                    continue

            if action.get("action") == "call_tool":
                tool = action.get("tool", "")
                params = action.get("params", {})
                logger.info("react turn=%d tool=%s", turn + 1, tool)

                try:
                    result = await self.gateway.call_tool(tool, params)
                except ToolAccessDenied as e:
                    logger.warning("tool_access_denied in react loop turn=%d tool=%s: %s", turn + 1, tool, e)
                    return {**state, "token_usage": token_usage, "error": {"code": "tool_access_denied", "reason": str(e)}}

                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": f"Tool result:\n{json.dumps(result, indent=2)}"})
                continue

            # Unrecognised action — nudge the LLM
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "Unrecognised action. Use 'call_tool' or 'respond'."})

        return {**state, "token_usage": token_usage, "error": {
            "code": "max_turns_exceeded",
            "reason": f"exceeded {MAX_TURNS} turns without final response",
        }}
