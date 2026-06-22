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

PHASES = [
    "reconnaissance",
    "flow_trace",
    "abstraction_analysis",
    "synthesis",
]


def _clean_raw(raw: str) -> str:
    raw = _THINK_RE.sub("", raw).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw[: raw.rfind("```")].strip() if "```" in raw else raw
    return raw


def _extract_json(text: str) -> dict | None:
    cleaned = _THINK_RE.sub("", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None


def _validate_synthesis(parsed: dict) -> str | None:
    """Return None if the synthesis output satisfies ARCHITECT_OUTPUT_SCHEMA,
    otherwise a short error message describing the first violation."""
    try:
        jsonschema.validate(parsed, ARCHITECT_OUTPUT_SCHEMA)
        return None
    except jsonschema.ValidationError as e:
        return e.message


class ArchitectAgent:
    name = "architect"
    allowed_tools = ["codebase_search", "adr_read", "code_health_score", "codebase_hotspots", "logical_coupling", "issue_create"]
    memory_namespace = "architect"

    def __init__(self, gateway: GatewayClient, llm_provider: LLMProvider, memory_store=None):
        self.gateway = gateway
        self.llm = llm_provider
        self.memory = memory_store

    async def _call_tool(self, tool_name: str, params: dict) -> dict | None:
        try:
            return await self.gateway.call_tool(tool_name, params)
        except ToolAccessDenied as e:
            logger.error("tool_access_denied: %s", e)
            return None

    async def _llm_retry(self, messages: list, validate=None) -> dict | None:
        """Call the LLM until it returns a parseable JSON object that also passes
        the optional ``validate`` callable (returns an error string, or None when OK).
        Failed attempts are fed back to the model as a correction before retrying."""
        for attempt in range(MAX_ITERATIONS):
            try:
                response = await self.llm.chat(messages=messages)
            except Exception as e:
                logger.warning("phase LLM call failed: %s", e)
                return None
            raw = _clean_raw(response.content)
            parsed = _extract_json(raw)
            error = "could not parse" if parsed is None else (validate(parsed) if validate else None)
            if parsed is not None and error is None:
                return parsed
            logger.warning("attempt %d: %s", attempt + 1, error)
            if attempt < MAX_ITERATIONS - 1:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Your response was not valid. Return ONLY a valid JSON object with no markdown fences. Error: {error}",
                })
        return None

    async def _phase_reconnaissance(self, task: str, phase_results: dict) -> dict | None:
        logger.info("phase: reconnaissance")
        tree = await self._call_tool("codebase_search", {"query": f"directory structure and dependencies for: {task}", "repo": self.gateway.gateway_url, "top_k": 10})
        tree_data = tree or {"result": "no data"}
        hotspots = await self._call_tool("codebase_hotspots", {"repo": self.gateway.gateway_url, "top_n": 10})
        hotspot_data = hotspots or {"result": "no data"}
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "phase": "reconnaissance",
                "task": task,
                "directory_tree_and_deps": tree_data,
                "complexity_hotspots": hotspot_data,
            })},
        ]
        return await self._llm_retry(messages)

    async def _phase_flow_trace(self, task: str, phase_results: dict) -> dict | None:
        logger.info("phase: flow_trace")
        recon = phase_results.get("reconnaissance", {})
        critical_path = recon.get("critical_path_suggestion", task)
        files = await self._call_tool("codebase_search", {"query": f"entry point, service layer, data access for: {critical_path}", "repo": self.gateway.gateway_url, "top_k": 10})
        files_data = files or {"result": "no data"}
        context = {k: v for k, v in recon.items() if k != "phase"}
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "phase": "flow_trace",
                "task": task,
                "critical_path": critical_path,
                "reconnaissance_context": context,
                "source_files": files_data,
            })},
        ]
        return await self._llm_retry(messages)

    async def _build_context(self, phase_results: dict, *phases: str) -> dict:
        context = {}
        for p in phases:
            if p in phase_results:
                context[p] = {k: v for k, v in phase_results[p].items() if k != "phase"}
        return context

    async def _phase_abstraction_analysis(self, task: str, phase_results: dict) -> dict | None:
        logger.info("phase: abstraction_analysis")
        recon = phase_results.get("reconnaissance", {})
        interfaces_to_examine = recon.get("interfaces_to_examine", [])
        query = "interfaces, abstractions, and their implementations"
        if interfaces_to_examine:
            query = " and ".join(interfaces_to_examine[:5])
        abstractions = await self._call_tool("codebase_search", {"query": query, "repo": self.gateway.gateway_url, "top_k": 10})
        abs_data = abstractions or {"result": "no data"}
        context = await self._build_context(phase_results, "reconnaissance", "flow_trace")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "phase": "abstraction_analysis",
                "task": task,
                "prior_context": context,
                "interface_and_implementation_files": abs_data,
            })},
        ]
        return await self._llm_retry(messages)

    async def _phase_synthesis(self, task: str, phase_results: dict) -> dict | None:
        logger.info("phase: synthesis")
        adrs = await self._call_tool("adr_read", {"query": task, "repo": self.gateway.gateway_url, "top_k": 5})
        adr_data = adrs or {"result": "no data"}
        context = await self._build_context(phase_results, *PHASES)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "phase": "synthesis",
                "task": task,
                "phase_results": context,
                "architecture_decision_records": adr_data,
            })},
        ]
        return await self._llm_retry(messages, validate=_validate_synthesis)

    async def _load_memory_context(self, task: str) -> list:
        if not self.memory:
            return []
        return await self.memory.search(self.memory_namespace, task, top_k=3)

    def _init_token_usage(self, state: AgentState) -> dict:
        current = state.get("token_usage")
        if current:
            return dict(current)
        return {"prompt_tokens": 0, "completion_tokens": 0}

    async def _run_all_phases(self, task: str, phase_results: dict) -> None:
        phase_handlers = {
            "reconnaissance": self._phase_reconnaissance,
            "flow_trace": self._phase_flow_trace,
            "abstraction_analysis": self._phase_abstraction_analysis,
            "synthesis": self._phase_synthesis,
        }
        for phase_name in PHASES:
            phase_results[phase_name] = await phase_handlers[phase_name](task, phase_results)
            if phase_results[phase_name] is None:
                logger.warning("phase %s returned None, continuing", phase_name)

    def _filter_phase_results(self, phase_results: dict) -> dict:
        filtered = {}
        for k, v in phase_results.items():
            if k != "synthesis" and v is not None:
                filtered[k] = v
        return filtered

    async def _save_to_memory(self, final_output: dict, task: str) -> None:
        if not self.memory:
            return
        adr_title = final_output.get("title", task)[:60]
        await self.memory.write(self.memory_namespace, f"adr:{adr_title}", final_output)

    async def run(self, state: AgentState) -> AgentState:
        task = state["task"]

        memory_context = await self._load_memory_context(task)
        token_usage = self._init_token_usage(state)
        phase_results: dict[str, dict | None] = {}
        await self._run_all_phases(task, phase_results)

        final_output = phase_results.get("synthesis")
        if not final_output:
            logger.error("synthesis phase failed — no final output produced")
            return {**state, "token_usage": token_usage, "error": {"code": "invalid_output", "reason": "architecture review synthesis failed — no valid JSON produced"}}

        final_output["_phases"] = self._filter_phase_results(phase_results)
        await self._save_to_memory(final_output, task)

        return {**state, "agent_output": final_output, "token_usage": token_usage}
