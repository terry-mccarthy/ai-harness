import json
import logging
import os
import re
import jsonschema
from pathlib import Path
from ollama import AsyncClient
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_agents.types import AgentState, REVIEWER_OUTPUT_SCHEMA

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

    def __init__(
        self,
        gateway: GatewayClient,
        llm_client: AsyncClient,
        model: str = "qwen2.5-coder",
        num_ctx: int = 8192,
        temperature: float = 0.1,
        num_predict: int = 1024,
    ):
        self.gateway = gateway
        self.llm = llm_client
        self.model = model
        self._options = {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        }

    async def run(self, state: AgentState) -> AgentState:
        diff_text = state["diff"]
        task = state["task"]

        try:
            diff_result = await self.gateway.call_tool("git_diff", {"diff_text": diff_text})
            lint_result = await self.gateway.call_tool("run_linter", {"diff_text": diff_text})
        except ToolAccessDenied as e:
            logger.error("tool_access_denied: %s", e)
            return {**state, "error": {"code": "tool_access_denied", "reason": str(e)}}

        user_message = f"""Task: {task}

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
        logger.debug("llm options: %s", self._options)

        raw_output = None
        for attempt in range(MAX_ITERATIONS):
            response = await self.llm.chat(
                model=self.model,
                messages=messages,
                options=self._options,
            )
            raw = _clean_raw(response.message.content)
            logger.debug("attempt %d cleaned response:\n%s", attempt + 1, raw)

            try:
                parsed = json.loads(raw)
                jsonschema.validate(parsed, REVIEWER_OUTPUT_SCHEMA)
                raw_output = parsed
                break
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                logger.warning("attempt %d: invalid output: %s", attempt + 1, e)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Your previous response was invalid: {e}\nTry again. Raw JSON only.",
                })

        if raw_output is None:
            return {**state, "error": {"code": "invalid_output", "reason": "max retries exceeded"}}

        return {**state, "agent_output": raw_output}
