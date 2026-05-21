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


@mcp.tool()
def observability_query(query: str) -> dict:
    """Run an observability query."""
    return {"result": "stub", "tool": "observability_query", "query": query}


@mcp.tool()
def runbook_read(runbook_name: str) -> dict:
    """Read a runbook by name."""
    return {"result": "stub", "tool": "runbook_read", "runbook_name": runbook_name}


@mcp.tool()
def log_search(query: str) -> dict:
    """Search logs for a query."""
    return {"result": "stub", "tool": "log_search", "query": query}


@mcp.tool()
def shell_exec(command: str) -> dict:
    """Execute a shell command (stub — does not actually run commands)."""
    return {"result": "stub", "tool": "shell_exec", "command": command}


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9005)
