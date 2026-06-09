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

_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[3] / "prompts"))
SYSTEM_PROMPT = (_PROMPTS_DIR / "sre.md").read_text()

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
MAX_ITERATIONS = 3


def _clean_raw(raw: str) -> str:
    raw = _THINK_RE.sub("", raw).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw[: raw.rfind("```")].strip() if "```" in raw else raw
    return raw


class SREAgent:
    name = "sre"
    allowed_tools = ["observability_query", "log_search", "runbook_read", "shell_exec"]
    memory_namespace = "sre"

    def __init__(self, gateway: GatewayClient, llm_provider: LLMProvider, memory_store=None):
        self.gateway = gateway
        self.llm = llm_provider
        self.memory = memory_store

    async def run(self, state: AgentState) -> AgentState:
        task = state["task"]

        memory_context = []
        if self.memory:
            results = await self.memory.search(self.memory_namespace, task, top_k=3)
            memory_context = results

        try:
            metrics = await self.gateway.call_tool("observability_query", {"query": task})
            logs = await self.gateway.call_tool("log_search", {"query": task})
            runbook = await self.gateway.call_tool("runbook_read", {"runbook_name": task})
        except ToolAccessDenied as e:
            logger.error("tool_access_denied: %s", e)
            return {**state, "error": {"code": "tool_access_denied", "reason": str(e)}}

        context_block = ""
        if memory_context:
            context_block = f"\nPast incidents from memory:\n{json.dumps(memory_context, indent=2)}\n"

        user_message = f"""Incident: {task}
{context_block}
Observability data:
{json.dumps(metrics, indent=2)}

Log search results:
{json.dumps(logs, indent=2)}

Runbook lookup:
{json.dumps(runbook, indent=2)}

Return your incident report as raw JSON."""

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
                jsonschema.validate(parsed, SRE_OUTPUT_SCHEMA)
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

        # Write incident summary to memory
        if self.memory and raw_output:
            incident_key = f"incident:{state['thread_id'][:8]}"
            await self.memory.write(self.memory_namespace, incident_key, raw_output)

        return {**state, "agent_output": raw_output}
