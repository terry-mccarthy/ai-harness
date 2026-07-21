import json
import logging
import os
import re
import jsonschema
from pathlib import Path
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_agents.types import AgentState, ADVERSARIAL_CODE_CRITIC_SCHEMA
from harness_agents.llm import LLMProvider

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3

_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[3] / "prompts"))
SYSTEM_PROMPT = (_PROMPTS_DIR / "adversarial_code_critic.md").read_text()

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _clean_raw(raw: str) -> str:
    """Strip thinking blocks and markdown fences from a model response."""
    raw = _THINK_RE.sub("", raw).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw[: raw.rfind("```")].strip() if "```" in raw else raw
    return raw


class AdversarialCodeCritic:
    """Attacks a first-pass CodeReviewerAgent output. Confirms, refutes, escalates,
    downgrades, or leaves unresolved each finding — a CRITICAL confirm/escalate
    requires a concrete exploit_scenario, not a bare severity label."""

    name = "adversarial_code_critic"
    allowed_tools = ["git_diff", "run_linter"]
    memory_namespace = "adversarial_code_critic"

    def __init__(self, gateway: GatewayClient, llm_provider: LLMProvider):
        self.gateway = gateway
        self.llm = llm_provider

    def _check_token_budget(self, token_usage: dict, token_budget: int | None) -> dict | None:
        if token_budget is None or token_usage["completion_tokens"] < token_budget:
            return None
        logger.warning(
            "token_budget_exceeded: completion_tokens=%d budget=%d",
            token_usage["completion_tokens"], token_budget,
        )
        return {
            "code": "token_budget_exceeded",
            "reason": f"completion tokens {token_usage['completion_tokens']} exceeded budget {token_budget}",
        }

    async def _retry_until_valid(self, messages: list, token_usage: dict, token_budget: int | None):
        for attempt in range(MAX_ITERATIONS):
            try:
                response = await self.llm.chat(messages=messages)
            except Exception as e:
                return None, token_usage, {"code": "provider_error", "reason": str(e)}
            raw = _clean_raw(response.content)
            logger.debug("attempt %d cleaned response:\n%s", attempt + 1, raw)

            token_usage["prompt_tokens"] += response.prompt_tokens
            token_usage["completion_tokens"] += response.completion_tokens

            try:
                parsed = json.loads(raw)
                jsonschema.validate(parsed, ADVERSARIAL_CODE_CRITIC_SCHEMA)
                return parsed, token_usage, None
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                logger.warning("attempt %d: invalid output: %s", attempt + 1, e)
                budget_error = self._check_token_budget(token_usage, token_budget)
                if budget_error:
                    return None, token_usage, budget_error
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Your previous response was invalid: {e}\nTry again. Raw JSON only.",
                })

        return None, token_usage, {"code": "invalid_output", "reason": "max retries exceeded"}

    async def _gather_tool_results(self, diff_text: str) -> tuple[dict, dict] | None:
        try:
            diff_result = await self.gateway.call_tool("git_diff", {"diff_text": diff_text})
            lint_result = await self.gateway.call_tool("run_linter", {"diff_text": diff_text})
            return diff_result, lint_result
        except ToolAccessDenied as e:
            logger.error("tool_access_denied: %s", e)
            return None

    async def _build_user_message(self, task: str, diff_text: str, first_pass_output: dict | None) -> str | None:
        tool_results = await self._gather_tool_results(diff_text)
        if tool_results is None:
            return None
        diff_result, lint_result = tool_results

        return f"""Task: {task}

First-pass reviewer output to attack:
{json.dumps(first_pass_output or {}, indent=2)}

Diff tool result:
{json.dumps(diff_result, indent=2)}

Linter result:
{json.dumps(lint_result, indent=2)}

Return your structured critique as raw JSON."""

    async def run(self, state: AgentState) -> AgentState:
        user_message = await self._build_user_message(
            state["task"], state["diff"], state.get("first_pass_output")
        )
        if user_message is None:
            return {**state, "error": {"code": "tool_access_denied", "reason": "Failed to gather tool results"}}

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        token_usage = dict(state.get("token_usage") or {"prompt_tokens": 0, "completion_tokens": 0})
        token_budget = state.get("token_budget")

        raw_output, token_usage, error = await self._retry_until_valid(messages, token_usage, token_budget)

        if error:
            return {**state, "token_usage": token_usage, "error": error}
        return {**state, "agent_output": raw_output, "token_usage": token_usage}
