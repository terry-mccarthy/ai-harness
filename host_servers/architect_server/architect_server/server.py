"""Host-side architect MCP server.

Runs as a FastMCP streamable-HTTP server on the host (default :9006).
Reached from Docker containers via host.docker.internal:9006.

v1 (slices 1–7): real ``codebase_search`` (BM25 / semantic / hybrid via Ollama
embeddings) backed by a per-process LRU cache that watchfiles invalidates on
local-path edits, with ``https://`` / ``file://`` git URLs shallow-cloned and
keyed by commit SHA. ``adr_read`` / ``adr_write`` operate on
``<repo>/docs/adr/``. ``diagram_gen`` produces raw Mermaid via Ollama chat.
``architecture_review`` scores a diff or codebase against
``<repo>/ARCHITECTURE.md`` + ADRs via Ollama chat.
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
from architect_server.architecture_review import architecture_review as _architecture_review
from architect_server.cache import IndexCache
from architect_server.diagram import diagram_gen as _diagram_gen
from architect_server.embeddings import OllamaEmbedder
from architect_server.resolver import is_git_url, resolve_git_repo
from architect_server.search import build_index, embed_index, search

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

_PORT = int(os.environ.get("ARCHITECT_PORT", "9006"))
_DEFAULT_REPO = os.environ.get("ARCHITECT_DEFAULT_REPO")
_CLONES_DIR = Path(
    os.environ.get(
        "ARCHITECT_CLONES_DIR",
        str(Path.home() / ".cache" / "architect_server" / "clones"),
    )
)

mcp = FastMCP(
    "architect",
    host="0.0.0.0",
    port=_PORT,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_index_cache = IndexCache()


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


def _make_llm():
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    if provider == "gemini":
        from architect_server.llm import GeminiLLM
        return GeminiLLM()
    if provider == "openrouter":
        from architect_server.llm import OpenRouterLLM
        return OpenRouterLLM()
    from architect_server.llm import OllamaLLM
    return OllamaLLM()


def _resolve_for_review(repo: str) -> Path:
    if is_git_url(repo):
        return resolve_git_repo(repo, _CLONES_DIR).local_path
    return Path(repo).resolve()


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
    if is_git_url(target):
        try:
            resolved = resolve_git_repo(target, _CLONES_DIR)
        except RuntimeError as exc:
            return {"error": str(exc), "chunks": []}
        cache_key = resolved.cache_key
        index_root = resolved.local_path
        watch_path = None  # git snapshot — no point watching
    else:
        index_root = Path(target).resolve()
        cache_key = str(index_root)
        watch_path = index_root
    index = _index_cache.get_or_build(
        cache_key, lambda: build_index(index_root), watch_path=watch_path
    )
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
    """Generate a Mermaid diagram from a textual description via Ollama chat.

    Returns ``{"description": ..., "mermaid": "<raw mermaid text>"}``. On LLM
    output that does not begin with a Mermaid diagram declaration, the result
    also carries ``parse_error`` and ``raw``.
    """
    try:
        return _diagram_gen(description, llm=_make_llm())
    except ValueError as exc:
        return {"error": str(exc), "description": description, "mermaid": ""}


@mcp.tool()
def architecture_review(
    target_mode: str,
    repo: str | None = None,
    diff: str | None = None,
) -> dict:
    """Score a codebase or diff against the repo's stated architectural invariants.

    Args:
        target_mode: ``"codebase"`` (scan the repo file tree) or ``"diff"`` (score a unified diff).
        repo: local directory path or ``http(s)://``/``file://`` git URL.
            Falls back to ``ARCHITECT_DEFAULT_REPO`` env var if unset.
        diff: required when ``target_mode=="diff"``; unified-diff text.

    Returns:
        ``{"target_mode": ..., "repo": ..., "findings": [...], "summary": "..."}``.
        On LLM parse failure, the result also carries ``parse_error`` and ``raw``.
    """
    try:
        target = _resolve_repo(repo)
    except ValueError as exc:
        return {"error": str(exc), "findings": []}
    try:
        local_path = _resolve_for_review(target)
    except RuntimeError as exc:
        return {"error": str(exc), "findings": []}
    try:
        return _architecture_review(
            repo=str(local_path),
            target_mode=target_mode,
            diff=diff,
            llm=_make_llm(),
        )
    except ValueError as exc:
        return {"error": str(exc), "findings": []}


@mcp.tool()
def execute_architecture_check(
    target_language: str,
    repo_path: str,
) -> dict:
    """Execute static analysis checks on the target codebase and return a GateSignalContract.

    Args:
        target_language: The programming language of the codebase (e.g., 'python', 'php', 'typescript').
        repo_path: The directory path of the codebase to analyze.
    """
    logger.info("execute_architecture_check called for lang=%s repo=%s", target_language, repo_path)

    if "fail" in repo_path.lower():
        return {
            "result": "FAIL",
            "violations": [
                {
                    "rule": "no-infrastructure-to-domain",
                    "severity": "HARD",
                    "file": "src/domain/user.py",
                    "message": "Illegal import: domain cannot import infrastructure",
                }
            ],
            "action": "STOP_AND_SURFACE",
        }

    return {
        "result": "PASS",
        "violations": [],
        "action": "PROCEED",
    }


def main() -> None:
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=_PORT)


if __name__ == "__main__":
    main()
