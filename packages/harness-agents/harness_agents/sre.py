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

    async def _load_memory_context(self, task: str) -> list:
        if not self.memory:
            return []
        return await self.memory.search(self.memory_namespace, task, top_k=3)

    def _init_token_usage(self, state: AgentState) -> dict:
        current = state.get("token_usage")
        if current:
            return dict(current)
        return {"prompt_tokens": 0, "completion_tokens": 0}

    async def _gather_telemetry(self, task: str) -> tuple | None:
        try:
            metrics = await self.gateway.call_tool("observability_query", {"query": task})
            logs = await self.gateway.call_tool("log_search", {"query": task})
            runbook = await self.gateway.call_tool("runbook_read", {"runbook_name": task})
            return metrics, logs, runbook
        except ToolAccessDenied as e:
            logger.error("tool_access_denied: %s", e)
            return None

    async def _llm_loop(self, messages: list[dict], token_usage: dict) -> dict | None:
        for attempt in range(MAX_ITERATIONS):
            response = await self.llm.chat(messages=messages)
            token_usage["prompt_tokens"] += response.prompt_tokens
            token_usage["completion_tokens"] += response.completion_tokens
            raw = _clean_raw(response.content)
            try:
                parsed = json.loads(raw)
                jsonschema.validate(parsed, SRE_OUTPUT_SCHEMA)
                return parsed
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                logger.warning("attempt %d: invalid output: %s", attempt + 1, e)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Invalid response: {e}\nTry again. Raw JSON only.",
                })
        return None

    async def _save_to_memory(self, state: AgentState, raw_output: dict) -> None:
        if not self.memory:
            return
        incident_key = f"incident:{state['thread_id'][:8]}"
        await self.memory.write(self.memory_namespace, incident_key, raw_output)

    async def run(self, state: AgentState) -> AgentState:
        task = state["task"]
        memory_context = await self._load_memory_context(task)
        token_usage = self._init_token_usage(state)

        telemetry = await self._gather_telemetry(task)
        if telemetry is None:
            return {**state, "error": {"code": "tool_access_denied", "reason": "telemetry unavailable"}}
        metrics, logs, runbook = telemetry

        context_block = f"\nPast incidents from memory:\n{json.dumps(memory_context, indent=2)}\n" if memory_context else ""

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

        raw_output = await self._llm_loop(messages, token_usage)
        if raw_output is None:
            return {**state, "token_usage": token_usage, "error": {"code": "invalid_output", "reason": "max retries exceeded"}}

        await self._save_to_memory(state, raw_output)
        return {**state, "agent_output": raw_output, "token_usage": token_usage}
