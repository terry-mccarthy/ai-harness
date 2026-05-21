import logging
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import uvicorn

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "architect_stub",
    host="0.0.0.0",
    port=9004,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def codebase_search(query: str) -> dict:
    """Search the codebase for relevant code."""
    return {"result": "stub", "tool": "codebase_search", "query": query}


@mcp.tool()
def adr_read(title: str) -> dict:
    """Read an Architecture Decision Record by title."""
    return {"result": "stub", "tool": "adr_read", "title": title}


@mcp.tool()
def adr_write(title: str, content: str) -> dict:
    """Write an Architecture Decision Record."""
    return {"result": "stub", "tool": "adr_write", "title": title}


@mcp.tool()
def diagram_gen(description: str) -> dict:
    """Generate a diagram from a description."""
    return {"result": "stub", "tool": "diagram_gen", "description": description}


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9004)
