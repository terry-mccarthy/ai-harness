import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "github_mcp",
    host="0.0.0.0",
    port=9010,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics_route(request: Request) -> Response:
    """Prometheus metrics endpoint, scraped by the monitoring stack."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

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


async def _fetch_convention_files(
    owner: str, repo_name: str, branch: str, paths: list[str]
) -> list[dict]:
    files = []
    async with httpx.AsyncClient() as client:
        for path in paths:
            url = f"{_RAW_BASE}/{owner}/{repo_name}/{branch}/{path}"
            try:
                resp = await client.get(url, headers=_HEADERS, timeout=10)
                if resp.status_code == 200:
                    files.append({"path": path, "content": resp.text})
            except Exception:
                continue
    return files


def _rank_by_query(files: list[dict], query: str | None) -> list[dict]:
    """Rank files by query relevance. Works for both convention files (path+content) and ADR entries (name)."""
    if not query:
        return files
    q_lower = query.lower()
    scored = []
    for f in files:
        score = 0
        label = f.get("name", f.get("path", ""))
        if q_lower in label.lower():
            score += 10
        content = f.get("content", "")
        if content and q_lower in content.lower():
            score += content.lower().count(q_lower)
        scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored]


@mcp.tool()
async def issue_create(
    repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> dict:
    """Create a GitHub issue in the given repository.

    Args:
        repo: ``https://github.com/owner/repo``.
        title: Issue title.
        body: Issue body (Markdown).
        labels: Optional list of label names to apply.
    """
    owner, repo_name = _parse_github_url(repo)
    url = f"{_API_BASE}/repos/{owner}/{repo_name}/issues"
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=_HEADERS, json=payload, timeout=15)
    if resp.status_code == 403:
        logger.warning("GitHub API rate limited or token missing; issue not created")
        return {"created": False, "error": "rate_limited"}
    if resp.status_code == 401:
        logger.warning("GitHub API auth failed; issue not created")
        return {"created": False, "error": "unauthorized"}
    resp.raise_for_status()
    data = resp.json()
    return {"created": True, "issue_url": data["html_url"], "issue_number": data["number"]}


@mcp.tool()
async def repo_conventions_read(
    repo: str,
    query: str | None = None,
) -> dict:
    """Read coding conventions and style guides from a GitHub repo.

    Fetches common conventions files (``CONTRIBUTING.md``,
    ``docs/CODING_STANDARDS.md``, ``.editorconfig``, etc.) from the repo
    and returns their contents.  An optional free-text ``query`` can be
    provided to rank results by relevance.

    Args:
        repo: ``https://github.com/owner/repo``.
        query: Optional free-text query to rank conventions.
    """
    owner, repo_name = _parse_github_url(repo)
    branch = _default_branch(owner, repo_name)
    convention_paths = [
        "CONTRIBUTING.md",
        "docs/CODING_STANDARDS.md",
        "docs/CONTRIBUTING.md",
        ".editorconfig",
    ]
    files = await _fetch_convention_files(owner, repo_name, branch, convention_paths)
    if not files:
        return {"repo": f"{owner}/{repo_name}", "conventions": [], "message": "no conventions files found"}
    files = _rank_by_query(files, query)
    return {"repo": f"{owner}/{repo_name}", "conventions": files}


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


async def _fetch_adr_contents(
    owner: str, repo_name: str, branch: str, adr_files: list[dict]
) -> list[dict]:
    adrs = []
    async with httpx.AsyncClient() as client:
        for af in adr_files:
            content = await _fetch_raw(owner, repo_name, branch, af["path"], client)
            title, parsed_id = _parse_adr_metadata(content or "", af["path"])
            adrs.append({
                "id": parsed_id,
                "title": title,
                "status": _parse_adr_status(content or ""),
                "path": af["path"],
                "content": content or "",
            })
    return adrs


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

    adrs = await _fetch_adr_contents(owner, repo_name, branch, adr_files)
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


def _to_adr_entry(f: dict) -> dict:
    adr_id = f["name"].split("-")[0]
    return {
        "path": f["path"],
        "name": f["name"],
        "id": adr_id,
        "download_url": f.get("download_url", ""),
    }


async def _list_adr_files(owner: str, repo_name: str, branch: str) -> list[dict]:
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

    adr_files = [_to_adr_entry(f) for f in files if f.get("type") == "file" and f["name"].endswith(".md")]
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


def _parse_adr_status(content: str) -> str:
    """Extract status from ADR content (e.g. ``**Status:** accepted``)."""
    for line in content.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("**status:**"):
            return stripped.split("**status:**", 1)[1].strip()
    return "unknown"


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9010)
