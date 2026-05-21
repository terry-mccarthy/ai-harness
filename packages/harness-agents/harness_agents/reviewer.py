import json
import logging
import jsonschema
from ollama import AsyncClient
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_agents.types import AgentState, REVIEWER_OUTPUT_SCHEMA

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3

SYSTEM_PROMPT = """You are a senior security-focused code reviewer acting as the last line of defence before code ships.

You will receive tool results from git_diff and run_linter. Synthesise both into a structured review.

Look for:
- Security vulnerabilities: credential leaks, injection flaws (SQL, shell, path traversal), missing auth enforcement, insecure defaults, secrets in logs
- Code quality: missing error handling, dead code, resource leaks, incorrect types, silent failures
- Architectural concerns: hardcoded values, tight coupling, shared mutable state, missing abstractions

Be skeptical. Flag anything you would block in a real code review, not just the obvious.

Output format (strict JSON, no markdown fences):
{
  "verdict": "pass" | "fail",
  "findings": [
    {"severity": "CRITICAL"|"WARNING"|"INFO", "file": "...", "line": 0, "message": "...", "suggestion": "..."}
  ],
  "summary": "one paragraph summary"
}

Rules:
- verdict is "fail" if ANY finding is CRITICAL.
- verdict is "pass" only if there are zero CRITICAL findings.
- Raw JSON only. Do not include markdown fences or any text outside the JSON object."""


class CodeReviewerAgent:
    name = "code_reviewer"
    allowed_tools = ["git_diff", "run_linter"]
    memory_namespace = "code_reviewer"

    def __init__(self, gateway: GatewayClient, llm_client: AsyncClient, model: str = "qwen2.5-coder"):
        self.gateway = gateway
        self.llm = llm_client
        self.model = model

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

        raw_output = None
        for attempt in range(MAX_ITERATIONS):
            response = await self.llm.chat(model=self.model, messages=messages)
            raw = response.message.content.strip()
            logger.debug("attempt %d raw response:\n%s", attempt + 1, raw)
            # Strip markdown fences if the model ignores the instruction
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1]
                raw = raw[: raw.rfind("```")].strip() if "```" in raw else raw
                logger.debug("attempt %d post-strip:\n%s", attempt + 1, raw)

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
