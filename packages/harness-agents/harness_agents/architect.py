import json
import logging
import os
import re
from pathlib import Path

import jsonschema
from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_agents.types import AgentState, ARCHITECT_OUTPUT_SCHEMA
from harness_agents.llm import LLMProvider

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[3] / "prompts"))
SYSTEM_PROMPT = (_PROMPTS_DIR / "architect.md").read_text()

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
MAX_ITERATIONS = 3


def _clean_raw(raw: str) -> str:
    raw = _THINK_RE.sub("", raw).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw[: raw.rfind("```")].strip() if "```" in raw else raw
    return raw


class ArchitectAgent:
    name = "architect"
    allowed_tools = ["codebase_search", "adr_read", "adr_write", "diagram_gen"]
    memory_namespace = "architect"

    def __init__(self, gateway: GatewayClient, llm_provider: LLMProvider, memory_store=None):
        self.gateway = gateway
        self.llm = llm_provider
        self.memory = memory_store

    async def run(self, state: AgentState) -> AgentState:
        task = state["task"]

        # Read past ADRs from memory and codebase context from tools
        memory_context = []
        if self.memory:
            results = await self.memory.search(self.memory_namespace, task, top_k=3)
            memory_context = results

        try:
            codebase = await self.gateway.call_tool("codebase_search", {"query": task})
            past_adrs = await self.gateway.call_tool("adr_read", {"query": task})
        except ToolAccessDenied as e:
            logger.error("tool_access_denied: %s", e)
            return {**state, "error": {"code": "tool_access_denied", "reason": str(e)}}

        context_block = ""
        if memory_context:
            context_block = f"\nPast related decisions from memory:\n{json.dumps(memory_context, indent=2)}\n"

        user_message = f"""Task: {task}
{context_block}
Codebase search result:
{json.dumps(codebase, indent=2)}

Past ADRs:
{json.dumps(past_adrs, indent=2)}

Return your structured ADR as raw JSON."""

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        raw_output = None
        for attempt in range(MAX_ITERATIONS):
            response = await self.llm.chat(messages=messages)
            raw = _clean_raw(response.content)
            try:
                parsed = json.loads(raw)
                jsonschema.validate(parsed, ARCHITECT_OUTPUT_SCHEMA)
                raw_output = parsed
                break
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                logger.warning("attempt %d: invalid output: %s", attempt + 1, e)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Invalid response: {e}\nTry again. Raw JSON only.",
                })

        if raw_output is None:
            return {**state, "error": {"code": "invalid_output", "reason": "max retries exceeded"}}

        # Write new ADR to memory
        if self.memory and raw_output:
            adr_key = f"adr:{raw_output.get('title', task)[:60]}"
            await self.memory.write(self.memory_namespace, adr_key, raw_output)

        return {**state, "agent_output": raw_output}
