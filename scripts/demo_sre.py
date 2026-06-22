"""Demo: run DynamicSREAgent against a canned incident and print the report.

Wires formula_store (Dolt) and memory_store (pgvector) when env vars are
present; falls back to stub-only mode when they are not.

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


async def _build_memory_store():
    """Return a connected PostgresMemoryStore, or None if PG_DSN is not set."""
    pg_dsn = os.environ.get("PG_DSN")
    if not pg_dsn:
        return None
    from harness_memory.memory_store import PostgresMemoryStore
    store = PostgresMemoryStore(
        pg_dsn,
        os.environ.get("REDIS_URL", "redis://localhost:6379"),
        os.environ.get("EMBED_MODEL", "nomic-embed-text"),
        os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
    )
    await store.setup()
    return store


def _build_formula_store():
    """Return a DoltFormulaStore, or None if DOLT_HOST is not set."""
    dolt_host = os.environ.get("DOLT_HOST")
    if not dolt_host:
        return None
    from harness_memory.formula_store import DoltFormulaStore
    return DoltFormulaStore(
        host=dolt_host,
        port=int(os.environ.get("DOLT_PORT", "3306")),
        user=os.environ.get("DOLT_USER", "root"),
        password=os.environ.get("DOLT_PASSWORD", "root"),
        database=os.environ.get("DOLT_DATABASE", "harness"),
    )


def _banner(memory_store, formula_store) -> str:
    lines = ["Capabilities:"]
    lines.append(f"  memory store  : {'connected (past incidents loaded)' if memory_store else 'disabled (set PG_DSN to enable)'}")
    lines.append(f"  formula store : {'connected (skill guidance pre-loaded)' if formula_store else 'disabled (set DOLT_HOST to enable)'}")
    lines.append(f"  log_search    : {'semantic (run make seed-logs first)' if os.environ.get('PG_DSN') else 'stub'}")
    lines.append(f"  runbook_read  : {'semantic (run make seed-runbooks first)' if os.environ.get('PG_DSN') else 'stub'}")
    lines.append(f"  skill_search  : {'live formula lookup' if formula_store else 'stub'}")
    return "\n".join(lines)


async def main() -> None:
    gateway = GatewayClient(
        gateway_url=os.environ.get("MCPJUNGLE_URL", "http://localhost:8080"),
        governance_url=os.environ.get("GOVERNANCE_URL", "http://localhost:8090"),
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
    )

    memory_store = await _build_memory_store()
    formula_store = _build_formula_store()

    agent = DynamicSREAgent(
        gateway=gateway,
        llm_provider=_build_llm(),
        memory_store=memory_store,
        formula_store=formula_store,
    )

    print(f"Incident: {INCIDENT}\n")
    print(_banner(memory_store, formula_store))
    print("\nInvestigating...\n")

    state: AgentState = {
        "task": INCIDENT,
        "thread_id": str(uuid.uuid4()),
        "diff": "",
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
    }

    result = await agent.run(state)

    if memory_store:
        await memory_store.close()

    if result.get("error"):
        print(f"Error: {json.dumps(result['error'], indent=2)}")
        return

    report = result["agent_output"]
    print(json.dumps(report, indent=2))

    runbook = report.get("runbook_ref")
    print(f"\n  Runbook cited : {runbook or '(none)'}")
    print(f"  Severity      : {report.get('severity', '?')}")
    print(f"  Needs approval: {report.get('requires_human_approval', False)}")


if __name__ == "__main__":
    asyncio.run(main())
