import json
import logging
import os
import re
import jsonschema
from pathlib import Path
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_agents.types import AgentState, REVIEWER_OUTPUT_SCHEMA
from harness_agents.llm import LLMProvider

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3

_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[3] / "prompts"))
SYSTEM_PROMPT = (_PROMPTS_DIR / "code_reviewer.md").read_text()

# Match <think>...</think> blocks emitted by qwen3 and other reasoning models
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _clean_raw(raw: str) -> str:
    """Strip thinking blocks and markdown fences from a model response."""
    raw = _THINK_RE.sub("", raw).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw[: raw.rfind("```")].strip() if "```" in raw else raw
    return raw


class CodeReviewerAgent:
    name = "code_reviewer"
    allowed_tools = ["git_diff", "run_linter"]
    memory_namespace = "code_reviewer"

    def __init__(self, gateway: GatewayClient, llm_provider: LLMProvider, memory_store=None):
        self.gateway = gateway
        self.llm = llm_provider
        self.memory = memory_store

    async def _retry_until_valid(self, messages: list, token_usage: dict, token_budget: int | None):
        """Run the LLM retry loop. Returns (parsed_output, token_usage, error_dict | None)."""
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
                jsonschema.validate(parsed, REVIEWER_OUTPUT_SCHEMA)
                return parsed, token_usage, None
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                logger.warning("attempt %d: invalid output: %s", attempt + 1, e)

                if token_budget is not None and token_usage["completion_tokens"] >= token_budget:
                    logger.warning(
                        "token_budget_exceeded: completion_tokens=%d budget=%d",
                        token_usage["completion_tokens"], token_budget,
                    )
                    return None, token_usage, {
                        "code": "token_budget_exceeded",
                        "reason": f"completion tokens {token_usage['completion_tokens']} exceeded budget {token_budget}",
                    }

                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Your previous response was invalid: {e}\nTry again. Raw JSON only.",
                })

        return None, token_usage, {"code": "invalid_output", "reason": "max retries exceeded"}

    async def run(self, state: AgentState) -> AgentState:
        diff_text = state["diff"]
        task = state["task"]

        conventions = []
        if self.memory:
            conventions = await self.memory.search(self.memory_namespace, "repo conventions coding style", top_k=3)

        try:
            diff_result = await self.gateway.call_tool("git_diff", {"diff_text": diff_text})
            lint_result = await self.gateway.call_tool("run_linter", {"diff_text": diff_text})
        except ToolAccessDenied as e:
            logger.error("tool_access_denied: %s", e)
            return {**state, "error": {"code": "tool_access_denied", "reason": str(e)}}

        conventions_block = ""
        if conventions:
            conventions_block = f"\nRepo conventions from memory:\n{json.dumps(conventions, indent=2)}\n"

        user_message = f"""Task: {task}
{conventions_block}
Diff tool result:
{json.dumps(diff_result, indent=2)}

Linter result:
{json.dumps(lint_result, indent=2)}

Return your structured review as raw JSON."""

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        logger.debug("llm user_message:\n%s", user_message)

        token_usage = dict(state.get("token_usage") or {"prompt_tokens": 0, "completion_tokens": 0})
        token_budget = state.get("token_budget")

        raw_output, token_usage, error = await self._retry_until_valid(messages, token_usage, token_budget)

        if error:
            return {**state, "token_usage": token_usage, "error": error}
        return {**state, "agent_output": raw_output, "token_usage": token_usage}
