"""Host-side architect MCP server.

Runs as a FastMCP streamable-HTTP server on the host (default :9006).
Reached from Docker containers via host.docker.internal:9006.

v1 (slices 1–3): real codebase_search (BM25 / semantic / hybrid via Ollama
embeddings) and adr_read/adr_write against ``<repo>/docs/adr/``. ``diagram_gen``
remains a stub-echo until slice 6.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from architect_server.adr import read_adr, write_adr
from architect_server.embeddings import OllamaEmbedder
from architect_server.search import build_index, embed_index, search

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


def _make_embedder() -> OllamaEmbedder:
    return OllamaEmbedder()


@mcp.tool()
def codebase_search(
    query: str,
    repo: str | None = None,
    top_k: int = 5,
    mode: str = "hybrid",
) -> dict:
    """Search a codebase with BM25, semantic, or hybrid ranking.

    Args:
        query: natural-language or code query.
        repo: local directory path. Falls back to ARCHITECT_DEFAULT_REPO env var if unset.
        top_k: number of results to return (default 5).
        mode: ``bm25`` | ``semantic`` | ``hybrid`` (default ``hybrid``). Hybrid merges
            BM25 and dense rankings via Reciprocal Rank Fusion. ``semantic`` and
            ``hybrid`` require Ollama to be reachable at ``OLLAMA_HOST``.
    """
    try:
        target = _resolve_repo(repo)
    except ValueError as exc:
        return {"error": str(exc), "chunks": []}
    index = build_index(target)
    embedder = None
    if mode in ("semantic", "hybrid"):
        try:
            embedder = _make_embedder()
            embed_index(index, embedder)
        except Exception as exc:
            logger.warning("embedder unavailable, falling back to bm25: %s", exc)
            mode = "bm25"
            embedder = None
    results = search(index, query=query, top_k=top_k, mode=mode, embedder=embedder)
    return {
        "repo": str(index.root),
        "mode": mode,
        "chunks": [asdict(r) for r in results],
    }


@mcp.tool()
def adr_read(
    query: str | None = None,
    path: str | None = None,
    repo: str | None = None,
    top_k: int = 5,
) -> dict:
    """Read Architecture Decision Records from ``<repo>/docs/adr/``.

    Args:
        query: optional free-text query to rank ADRs by token overlap on title+content.
        path: optional repo-relative path to a single ADR (e.g. docs/adr/0001-foo.md).
        repo: local directory path. Falls back to ARCHITECT_DEFAULT_REPO env var if unset.
        top_k: max records to return when filtering by query (default 5).

    Returns:
        ``{"repo": "...", "adrs": [{"id": "0036", "title": ..., "status": ..., "path": ..., "content": ...}]}``
    """
    try:
        target = _resolve_repo(repo)
    except ValueError as exc:
        return {"error": str(exc), "adrs": []}
    adrs = read_adr(target, query=query, path=path, top_k=top_k)
    return {"repo": str(Path(target).resolve()), "adrs": adrs}


@mcp.tool()
def adr_write(title: str, content: str, repo: str | None = None) -> dict:
    """Persist a new Architecture Decision Record to ``<repo>/docs/adr/``.

    The next sequential 4-digit id is assigned automatically; the title is
    slugified for the filename. If ``content`` does not already start with a
    Markdown heading, a ``# {title}`` heading is prepended.

    Returns:
        ``{"repo": "...", "id": "0037", "path": "docs/adr/0037-...md"}``
    """
    try:
        target = _resolve_repo(repo)
    except ValueError as exc:
        return {"error": str(exc)}
    out = write_adr(target, title=title, content=content)
    return {"repo": str(Path(target).resolve()), **out}


@mcp.tool()
def diagram_gen(description: str) -> dict:
    """Generate a diagram from a description. (stub — replaced in slice 5)"""
    return {"result": "stub", "tool": "diagram_gen", "description": description}


def main() -> None:
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=_PORT)


if __name__ == "__main__":
    main()
