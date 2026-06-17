"""Host-side architect MCP server.

Runs as a FastMCP streamable-HTTP server on the host (default :9006).
Reached from Docker containers via host.docker.internal:9006.

v1 (slice 1): real codebase_search backed by BM25; adr_read/adr_write/diagram_gen
remain stub-echo until subsequent slices replace them.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from architect_server.search import build_index, search

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

_PORT = int(os.environ.get("ARCHITECT_PORT", "9006"))
_DEFAULT_REPO = os.environ.get("ARCHITECT_DEFAULT_REPO")

mcp = FastMCP(
    "architect",
    host="0.0.0.0",
    port=_PORT,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Per-process LRU is added in slice 4; for slice 1 we rebuild on every call.
# This keeps the implementation honest about indexing cost so the cache slice
# has a concrete latency target to beat.


def _resolve_repo(repo: str | None) -> str:
    target = repo or _DEFAULT_REPO
    if not target:
        raise ValueError(
            "No repo specified and ARCHITECT_DEFAULT_REPO unset. "
            "Pass a local directory path as `repo`."
        )
    return target


@mcp.tool()
def codebase_search(query: str, repo: str | None = None, top_k: int = 5) -> dict:
    """Search a codebase with a BM25 keyword query.

    Args:
        query: natural-language or code query.
        repo: local directory path. Falls back to ARCHITECT_DEFAULT_REPO env var if unset.
        top_k: number of results to return (default 5).
    """
    try:
        target = _resolve_repo(repo)
    except ValueError as exc:
        return {"error": str(exc), "chunks": []}
    index = build_index(target)
    results = search(index, query=query, top_k=top_k, mode="bm25")
    return {"repo": str(index.root), "chunks": [asdict(r) for r in results]}


@mcp.tool()
def adr_read(title: str) -> dict:
    """Read an Architecture Decision Record by title. (stub — replaced in slice 2)"""
    return {"result": "stub", "tool": "adr_read", "title": title}


@mcp.tool()
def adr_write(title: str, content: str) -> dict:
    """Write an Architecture Decision Record. (stub — replaced in slice 2)"""
    return {"result": "stub", "tool": "adr_write", "title": title}


@mcp.tool()
def diagram_gen(description: str) -> dict:
    """Generate a diagram from a description. (stub — replaced in slice 5)"""
    return {"result": "stub", "tool": "diagram_gen", "description": description}


def main() -> None:
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=_PORT)


if __name__ == "__main__":
    main()
