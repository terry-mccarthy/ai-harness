import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "github_mcp",
    host="0.0.0.0",
    port=9010,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "ai-harness-github-mcp/0.1.0",
}
if _GITHUB_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_GITHUB_TOKEN}"

_RAW_BASE = "https://raw.githubusercontent.com"
_API_BASE = "https://api.github.com"
_GITHUB_URL_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git|/tree/[^/]+|/blob/[^/]+|)?$"
)


def _parse_github_url(url: str) -> tuple[str, str]:
    """Extract owner/repo from a GitHub URL."""
    m = _GITHUB_URL_RE.match(url)
    if not m:
        raise ValueError(f"Invalid GitHub URL: {url!r}")
    return tuple(m.group(1).split("/"))


def _default_branch(owner: str, repo: str) -> str:
    """Detect the default branch of a repo."""
    url = f"{_API_BASE}/repos/{owner}/{repo}"
    resp = httpx.get(url, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json().get("default_branch", "main")


@mcp.tool()
async def codebase_search(query: str, repo: str, top_k: int = 5) -> dict:
    """Search a GitHub codebase using GitHub's code search API.

    Args:
        query: Natural-language or keyword query.
        repo: ``https://github.com/owner/repo``.
        top_k: Max results (default 5, max 30).
    """
    owner, repo_name = _parse_github_url(repo)
    search_query = f"{query} repo:{owner}/{repo_name}"
    url = f"{_API_BASE}/search/code"
    params = {"q": search_query, "per_page": min(top_k, 30)}
    headers = {**_HEADERS, "Accept": "application/vnd.github.v3+json"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params, timeout=15)
    if resp.status_code == 403:
        logger.warning("GitHub API rate limited or token missing; returning empty results")
        return {"repo": repo, "query": query, "results": [], "error": "rate_limited"}
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("items", [])[:top_k]:
        results.append({
            "path": item["path"],
            "repo": item["repository"]["full_name"],
            "html_url": item["html_url"],
            "matches": item.get("text_matches", []),
        })

    return {"repo": repo, "query": query, "results": results}


@mcp.tool()
async def adr_read(
    query: str | None = None,
    path: str | None = None,
    repo: str | None = None,
    top_k: int = 5,
) -> dict:
    """Read Architecture Decision Records from a GitHub repo.

    Args:
        query: Optional free-text query to rank ADRs.
        path: Optional path to a single ADR (e.g. ``docs/adr/0001-foo.md``).
        repo: ``https://github.com/owner/repo``. Falls back to ``ARCHITECT_DEFAULT_REPO`` env var if unset.
        top_k: Max records to return when filtering by query (default 5).
    """
    owner_repo = repo or os.environ.get("ARCHITECT_DEFAULT_REPO", "")
    if not owner_repo:
        return {"repo": "", "adrs": [], "error": "no_repo_specified"}

    owner, repo_name = _parse_github_url(owner_repo)
    branch = _default_branch(owner, repo_name)

    if path:
        return _fetch_single_adr(owner, repo_name, branch, path)

    adr_files = await _list_adr_files(owner, repo_name, branch)
    if not adr_files:
        return {"repo": f"{owner}/{repo_name}", "adrs": []}

    if query:
        adr_files = _rank_by_query(adr_files, query)[:top_k]

    adrs = []
    async with httpx.AsyncClient() as client:
        for af in adr_files:
            content = await _fetch_raw(owner, repo_name, branch, af["path"], client)
            adrs.append({
                "id": af.get("id", af["path"]),
                "title": af.get("title", ""),
                "status": af.get("status", "unknown"),
                "path": af["path"],
                "content": content or "",
            })

    return {"repo": f"{owner}/{repo_name}", "adrs": adrs}


def _fetch_single_adr(owner: str, repo_name: str, branch: str, path: str) -> dict:
    """Fetch a single ADR by path."""
    import httpx as sync_httpx
    url = f"{_RAW_BASE}/{owner}/{repo_name}/{branch}/{path}"
    resp = sync_httpx.get(url, headers=_HEADERS, timeout=10)
    if resp.status_code == 404:
        return {"repo": f"{owner}/{repo_name}", "adrs": [], "error": f"not_found: {path}"}
    resp.raise_for_status()
    content = resp.text
    title, adr_id = _parse_adr_metadata(content, path)
    return {
        "repo": f"{owner}/{repo_name}",
        "adrs": [{"id": adr_id, "title": title, "path": path, "content": content}],
    }


async def _list_adr_files(owner: str, repo_name: str, branch: str) -> list[dict]:
    """List ADR files from docs/adr/ via GitHub API."""
    url = f"{_API_BASE}/repos/{owner}/{repo_name}/contents/docs/adr"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=_HEADERS, timeout=10)
    if resp.status_code == 404:
        logger.info("No docs/adr/ directory found in %s/%s", owner, repo_name)
        return []
    resp.raise_for_status()
    files = resp.json()
    if not isinstance(files, list):
        return []

    adr_files = []
    for f in files:
        if f.get("type") == "file" and f["name"].endswith(".md"):
            adr_id = f["name"].split("-")[0]
            adr_files.append({
                "path": f["path"],
                "name": f["name"],
                "id": adr_id,
                "download_url": f.get("download_url", ""),
            })
    return sorted(adr_files, key=lambda x: x["name"])


async def _fetch_raw(
    owner: str, repo_name: str, branch: str, path: str, client: httpx.AsyncClient
) -> str | None:
    """Fetch a raw file from GitHub."""
    url = f"{_RAW_BASE}/{owner}/{repo_name}/{branch}/{path}"
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.text
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", path, exc)
    return None


def _parse_adr_metadata(content: str, path: str) -> tuple[str, str]:
    """Extract title and id from ADR content."""
    title = ""
    adr_id = ""
    name = path.rsplit("/", 1)[-1].replace(".md", "")
    adr_id = name.split("-")[0]
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
    return title, adr_id


def _rank_by_query(files: list[dict], query: str) -> list[dict]:
    """Simple token-overlap ranking."""
    query_lower = query.lower()
    q_tokens = set(query_lower.split())

    scored = []
    for f in files:
        name_lower = f["name"].lower()
        score = 0
        if query_lower in name_lower:
            score += 10
        name_tokens = set(name_lower.replace("-", " ").split())
        overlap = len(q_tokens & name_tokens)
        score += overlap
        scored.append((score, f))

    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored]


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9010)
