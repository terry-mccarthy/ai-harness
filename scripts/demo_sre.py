"""Demo: run DynamicSREAgent against a canned incident and print the report.

Usage:
    make demo-sre                          # Ollama (default)
    LLM_PROVIDER=openrouter make demo-sre  # OpenRouter
"""
import asyncio
import json
import os
import uuid

from harness_agents.dynamic_sre import DynamicSREAgent
from harness_agents.types import AgentState
from harness_gateway.client import GatewayClient

INCIDENT = (
    "Grafana cost dashboard shows the architect agent role consuming tokens "
    "at 4x the normal rate for the past 30 minutes. Two threads appear stuck "
    "in a loop with no final_response produced."
)


def _build_llm():
    provider = os.environ.get("LLM_PROVIDER", "ollama")
    if provider == "openrouter":
        from harness_agents.llm import OpenRouterProvider
        return OpenRouterProvider(
            api_key=os.environ["OPENROUTER_API_KEY"],
            model=os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "2048")),
        )
    from harness_agents.llm import OllamaProvider
    return OllamaProvider(
        host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
    )


async def main() -> None:
    gateway = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
    )
    agent = DynamicSREAgent(gateway=gateway, llm_provider=_build_llm())

    state: AgentState = {
        "task": INCIDENT,
        "thread_id": str(uuid.uuid4()),
        "diff": "",
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
    }

    print(f"Incident: {INCIDENT}\n")
    print("Investigating...\n")

    result = await agent.run(state)

    if result.get("error"):
        print(f"Error: {json.dumps(result['error'], indent=2)}")
        return

    report = result["agent_output"]
    print(json.dumps(report, indent=2))

    runbook = report.get("runbook_ref")
    if runbook:
        print(f"\n  Runbook cited: {runbook}")
    else:
        print("\n  (no runbook cited)")


if __name__ == "__main__":
    asyncio.run(main())
