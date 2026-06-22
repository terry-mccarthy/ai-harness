import asyncio
import logging
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import uvicorn

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "sre_stub",
    host="0.0.0.0",
    port=9005,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_store = None
_store_lock = asyncio.Lock()


async def _get_store():
    """Lazy-initialise PostgresMemoryStore when PG_DSN is configured."""
    global _store
    pg_dsn = os.environ.get("PG_DSN")
    if not pg_dsn:
        return None
    async with _store_lock:
        if _store is None:
            from harness_memory.memory_store import PostgresMemoryStore
            s = PostgresMemoryStore(
                pg_dsn,
                os.environ.get("REDIS_URL", "redis://localhost:6379"),
                os.environ.get("EMBED_MODEL", "nomic-embed-text"),
                os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            )
            await s.setup()
            _store = s
    return _store


@mcp.tool()
def observability_query(query: str) -> dict:
    """Run an observability query."""
    return {"result": "stub", "tool": "observability_query", "query": query}


@mcp.tool()
async def runbook_read(runbook_name: str) -> dict:
    """Search runbooks semantically by incident description."""
    store = await _get_store()
    if store is None:
        return {"result": "stub", "tool": "runbook_read", "runbook_name": runbook_name}
    from harness_memory.runbook_retriever import retrieve_runbooks
    return await retrieve_runbooks(store, runbook_name)


@mcp.tool()
async def log_search(query: str) -> dict:
    """Search logs semantically by error pattern or incident description."""
    store = await _get_store()
    if store is None:
        return {"result": "stub", "tool": "log_search", "query": query}
    from harness_memory.log_retriever import retrieve_logs
    return await retrieve_logs(store, query)


@mcp.tool()
def shell_exec(command: str) -> dict:
    """Execute a shell command (stub — does not actually run commands)."""
    return {"result": "stub", "tool": "shell_exec", "command": command}


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9005)
